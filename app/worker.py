"""队列消费循环。

用法：
    python -m app.worker                 # 常驻
    python -m app.worker --once          # 跑一批就退出（CI / 调试用）

流程（对应设计稿的写入管线）：
    抽取（LLM #1）→ 每个单元检索近邻候选 → 裁决建议（LLM #2）→ 确定性规则裁决
    → 四态落库（accept / merge / defer / drop）

失败语义：
  - 单条失败不中断整个 job，但会让 job 重试 —— 已落地的条目不会被重复写入，
    因为 source_ref 由「内容哈希」推导，重试时幂等命中。
  - 重试 N 次仍失败 → 进死信并留 last_error。死信不做静默处理：
    异步链路里最贵的 bug 是"用户的偏好就是没存进去，但没人知道"。
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import time
from typing import Any

from .config import Policy, Settings
from .decide import Fact, arbitrate, normalize
from .embedding import Embedder, build_embedder
from .llm import HeuristicLLM, LLM, OpenAICompatLLM
from .store import Store

log = logging.getLogger("mem.worker")


def build_deps(settings: Settings) -> tuple[Embedder, LLM]:
    """按环境变量组装依赖。没有 LLM 端点时自动退到规则替身，保证能跑起来。

    embedder 走 app.embedding.build_embedder —— **唯一**工厂。
    （曾经这里自己 if/else 造 embedder，结果和 mem.py 各用一套向量空间。）
    """
    from .embedding import build_embedder

    embedder = build_embedder(settings)

    if settings.llm_base_url:
        llm: LLM = OpenAICompatLLM(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
        )
    else:
        log.warning("未配置 MEM_LLM_BASE_URL，使用规则替身 HeuristicLLM（仅用于跑通链路）")
        llm = HeuristicLLM()
    return embedder, llm


class Worker:
    def __init__(
        self,
        store: Store,
        llm: LLM,
        embedder: Embedder,
        settings: Settings,
        policy: Policy | None = None,
        candidate_k: int = 5,
        candidate_max_dist: float = 0.35,
    ) -> None:
        self.store = store
        self.llm = llm
        self.embedder = embedder
        self.settings = settings
        self.policy = policy or Policy()
        self.candidate_k = candidate_k
        # 0.35 对应余弦相似度 ≈ 0.65，是给**真**向量模型用的阈值。
        # 换成 HashEmbedder 这类替身时它几乎筛不出候选（替身不是语义向量），
        # 演示脚本因此会显式放宽 —— 这个区别不要在生产里搞混。
        self.candidate_max_dist = candidate_max_dist

    # -- 一个 job -----------------------------------------------------------
    def process(self, job: dict) -> None:
        payload = job["payload"] or {}
        user_id = job["user_id"]

        # 按 job_type 派发：consolidate（抽取+裁决）/ distill（提炼原始轮次）
        job_type = job.get("job_type") or "consolidate"
        if job_type == "distill":
            self._process_distill(job, user_id, payload)
            return
        if job_type != "consolidate":
            log.info("job %s 类型 %s 暂无自动处理器，跳过", job["id"], job_type)
            return

        text = payload.get("text") or ""
        source = payload.get("source") or "conversation"
        if not text.strip():
            log.info("job %s 空文本，跳过", job["id"])
            return

        facts = self.llm.extract(user_id=user_id, text=text, source=source)
        if not facts:
            log.info("job %s 未抽取到记忆单元", job["id"])
            return

        errors: list[str] = []
        for fact in facts:
            try:
                self._process_unit(job["id"], fact)
            except Exception as exc:  # 单条失败不拖累其他条
                log.exception("job %s 单元处理失败: %s", job["id"], fact.content[:80])
                errors.append(f"{exc.__class__.__name__}: {exc}")

        if errors:
            # 抛出去让 fail() 记账并重试；已落库的条目靠 source_ref 幂等跳过
            raise RuntimeError(f"{len(errors)}/{len(facts)} 个单元失败: {errors[0]}")

    def _process_distill(self, job: dict, user_id: str, payload: dict) -> None:
        """提炼任务：把过老的 raw_turn 压成 summary（默认 heuristic 后端）。

        只有在配了 LLM 端点时才允许 backend=llm；否则固定 heuristic ——
        自动提炼绝不能因为"模型不可用"而变成记忆静默丢失。
        """
        from . import distill

        backend = payload.get("backend") or "heuristic"
        if backend == "agent":
            raise ValueError("backend=agent 需要人工写回，不能走队列")
        if backend == "llm" and not self.settings.llm_base_url:
            # 配了 llm 后端却没端点：回落 heuristic 并告警（不能因为模型不可用就丢记忆）
            log.warning("job %s 请求 backend=llm 但未配置 LLM，回落 heuristic", job["id"])
            backend = "heuristic"

        results = distill.distill_run(
            self.store,
            user_id,
            backend=backend,
            min_age_days=float(payload.get("min_age_days", 7)),
            batch_size=int(payload.get("batch_size", 20)),
            max_batches=int(payload.get("max_batches", 5)),
            close_raw=bool(payload.get("close_raw", False)),
        )
        if not results:
            log.info("job %s 提炼：没有可提炼的原始轮次", job["id"])
            return
        for r in results:
            log.info("job %s 提炼 summary=%s 覆盖 %s 条 (backend=%s)",
                     job["id"], str(r.summary_id)[:8], r.linked, r.backend)

    def _process_unit(self, job_id: int, fact: Fact) -> dict[str, Any]:
        # 长期事实走"按键精确取用"的路径，与情景记忆分开（决策一）
        if fact.kind == "longterm" and fact.fact_key:
            return self._process_longterm(job_id, fact)

        embedding = self.embedder.embed([fact.content])[0]
        candidates = self.store.conflict_candidates(
            fact.user_id, embedding, k=self.candidate_k, max_dist=self.candidate_max_dist
        )
        proposal = self.llm.judge(fact=fact, candidates=candidates)
        decision = arbitrate(proposal, fact, candidates, self.policy)

        source_ref = self._source_ref(job_id, fact)
        result = self.store.apply(decision, fact, embedding, source_ref)

        log.info(
            "job=%s model=%s final=%s reason=%s target=%s id=%s cascade=%s",
            job_id,
            decision.model_action or "-",
            decision.action,
            decision.reason,
            (decision.target_id or "-")[:8],
            str(result.get("id") or "-")[:8],
            result.get("reason", "-"),
        )
        if decision.needs_review:
            log.warning(
                "需人工确认: action=%s reason=%s content=%s",
                decision.action,
                decision.reason,
                fact.content[:120],
            )
        return result

    def _process_longterm(self, job_id: int, fact: Fact) -> dict[str, Any]:
        """长期事实：门槛最严，且**绝不**覆盖人工写入的当前值。"""
        min_conf = self.policy.min_write_confidence("longterm")
        if fact.source not in self.policy.protected_sources and fact.confidence < min_conf:
            log.info(
                "job=%s 长期事实被门槛拦下 (conf=%.2f < %.2f) key=%s",
                job_id,
                fact.confidence,
                min_conf,
                fact.fact_key,
            )
            return {"applied": False, "action": "drop", "reason": "below_write_threshold(longterm)"}

        existing = {f["key"]: f for f in self.store.recall_facts(fact.user_id, [fact.fact_key])}
        current = existing.get(fact.fact_key or "")
        if current is not None:
            if current["source"] in self.policy.protected_sources and (
                fact.source not in self.policy.protected_sources
            ):
                # 人工写入的当前值，模型不得自动覆盖
                log.warning("job=%s 长期事实被人工来源保护 key=%s", job_id, fact.fact_key)
                return {"applied": False, "action": "drop", "reason": "protected_source"}
            if current["value"] == fact.value and current["source"] == fact.source:
                self.store.bump_fact_usage(fact.user_id, [fact.fact_key])
                return {"applied": False, "action": "merge", "reason": "fact_unchanged"}

        fact_id = self.store.set_fact(
            user_id=fact.user_id,
            key=fact.fact_key or "",
            value=fact.value if fact.value is not None else fact.content,
            confidence=fact.confidence,
            source=fact.source,
            importance=fact.importance,
        )
        log.info(
            "job=%s longterm key=%s conf=%.2f -> %s",
            job_id,
            fact.fact_key,
            fact.confidence,
            (fact_id or "-")[:8],
        )
        return {"applied": bool(fact_id), "action": "accept", "id": fact_id, "reason": "longterm_fact"}

    # -- 循环 ---------------------------------------------------------------
    def run_once(self) -> int:
        """领取并处理一批，返回处理条数。"""
        jobs = self.store.claim(self.settings.batch_size, self.settings.worker_id)
        if not jobs:
            return 0
        for job in jobs:
            try:
                self.process(job)
            except Exception as exc:
                status = self.store.fail(job["id"], f"{exc.__class__.__name__}: {exc}")
                log.error("job %s 失败 -> %s (第 %s 次)", job["id"], status, job["attempts"])
            else:
                self.store.ack(job["id"])
        return len(jobs)

    def run_forever(self, idle_sleep: float = 1.0, reap_every: float = 60.0) -> None:
        log.info("worker %s 启动，batch=%s", self.settings.worker_id, self.settings.batch_size)
        last_reap = 0.0
        while True:
            try:
                now = time.monotonic()
                if now - last_reap > reap_every:
                    reclaimed, dead = self.store.reap(self.settings.lease_seconds)
                    if reclaimed or dead:
                        log.warning("回收卡死任务: %s，转死信: %s", reclaimed, dead)
                    last_reap = now

                if self.run_once() == 0:
                    time.sleep(idle_sleep)
            except KeyboardInterrupt:
                log.info("收到中断，退出")
                return
            except Exception:
                # 循环不能死。数据库连接抖一下就退出进程，等于把所有任务卡住。
                log.exception("循环异常，5 秒后继续")
                time.sleep(5)

    # -- 幂等键 -------------------------------------------------------------
    @staticmethod
    def _source_ref(job_id: int, fact: Fact) -> str:
        """用内容哈希而不是下标：抽取是非确定性的，重试时第 idx 条可能换了内容。"""
        digest = hashlib.sha1(normalize(fact.content).encode("utf-8")).hexdigest()[:16]
        return f"job:{job_id}:{digest}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="记忆写入 worker")
    parser.add_argument("--once", action="store_true", help="只处理一批就退出")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--trust-llm-rewrite",
        action="store_true",
        help="允许 LLM 的 new_content 落库（默认用抽取原文，更安全）",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    settings = Settings.from_env()
    embedder, llm = build_deps(settings)
    policy = Policy(trust_llm_rewrite=args.trust_llm_rewrite)

    with Store(settings.dsn, embedder) as store:
        worker = Worker(store, llm, embedder, settings, policy)
        if args.once:
            n = worker.run_once()
            log.info("处理 %s 条", n)
            return 0
        worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
