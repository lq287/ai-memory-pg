# AI 记忆层

[![repo](https://img.shields.io/badge/GitHub-lq287%2Fai--memory--pg-181717?logo=github)](https://github.com/lq287/ai-memory-pg)
[![plugin](https://img.shields.io/badge/DSH%20plugin-dsh--memory--pg-4B32C3)](plugin/README.md)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**单个 PostgreSQL 做长期记忆。** 不是"向量库 + 提示词拼接"，而是一套有 schema、有裁决、
有时间语义的记忆系统：13 张表、43 个函数、8 份 SQL，外加一个 DeepSeek Harness 插件。

写入时克制（模型提议、规则裁决、取代不删除）、存储时分离（事实走键、事件走向量、关系走图）、
读取时精准（三路召回、RRF 融合、分类型衰减、预算封顶）。

## 能力一览

| 能力 | 一句话 |
|---|---|
| 四层记忆 | 工作记忆 / 任务状态 / 长期事实 / 情景事件，各有自己的生命周期 |
| 四态写入 | `accept` / `merge` / `defer` / `drop`——复述变强化，矛盾等裁决，寒暄被丢弃 |
| 三路召回 | 向量 ⊕ 关键词 ⊕ 关系图 → RRF → 高斯衰减 × 对数频率 × 重要性 → token 预算 |
| 双时态 | `valid_until IS NULL` = 当前；取代不物理删，历史可回溯 |
| 分类型 TTL | raw_turn 30d / episode 90d / summary 3650d / 长期事实不过期，查询层过滤 |
| 提炼 | raw_turn 过期前压成 summary，保留血缘；3 个后端（抽取 / Agent / LLM + 接地校验） |
| 时段关键词 | 日/周/月/年 rollup：`2026-W43: pgvector/编译/msvc…`，可召回 |
| 图扩展 | 实体 + 关系 + 多跳遍历，作为召回的第三路（不用 Apache AGE） |
| 整用户清除 | `purge_user` 硬删 8 张表，二次确认 + dryrun 对账 |
| DSH 插件 | 7 个原生工具 + **每轮自动注入记忆前言** |

## 快速开始

需要 PostgreSQL 14+ 与 `pgvector`（`pg_trgm` 可选但强烈建议）。

```bash
# A. Docker（最省事；本仓库的 docker-compose.yml 未在开发机上验证过——那台机器装不上 Docker）
docker compose up -d

# B. 用已有的 PostgreSQL
createdb -U postgres mem && psql -U postgres -d mem -c \
  "CREATE EXTENSION vector; CREATE EXTENSION pg_trgm;"
for f in sql/01_schema.sql sql/02_retrieval.sql sql/03_write.sql sql/04_workmem.sql \
         sql/05_distill.sql sql/06_graph.sql sql/07_purge.sql sql/08_digest.sql; do
  psql "$MEM_PG_DSN" -v ON_ERROR_STOP=1 -f "$f"
done

# 依赖只有两个：PG 驱动 + vector 适配器
pip install -r requirements.txt --target .pylibs
```

日常入口一条命令，任意目录可用：

```bash
mem.cmd doctor                              # 连库/扩展/表/规模/健康度/图层
mem.cmd recall "pgvector 怎么装的" --limit 3  # 三路召回
mem.cmd fact get                            # 长期事实（精确键查）
mem.cmd context                             # 会话前言：事实 + 近期摘要 + 进行中任务
```

首次使用先 `mem.cmd init` 装 schema（幂等）。向量化默认走本地 ONNX（bge-base-zh-v1.5，
768 维、离线、免密钥）；没配模型时退化成 hash 替身——**替身不是语义向量，别用在真实记忆上**。

## 架构

### 13 张表

| 层 | 表 | 生命周期 |
|---|---|---|
| 工作记忆 | `working_sets` / `working_items` | 会话级，过期即删 |
| 任务状态 | `tasks` / `task_events` | 任务级，状态机由数据库触发器守卫 |
| 长期事实 | `facts` | 不过期，按 `last_used_at` 衰减 |
| 情景事件 | `episodes` | 按 kind 分 TTL，双时态 |
| 派生摘要 | `episodes(kind='summary')` + `distill_links` | 3650 天，带血缘 |
| 时段关键词 | `memory_digests` | 每 (user, period, bucket) 一条 |
| 图结构 | `entities` / `relations` / `episode_entities` | 关系双时态 |
| 支撑 | `memory_conflicts` / `memory_jobs` | 待裁决冲突、异步队列（含死信） |

### 写一条记忆

```
原文 → Fact(抽取) → 向量候选(余弦距离阈值) → 模型/规则提议 action
     → arbitrate() 规则仲裁 → store.apply() 四态落库 → best-effort 挂图
```

规则护栏（`app/decide.py`，纯函数、可单测）：模型编造的 `memory_id` 一律降级、
弱证据不许抬强记忆、`longterm` 门槛最高、同一 `source_ref` 幂等。

### 读一条记忆

```
向量路 (pgvector HNSW, ef_search 按查询算)
词法路 (strpos 精确子串 OR <% 三元组)   ──→ RRF(k=60) ──→ 高斯衰减 × 频率 × 重要性 ──→ 预算截断
图路   (递归 CTE 走关系，种子取查询里的已知实体)
```

三路等权、缺一路不补偿。默认返回摘要层，要全文再 `--full`（渐进披露第二层）。

## 打分与调参

```
score = rrf × decay ^ ((max(0, age_days - offset) / scale) ^ 2)
             × (1 + freq_weight · log10(1 + use_count))
             × (1 - w + w · importance)
```

| 参数 | 值 | 理由 |
|---|---|---|
| `rrf_k` | 60 | 常规取值，越大越平缓 |
| `offset` / `scale` / `decay` | 180d / 1825d / 0.5 | 半年内不衰减，5 年降到 0.5——老案例不会因为"老"被判死 |
| `freq_weight` | 0.2 | `log10` 保证高频不无限膨胀 |
| `importance_weight` | **0.25** | 见下 |

**实测标定出来的坑：乘数会主导 RRF。** RRF 的分数带极窄——rank1 `1/61 = 0.01639`、
rank3 `0.01587`（低 3.2%）、rank4 `0.01563`（低 4.7%）。而 `1-w+w·importance` 在 w=0.5 时
对 importance 0.6 vs 0.8 就有 **12.5%** 摆幅，足以推翻 3 个名次的语义差距。
校正原则：**乘数摆幅必须小于相邻名次差** → 默认 0.25（同样差距只有 5%）。

同一原因还有第二个决定：`mem.py recall` **默认不记 `use_count`**，要记加 `--mark-used`。
调试时反复召回同几条会把计数刷到 15，`1 + 0.2·log10(16) = 1.24` 的加成让它们对任何查询
都进前列，而且自我强化、不报错。

### 两条衰减曲线不能混用

| | 按什么衰减 | 为什么 |
|---|---|---|
| 情景记忆 | `event_time` | 越久远的事件越不重要 |
| 长期事实 | `last_used_at` | 事实本身不变旧，变的是"多久没被需要" |

`facts` 表**没有** `event_time` 列——用列的存在性防呆，从类型层面不给写错的机会。

### 关键词路：中文必须靠子串

```
similarity('时区', '用户的时区是上海')   = 0.000   ← 三元组在中文短查询上失效
word_similarity('timezone', 长英文句子)  = 1.000
```

所以词法路 = `strpos(content, q) > 0 OR q <% content`：中文靠精确子串，英文靠三元组。

## 六层能力的关键取舍

**1. 四态写入与取代。** `merge` 不新建行，只抬 confidence/importance 并刷新 `last_used_at`；
`defer` 落库但标 `review_state='pending'`（默认不召回）+ 建冲突边，裁决后再 supersede。
判据统一是 `valid_until IS NULL`，不是 `superseded_by IS NULL`——后者表达不了软删除。

**2. 分类型 TTL。** 到期只在查询层过滤，不做清理作业。`expires_at` 由触发器按 kind 算好，
所以"过期"是一列数据，而不是一堆脚本。

**3. 提炼（raw_turn 过期前必须压成 summary）。** 三个后端：`heuristic`（抽取式、零依赖、
**天然不产生幻觉**）、`agent`（读原文写要点，走 plan/apply）、`llm`（提示词 + 接地校验）。
默认不是 LLM 是有意的：提炼跑在后台批量执行，一次调用失败就是"记忆静默丢失"。
`llm` 后端落库前过 `_grounded()`：**摘要里每个数字都必须在原文出现过**，不过就重试，
再不过就抛错进死信——绝不静默落一条可疑摘要。

**4. 图扩展（第三路召回）。** 没有引入 Apache AGE：它要在 Windows 上编 flex/bison 并改
PG 解析钩子，而需求只是"从种子实体走 N 跳取记忆"，递归 CTE 足够，表结构将来换 AGE 也不用动。
三条取向：**不猜实体**（只认显式声明或库里已知的名字，不做 NER）、**不删关系**（双时态）、
**不拖死写入**（挂图 best-effort，失败只记 warning）。

**5. 日/周/月/年关键词。** 把时间桶里的记忆压成「关键词 + 代表事项」，落成一条可召回的
summary 记忆。三个判断：按 **`event_time`** 分桶（补录旧记忆会落到它应在的时段）、
**半开区间**（避免相邻两桶都含边界日）、**摘要不进原料**（年摘要不把日摘要再摘一遍，
判据是保留标签 `sys:digest`）。抽词是中文 n-gram + **子串抑制** + tf-idf；子串抑制让
`pgvector` 不会同时留下 `pgvecto/gvector` 之类的碎片，约束是**每个关键词必须是原文子串**。

**6. 整用户清除。** `purge_user(p_user_id, p_confirm)` 要求 `p_confirm` 与 `p_user_id`
完全一致（护栏在 SQL 里，绕过 CLI 也挡得住），`purge_user_dryrun` 先对账。
清 8 张表——漏一张就是"删了但还是能查到"。

## DSH 插件

`plugin/` 把上面所有能力接进 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)：
7 个原生工具（`memory_recall` / `memory_add` / `memory_fact_get` / `memory_fact_set` /
`memory_context` / `memory_digest` / `memory_health`），外加**每轮自动注入**记忆前言——
不再依赖"有人记得去查"。安装与配置见 [`plugin/README.md`](plugin/README.md)。

```bash
dsh plugin --profile web add dsh-memory-pg   # 再把 dsh-memory-pg 加进 profile 的 bundles，重启 dsh
```

插件是薄前端：背后是常驻的 `python -m app.sidecar`（JSON-lines over stdio）。不把四态裁决
和向量化在 JS 里重写一遍——这个项目已经因为"两处各自实现"翻过两次车。冷启动约 1s，
之后每次 10–50ms；没用到记忆的会话不付这个钱。

## 验证

```bash
powershell -File scripts/verify-all.ps1 -Dsn "postgresql://mem:mem@127.0.0.1:5432/mem"
```

| 层 | 内容 | 结果 |
|---|---|---|
| 单测 | decide 28 + pipeline 41 + distill 25 + digest 21 | **115 条**，零依赖、不连库 |
| SQL | 8 份 SQL + `99_selftest.sql` | **19 组**行为断言 |
| Python↔PG | verify_all / workmem / distill / graph / digest | 57 / 43 / 33 / 53 / 31 = **217 项** |
| 跨引擎 | `scripts/sqlcheck.mjs`（PGlite = 真 PG 18 编译成 WASM） | 8 份 SQL + 7 组取证 |
| 插件 | `node plugin/test/smoke.mjs` | 12 项（含"杀掉子进程能自愈"） |

断言写的是行为，不是"函数能跑"：比如"两路只召回 1 条、三路召回 2 条，且被带出那条
`rk_g` 有值 `rk_k` 为空"、"清除后旁观用户必须毫发无损"、"空图数组不该改变结果"。

召回质量基线（38 条语料 / 12 条查询）：**Top-1 = 10/12，Top-3 = 11/12**，查询侧不加指令前缀
（加了 Top-1 反而降到 9/12）。

## 已知边界

1. **记忆不会自动进对话，除非装插件。** 宿主没有会话钩子，`mem.cmd` 是 pull；
   `dsh-memory-pg` 的 `agent/pre-step` 注入是 push。两者都可用，后者需重启宿主。
2. **`llm` 提炼后端没接过真端点**（`MEM_LLM_BASE_URL` 未配），接地校验只用假 LLM 测过。
3. **HNSW 没在真实量级验过**：库里百余条记忆，规划器必然走顺序扫描。
4. **没有多进程并发验证**：`SKIP LOCKED` / advisory lock 需两条以上真连接。
5. **Docker 路线未验证**：本机装不上 Docker，全部验证都在原生 PG 18.6 上做的。
6. **图的质量靠人喂**：一条乱挂的边会持续污染召回且无告警——这正是"绝不猜实体"的原因。
7. **口语化召回会退化**：语料里工程笔记一多，向量排名就被同质文本稀释。
   稳定偏好请写 `facts`（精确键查），不要写成 episode 去抢排名。
8. **`distill` 尚未在真实数据上触发**：还没有 `raw_turn` 类记忆。

## 目录

```
sql/       01_schema 02_retrieval 03_write 04_workmem 05_distill 06_graph 07_purge 08_digest
           99_selftest.sql（19 组行为断言，跑完自清）
app/       decide（裁决纯函数）· store（PG 读写）· retrieve · embedding（唯一工厂）
           onnx_embed · llm · worker（队列消费）· workmem · distill · graph · digest
           facade（门面）· sidecar（插件用的常驻服务）· demo
scripts/   mem.py（17 个命令）· verify_all / workmem / distill / graph / digest
           sqlcheck.mjs · pgserver.mjs（PGlite）· verify-all.ps1 · reembed · eval_recall
plugin/    dsh-memory-pg：lib/index.js · lib/sidecar.js · cordis.patch.yml · test/smoke.mjs
tests/     test_decide · test_pipeline · test_distill · test_digest（115 条，零依赖）
mem.cmd    任意目录可用的入口（自动设 PYTHONPATH/DSN/UTF-8）
```

## 踩过的坑（都写进了断言或注释）

| 坑 | 教训 |
|---|---|
| `reembed.py` 把 43 条真实向量覆盖成 hash 替身 | 模型对齐逻辑只能写一处（`align_to_database`），并硬拦"替身覆盖真向量" |
| 同一列出现两种 `embedding_model` 标签 | 那列的职责就是标记向量空间，标签撒谎 = 防线失效 |
| `use_count` 被测试刷高，永久占据榜首 | `retrieved ≠ used`；默认不记，要记显式加参数 |
| 保留标签用裸 `digest`，自己记录时撞车 | 系统判定用的标签必须带命名空间（`sys:digest`） |
| `UPDATE` 不带 `RETURNING` → "last operation didn't produce records" | 写入助手契约要求返回一行 |
| 改 `RETURNS TABLE` 的列数必须显式 `DROP FUNCTION` | 返回类型变了就是换函数，`CREATE OR REPLACE` 不覆盖 |
| `SELECT (f()).*` 按输出列重复求值 | 用 `SELECT * FROM f()` |
| 从别的工作区存记忆每次弹授权 | 真因是 `.pylibs` 的 NTFS ACL 丢了 `EVERYONE` 读权限（沙箱内 pip 安装导致），见 `scripts/fix-pylibs-acl.ps1` |
| 沙箱内写 `__pycache__` 被拒 | 记忆脚本零磁盘写入（`PYTHONDONTWRITEBYTECODE=1`） |
| `.ps1` 要 UTF-8 带 BOM；`.cmd` 必须纯 ASCII | 两种编码要求正好相反，都踩过 |

## 许可

MIT
