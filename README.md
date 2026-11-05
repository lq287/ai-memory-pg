# AI 记忆层 · 实现级交付

[![repo](https://img.shields.io/badge/GitHub-lq287%2Fai--memory--pg-181717?logo=github)](https://github.com/lq287/ai-memory-pg)
[![plugin](https://img.shields.io/badge/DSH%20plugin-dsh--memory--pg-4B32C3)](plugin/README.md)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

单 PostgreSQL（pgvector + pg_trgm），可选 Apache AGE。四个不可逆决策落成 schema 与
管线，并且**每一条都在真库里跑过**。

> 仓库：https://github.com/lq287/ai-memory-pg ｜ DSH 插件：见 [§十五](#十五dsh-插件dsh-memory-pg) 与 `plugin/README.md`

> 本版按最新设计稿重写，与上一版的差异集中在这一段，其余章节同步更新。
>
> | 维度 | 上一版 | 本版 |
> |---|---|---|
> | 写入四态 | ADD / UPDATE / DELETE / NONE | **accept / merge / defer / drop** |
> | merge 的语义 | 无 | 不新建行，只抬置信度/重要性（复述强化） |
> | defer 的语义 | 无 | 落库但标 `review_state='pending'`，默认不召回；建冲突边等裁决 |
> | 取代时机 | LLM 说 DELETE 就降级为 UPDATE | **矛盾被裁决之后**才 supersede（`resolve_conflict`） |
> | 时间衰减 | 指数半衰期 90 天 + 下限 0.2 | **高斯 offset=180d / scale=1825d / decay=0.5** |
> | 频率加成 | `1 + 0.2·ln(1+n)` | `1 + 0.2·log10(1+n)` |
> | 保留策略 | 无 | **分类型 TTL**：raw_turn 30d / episode 90d / summary 3650d / longterm 不过期 |
> | 长期事实衰减 | 无（只按置信度排序） | 按 **last_used_at** 衰减，与情景记忆不同曲线 |
> | token 控制 | 只给 summary | summary + **硬预算封顶**（按分数截断） |
> | 长期事实入口 | 无（全走 episodes） | 抽取阶段就分流：带 `fact_key` 的进 `facts` 表 |

```
sql/01_schema.sql     episodes / facts / memory_conflicts / memory_jobs + TTL 触发器 + 索引
sql/02_retrieval.sql  向量 ⊕ 词法 ⊕ 图 → RRF → 高斯衰减 + 对数频率；事实层独立按 last_used_at 衰减
sql/03_write.sql      四态落库动作 + 冲突裁决 + 软删除/恢复 + SKIP LOCKED 队列（含死信）
sql/04_workmem.sql    tasks / task_events / working_sets / working_items + 状态机守卫 + 装配函数
sql/05_distill.sql    distill_links + 候选筛选 + 血缘函数（raw_turn → summary）
sql/06_graph.sql      entities / relations / episode_entities + 递归 CTE 多跳遍历（第三路召回）
sql/07_purge.sql      整用户物理清除（二次确认 + dryrun 对账）
sql/99_selftest.sql   18 组行为断言，跑完自清；改任何 SQL 后都该重跑这个

app/decide.py         裁决纯函数（无 IO、无依赖、可单测）—— 四态矩阵全在这里
app/llm.py            抽取 + 四态建议两个提示词 + 严格 JSON 收敛 + 规则替身
app/embedding.py      向量化**唯一工厂**（hash / onnx / 兼容端点三选一）
app/onnx_embed.py     本地 ONNX 向量化：bge-base-zh-v1.5，768 维，离线免密钥
app/workmem.py        工作记忆层 + 任务状态层（装配，不检索）
app/graph.py          图扩展层：实体/关系/挂载 + 第三路召回（不猜实体、不删关系）
app/distill.py        提炼：heuristic 抽取式 / agent 人工写回 / llm + 接地校验 / 血缘落库
app/store.py          PG 读写，SQL 与 sql/*.sql 一一对应
app/worker.py         队列消费 + 长期事实/情景记忆分流 + 退避/死信/租约回收
app/retrieve.py       取记忆唯一入口：事实层优先 + 情景层 + token 预算封顶
app/demo.py           一条命令跑通 四态 + TTL + 冲突裁决 + 预算装配

scripts/sqlcheck.mjs      用 PGlite（真 PostgreSQL 18 编译成 WASM）跑通七份 SQL + 7 组取证
scripts/pgserver.mjs      同上，但开成 PG wire 端口，给 psycopg 连
scripts/verify_all.py     Python↔PG 交界面的全 API 面验证（57 项）
scripts/verify_workmem.py 任务状态 + 工作记忆（43 项：状态机/快照/装配/级联）
scripts/verify_distill.py 提炼（33 项：候选/血缘/幂等/plan+apply/一等公民）
scripts/verify_graph.py   图扩展 + 整用户清除（53 项：归并/多跳/防环/第三路生效/误伤检查）
scripts/reembed.py        换 embedding 模型后重算向量（含维度迁移）
scripts/eval_recall.py    召回质量评估（Top-1 / Top-3），可复现实验
scripts/mem.py            记忆库命令行：add/recall/fact/task/work/distill/graph/purge/health
scripts/verify-all.ps1    一条命令跑完以上全部（含起停服务）

tests/test_decide.py    28 条：四态护栏逐条对应一种线上事故
tests/test_pipeline.py  33 条：抽取→候选→建议→裁决→预算装配 全链路（不连库）
tests/test_distill.py   25 条：LLM 后端的接地校验（编数字必被拦）+ 后端选择
```

## 一条命令验证全部

```powershell
powershell -ExecutionPolicy Bypass -File scripts/verify-all.ps1
```

按层跑，任何一层挂掉都会明确报出是哪一层：

| 层 | 内容 | 期望 |
|---|---|---|
| 语法 | `python -m compileall` | 无错误 |
| 单测 | `test_decide` + `test_pipeline` + `test_distill` | 86 条全绿 |
| SQL | 七份 SQL + `99_selftest.sql` + 7 组取证 | 18 组断言全过 |
| Python↔PG | `verify_all.py` + `verify_workmem` + `verify_distill` + `verify_graph` | 57 + 43 + 33 + 53 项全过 |
| 端到端 | `app.demo` + `worker --once` | 四态齐全、CLI 退出码 0 |

⚠️ 脚本里有两条铁律，都是踩出来的：

1. **永远不要** `Get-Process node | Stop-Process` —— 宿主/编辑器本身可能就跑在 node 上，
   一把梭会把界面连同会话一起杀掉。只按 `Start-Process` 返回的 PID 定点关闭
   （脚本已这么做，默认用 55433 端口避开冲突）。
2. `.ps1` 必须存成 **UTF-8 带 BOM**，否则 Windows PowerShell 5.1 按 ANSI 解码，
   中文字符串会把引号吃坏、脚本直接解析失败。

## 快速开始

两条路线，选一条。**A 是生产路线，B 不需要 Docker 也不需要管理员权限**（我就是用 B 验证的）。

### A. Docker（部署用）

```bash
docker compose up -d                # 首次启动自动执行 01_schema.sql
python -m pip install -r requirements.txt
psql "postgresql://mem:mem@127.0.0.1:55432/mem" -f sql/02_retrieval.sql
psql "postgresql://mem:mem@127.0.0.1:55432/mem" -f sql/03_write.sql
psql "postgresql://mem:mem@127.0.0.1:55432/mem" -f sql/99_selftest.sql   # 期望 14 组 PASS
python -m app.demo
python -m app.worker
```

### B. PGlite（本机没有 Docker / 没有管理员权限时）

```bash
npm install                                     # PGlite + pgvector 扩展
npm run check                                   # 四份 SQL 全跑 + 7 组取证数字

npm run serve                                   # 开一个能连的 PG 端口（Ctrl+C 退出）
# 另开终端
python -m pip install -r requirements.txt
$env:MEM_PG_DSN = "postgresql://postgres@127.0.0.1:55432/postgres"
python -m app.demo
```

`npm run check` 的实测输出（不是设想，是跑出来的）：

```
▶ sql/01_schema.sql ... OK     ▶ sql/03_write.sql ... OK
▶ sql/02_retrieval.sql ... OK  ▶ sql/99_selftest.sql ... OK
宿主：PostgreSQL 18.6  ·  vector 0.8.7  ·  pg_trgm 1.6
```

⚠️ PGlite 只是**开发/验证**用：内存库、单连接、无认证、性能与真 PG 无可比性。

单测不连库、不联网：

```bash
python tests/test_decide.py
python tests/test_pipeline.py
```

---

## 一、四个不可逆决策怎么落成 schema

### 决策一：长期事实与情景事件分开存储

不是"用两张表"这么简单，是**类型层面就不让你写错**：

- `facts` 表**没有 event_time 列**。事实本身不会"变老"，衰减的是"近期没被需要"，
  所以只有 `last_used_at`。想按事件时间衰减它，列都不存在。
- 抽取阶段就分流：带 `fact_key` 的单元走 `facts`（`set_fact`，先失效后插入），
  不带的是情景事件走 `episodes`（向量召回）。分流点在 `worker._process_unit`。
- 写入门槛按类型分档（`Policy.min_write_confidence`），长期层最严：

| kind | 门槛 | 理由 |
|---|---|---|
| `raw_turn` | 0.20 | 原始轮次密度最低，留着给后续提炼 |
| `episode` | 0.35 | 情景事件宽松一些，漏记比记错更贵 |
| `summary` | 0.50 | 派生摘要要能代表原材料 |
| `longterm` | 0.80 | 模型推断的事实必须够硬；人工写入不受此限 |

### 决策二：模型只提议，规则做裁决

`app/decide.py` 是纯函数，四态判定表（按优先级，第一条命中即返回）：

| 模型说 | 条件 | 规则裁决 | reason |
|---|---|---|---|
| 不认识的动作 | — | **drop** | `invalid_action` |
| drop | — | drop | `model_drop` |
| 任意 | 置信度 < 该 kind 的门槛 | **drop** | `below_write_threshold(kind)` |
| accept | 候选里有逐字相同的行 | **merge**（幂等） | `accept_deduped_to_merge` |
| accept | 其余 | accept | `model_accept` |
| merge / defer | `memory_id` 不在候选集里 | **accept** + 待复核 | `unknown_target` |
| merge / defer | 目标 `source = human` | **drop** | `protected_source` |
| merge | 目标置信度 − 新置信度 ≥ 0.15 | **drop** | `merge_confidence_gap` |
| merge | 其余 | merge（+0.05 conf / +0.02 imp） | `model_merge` |
| defer | 与目标内容归一化后相同 | **merge** | `defer_no_conflict` |
| defer | 其余 | defer + 冲突边 | `model_defer` |

四个细节值得单独说：

- **模型编 id 不丢信息**。原稿的 `next(c for c in candidates if ...)` 会抛 StopIteration；
  现在的策略是"不能确认是同一条，就当新内容收下"，但标 `review_state='pending'` 等复核。
- **human 来源一律不动**。不是"高置信才保护"—— `source='human' + confidence=0.3`
  照样不许模型改。置信度低只说明系统不确定，不说明可以覆盖。
- **弱证据不许抬高强记忆**。merge 是把旧行置信度往上推，所以必须挡住
  "0.60 的推断把 0.95 的旧结论推到 0.65"这种温水煮青蛙。
- **落库内容用抽取原文**，不落模型的 `new_content`（默认 `trust_llm_rewrite=False`）。

### 决策三：记忆单元按事件切分

`kind` 字段 + 抽取提示词双重约束。固定长度切片会把"问题→决策→结果"切成碎片，
所以提示词里写明"一个单元对应一个有始有终的事件"，而 TTL 策略挂在 kind 上，
切错了在过期时间上立刻能看出来。

---

## 二、四态落库与"取代而非删除"

```
accept  → INSERT 新行
merge   → UPDATE 旧行 SET confidence = LEAST(1.0, confidence+δ), use_count+1
          （不新建行；带 AND valid_until IS NULL，rowcount=0 说明目标已失效）
defer   → INSERT 新行 review_state='pending'（默认不召回）
          + INSERT memory_conflicts（existing_id, incoming_id, status='open'）
drop    → 什么都不做
```

**矛盾被裁决之后**才取代。`resolve_conflict(conflict_id, resolution)`：

| resolution | 效果 |
|---|---|
| `superseded` | 旧行 `valid_until=now()` + `superseded_by=新行`；新行 `review_state='none'` |
| `kept_old` | 旧行不动；新行 `review_state='rejected'` + 闭合（留在表里可追溯） |
| `merged` | 旧行闭合、新行转正，且新行吸收两条的置信度/重要性上限 |

一个函数做完，保证"边状态"和"行状态"不会只改一半；重复裁决直接报错。
遗忘走 `forget_episode()`（软删除 + tombstone，可 `restore_episode()` 恢复），
物理删除只在用户级 purge。

**为什么当前有效的判据是 `valid_until IS NULL` 而不是 `superseded_by IS NULL`：**
后者表达不了"失效但没有接替者"（软删除）的情形 —— 那条记忆会继续被召回。
这个 bug 是真连库跑 demo 才抓到的，selftest `[7b]` 现在盯着它。

---

## 三、检索：召回 → 融合 → 塑形 → 预算

一条 SQL 里做完前三步（`sql/02_retrieval.sql`）：

1. **两路并行召回**：向量（HNSW）+ 词法。
2. **RRF 融合**，`k=60`：`1/(60+rank_v) + 1/(60+rank_k)`。
3. **复合打分**（四个乘数，都可单独关掉）：

       score = rrf
             × 高斯衰减(max(0, age-offset)/scale)      -- 时间：越久越轻
             × (1 + 0.2·log10(1 + use_count))          -- 频率：对数，防膨胀
             × (1 - w + w·importance)                  -- 重要性：w 默认 0.5

4. **标签过滤**（可选）：`tags && ARRAY[...]`，走 `idx_ep_tags` 的 GIN。
5. **预算封顶**在应用层（`retrieve.render_memories`）：按分数顺序累加，
   超预算截断，但第一条永远不整个丢掉。

> `importance` 和 `tags` 这两个字段最初只建了列、没进查询 —— 属于"说好要用却没用"的
> 静默缺口（不报错，只是重要记忆没被优待、按标签也筛不出来）。现在由 selftest `[14]`
> 三向断言盯着：权重可控（0/1 两端）、标签过滤生效、空标签不过滤。

### 高斯衰减（本版变化最大的一处）

    decay_factor = decay ^ ((max(0, age_days - offset_days) / scale_days) ^ 2)

`offset=180 / scale=1825 / decay=0.5` 的实测值（`npm run check` ③ 打的就是这张表）：

| 记忆年龄 | 0d | 90d | 180d | 365d | 730d | 2005d | 3650d |
|---|---|---|---|---|---|---|---|
| 高斯（本版） | 1.000 | 1.000 | 1.000 | 0.993 | 0.939 | 0.500 | 0.082 |
| 指数半衰期 90d（上一版） | 1.000 | 0.500 | 0.250 | 0.060 | 0.004 | ≈0 | ≈0 |

上一版的曲线把一年前的记忆压到 0.06，等于**静默删除**"去年那次事故"。高斯 + 免罚期
才是记忆的自然消退：近期经历不被额外加权，老案例也不会因为"老"被判死。
要更激进的近期偏好，把 `offset/scale` 一起调小即可（`scale=90, offset=0` → 90 天即 0.5）。

频率加成改成 `1 + 0.2·log10(1+use_count)`：`use_count=9` 时是 1.2，`=99` 时是 1.4，
高频条目不会无限膨胀压过新鲜内容。

### ⚠️ 一个实测标定出来的坑：乘数会主导 RRF

上面两个乘数（频率、重要性）本来是设计成"微调"的，但**实测下来它们会主导排序**。
原因是 RRF 的分数带极窄：

```
rank 1 → 1/(60+1) = 0.016393
rank 3 → 1/(60+3) = 0.015873   （比 rank1 低 3.2%）
rank 4 → 1/(60+4) = 0.015625   （比 rank1 低 4.7%）
```

也就是说**任何超过 5% 的乘数摆幅都能推翻 2~4 个名次的语义差距**。而
`(1-w+w·importance)` 在 w=0.5 时，对 importance 0.6 vs 0.8 就有 **12.5%** 的摆幅 ——
我手写时随手给技术笔记打了 0.8、普通记忆默认 0.6，于是"我写过的笔记"稳定压过
真正相关的记忆，看起来像"召回不准"，其实是打分被重要性劫持了。

校正原则：**乘数摆幅必须小于相邻名次差**。

| 参数 | 原值 | 现值 | 理由 |
|---|---|---|---|
| `importance_weight` | 0.5 | **0.25** | 同样的 importance 差 0.2，摆幅从 12.5% 降到 5%，落在一个名次台阶内 |
| 使用反馈时机 | 每次 `recall` 都 +1 | **只在 `--mark-used` 时 +1** | `retrieved ≠ used`：调试时反复召回同几条，会把它们永久顶到榜首 |

第二条是踩出来的：`use_count` 最高的那条被刷到 15 次（全是我的测试），
`1+0.2·log10(16)=1.24` 的加成让它对**任何**查询都进前几名。
这类污染不会自我修复，只会自我强化 —— 所以宁可不记，也要记准。

### 两条衰减曲线不能混用

| | 按什么衰减 | 为什么 |
|---|---|---|
| 情景记忆 | `event_time` | 越久远的事件越不重要 |
| 长期事实 | `last_used_at` | 事实本身不变旧，衰减的是"近期没被需要" |

事实层用 `recall_facts()`：`score = importance × 高斯(last_used_at 年龄)`，
免罚期 30 天、尺度 365 天。混用会系统性误伤高频有价值的旧事实，而且很难从症状
反推回原因 —— 所以干脆用列的存在性来防呆。

### 关键词路：中文必须靠子串

实测（`npm run check` ①② 打的就是这些数）：

```
similarity('时区', '用户的时区是上海')        = 0.000
word_similarity('时区', '用户的时区是上海')   = 0.000
word_similarity('timezone', 长英文记忆)       = 1.000
```

pg_trgm 给字符串两端补空格再造三元组：`'时区'` → `'  时'`,`' 时区'`,`'时区 '`，
其中两个带空格，而长文本里只有 `'的时区'` —— 交集为 0。所以中文短查询用 `%` 或 `<%`
**双双召回不到**。判据因此是两条 OR：`strpos(content,$3) > 0`（中文/短查询，零歧义）
或 `$3 <% content`（英文/长查询，可走 GIN）。真实语料上 `content % q` 大概率是 0 行。

---

## 四、分类型 TTL：查询层过滤，不做清理作业

| kind | TTL | 过期后 |
|---|---|---|
| `raw_turn` | 30 天 | 不再召回（行还在，供后续提炼/审计） |
| `episode` | 90 天 | 不再召回 |
| `summary` | 3650 天 | 不再召回 |
| `longterm`（episodes 里的） | 不过期 | — |
| `facts` | 不过期 | 靠 `superseded_by` 与衰减权重管理 |

- `ttl_interval(kind)` 是**唯一**真源；`expires_at` 由 `BEFORE INSERT` 触发器算出，
  任何写入路径都不可能忘记算。
- 过期在查询层过滤：`AND (expires_at IS NULL OR expires_at > now())`。
  定时清理作业的问题是有窗口期，而窗口期内过期记录照样会被召回，作业挂了还没人知道。
- `lifecycle_state`（fresh/aging/faded/expired）是**派生展示字段**，
  由 `lifecycle_of(kind, ingested_at)` 算、`refresh_lifecycle()` 批量刷，
  **不参与检索** —— 真相只有 `expires_at`。

---

## 五、参数速查表

| 参数 | 值 | 说明 |
|---|---|---|
| HNSW `m` | 8 | 从默认 16 下调，索引体积近半；百万级以内召回损失可接受 |
| HNSW `ef_construction` | 64 | 保持默认，只影响建索引质量，不占查询期内存 |
| `hnsw.ef_search` | `max(4*limit, vec_k)`，量级 20–100 | **按查询设**，`set_config(...,true)` 不污染连接池 |
| `hnsw.iterative_scan` | `relaxed_order`（pgvector ≥ 0.8） | 有 `user_id` 过滤时几乎必开 |
| RRF `k` | 60 | 原始论文默认值 |
| 情景衰减 `offset` / `scale` / `decay` | 180d / 1825d / 0.5 | 高斯 + 免罚期（见上表） |
| 事实衰减 `offset` / `scale` / `decay` | 30d / 365d / 0.5 | 按 `last_used_at`，不是事件时间 |
| 频率加成权重 | 0.2（log10） | 对数保证高频不膨胀 |
| **重要性权重** | 0.5 | `1 - w + w·importance`，落在 [0.5, 1.0]；0 = 不参与 |
| 标签过滤 | 无（不过滤） | `tags && $14` 走 GIN；空则不过滤 |
| 召回条数 | 1–2（简单）/ 5–10（复杂） | `plan_recall()` 按复杂度决定 |
| token 预算 | 400（简单）/ 1200（复杂） | `render_memories` 按分数截断 |
| 向量维度 | 768 或更低 | 降维要走 Matryoshka 截断**并重新 L2 归一化** |
| 裁决候选数 | 3–5 | 多了模型反而选错；余弦距离阈值 0.35 ≈ 相似度 0.65 |
| `pg_trgm.word_similarity_threshold` | 默认 0.6，按语料标定 | 只管 `<%`（英文/长查询）那一路 |

成本对比那组数字（自建 $163–332 vs Zep $375–750 vs Letta ~$1,020）量级可信，但省下的
大头通常不是数据库，而是 **embedding 调用费**：20 万次读写 ≈ 40 万次 embedding。
先把 embedding 换成 768 维小模型，比调 HNSW 省得多。

---

## 六、落地路径

- **阶段一**：`facts` 存长期事实（按键取）+ `episodes` 存情景事件（语义召回）。
  解决 80% 的"记不住偏好"和"找不到历史案例"。
- **阶段二**：写入加四态裁决 + 双时态字段。解决"旧值压新值"和"脏数据落库"。
- **阶段三**：检索加词法混合 + RRF + 分类型衰减 + 预算封顶。解决精确 token
  （ID、错误码）被向量距离淹没，以及 token 成本失控。
- **阶段四**：只有当关系型问题（"这个客户所属集团下其他子公司怎么处理的"）
  在真实请求里反复出现、且向量明显答不对时，再加 Apache AGE。图是最后加的一层。

一句话：**写入时克制**（模型提议、规则裁决、四态分流、取代不删除）、
**存储时分离**（事实走键、事件走向量、关系走图）、
**读取时精准**（多路召回、RRF 融合、分类型衰减、渐进披露、预算封顶）。

---

## 七、我验证了什么 / 没验证什么

### 真跑过的（PostgreSQL 18.6 + pgvector 0.8.7 + pg_trgm 1.6）

两条独立路径都跑通了 —— PGlite（WASM）与**本机原生 Windows PG**（MSVC 编译）：

```
powershell -File scripts\verify-all.ps1 -Dsn "postgresql://mem:mem@127.0.0.1:5432/mem"
→ PostgreSQL 18.6 on x86_64-windows, compiled by msvc-19.44.35229
→ 装载 01..07 + 18 组 SQL 断言        [OK]
→ verify_all.py 57/57                 [OK]
→ verify_workmem 43/43, verify_distill 33/33, verify_graph 53/53   [OK]
→ app.demo + worker --once            [OK]
→ 全部验证通过 (exit 0)
```

- **七份 SQL 全部执行通过**：`npm run check`。37 个对象、37 个自定义函数。
- **`99_selftest.sql` 的 18 组断言全过**：失效链、事实唯一性、外键延迟、队列领取/回收/死信、
  RRF 双路独占命中、高斯曲线与公式逐项对齐、未来事件不放大、两条失效路径都不召回、
  **TTL 按 kind 计算 + 过期不召回**、**merge 不新建行且改不到失效行**、
  **defer 待裁决不召回 + 裁决后取代 + 重复裁决被拒**、
  **图扩展的归并/幂等/防环/第三路进 RRF**、**整用户清除的二次确认与跨层干净**。
- **`python -m app.demo` 跑通四态**：日志里能看到
  `longterm → set_fact`、`model=accept`、`model=defer`（建冲突边）、`model=merge`（抬信号），
  以及裁决后新值转正、旧值退出召回。
- **86 条 Python 单测**（28 + 33 + 25）全绿。

- **`verify_all.py` 57/57 项通过**：Store / Worker / retrieve 的每个对外入口都在真库上打了一遍 ——
  入队去重、领取/确认/失败退避/租约回收、aborted 事务后仍能记账、四态落库、
  两条裁决路径（`resolve_conflict` / `resolve_conflict_for`）、软删除与恢复、TTL、
  事实取代与 bump、预算截断、health/stats、黑盒 `worker --once`。
- **`verify_graph.py` 53/53 项通过**：实体规范化归并、别名不新增实体、label 不被降级、
  关系幂等与置信度取高、自环被拒、多跳与深度上限、成环不死循环、关闭的关系不参与遍历、
  未知实体不挂载、**图失败不拖死记忆写入**、**第三路真的把两路够不到的记忆带出来**、
  `via_entity` 必须对应最近那一跳、关闭图路后结果与两路完全一致、
  整用户清除的 dryrun 对账与"不误伤他人"。

真库里才暴露、静态看 SQL 绝对看不出的 bug（都已修并写进断言）：

1. **`embedding <=> %(vec)s` 报 UndefinedFunction** —— psycopg 把 `list[float]` 编成
   `double precision[]`，必须显式 `::vector`。
2. **任务失败后连"记录失败"都失败** —— 连接停在 aborted 事务里，`fail()` 抛
   `InFailedSqlTransaction`，任务永久卡在 `processing`。现在所有读写都过
   `_read()`/`_write()`，异常必回滚。
3. **`valid_until='infinity'` 让 psycopg 抛 DataError**（JDBC 同样）；
   换 `'9999-12-31'` 哨兵在 UTC+8 会话下溢出成 `10000-01-01`。最终用 `NULL = 未失效`。
4. **判据用 `superseded_by IS NULL` 时，软删除的行仍会被召回** ——
   `invalidate` 没有接替者可写。判据统一成 `valid_until IS NULL`。
5. **PREPARE 的参数类型表漏一个 `float8`** —— `LIMIT $11` 拿到的是 boolean，
   报 `argument of LIMIT must be type bigint`。SQL 里 12 个占位符就要 12 个类型。
6. **`resolve_conflict_for` 报 AmbiguousColumn** —— `RETURNS TABLE` 里的 `existing_id`
   与 `memory_conflicts.existing_id` 撞名。plpgsql 是**运行时**解析 SQL 的：建函数不报错，
   只有真调用才炸；而 SQL 自检当时只覆盖了 `resolve_conflict`。现在两边都限定表别名，
   并且 `[12]` 会真的调一次 `resolve_conflict_for`。

### 没验证的

- **没有在真的 `pgvector/pgvector:pg17` 镜像上跑过**（本机没有 Docker）。但已在**本机原生
  PostgreSQL 18.6（MSVC 编译）+ pgvector 0.8.7** 上全套跑通，这条风险已大幅降低。
- **HNSW 的真实召回率与执行计划**：库里只有十几条记忆，规划器必然走顺序扫描，
  `ef_search`/`iterative_scan` 的行为要等真实数据量（万级）才能验。
- **多进程并发**：`SKIP LOCKED` 与 advisory lock 需要两个以上真连接才能验；
  selftest 里的领取/回收是逻辑验证，不是并发验证。
- **四态裁决的效果**：`HeuristicLLM` 是规则替身，它能证明"管线通、退化路径不炸"，
  不能证明"defer 判得准"。这个词要拿真模型 + 真实语料调。
  （向量侧已经是真模型了：见第八节的召回评估数字。）

### 与设计稿不一致的两处（都是有意为之）

1. 设计稿写 `valid_until DEFAULT '9999-12-31'` —— 本实现是 `NULL = 未失效`，
   理由见上面第 3、4 条 bug（哨兵会溢出、且 `superseded_by` 表达不了软删除）。
2. 设计稿写 `facts` 主键 `(user_id, key, valid_from)` —— 本实现是代理主键 +
   `UNIQUE (user_id, key) WHERE valid_until IS NULL`。`now()` 是事务开始时间，
   同一事务写同一 key 两次会撞主键；而那个主键还允许同一 key 有多条"当前值"，
   与表的语义相反。

## 八、记忆写进数据库（日常用法）

约定：**记忆不再写文件，一律写进这个 PostgreSQL**。入口是 `scripts/mem.py`：

```bash
python scripts/mem.py doctor                            # 体检：连库/扩展/表/规模
python scripts/mem.py init                              # 装载 sql/01..03（幂等）
python scripts/mem.py add "用户偏好深色主题"             # 写一条情景记忆（走四态裁决）
python scripts/mem.py recall "时区"                     # 混合召回
python scripts/mem.py recall "pgvector 怎么编译" --full   # 摘要不够时下钻全文
python scripts/mem.py recall "环境变量" --tags powershell   # 标签过滤（逗号/空格都行）
python scripts/mem.py recall "编译" --importance-weight 0   # 关掉重要性权重
python scripts/mem.py fact set timezone Asia/Shanghai   # 长期事实（按键精确取用）
python scripts/mem.py fact get [key]
python scripts/mem.py list --limit 20                   # 记忆 + TTL + 评审状态
python scripts/mem.py conflicts [--resolve superseded]
python scripts/mem.py forget <id> | restore <id>
python scripts/mem.py health
```

DSN 优先级：`MEM_PG_DSN` > `postgresql://mem:mem@127.0.0.1:5432/mem`。

### 向量化：本地 ONNX（默认）

默认用 `onnx` —— 本地 `bge-base-zh-v1.5`（int8 量化 98 MB、768 维、CLS 池化），
离线、免密钥、维度正好等于 `episodes.embedding vector(768)`，**换上去不需要改列**。

```powershell
$env:MEM_EMBED_MODEL = "onnx"      # 或 onnx:<模型目录>
$env:MEM_EMBED_DIM   = "768"
$env:MEM_ONNX_MODEL_DIR = "vendor/models/bge-base-zh-v1.5"
```

实测召回（14 条语料 / 12 条测试查询，`scripts/eval_recall.py`）：
**Top-1 = 10/12、Top-3 = 12/12**；查询侧**不加**指令前缀（加了 Top-1 反而掉到 9/12，
符合 bge v1.5 已取消指令要求）。

## 九、工作记忆层与任务状态层

这两层在设计稿里是**独立于记忆检索**的，边界必须守住：它们的访问模式不是"召回"。

| 层 | 表 | 访问方式 | 生命周期 |
|---|---|---|---|
| 任务状态 | `tasks` + `task_events` | 按 `task_id`/`user_id` **直查** | 终态后保留供审计，可设 retention |
| 工作记忆 | `working_sets` + `working_items` | 按 `set_id` **装配**（有序、可复现） | 默认 24h，**过期即删** |

### 三个关键设计点

1. **pin 时快照，不引用活数据。** 底层 `episodes` 之后被取代/过期/软删除，本任务读到的
   上下文**不会跟着变** —— 否则同一个任务两次装配得到不同上下文，行为不可复现。
   `working_items.ref_id` 只是溯源软引用（不建外键）。
2. **装配不检索。** `render()` 不接收 query，永远按 `position` 升序返回同样的结果。
   顺序由调用方显式决定（`--position`），系统从不重排。
3. **过期即删，与记忆相反。** 情景记忆"取代不删除"，工作记忆是临时上下文缓存，
   过期就物理删 —— 留着只会变成第二份脏语料。

### 状态机由数据库守卫

非法迁移直接报错，而不是悄悄写进去：

```
pending  -> running / blocked / cancelled
running  -> running(改 step) / done / failed / blocked / cancelled
blocked  -> running / failed / cancelled
终态     -> 任何迁移都被拒（要续做请新开任务）
```

另有两条 CHECK：终态必须带 `finished_at`；`failed` 必须带 `error`。
每次推进都追加一条 `task_events`，事后能回答"当时怎么一步步走到这里"。

### 用法

```bash
# 任务
python scripts/mem.py task start --kind deploy --title "上线验证"
python scripts/mem.py task update <id> --status running --step build --step-index 1 --progress 0.4
python scripts/mem.py task get <id>          # 状态 + 事件流
python scripts/mem.py task list [--all]
python scripts/mem.py task done|fail|cancel <id> [--result JSON] [--error "..."]
# 注意：只有 done 把 progress 推到 100%，failed/cancelled 保留真实进度

# 工作记忆
python scripts/mem.py work open --task <id> --note "本次上下文" --budget 800
python scripts/mem.py work pin-note   <set> "先跑 selftest 再交付"
python scripts/mem.py work pin-memory <set> <episode_id> [--full]
python scripts/mem.py work pin-fact   <set> <key>
python scripts/mem.py work pin-task   <set> <task_id>
python scripts/mem.py work show   <set>
python scripts/mem.py work render <set> [--budget N]   # 按预算装配成文本
python scripts/mem.py work clear  <set> | work expire
```

代码里用 `app/workmem.py` 的 `WorkMemory(store)`，接口同名。

### 这两个层踩出来的两个坑（都记进库了）

- **`SELECT (f()).*` 会把函数按输出列重复求值。** `tasks` 有 16 列，一次
  `task_start` 就插了 16 行、事件重复十几条 —— 表现为"调一次多出十几个任务"。
  正确写法是 `SELECT * FROM f(...)`。
- **psycopg 不认自定义复合类型。** 函数返回 `TABLE (task tasks, events jsonb)` 时，
  `task` 会变成 `(uuid,text,...)` 字符串，Python 侧 `t["title"]` 直接 TypeError。
  函数返回层用 `to_jsonb(t)` 最稳。

## 十、distill 提炼：raw_turn 到期前必须压成 summary

设计稿写"原始轮次 30 天 TTL，过期后仅保留提炼结果"。**如果不做提炼，30 天后用户那句话就彻底没了** ——
这是记忆系统"悄悄丢东西"的典型形态：不报错、不告警，只是某天开始查不到。

| 项 | 值 |
|---|---|
| 输入 | `episodes.kind='raw_turn'`，且 `event_time` 够老（默认 7 天）、未过期、未被提炼过 |
| 输出 | `episodes.kind='summary'`，TTL **3650 天**，有向量、能被正常召回（一等公民） |
| 血缘 | `distill_links(summary_id, raw_id)`，双向可查 |
| 原始轮次 | **不因为被提炼就删**，按自己的 TTL 自然过期；要提前回收才用 `--close-raw` |

### 两个后端

| 后端 | 谁写摘要 | 需要 LLM | 质量 | 适用 |
|---|---|---|---|---|
| `heuristic` | 抽取式：挑信息量最高的几句 | ✘ | 一般，**但不编造** | 默认、后台批量 |
| `agent` | **我**（Agent-in-the-loop）读原文写要点 | ✘ | 接近 LLM | 重要批次 |
| `llm` | 提示词驱动 + **接地校验** | ✔ | 最好 | 配好 `MEM_LLM_BASE_URL` 后 |

**默认不用 LLM 是有意的**：提炼跑在后台批量执行，一次调用失败就等于"记忆静默丢失"；
抽取式后端不会失败、结果可复现，而且抽取天然不产生幻觉。LLM 是升级项，不是前提。

### `llm` 后端的接地校验（这一节是"能不能用 LLM"的关键）

配了端点之后，最大的风险从"调用失败"变成"**编造精确值**"：模型写出一个原文里不存在的
端口、版本、阈值、路径。这种错误在记忆系统里危害最大 —— 它会被当成事实反复召回，
而且事后无法与真实的精确值区分。

所以 `LLMDistiller` 落库前过一道 `_grounded()`：**摘要里出现的每个数字都必须在原文里出现过**。

```
模型输出 "- 端口是 9999"  + 原文 "端口是 5432"   → 校验失败 → 重试
重试 2 次仍不接地                              → 抛错（队列重试 / 进死信）
```

**绝不静默落一条可疑摘要** —— 宁可漏掉一批提炼（候选还在，下轮再来），
也不能把编造的精确值写进长期记忆。

一个实现细节：模型很爱输出 `1. 2. 3.` 编号，如果直接拿去比数字，
每条正常输出都会被判成幻觉。所以行首的列表标记（`-` `*` `•` `1.` `1)` `1、` `①`）
在校验**之前**剥掉；而 `5432 是端口` 这种以数字开头的内容不会被误伤
（模式要求序号后面紧跟 `.` `)` `、` 或圈号）。`tests/test_distill.py` 里两条断言分别锁住这两种情况。

### 怎么让 Agent 来写（推荐路径）

```bash
# 1. 建提炼任务 + 工作集，把候选原文 pin 进去（复用第 9 节的两层）
python scripts/mem.py distill plan
#    → 输出 task_id / set_id / 待压条数 / 以及装配好的原文

# 2. Agent（我）读上面那段原文，写摘要，然后写回
python scripts/mem.py distill apply <task_id> --file 摘要.md
#    → 写 summary 记忆 + 落血缘 + 关闭任务

# 其他用法
python scripts/mem.py distill status          # 看待提炼积压（含 7 天内将过期的）
python scripts/mem.py distill run             # heuristic 全自动跑一批
python scripts/mem.py distill run --close-raw # 顺便提前闭合原始轮次
```

任务进 `tasks`（kind=distill），进度与事件流都在；原文进 `working_sets` 的 pin 项。
**所以"提炼"这件事本身就是可追踪的工作流，不是一次黑箱调用。**

### 一条容易忽略的不变量

`heuristic` 的每条要点都必须是**原文片段**（`verify_distill.py` 会逐条断言 `b[:15] in 原文`）。
抽取式做不到"改写得更漂亮"，代价换来的是"绝不可能凭空生成一条假记忆"。

## 十一、图扩展：第三路召回（没有用 Apache AGE）

设计稿把图放在第四阶段，并写着"只有当关系型问题反复出现、向量明显答不对时再加"。
现在加上了，但**没有引入 Apache AGE**：

| | Apache AGE | 本实现（递归 CTE） |
|---|---|---|
| 装法 | Windows 上要 flex/bison + 改 PG 解析钩子编译 | 纯 SQL，`psql -f` 就完事 |
| 查询语言 | Cypher（第二套语言） | SQL |
| 版本绑定 | 与 PG 大版本强耦合 | 无 |
| 够不够用 | 开放式图查询才需要 | "从种子实体走 N 跳取记忆"完全够 |

这个取舍是**有条件的**：真要做 Cypher 风格的开放式图查询，就把这一层换到 AGE，
`entities / relations / episode_entities` 三张表的结构不用动。

### 它解决的是哪种问题

```
记忆 A: "阿里云是某集团下属子公司，用的是杭州机房"     ← 有"阿里云"三个字
记忆 B: "该集团要求所有子公司统一走内网审计"           ← 没有"阿里云"

查询: "阿里云那边的合规要求是什么？"
  向量路: 命中 A（B 的语义在"合规/审计"上偏，容易被 A 压过）
  词法路: 只能命中 A（B 里没有"阿里云"）
  图路  : 阿里云 --belongs_to--> 某集团，B 挂在"某集团"上 → **B 被带出来**
```

`verify_graph.py` 里就是这么断言的：两路只召回 1 条，三路召回 2 条，
且被带出来的那条 `rk_g` 有值、`rk_k` 为空（证明它是图路单独贡献的）。

### 三条设计取向（都是为了不把图用坏）

1. **不猜实体.** 实体只有两个来源：显式声明（`mem.py graph add` / `add --entity`），
   或从正文里认出**库里已知**的实体名。绝不做 NER 猜新实体 —— 猜错的实体比没有实体更糟：
   它会把不相关的记忆用"关系"粘在一起，而这种污染不会自己消失。
2. **不删关系.** `relations` 也是双时态（`valid_until IS NULL` = 当前有效），
   口径与 `episodes` 完全一致，历史可回溯。
3. **不拖死写入.** 挂图是 best-effort：`_link_graph()` 里任何异常只记 warning，
   **绝不让一次记忆写入因为图失败而失败**。`verify_graph.py` 有一条断言专门制造
   `auto_link` 抛异常，验证返回 0 且不影响写入。

### 用法

```bash
# 建实体 / 建关系（自动建两端）/ 把已有记忆挂上去
python scripts/mem.py graph add 阿里云 --label org --alias Aliyun 阿里云公司
python scripts/mem.py graph relate 阿里云 belongs_to 某集团 --confidence 0.9
python scripts/mem.py graph link <记忆id> --entity 阿里云

# 写记忆时顺手声明实体（会建实体 + 挂图 + 再从正文补挂已知实体）
python scripts/mem.py add "阿里云改用杭州机房" --entity 阿里云 --entity 杭州机房

# 看
python scripts/mem.py graph stats                 # 实体 + 当前有效关系
python scripts/mem.py graph neighbors --seed 阿里云 --depth 2
python scripts/mem.py graph route "阿里云那边的合规要求"   # 第三路会给召回什么

# 召回时默认就带第三路；关掉对比：
python scripts/mem.py recall "阿里云合规" --no-graph
```

召回输出多了一列 `rk_g`，路径标记多了一位：`vk-` = 两路命中，`vkg` = 三路都命中，
`--g` = 只有图路命中（这种就是图扩展真正带来新信息的情形）。

### 两个并发/正确性细节

- **成环**：递归 CTE 带 `visited` 数组防环，`depth` 上限兜底。selftest `[17]` 造了一个
  3 节点环，断言"深度 5 仍是 3 个实体"。
- **无向遍历**：关系两个方向都算。所以"关掉 belongs_to 但仍能从 serves 绕回来"是**预期行为**，
  selftest 里必须全关才断言"只剩种子"—— 这条一开始我写错了断言，是测试教会我的。

### 一个必须说清的副作用

RRF **不做归一化**：三路等权。所以图路能救回"向量排第 8"的记忆，也能把"向量排第 3"的挤下去。
真库上的实测（`mem.py recall "记忆库跑在什么数据库上"`）：

```
带图路：rk_v=2 rk_g=10 ← 向量第 2，图第 10 → 合起来第 1
        rk_v=1 rk_g=1  ← 两路都强
        rk_v=8 rk_g=11 ← 向量只排第 8，靠图路进前四
不带图：向量排名说了算
```

这是**设计意图**（关系相关的记忆就该被提到前面），但它同时意味着：
**图里的实体必须是对的**。一条乱挂的边会持续把无关记忆推进召回结果，
而且没有任何指标会报警。这就是"绝不猜实体"那条取向的真正原因 ——
不是洁癖，是这条路径的收益和风险来自同一个地方。


## 十二、整用户清除（GDPR 式硬删）

记忆系统里"用户要求删除"是绕不过去的：软删除（`forget_episode`）保留的是"可回溯"，
而合规要求的是"真的不存在了"。

```bash
python scripts/mem.py purge            # 只打印清单，不动数据
python scripts/mem.py purge --yes      # 真删（不可恢复）
```

三条护栏：

1. **二次确认**：SQL 层的 `purge_user(p_user_id, p_confirm)` 要求 `p_confirm` 与
   `p_user_id` **完全一致**，否则抛错。CLI 的 `--yes` 只是个额外的门闩 ——
   真正的保险在数据库里，因为它挡得住任何绕过 CLI 的调用。
2. **dryrun 对账**：`purge_user_dryrun()` 先给出每张表要删多少行，
   `verify_graph.py` 会断言"dryrun 的数字与实际删除一致"。
3. **跨层清干净**：一个用户的痕迹散在 8 张表里 ——
   `distill_links`（血缘）→ `relations`/`episode_entities`/`entities`（图）→
   `working_items`/`working_sets`/`task_events`/`tasks`（工作记忆与任务）→
   `memory_jobs`/`memory_conflicts`（队列与冲突）→ `episodes`/`facts`。
   **漏一张表就是"删了但还是能查到"**。
   图那三张表用 `to_regclass` 兜底，所以 `07_purge.sql` 在没有装图层的库上也能加载。

`verify_graph.py` 里同时验了"不误伤他人"：另一个用户的记忆、事实、实体在清除后必须完好。

## 十三、日/周/月/年关键词提炼

情景记忆是"一条条的"，但人回忆时是按**时段**回忆的——"上周主要在忙什么""上个月有哪些事"。
逐条向量召回答不了这种问题；而且 `raw_turn` 30 天就过期，过一个月连原料都没了。

所以加一层 rollup：把时间桶里的记忆压成「关键词 + 代表事项」，落成一条**可召回的 summary 记忆**。

```bash
D:\DSH proj\AI记忆\mem.cmd digest build --period week     # 构建（幂等，可重复跑）
D:\DSH proj\AI记忆\mem.cmd digest build --period all      # 日/周/月/年一起
D:\DSH proj\AI记忆\mem.cmd digest show                    # 近几期
D:\DSH proj\AI记忆\mem.cmd digest keywords                # 跨期关键词排行
D:\DSH proj\AI记忆\mem.cmd digest stats                   # 每期覆盖情况
```

| 项 | 值 |
|---|---|
| 表 | `memory_digests`（结构化词项 + `keywords text[]` 走 GIN） |
| 可召回产物 | `episodes(kind='summary', tags=['sys:digest','week','2026-W43'])`，TTL 3650 天 |
| 分桶依据 | **`event_time`**，不是 `ingested_at` |
| 桶定义 | 半开区间 `[start, end)`；周 = ISO 周（周一起） |
| 抽词 | 中文 2~4 字 n-gram + 拉丁词 → 停用词 → **子串抑制** → tf-idf |
| 幂等键 | `UNIQUE(user_id, period, bucket_start)` + 摘要记忆的 `source_ref` |

### 三个不是随手定的判断

**1. 按 `event_time` 分桶。** "去年十月发生了什么"问的是事情何时发生。
补录一条 2019 年的记忆，它就该出现在 2019 年的年度摘要里，而不是"入库那天"。
`verify_digest.py` 专门插了一条 backdated 记忆来断言这件事。

**2. 半开区间。** 时间分桶最常见的 bug 就是"月末最后一天算给谁"。
闭区间会让相邻两桶都含边界日，年度汇总凭空多算。selftest `[19]` 断言了
`2026-12-01 + 1 month = 2027-01-01`、闰年 2 月、以及"相邻周桶不重叠"。

**3. 摘要不进原料。** 摘要记忆本身也是一条 episode。若不排除，年摘要会把日摘要的文本
再摘一遍——关键词自我强化，越滚越偏。判据是 `NOT (tags && ARRAY['sys:digest'])`。

保留标签**必须带命名空间**，这不是洁癖：我第一次记录本轮结论时顺手写了
`add "..." --tags digest`，三条普通记忆立刻被当成摘要排出了原料 ——
现象只是"摘要覆盖的记忆数不对"，**不报错**。改成 `sys:digest` 后这类撞车不再可能。

### 关键词为什么不用分词器也能看

```
中文 n-gram（2~4 字）+ 拉丁词  →  去停用词  →  子串抑制  →  tf-idf 排序
```

**子串抑制**是这套做法的关键。不做的话，搜 `pgvector` 会同时留下
`pgvecto / gvector / pgvect / ector`——全进关键词列表，人读不了。
规则是"被更高频（或同频但更长）的词包含时丢弃自己"，于是留下的总是更完整的那一个。

实测（本库 44 条记忆，一周）：

```
dsh / 记忆 / 模型 / 失败 / 实体 / sql / 必须 / 默认
```

`verify_digest.py` 里有一条硬断言：**每个关键词都必须能在原文里找到**
（中文查子串、英文查小写子串）。和提炼层一个原则——宁可读起来糙，也不要出现原文没有的概念。

### 为什么 tf-idf 的语料是"整段区间"

单桶各算各的 idf，得到的权重互不可比（一个桶里只有 3 条记忆时 idf 毫无意义）。
所以 `build_range` 把区间内所有文档当语料算一次 df，再回头给每个桶加权。
`verify_digest.py` 的 [4] 就是这条的直接证据：相邻两期（编译主题 / docker 主题）
关键词集合必须不同，且各自体现自己的主题。

## 十四、记忆是怎么（以及怎么没有）进入对话的

这个问题必须说清楚，因为答案是**"不会自动进入"**。

### 现状：pull，不是 push

```
用户发一句话
   ↓
DSH 把我需要的上下文给我：系统提示 + 技能目录 + ~/.dsh/AGENTS.md + 项目 AGENTS.md
   ↓
这些里面没有任何"记忆内容"，只有一句**指令**："任务依赖既有知识时，先跑 mem.py"
   ↓
我决定要不要查 → 跑 mem.cmd context / recall → 结果进上下文
```

也就是说：**记忆库是工具，不是管道。** 数据库里有 44 条记忆，不代表我在第 45 次对话里
自动知道它们；得有人（我）去查。这个"有人"目前靠两件事保证：

1. `~/.dsh/AGENTS.md` 每次都在上下文里，里面写了查询协议；
2. `mem.cmd context` 足够便宜（~550ms），让"查一下"的成本低到可以每轮都做。

### 为什么不能真的自动

我查过宿主：`lib/` 里只有入口 stub，`SessionStart` / `hooks` 这类钩子关键字
一处都搜不到——**DSH 没有"每轮开始注入一段外部内容"的扩展点**。
所以任何"自动注入"都得由宿主实现，应用侧做不了。

### 能做的三种程度

| 做法 | 自动化程度 | 代价 |
|---|---|---|
| AGENTS.md 写死协议（**当前**） | 半自动：靠模型遵守指令 | 每轮多几百 token 指令 + 我的自觉 |
| `mem.cmd context` 前言 | 手动一条命令拿到"现在该知道什么" | 一次 ~550ms + 输出进上下文 |
| 真·自动注入 | 需要宿主钩子（DSH 目前没有） | 改宿主 |

`mem.cmd context` 就是为第二条准备的，它把三层压成一段有预算上限的文本：

```
[事实]     长期事实（按键直查，最稳）
[week/day] 最近的时段摘要（比逐条记忆更适合"我在忙什么"）
[进行中]    tasks_live（状态层，不靠检索）
[待裁决]    未裁决的冲突（不裁决就是脏数据）
```

```bash
D:\DSH proj\AI记忆\mem.cmd context                    # 默认 1200 字符上限
D:\DSH proj\AI记忆\mem.cmd context --with-conflicts   # 连待裁决冲突一起
```

结论：**"每次都查"在应用侧能做的极限，是让它便宜、让指令明确**。
真正的 push 得等宿主提供钩子。



## 十五、DSH 插件（`dsh-memory-pg`）

前面所有能力都是"命令行 + AGENTS.md 里的约定"。这一节把它变成**宿主插件**：
模型有原生工具可调，记忆前言**每轮自动进上下文**——不再依赖"有人记得去查"。

```
plugin/
  package.json        dsh.bundle.patch 指向下面这份补丁
  cordis.patch.yml    insert 一行 id=memory-pg（projectRoot 是占位符，必须改）
  lib/index.js        7 个工具 + agent/pre-step 自动注入
  lib/sidecar.js      常驻 python 子进程管理（懒启动 / 超时 / 死了自愈）
  test/smoke.mjs      12 项自检（不需要 DSH）
  README.md           安装、配置、设计说明
app/facade.py         门面：参数默认值与返回结构只定义一次
app/sidecar.py        JSON-lines 服务（stdout 只写协议，日志走 stderr）
```

### 架构：为什么不把逻辑用 Node 重写

插件背后是 `python -m app.sidecar` 常驻子进程。理由不是偷懒：

- 四态裁决、向量化、时段分桶、图遍历全在 `app/` 里，已有 200+ 项断言盯着；
  用 JS 重写一遍必然出现两套语义——这个项目已经因为"两处各自实现"翻过两次车
  （`mem.py` 与 `reembed.py` 各自对齐 embedding 模型、`onnx_embed` 标签写死）。
- 成本可控：**冷启动一次 ~1s**（Python + psycopg + ONNX 模型 + SCRAM），
  之后每次 10–50ms。插件懒启动，没用到记忆的会话一分钱不付。

### 两个能力

| 能力 | 说明 |
|---|---|
| 7 个工具 | `memory_recall` / `memory_add` / `memory_fact_get` / `memory_fact_set` / `memory_context` / `memory_digest` / `memory_health` |
| **每轮自动注入** | `ctx.on("agent/pre-step")` 里插一条 plugin 消息（事实 + 近期摘要 + 进行中任务），按 `contextBudget` 截断，同一轮只采一次 |

失败语义：工具**永不抛异常**，出错返回 `…失败：<原因>` 文本；自动注入失败直接跳过。
记忆坏掉只是少了一层增强，不会让会话挂掉。

### 实测（隔离 profile `memtest`，不动现有 web 会话）

```
dump-config          → id: memory-pg 行存在，用户覆盖层把占位 projectRoot 改成真路径
memory_health        → 用户 owner｜向量模型 bge-base-zh-v1.5 / 177 条记忆 / 86 条事实
自动注入             → 让它"不许调工具，只回答问题里已有的信息"，
                       它准确列出 [事实] 节的 9 个键，并说明来自「长期记忆（自动注入）」
recall + fact 读写   → 三步串起来通过
tarball 安装         → 13.4 kB / 6 文件，从 tgz 装进 profile 后同样跑通
smoke.mjs            → 12/12（含"杀掉子进程能自愈"）
无孤儿进程           → 运行前后 python 进程数一致
```

### 落坑记录（都是真跑出来的）

1. `defineTool` 必须声明 `output.schema` + `render`，漏了在 apply 时抛
   `Cannot read properties of undefined (reading 'render')`。
2. `ctx.effect` 收的是**零参工厂**（返回 disposer），不是 `(dispose) => …`。
3. 参数 DSL 里每个字段必须有 `type`；任意值用 `json`，否则 `UNSUPPORTED_SCHEMA`。
4. 第三方插件在 profile 里**解析不到 `@deepseek-ai/*`**（那两家皮肤包都没 import）。
   所以消息对象直接构造，不 import `createUserMessage`（它只是
   `{role:"user", …input, id: randomUUID()}` 的冻结版）。
5. `file:` 依赖 + pnpm 软链会让 Node 按**真实路径**找 `node_modules`，找不到 profile 的
   依赖 → 用 `dependenciesMeta.injected = true` 让它复制。
6. `[IO.File]::WriteAllText(path, text, [Text.Encoding]::UTF8)` 会写 **BOM**，
   `JSON.parse` 直接挂（和 `.ps1` 需要 BOM 正好相反的坑）。
7. sidecar 响应里有 `UUID` → `json.dumps` 抛错 → 整个请求变成协议错误。
   `_jsonable()` 现在对未知类型一律退化成 `str`：宁可丢结构，不可丢响应。

### 发布

```powershell
cd "D:\DSH proj\AI记忆\plugin"
npm pack                 # 产出 dsh-memory-pg-0.1.0.tgz（13.4 kB，6 个文件）
npm login                # ← 需要你自己的 npm 账号
npm publish --access public
```

名字 `dsh-memory-pg` 在 npm 上未被占用（`dsh-plugin-memory` 已被别人占了 0.16.0）。
本机 `npm whoami` 目前是未登录状态，所以**发布这最后一步得你来跑**。

装进日常用的 web profile（**需要重启 dsh web**，会新开会话）：

```powershell
dsh plugin --profile web add dsh-memory-pg
# 然后编辑 %USERPROFILE%\.dsh\profiles\web\package.json，
#   在 dsh.profile.bundles 里加 "dsh-memory-pg"
# 再把 projectRoot 覆盖成真实路径（见 plugin\cordis.patch.yml 顶部注释）
```
