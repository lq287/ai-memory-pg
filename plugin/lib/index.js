/**
 * @deepseek-ai/dsh-memory-pg —— 把 PostgreSQL 记忆库接进 DSH。
 *
 * 两个能力：
 *  1. **工具**：memory_recall / memory_add / memory_fact_get / memory_fact_set /
 *     memory_context / memory_digest / memory_health —— 模型直接调，不用 shell。
 *  2. **每轮自动注入**：在 `agent/pre-step` 里把「事实 + 近期时段摘要 + 进行中任务」
 *     作为一条 plugin 消息插进请求，于是"记忆会自己进上下文"，
 *     不再是"得有人记得去查"。
 *
 * 为什么背后是一个常驻 Python 子进程（见 ./sidecar.js）：
 *   四态裁决、向量化、时段分桶、图遍历全部在记忆库项目的 app/ 里，已经有 200+ 项
 *   断言盯着。插件如果自己用 Node 重写一遍，必然出现两套语义（这个项目已经因为
 *   "两处各自实现" 出过两次事故）。所以插件只做两件事：注册工具、转发请求。
 *
 * 失败语义：记忆不可用时工具返回**错误文本**（isError 由内容决定，不抛异常），
 * 自动注入直接跳过 —— **绝不让会话挂掉**。记忆是增强层，退化了仍然正确。
 */

import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { MemorySidecar } from "./sidecar.js";

export const name = "memory-pg";
export const inject = ["tools", "agents"];

export const Config = z.object({
  /** 记忆库项目目录（含 app/ 与 .pylibs/），必填。 */
  projectRoot: z.string().required(),
  /** 记忆归属用户。 */
  userId: z.string().default("owner"),
  /** PostgreSQL DSN；留空则用子进程环境里的 MEM_PG_DSN。 */
  dsn: z.string(),
  /** python 可执行文件。 */
  pythonExe: z.string().default("python"),
  /** 是否注册工具。 */
  tools: z.boolean().default(true),
  /** 是否在每轮请求前自动注入记忆前言。 */
  autoContext: z.boolean().default(true),
  /** 自动注入的字符预算（粗略 token 控制）。 */
  contextBudget: z.number().min(200).max(8000).default(900),
  /** 自动注入里带多少条长期事实。 */
  factLimit: z.number().min(1).max(50).default(10),
  /** 自动注入里是否带进行中任务。 */
  includeTasks: z.boolean().default(true),
  /** 同一轮内重新采样前言的步数间隔（0 = 一轮只注入一次）。 */
  refreshSteps: z.number().min(0).max(50).default(0),
  /** 单次 sidecar 调用超时（毫秒）。 */
  timeoutMs: z.number().min(1000).max(120000).default(30000),
});

// ---------------------------------------------------------------------------
// 工具构造辅助
//
// 每个工具统一返回 {"text": "..."}，输出 schema 与 render 只写一次。
// 为什么统一：defineTool 要求声明 output.schema + render（漏了会在 apply 时抛
// "Cannot read properties of undefined (reading 'render')"），而且模型最终看到的
// 就是 render 出来的文本 —— 与其 7 个工具各写一遍，不如在这里定一次契约。
// ---------------------------------------------------------------------------
const TEXT_OUTPUT = {
  schema: {
    type: "object",
    additionalProperties: false,
    properties: {
      text: { type: "string", required: true },
    },
  },
  render: (_args, value) => [{ type: "text", text: String(value?.text ?? "") }],
};

/** 统一的错误文本：给出下一步该怎么查，而不是只报一句异常。 */
function errText(prefix, err) {
  const hint = err?.hint ? `（${err.hint}）` : "";
  return `${prefix}失败：${err?.message || err}${hint}`;
}

/**
 * 造一个"输入参数 → 一行文本"的工具。
 * @param {object} spec
 * @param {string} spec.name
 * @param {string} spec.description
 * @param {object} [spec.parameters]
 * @param {(args: object) => Promise<string>} spec.run 返回给模型看的文本
 */
function textTool(spec) {
  return defineTool({
    name: spec.name,
    description: spec.description,
    parameters: spec.parameters || {},
    output: TEXT_OUTPUT,
    execute: async (args) => ({ text: await spec.run(args || {}) }),
  });
}

/** 把召回结果渲染成紧凑多行文本（模型读起来省 token）。 */
function renderHits(result) {
  const lines = [];
  const g = result.graph || {};
  const seeds = Array.isArray(g.seeds) && g.seeds.length ? ` ·图种子 ${g.seeds.join("/")}` : "";
  lines.push(`召回 "${result.query}"：${result.hits.length} 条${seeds}`);
  for (const [i, h] of result.hits.entries()) {
    lines.push(`${i + 1}. [${h.score.toFixed(4)} ${h.paths}] ${h.summary}`);
    if (h.content) lines.push(`   ${String(h.content).replace(/\s+/g, " ").slice(0, 400)}`);
  }
  if (!result.hits.length) lines.push("（无命中）");
  return lines.join("\n");
}

// ---------------------------------------------------------------------------
// 插件主体
// ---------------------------------------------------------------------------
/**
 * @param {import("@deepseek-ai/cordis").Context} ctx
 * @param {ReturnType<typeof Config>} config
 */
export function apply(ctx, config) {
  const log = (line) => {
    const logger = ctx.logger?.("memory-pg");
    if (logger?.info) logger.info(line);
    else console.error(`[memory-pg] ${line}`);
  };

  const sidecar = new MemorySidecar({
    projectRoot: config.projectRoot,
    pythonExe: config.pythonExe,
    dsn: config.dsn,
    userId: config.userId,
    timeoutMs: config.timeoutMs,
    log,
  });
  // 清理：cordis 的 effect 收一个**零参工厂**，它返回 disposer。
  // 写成 ctx.effect((dispose) => ...) 会抛 "dispose is not a function"。
  ctx.effect?.(() => () => void sidecar.dispose());

  // -------------------------------------------------------------- 工具
  if (config.tools) {
    ctx.tools.register(
      textTool({
        name: "memory_recall",
        description:
          "从长期记忆库召回与查询相关的记忆（向量 ⊕ 关键词 ⊕ 关系图三路融合）。" +
          "用户提到『上次 / 之前 / 我们约定过 / 我的偏好』时先调这个，不要凭印象回答。" +
          "默认返回摘要层；需要原文再带 full=true。",
        parameters: {
          query: {
            type: "string",
            required: true,
            description: "检索语句，用具体关键词而不是整句话。",
          },
          limit: { type: "integer", description: "返回条数（默认 5）。" },
          tags: {
            type: "array",
            items: { type: "string" },
            description: "只返回带这些标签的记忆（交集过滤）。",
          },
          full: { type: "boolean", description: "同时返回原文（渐进披露第二层）。" },
          graph: { type: "boolean", description: "是否启用关系图第三路（默认 true）。" },
        },
        run: async (args) => {
          try {
            const r = await sidecar.request("recall", {
              query: args.query,
              limit: args.limit,
              tags: args.tags,
              full: args.full,
              graph: args.graph,
            });
            return renderHits(r);
          } catch (err) {
            return errText("召回", err);
          }
        },
      }),
    );

    ctx.tools.register(
      textTool({
        name: "memory_add",
        description:
          "把一条值得长期记住的信息写进记忆库。走完整裁决链路（新增 / 强化已有 / 待裁决 / 丢弃），" +
          "所以重复或矛盾的内容不会污染记忆。适合写：决定、约定、具体值（端口/路径/版本）、踩过的坑与修法。",
        parameters: {
          text: {
            type: "string",
            required: true,
            description: "自包含的一句话结论：是什么 + 在哪/怎么验证 + 为什么。别写『见上一步』。",
          },
          kind: {
            type: "string",
            enum: ["episode", "raw_turn", "summary", "longterm"],
            description: "默认 episode；原始对话轮次用 raw_turn（30 天 TTL，靠提炼延续）。",
          },
          importance: { type: "number", description: "0~1，默认 0.6。" },
          tags: { type: "array", items: { type: "string" }, description: "标签。" },
          entities: {
            type: "array",
            items: { type: "string" },
            description: "这条记忆涉及的实体名（会建立实体并挂进关系图）。",
          },
          source: { type: "string", description: "conversation / tool / human / import。" },
        },
        run: async (args) => {
          try {
            const r = await sidecar.request("add", {
              text: args.text,
              kind: args.kind,
              importance: args.importance,
              tags: args.tags,
              entities: args.entities,
              source: args.source || "conversation",
            });
            return r.action === "drop"
              ? `未写入（裁决=${r.action}，${r.reason}）`
              : `已写入：裁决=${r.action}（${r.reason}） id=${String(r.id || "-").slice(0, 8)}`;
          } catch (err) {
            return errText("写入", err);
          }
        },
      }),
    );

    ctx.tools.register(
      textTool({
        name: "memory_fact_get",
        description:
          "按键精确取长期事实（不走向量检索，最快最准）。适合稳定配置与偏好，例如时区、编辑器、回答风格。",
        parameters: {
          keys: {
            type: "array",
            items: { type: "string" },
            description: "事实键；留空则返回当前所有事实（按重要性/衰减排序）。",
          },
          limit: { type: "integer", description: "最多返回几条（默认 50）。" },
        },
        run: async (args) => {
          try {
            const r = await sidecar.request("fact_get", { keys: args.keys, limit: args.limit });
            const lines = r.facts.map(
              (f) => `- ${f.key} = ${JSON.stringify(f.value)} (conf=${f.confidence}, ${f.age_days}d)`,
            );
            return lines.length ? lines.join("\n") : "（没有匹配的事实）";
          } catch (err) {
            return errText("取事实", err);
          }
        },
      }),
    );

    ctx.tools.register(
      textTool({
        name: "memory_fact_set",
        description:
          "写一条长期事实（按键覆盖旧值，旧值进历史不丢失）。稳定偏好与环境配置用这个，" +
          "不要写成普通记忆 —— 事实是精确键查，不受向量排名影响。",
        parameters: {
          key: {
            type: "string",
            required: true,
            description: "事实键，建议加前缀：env.* / pref.* / mem.*。",
          },
          value: {
            type: "json",
            description: "任意 JSON 值（字符串、数字、对象、数组都行）。",
          },
          importance: { type: "number", description: "0~1，默认 0.6。" },
        },
        run: async (args) => {
          try {
            const r = await sidecar.request("fact_set", {
              key: args.key,
              value: args.value,
              importance: args.importance,
            });
            return `已写入事实 ${r.key} = ${JSON.stringify(r.value)}`;
          } catch (err) {
            return errText("写事实", err);
          }
        },
      }),
    );

    ctx.tools.register(
      textTool({
        name: "memory_context",
        description:
          "取一份『现在该知道什么』的记忆前言（事实 + 近期日/周关键词摘要 + 进行中任务）。" +
          "会话开头想知道背景、或用户问『你还记得什么』时用它，比逐条检索省 token。",
        parameters: {
          max_chars: { type: "integer", description: "字符预算，默认 1200。" },
          with_conflicts: { type: "boolean", description: "附带未裁决的冲突条数。" },
        },
        run: async (args) => {
          try {
            const r = await sidecar.request("context", {
              max_chars: args.max_chars,
              with_conflicts: args.with_conflicts,
            });
            return r.empty ? "（记忆库还没有可作为前言的条目）" : r.text;
          } catch (err) {
            return errText("取前言", err);
          }
        },
      }),
    );

    ctx.tools.register(
      textTool({
        name: "memory_digest",
        description:
          "生成或查看按时间（日/周/月/年）聚合的关键词摘要，回答『上周/上个月主要在忙什么』。" +
          "build=true 会重建对应时段（幂等，可重复跑）。",
        parameters: {
          period: {
            type: "string",
            enum: ["day", "week", "month", "year", "all"],
            description: "时间粒度，默认 week。",
          },
          build: { type: "boolean", description: "true = 立即重建（默认只看不建）。" },
          limit: { type: "integer", description: "看几期（默认 5）。" },
          top: { type: "integer", description: "每期保留几个关键词（默认 12）。" },
        },
        run: async (args) => {
          const period = args.period || "week";
          try {
            if (args.build) {
              const r = await sidecar.request("digest_build", { period, top: args.top });
              const lines = r.built.map(
                (b) => `- [${b.label}] ${b.episode_count} 条：${b.keywords.slice(0, 8).join("、")}`,
              );
              return r.count ? `重建 ${r.count} 期：\n${lines.join("\n")}` : "没有可摘要的时段";
            }
            const r = await sidecar.request("digest_show", {
              period: period === "all" ? undefined : period,
              limit: args.limit || 5,
            });
            const lines = r.digests.map(
              (d) =>
                `- [${d.period} ${d.bucket_start}] ${d.episode_count} 条 ${String(d.summary).slice(0, 80)}`,
            );
            return lines.length ? lines.join("\n") : "（还没有摘要，可以 build=true 生成）";
          } catch (err) {
            return errText("时段摘要", err);
          }
        },
      }),
    );

    ctx.tools.register(
      textTool({
        name: "memory_health",
        description: "记忆库体检：连库、向量模型、规模、健康度、图层统计。记忆行为异常时先跑这个。",
        parameters: {},
        run: async () => {
          try {
            const r = await sidecar.request("health", {});
            const q = r.quality || {};
            const g = r.graph || {};
            return (
              `用户 ${r.user}｜向量模型 ${r.embedder}\n` +
              `当前有效记忆 ${r.episodes_live} 条、事实 ${r.facts_live} 条\n` +
              `待裁决 ${q.pending_review ?? "?"}｜已过期未清 ${q.expired_live ?? "?"}｜` +
              `缺向量 ${q.missing_embedding ?? "?"}｜未决冲突 ${q.open_conflicts ?? "?"}\n` +
              `图层：实体 ${g.entities ?? 0}、关系 ${g.relations_live ?? 0}、挂图记忆 ${g.linked_episodes ?? 0}`
            );
          } catch (err) {
            return errText("体检", err);
          }
        },
      }),
    );
  }

  // -------------------------------------------------------------- 自动注入
  if (!config.autoContext) return;

  /** 每个 (会话, 轮) 缓存一份前言，避免每个 step 都查一次库。 */
  const cache = new Map();
  const HEAD = "长期记忆（自动注入）";

  ctx.on(
    "agent/pre-step",
    async ({ agent, turn, step, signal }, next) => {
      const decision = await next();
      if (decision?.kind === "reject" || signal?.aborted) return decision;

      // 一轮注入一次；refreshSteps > 0 时按步数间隔重新采样
      const refresh = config.refreshSteps;
      const due = step === 1 || (refresh > 0 && (step - 1) % refresh === 0);
      if (!due) return decision;

      const key = `${agent?.id ?? "?"}:${turn}`;
      let text = cache.get(key);
      if (text === undefined) {
        try {
          const r = await sidecar.request("context", {
            max_chars: config.contextBudget,
            fact_limit: config.factLimit,
            with_tasks: config.includeTasks,
          });
          text = r.empty ? "" : r.text;
        } catch (err) {
          // 记忆不可用 → 静默跳过，绝不让请求失败
          log(`自动注入跳过：${err?.message || err}`);
          text = "";
        }
        cache.set(key, text);
        if (cache.size > 8) cache.delete(cache.keys().next().value);
      }
      if (!text) return decision;

      const body = `${HEAD}（每次请求自动装配；需要更具体的内容再调 memory_recall）\n${text}`;
      return {
        kind: "enter",
        messages: [
          ...(decision.messages || []),
          // 直接构造消息：@deepseek-ai/dsh-llm 的 createUserMessage 只是
          // {role:"user", ...input, id: randomUUID()} 的冻结版，见该包实现。
          // 第三方插件在 profile 里解析不到 @deepseek-ai/*，所以这里不 import 它。
          {
            id: globalThis.crypto.randomUUID(),
            role: "user",
            content: [{ type: "text", text: body }],
            source: {
              kind: "plugin",
              plugin: name,
              form: "snapshot",
              sections: [{ name: HEAD, text: body }],
            },
          },
        ],
      };
    },
    { prepend: true },
  );
}
