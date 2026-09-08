# SmileX Agent Memory — 现状架构总览(as-built)

> **版本**: v2.2 · 2026-09-04(结构重构: 大文件拆包、contracts 下沉、
> benchmarks _shared 提公;全部公共导入路径经 shim/门面保留,见 §2 末尾约定)
> **性质**: 本文描述**已实现的现状**;[agent-memory-design.md](agent-memory-design.md)
> 与 [modules/](modules/) 系列是设计时文档(2026-06,部分选型已演进,
> 如向量存储实际为 sqlite-vec 单轨而非 ChromaDB 双轨,阅读时以本文为准)。
> **配套**: [README.md](../../README.md)(使用方式 + benchmark 成绩)

***

## 目录

1. [一图总览](#1-一图总览)
2. [分层契约与包结构](#2-分层契约与包结构)
3. [公开 API(middlewares)](#3-公开-apimiddlewares)
4. [写入路径详解](#4-写入路径详解)
5. [检索路径详解](#5-检索路径详解)
6. [可插拔组件:六个 Protocol](#6-可插拔组件六个-protocol)
7. [可观测性子系统](#6a-可观测性子系统memoryobservability)
8. [存储层](#7-存储层)
8. [调度与生命周期任务](#8-调度与生命周期任务)
9. [并发控制](#9-并发控制)
10. [知识质量](#10-知识质量)
11. [服务化层](#11-服务化层)
12. [安全注记](#11a-安全注记15-4-落地状态)
13. [降级矩阵](#12-降级矩阵)
13. [性能注记](#13-性能注记)
14. [扩展指南](#14-扩展指南)
15. [关键设计决策速查](#15-关键设计决策速查)
16. [名词解释表](#16-名词解释表)

***

## 1. 一图总览

```
                     Agent / LLM 应用
                          │
            ┌─────────────┴─────────────┐
            │  MemoryMiddleware(唯一门面) │  middlewares/memory.py
            └──────┬──────────────┬─────┘
              write│         recall│
   ┌───────────────┤              ├────────────────┐
   │ 写入路径        │              │ 检索路径          │
   │               ▼              ▼                │
   │  FactExtractor ──► L0 工作记忆(cachebox LRU)     │
   │  (默认 PassThrough) │      ContextBuilder       │
   │                    ▼      ├─ L0 全量扫描          │
   │  PromotionManager(超阈值/批量 → L1)              │
   │                    │      ├─ L1 双通道: 向量KNN    │
   │                    │      │   + FTS5 BM25        │
   │                    │      │   → RRF 融合          │
   │                    │      ├─ L2 多策略: 时序/图谱   │
   │                    │      │   /空间/因果 → RRF     │
   │                    │      ├─ Reranker(可选,按层精排) │
   │                    │      └─ 贪心 token 装填        │
   │                    ▼                           │
   └──────────► SQLite 单文件库(schema v13)  ◄────────┘
            entities / triples / temporal_fragments
            causal_chains / locations(R-tree) / vec / fts
                   ▲                    ▲
     MemoryTaskScheduler(consolidate/forget/…  5 核心任务)
     bootstrap(冷启动/种子/批量导入/跨项目克隆)
```

## 2. 分层契约与包结构

六层架构,依赖方向严格向下(`memory/models` 最底层),禁止反向依赖;
`middlewares/` 与 `server/` 只依赖下层公开接口:

```
src/smilex/
├── middlewares/          # 门面: memory.py(宿主: 构造/生命周期/会话/归档)
│   ├── _write_path.py    #   M.3 写入 mixin(write/_write_relations/实体解析)
│   ├── _recall_path.py   #   M.4 检索 mixin(recall/归档并入/scope 折算)
│   ├── _bootstrap_facade.py  # M.2 冷启动门面 mixin(初始化/扫描导入/克隆)
│   └── dto.py            #   纯 re-export shim → memory/contracts.py
├── memory/
│   ├── contracts.py      # DTO 契约(WriteRequest/RecallRequest/...)— 2026-09
│   │                     #   自 middlewares/dto.py 下沉,断开 bootstrap 反向依赖
│   ├── embedder.py       # 基础设施客户端三件套(Protocol+默认实现+可选重后端,
│   ├── reranker.py       #   无生命周期语义,2026-09 自 lifecycle/ 上移;
│   ├── extractor.py      #   storage 层由此不再倒挂依赖 lifecycle)
│   ├── models/           # L0 数据: fuzzy.py(FuzzyMemory/TimeRange/FuzzyLocation)
│   │                     #       graph.py(Entity/Triple/CausalChain) scope.py enums.py
│   │                     #       serialization.py(msgpack)
│   ├── storage/          # L1 存储: sqlite_engine / storage_engine / vector_store
│   │   ├── queries/      #   temporal / graph / spatial / causal / fts / hybrid
│   │   └── schema/       #   001..013 迁移 SQL,SCHEMA_VERSION=13
│   ├── lifecycle/        # L2 生命周期: context_builder / promotion / l0_working_memory
│   │                     #   / l0_snapshot / token_counter(embedder/reranker/extractor
│   │                     #   旧路径留 shim)
│   ├── scheduler/        # L3 调度: scheduler / triggers / checkpoint
│   │   ├── tasks/        #   5 核心任务各一模块 + _common + __init__ 薄门面
│   │   └── bootstrap/    #   冷启动: 向导/README解析/模板/种子/批量导入/克隆
│   │       └── import_parsing.py  # 批量导入的纯解析函数(AST提取/git log解析/分块)
│   ├── concurrency/      # L4 并发: lock_manager / conflict_detection / resolution / controller
│   └── quality/          # L5 质量: contradiction / archiver / scope_promoter / quantization
├── cli/                  # CLI 包: __init__(子命令) + adapters(工具适配器) + inject(配置注入)
├── server/               # 可选: mcp_server / api / app / config / panel/
└── utils/                # ids(ULID) / timeutil(UTC ISO)
```

分层规则由 `tests/unit/test_architecture.py` 守卫: `memory/**` 禁止 import
`smilex.middlewares`(防 dto 循环复发);`memory/storage/**` 禁止 import
`memory.lifecycle`;pyproject version 与 `smilex.__version__` 必须一致。
旧公共路径(`middlewares.dto` / `memory.lifecycle.embedder` 等)全部保留为
纯 re-export shim,零 API 破坏。

## 3. 公开 API(middlewares)

`MemoryMiddleware` 是唯一门面,构造参数即全部注入点
(`middlewares/memory.py`):

```python
MemoryMiddleware(
    db_path, engine=None,            # 单文件库路径
    embedder=None,                   # Embedder Protocol(默认 HashEmbedder)
    token_counter=None,              # tiktoken + 本地 fallback
    l0=None,                         # L0 工作记忆(cachebox LRU)
    concurrency=None,                # ConcurrencyController
    promotion_threshold=800,         # L0→L1 晋升 token 阈值;0 = 全部直送 L1
    reranker=None,                   # Reranker Protocol(默认 Noop)
    fact_extractor=None,             # FactExtractor Protocol(默认 PassThrough)
    facts_bypass_l0=True,            # 抽取出的短事实跳过 L0 直送 L1
)
```

| 方法                                                                                        | 语义                                                                      |
| ----------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `write(WriteRequest, *, session_id, scope_id=None, chain_id=None, detect_conflicts=True)` | 见 §4;返回 `WriteResponse(status, memory_ids, layers_affected, conflicts)` |
| `recall(RecallRequest, *, session_id=None, include_archived=False)`                       | 见 §5;返回 `RecallResponse(context_text, sources, layers_used, truncated)` |
| `initialize_project / import_source / inject_seeds / clone_project`                       | 项目冷启动 / Git-Markdown-文本导入 / 种子注入 / 跨项目克隆                                |
| `bootstrap_project(name, *, project_path, ...)`                                            | 冷启动+扫描编排(MCP/CLI 共用):README 自动读取 + git/markdown/源码导入;同名项目 scope 复用,幂等可重跑 |
| `archive_expired(policy, scope)` / `restore_archived(...)`                                | 归档与恢复                                                                   |
| `close_session(session_id, persist=True)` / `restore_session(...)`                        | L0 快照落盘(007/009 表)/ 恢复                                                  |

关键 DTO(`memory/contracts.py`;`middlewares/dto.py` 为兼容 shim):

- `WriteRequest(content, entities, relations: list[TripleInput], importance∈[0,1],
  time_range, location, scope, emotion_weight, expires_at)` — importance
  越界在 `__post_init__` 校验拒绝
- `RecallRequest(query, scope_filter, time_range, location, entity_filter,
  top_k=10, token_budget=4000)`
- `MemoryRef(id, layer, score∈[0,1], scope, snippet)` — snippet 截断 120 字符
- `WriteStatus`: SAVED / CONFLICT / PARTIAL;`ConflictInfo` 携带检测类型与双方版本

`FuzzyMemory`(`memory/models/fuzzy.py`)全字段可选(渐进式存储):
`id`(ULID)、`content`、`content_embedding`、`time_range: TimeRange`
(exact / approx\_start+approx\_end 二选一)、`location: FuzzyLocation`
(exact\_coords / hierarchy / region / location\_id 四种表达)、
`entities/relations`、`scope`、`importance=0.5`、`emotion_weight=0.0`、
`access_count`、`layer`、`created_at/updated_at/expires_at`。

## 4. 写入路径详解

`write()` 按固定顺序执行,任何一步失败都有明确语义:

```
1. 实体锁      按 entity id 排序 acquire_many(EXCLUSIVE)   ← 排序即死锁预防
2. 冲突检测    ConflictDetector 逐三元组:
                 - expected_version 不符 → VERSION_STALE
                 - base_version 并发写   → WRITE_WRITE
                 - 默认策略 AUTO_LAST(LWW);reject/manual → WriteStatus.CONFLICT
                 - DB 层兜底: 同 (scope,subject,predicate) 活跃行检查
3. 事实抽取    facts = await extractor.extract(content)
                 PassThrough → [content](原文整块)
                 LLM         → 原子事实列表;任何异常降级回 [content]
4. 逐条入库    每条 fact 一个 FuzzyMemory(继承 scope/importance/time_range)
                 - 常规: l0.put();内容 > promotion_threshold 时自动晋升
                 - facts_bypass_l0 且非 PassThrough: 直接 promotion.put 到 L1
                 (triple_ids 只挂在第一条上,避免图重复)
5. 图谱写入    entities 名称批量解析(H1: 单次查询),triples 单事务写入
```

**晋升**(`lifecycle/promotion.py`)。晋升 = 写 `temporal_fragments`
(layer='L1')+ `VectorStore` 写向量 + 从 L0 移除,三步同一事务
(事务由调用方管理,类内不 commit)。两种触发:

- 自动:`put()` 时内容 token 数 > `threshold`(默认 800;benchmark 常传 0
  全直送)
- 批量:`check_session()` 扫描整个 session,单次 `embed_batch` 批量晋升
  (H6,线程卸载)

**时间锚定**(重要修复):`time_start = time_range.exact > approx_start >
created_at`。修复前一律用墙钟 created\_at,历史对话灌入时真实发生时间
丢失,时序检索(`query_at_time` / `query_in_range`)失真。

## 5. 检索路径详解

`ContextBuilder.build_context()`(`lifecycle/context_builder.py`)完整管线:

```
收集 _collect
  L0(session_id 给定时) l0.list() 全量 → 双轮稳定排序:
                          先 updated_at 降序,再 importance 降序(主键)
                          score = importance
  L1(query_text 给定时)  向量通道: VectorStore.knn_search(k=l1_top_k=5)
                          关键词通道: bm25_fragment_search(top_k=2×l1_top_k)
                          RRF 融合(k=60): score(d) = Σ 1/(k + rank_i(d))
                          归一化: ÷ (通道数 × 1/(k+1)) → [0,1]
  L2(entity/time/causal 给定时)
                        hybrid_memory_search(top_k=10):
                          temporal → query_in_range
                          graph    → find_n_degree_relations(递归 CTE BFS)
                          spatial  → query_in_area(R-tree bbox + Haversine)
                          causal   → trace_causal_chain
                          各策略 top_k_per_strategy=20 → RRF(k=60)
  去重: memory_id 全局唯一,优先级 L0 > L1 > L2
  排序: (层优先级, -score)

精排 _rerank_candidates(仅注入非 Noop Reranker 且有 query_text 时)
  对 L1/L2 各层: 前 max_candidates(50)条送 CrossEncoder;
  分数 min-max 归一替换;层内未精排的溢出条目 score 压到 ≤0(排到精排后);
  L0 永不精排;异常 → 保留原分数继续(降级)

装填 _greedy_fill
  budget = token_budget × 0.7(SAFETY_MARGIN)
  按序逐条: 渲染行 "[L1] content" 计 token(H2: 只在此计一次),
  装得下→入选;装不下→truncated=True 并继续尝试更小的后续条目
```

**FTS5 BM25 通道细节**(`storage/queries/fts.py`):trigram tokenizer
(CJK 子串 + ASCII 大小写不敏感);问句 `\w+` 分词,丢弃 <3 字符词
(trigram 最小长度),去重后取前 16 词,引号转义后 OR 连接
(AND 语义对长问句过严);`\w+` 分词天然剥离引号 → FTS 语法注入不可能。
schema < 13 的老库 `fts_fragments` 不存在时 `OperationalError` 捕获 →
降级纯向量。

**为什么双通道**:向量近似检索对专有名词、日期、精确词命中率差
(LoCoMo 实测裸 BGE-M3 R\@10 86.7%);BM25 补关键词面,RRF 融合后
96.7%(conv-26 采样),全量 95.3%。两路互补,单路都到不了。

**服务化出口(2026-09 接线)**:L2 策略触发参数(entity/time)此前
只在库内 API 可达;现 MCP `memory_recall` 的 `entity`/`time_start`/
`time_end` 与 `POST /api/recall-test` 同名字段经实体名解析
(`mcp_server.resolve_entity_ref`,只读不建实体)透传到
`RecallRequest.entity_filter/time_range`;图遍历另有独立工具
`memory_graph_query`(path/neighbors/causal,见 §11),把
`find_path`/`find_n_degree_relations`/`trace_causal_chain` 接出
服务层,neighbors/path 支持按 relation_types 过滤。

## 6. 可插拔组件:六个 Protocol

同构设计——默认零依赖实现 + 可选强实现,core 永不强制引入
LLM 或重模型;均有 `Config` dataclass + `get_xxx()` 工厂
(`EmbedderConfig`/`RerankerConfig`/`ExtractorConfig`)。前三个位于
`memory/{embedder,reranker,extractor}.py`(`memory/lifecycle/` 下旧路径
为 shim):

| Protocol                                           | 默认(零依赖)                                  | 可选增强                                                                                                               | extra         |
| -------------------------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------ | ------------- |
| `Embedder.dimension / embed / embed_batch`         | `HashEmbedder`(SHA-256 确定性哈希,1024 维,无语义) | `SentenceTransformerEmbedder`(BGE-M3,懒加载,`_sanitize` 剥 `<\|...\|>` 特殊 token 字面量——ST v6 丢弃 tokenizer\_kwargs 后的必须品) | `[embedding]` |
| `Reranker.rerank(query, documents) -> list[float]` | `NoopReranker`(返回 \[])                   | `CrossEncoderReranker`(bge-reranker-v2-m3,懒加载,batch 16 防 MPS 卡死,`asyncio.to_thread` 包裹 predict)                    | `[rerank]`    |
| `FactExtractor.extract(content) -> list[str]`      | `PassThroughExtractor`(\[content])       | `LLMFactExtractor`(OpenAI 兼容,env `SMILEX_EXTRACT_API_KEY/BASE_URL/MODEL`,temperature 0,失败/无 key → \[content])      | `[llm]`       |

同构家族还有三个(§15.3/§15.4/P3 演进,2026-09 新增):

| Protocol                     | 默认(零依赖)     | 可选增强                                                       | extra       |
| ---------------------------- | ---------------- | -------------------------------------------------------------- | ----------- |
| `PIIMasker.mask(text) -> str` | `NoopPIIMasker`(直通) | `RegexPIIMasker`(email/手机/身份证校验位/银行卡 Luhn/IPv4/API key;redact/hash/mask 三策略,幂等) | 无(内置) |
| `Tracer.span(name, **attrs)` | `NoopTracer`(空 CM) | `OTelTracer`(opentelemetry,宿主自配 provider 优先)            | `[tracing]` |
| `Summarizer.summarize(text) -> str` | `RuleSummarizer`(截取+句读回退) | `LLMSummarizer`(OpenAI 兼容,env `SMILEX_SUMMARIZE_*`,GLM 附 thinking disabled,失败回退规则版;插入点 = summarize 调度任务) | `[llm]` |

server 层 config.toml 对应开关:`embedder` / `reranker` / `fact_extractor` /
`pii_masker` / `tracing` / `summarizer` / `access_tracking` /
`max_records_per_scope` / `protect_importance`。

## 6a. 可观测性子系统(`memory/observability/`)

- **metrics**: 零依赖 Prometheus 文本注册表(Counter/Gauge/Histogram;
  Gauge 支持 callback 惰性求值——队列深度/库体积 scrape 时才读,写路径零
  开销);`GET /metrics` 暴露(注册在 MCP 兜底 mount 之前)。
- **audit**: 变更事件 JSONL(默认库文件旁 `audit-<db>.jsonl`),只记
  session/scope/memory_ids/status/耗时/`content_sha256`(脱敏后指纹),
  **不落原文** → 审计自身不成 PII 泄漏面;失败降级不抛。
- **telemetry**: `Telemetry` 三件套容器 + `StandardMetrics`(write/recall
  计数与延迟)+ `make_scheduler_observer`(任务终态计数 + 失败告警)+
  进程级 `IntegrityReport` 持有者(巡检任务写,health/gauge 读)。
- **logging**: structlog 门面(JSON→stderr),import 即安全默认(WARNING
  级安静),入口 `configure_logging(level)` 提级。
- 消费端 `TelemetryMemoryMiddleware`(组合包装,覆写 write/recall/
  bootstrap 入口,核心逻辑零改动);`db_integrity` 为第 6 个核心调度任务
  (LOW/每日 quick_check)。`/api/health` 增 db/scheduler/alerts 三段,
  `status`/`started_at`/`uptime_s` 契约字段不变(daemon 依赖)。

## 7. 存储层

单文件 SQLite,`SCHEMA_VERSION = 15`。引擎
(`storage/sqlite_engine.py`):aiosqlite + `DEFAULT_PRAGMAS`
(WAL / synchronous=NORMAL / temp\_store=MEMORY / mmap 256MB /
cache 64MB / foreign\_keys / busy\_timeout 5000ms);迁移按
`PRAGMA user_version` 顺序执行,010 需 sqlite-vec 扩展
(加载失败有跳过/自愈逻辑)。

| schema  | 表/结构                       | 要点                                                                         |
| ------- | -------------------------- | -------------------------------------------------------------------------- |
| 001/002 | entities / triples         | triples 带 valid\_from/valid\_to 双时态、predecessor\_id 因果链指针                  |
| 003     | locations + R-tree 虚拟表     | bbox 预过滤 + Haversine 精算;层级路径 LIKE 查询                                       |
| 004     | temporal\_fragments        | L1 主体: content/layer/time\_start/time\_end/scope/importance                |
| 005     | causal\_chains             | 调度器 causal 任务维护                                                            |
| 006     | vector\_links              | 向量 ↔ 三种业务行(fragment/entity/triple)关联                                       |
| 007/009 | checkpoints / l0\_snapshot | 任务断点(msgpack BLOB cursor)/ L0 快照(带过期清理)                                    |
| 008     | project\_current\_state    | 项目态                                                                        |
| 010     | vec 虚拟表                    | sqlite-vec FLOAT\[1024]\(EMBEDDING\_DIM=1024,设计的 1536 已废弃)                 |
| 011     | 归档 + 谓词字典                  | Archiver 冷热分层                                                              |
| 012     | 性能索引                       | <br />                                                                     |
| 013     | fts\_fragments             | FTS5 external-content,trigram;INSERT/UPDATE/DELETE 触发器保持同步;`rebuild` 回填老数据 |
| 014     | access\_stats               | temporal_fragments 增 access_count/last_accessed_at(检索反馈闭环;FTS 触发器只在 UPDATE OF content 联动,计数不扰索引) |
| 015     | error\_fingerprints          | 错误指纹注册表(sha256(scope\|code\|归一msg)[:16] 主键,count/lesson\_id;教训闭环) |

查询模块(`queries/`,全部接收 `aiosqlite.Connection`,事务由调用方管理):

- **temporal**: `query_at_time` / `query_in_range` / 实体时间线
- **graph**: `find_path` / `find_n_degree_relations` — 纯 SQL
  `WITH RECURSIVE` CTE BFS(`ORDER BY depth`,comma-path 环检测,M1
  优先队列)。**NetworkX 不承担存储**,只在调度器 semantic 任务里算
  连通分量
- **spatial**: R-tree bbox → Haversine 半径过滤;`query_in_location`
  层级路径
- **causal**: `trace_causal_chain` 反向单 CTE(H4)/ 正向分批 `IN` 逐层
- **fts**: `bm25_fragment_search`(见 §5)
- **hybrid**: 多策略编排 + `rrf_fusion`(rankings, k=60)

## 8. 调度与生命周期任务

`MemoryTaskScheduler`(`scheduler/scheduler.py`)——**拉取式**设计:
触发器不推队列,`drain()` 到期才出队任务。

| 触发器                                                    | 语义                  |
| ------------------------------------------------------ | ------------------- |
| `TimeTrigger(interval_seconds, fire_immediately)`      | 固定周期                |
| `EventTrigger(mapping: event_type → (task, priority))` | 事件映射                |
| `AdaptiveTrigger(rules, cooldown_seconds)`             | 规则条件触发,冷却期内不再评估(防抖) |

抢占(`preempt(task_id, policy)`):NONE / IMMEDIATE / GRACEFUL /
**COOPERATIVE**(任务在检查点调 `InterruptContext.checkpoint()`
主动让出,进度+步号+游标存 checkpoints 表,msgpack 编码)/
PRIORITY\_INHERITANCE;`resume` 从断点续跑。

核心任务(`scheduler/tasks/` 包: consolidate/forget/summarize/causal/
semantic 各一模块 + `_common.py` 共享子句,`__init__.py` 的 `CoreTaskRunner`
为薄门面;`register_core_tasks` 配默认触发器):

| 任务          | 行为                                                                                                    | 关键参数                              |
| ----------- | ----------------------------------------------------------------------------------------------------- | --------------------------------- |
| consolidate | L1 fragments 按 entity/scope 分组聚合,固化写入 L2(triples)                                                     | 分组 flush(H1)                      |
| forget      | 留存分 `score = importance × 0.5^(age/half_life) × min(3, 1+log10(1+access_count))`,age 锚点取 max(updated\_at, last\_accessed\_at)(使用即续命+常用即升值);低于 threshold → 删除(默认)或降权;删除守卫 importance ≥ protect\_importance(默认 0.9,confirm\_protected 可越过);尾段容量 pass: max\_per\_scope 超额修剪 | half\_life\_days=30,threshold=0.1,protect=0.9,max\_per\_scope=0(关) |
| dedup       | 近重复合并(§ 主动优化): FTS trigram 短语找同 scope/layer 候选 + 字符 bigram Jaccard ≥ 0.7 确认;幸存者=较早 created\_at,吸收访问计数/时间区间/实体;重复行连同向量删除 | 每日 LOW,threshold=0.7,min\_length=12 |
| summarize   | 规则式摘要(非 LLM),summary\_length 可调                                                                       | <br />                            |
| causal      | 遍历 predecessor\_id 链维护 causal\_chains 表                                                               | <br />                            |
| semantic    | NetworkX 构实体图,预计算连通分量("语义社区")缓存                                                                       | <br />                            |

`bootstrap/` 子包(冷启动):向导问答(`project_bootstrap` +
`onboarding`,同名项目按 entity_id 复用既有 scope)、README 解析
(`readme_parser`)、项目模板(`templates/{agent,cli,data,web}.yaml`)、
种子注入(`seeds/seed_injector`)、批量导入(`bulk_importer` 编排 +
`import_parsing` 纯解析函数,Git/Markdown/
源码/文本;markdown 与源码目录扫描跳过 node_modules/.venv 等依赖与构建
目录,`max_files` 截断;源码通道 `.py` 走 AST——模块 docstring/顶层
类与函数/内外部依赖(标准库过滤,绝对导入按本目录/src 布局/仓库根
顺序解析),其余扩展名读文件头注释兜底,
文件/类/外部库分别落 `file:`/`class:`/`tech:` 实体)、跨项目克隆
(`cross_project_cloner`;2026-09 强化: ①**向量重建** — middleware 注入
vector_store,克隆/导入的新实体与新片段即时重嵌(名字/内容),
`CloneResult.vectors_rebuilt/vectors_missing` 计数,消除了"快照不含
向量 → 导入后 KNN 通道空转"的静默降级;②**merge 归并** — 目标已有
同名实体(entity_id)归并进 ID 映射表,端点三元组改写指向既有实体而非
静默丢弃;③**locations 对称化** — 随包导出/导入(新 ULID + parent
层级 + 片段 location_id 改写;location_id 全表 UNIQUE,同 id 即同地点
按全表归并);④包格式 1.1 + 版本兼容改"主版本相等且次版本 ≤ 当前",
1.0 旧包可导入;⑤L3 语义社区缓存(fragment_id 前缀 `semantic:community:`)
恒不复制 — 派生缓存 key 由源库实体 id 决定,克隆后必然失配,由
semantic 任务重建;错误指纹表为设备本地数据不随包导出)、主动学习
(`active_learner`)。MCP `memory_init_project(project_path=...)`
与 CLI `smilex-memory init --scan` 经 `MemoryMiddleware.bootstrap_project`
复用同一编排。

## 9. 并发控制

三道防线(`concurrency/`):

1. **应用层锁** `LockManager`:SHARED / EXCLUSIVE / UPDATE 兼容矩阵
   (读共享、写排他、UPDATE 锁中间态),FIFO 等待队列,
   `default_timeout` 超时兜底(死锁预防第二重保障);
   `acquire_many` **按资源名排序获取**(主要死锁预防);
   `upgrade` 支持锁升级(带死锁检测);`release_all(holder)` 崩溃清理
2. **乐观版本** `ConflictDetector`:内存版本注册表;
   `expected_version` 不符 → VERSION\_STALE,`base_version` 并发写 →
   WRITE\_WRITE(CAS 风格);`ConflictResolver` 策略含 AUTO\_LAST(LWW)
3. **DB 兜底** `ConcurrencyController.guard_triple_write`:同
   (scope, subject, predicate) 活跃行唯一性检查(跨进程也成立)

## 10. 知识质量

`quality/`:

- **ContradictionDetector**:四类矛盾——VALUE(逆谓词对,如
  喜欢/不喜欢)、NUMERIC(数值区间重叠)、TEMPORAL(时间区间重叠)、
  CAUSAL(因果环);`check_new`(写入时)+ `scan`(全量巡检)
- **Archiver**:冷热分层;默认保留 triples 365 天 / fragments 180 天;
  归档去向量(省空间)、`restore_archived` 可恢复;recall 传
  `include_archived=True` 时经非向量通道追加,内容前缀 `[归档]`;
  已注册为可调度任务
- **ScopePromoter**:检测"多项目重复出现"的模式 → 提升到 global scope
  (带最小项目数阈值,支持手动确认)
- **quantization**:int8 标量量化工具(未接进 VectorStore,预留)

## 11. 服务化层

`server/`(可选 `[server]` extra),配置 `~/.smilex/config.toml`
(`db_path / host / port=8765 / embedder / reranker / fact_extractor /
token_budget=4000 / enable_scheduler=true`,CLI 可覆盖 db/host/port):

- **MCP**(stdio + streamable-http `/mcp`):`memory_recall(query,
  session_id, project, top_k, token_budget, entity, time_start,
  time_end)`(entity=实体名/归一化 ID/ULID,解析后触发 L2 图谱策略;
  ISO 时间对触发时序策略)/ `memory_write(content,
  session_id, scope, scope_id, entities, relations, importance)` /
  `memory_init_project(name, project_path, scan_git, scan_markdown,
  scan_code, max_commits, ...)`(冷启动+扫描生成初始记忆;stdio 模式
  project_path 缺省时按 db 路径 `<项目根>/.smilex/memory.db` 推断,
  HTTP 模式需显式传参)/ `memory_graph_query(mode, ...)`
  (图遍历出口:path 两实体最短路径 / neighbors N 度关系 / causal 沿
  predecessor 链追溯;path/neighbors 支持 relation_types 逗号分隔
  过滤,查询层 `find_path`/`find_n_degree_relations` 新增同名参数透传;
  实体参数接受名称,节点/边输出带实体名与谓词摘要)/ `memory_stats`/
  `memory_report_error`(错误指纹登记与教训闭环,见 server/lessons.py);
  Reranker/Extractor 按 config 工厂构造注入
- **REST**(`api.py`):`GET /stats` `/memories` `/memory/{id}`
  `/tasks` + `POST /recall-test`(body 增加可选 entity/time_start/
  time_end 聚焦字段,与 memory_recall 同语义;实体名解析失败安全降级
  为无聚焦检索)
- **Web 面板**:只读静态页(概览统计/记忆浏览/召回测试),写入统一走 MCP
- **CLI**(`cli/` 包 → `smilex-memory`,入口点 `smilex.cli:main`):
  `serve` / `mcp` / `init`
  (向 Kimi Code / Claude Code 注入工具配置 + 记忆使用约定;`--scan`
  时经 `bootstrap_project` 冷启动并扫描 README/git/markdown/源码生成
  初始记忆,stdio 模式写项目内库、HTTP 模式直写全局库) / `doctor`;
  三平台自启动注册脚本在 `scripts/register-service-*`

## 11a. 安全注记(§15.4 落地状态)

- **PII 脱敏**: `memory/pii.py`(默认 noop;见 §6 表格)— write() 入口对
  content/实体名/三元组自由文本统一脱敏,幂等
- **API Key 鉴权**: `server/auth.py` 中间件挂主 app 最外层(覆盖 MCP mount);
  Bearer/X-API-Key 双方案,恒时比较;豁免静态资源与 /api/health(无 key
  时 health 裁剪为契约字段);`SMILEX_API_KEY` 环境变量优先于配置文件
- **文件加密(SQLCipher,实验性)**: `SQLiteEngine(encryption_key=)` 连接
  首语句 `PRAGMA key` + `cipher_version` 校验(非加密构建即中止);
  `storage/sqlcipher.py` pysqlcipher3 shim;`[encryption]` extra
- **SQL 注入防护(2026-09 全量审计)**: 无用户可控字符串进入 SQL 文本 —
  FTS MATCH 短语经 `_fts_phrase` 双引号转义后参数绑定;scope 值全程 `?`
  绑定(`queries/_scope.build_scope_clause`);f-string SQL 的插值片段全部
  是白名单表名(`temporal.py` 显式校验)/模块常量/`?` 占位列表;面板
  `kind`/`limit` 等经 FastAPI 校验器约束。pragma 值已改单引号包裹转义
- **作用域隔离**: 所有查询经 `build_scope_clause`/`_scope_clause` 强制带
  scope 过滤,参数化;L0 会话按 session_id 隔离
- **向量隔离**: 原设计"ChromaDB collection 按 scope 隔离"随 ChromaDB
  废弃(决策 D1)不适用 — sqlite-vec 单库方案下向量行经 vector_links
  关联业务行,检索路径全部带 scope 过滤,天然隔离
- **JWT/OAuth**: 未实现(本地单用户场景 API Key 已覆盖;mcp SDK 的
  TokenVerifier/AuthSettings 通道已预留,多租户需求出现时接入)

## 12. 降级矩阵

系统各环节的失败语义(全部"降级不崩"):

| 环节            | 失败条件                                  | 行为                           |
| ------------- | ------------------------------------- | ---------------------------- |
| 事实抽取          | 无 key / API 异常 / 空结果                  | 返回 \[content] 原文整块           |
| 精排            | 无 query / Noop / 模型异常 / 分数数不齐         | 保留原 RRF 分数                   |
| FTS 通道        | schema < 13 无 fts 表(OperationalError) | 纯向量通道                        |
| 逐层检索          | L0/L1/L2 各自参数为空                       | 跳过该层(纯 L0 / 纯 L1 / 纯 L2 均合法) |
| 冲突            | 版本冲突 + reject/manual 策略               | WriteStatus.CONFLICT,不写      |
| sqlite-vec 缺失 | 010 迁移加载扩展失败                          | 跳过 + 自愈重试                    |
| 装填            | 单条超预算                                 | truncated=True,继续尝试更小条目      |

## 13. 性能注记

代码内标注的优化点(编号沿用注释):

- H1 批量实体名解析 + 三元组单事务写入;H3 embed 线程卸载;
  H4 因果链反向单 CTE;H5 断点负载瘦身;H6 check\_session 批量晋升;
  M1 图 BFS CTE 优先队列
- H2:token 计数只在 `_greedy_fill` 对渲染行计一次(收集阶段不预计算,
  避免双倍编码)
- 精排代价:本机 MPS \~4.4s/查询(50 候选)——超出 recall P99 预算,
  生产低延迟场景减小 `max_candidates` 或换 reranker-base
- 微基准:`tests/benchmarks/`(recall/write 延迟、库体积);
  评测:`benchmarks/`(四套,成绩见 README)

## 14. 扩展指南

- **换嵌入模型**:实现 `Embedder` Protocol + 在 `get_embedder()` 工厂
  加分支(config `embedder` 指到新 key);维度变了需重建 vec 表
  (schema 010 固定 FLOAT\[1024],注意迁移)
- **换精排模型**:`Reranker` Protocol + `RERANKER_BACKENDS`;
  记得暴露 `max_candidates` 属性(ContextBuilder 按它截断)
- **换抽取实现**(如本地小模型):`FactExtractor` Protocol +
  `EXTRACTOR_BACKENDS`;返回 `list[str]`,空列表会降级回原文
- **新检索策略**:加进 `queries/hybrid.py` 的策略列表
  (`HybridQuery` 加字段 + `_run_xxx_strategy`),RRF 自动融合
- **新调度任务**:`CoreTaskRunner` 加方法 + `register_core_tasks`
  配触发器;长任务记得走 `ctx.checkpoint()` 协作协议
- **schema 变更**:`schema/` 加 `0XX_*.sql`,`SCHEMA_VERSION` + 1,
  老库按 user\_version 顺序补跑;FTS 类还需同步触发器与 `rebuild`

## 15. 关键设计决策速查

| 决策     | 选择                                      | 理由                                   |
| ------ | --------------------------------------- | ------------------------------------ |
| 存储     | 单文件 SQLite(sqlite-vec + R-tree + FTS5)  | 嵌入式零部署,五维(时序/空间/图谱/因果/语义)一个文件        |
| 检索     | 向量 + BM25 双通道 RRF,精排可选                  | 双通道互补(语义 + 精确词),实测比裸向量 +10pp R\@10   |
| LLM 依赖 | core 零 LLM,三 Protocol 注入点               | 库可纯本地跑;mem0 式事实抽取按需开                 |
| 记忆形成   | 原文分层(默认)vs 原子事实卡(--fact-extraction)     | benchmark 证明后者 +3.4pp 且小上下文即够用       |
| 时间语义   | WriteRequest.time\_range 锚定 time\_start | 历史对话保留真实时间,时序检索可信                    |
| 并发     | 应用层锁 + 乐观版本 + DB 兜底                     | 三道防线,aio 单线程下仍防跨进程竞争                 |
| 调度     | 拉取式触发 + 协作式抢占断点                         | 无后台线程,断点可续跑,冷启动成本可控                  |
| 图计算    | SQL 递归 CTE 而非 NetworkX 存储               | 数据不双写,BFS 下推到 SQLite;NetworkX 只做离线分析 |
| ID     | ULID                                    | 时间有序,字符串友好                           |

## 16. 名词解释表

按主题分组;首次出现的术语在正文各节有更详细的展开。

### 记忆模型与分层

| 名词 | 解释 |
|---|---|
| L0(工作记忆) | 进程内 cachebox LRU 缓存,按 session 隔离,驻留期不嵌入向量;类比"正在想的事" |
| L1(短时记忆) | `temporal_fragments` 表 + 向量索引,可全文/语义检索;类比"最近几天的事" |
| L2(长时记忆) | 知识图谱(entities/triples)+ 因果链,由 consolidate 任务从 L1 固化;类比"长期知识" |
| L3(语义层) | 设计中的语义社区层;现状仅 semantic 任务用 NetworkX 预计算连通分量缓存,不在检索主路径 |
| FuzzyMemory | 核心记忆对象,全字段可选的"渐进式存储":时间可模糊、位置可只给层级,不要求一次填全 |
| TimeRange | 模糊时间表达:exact(精确时刻)或 approx_start+approx_end(约略区间),二选一 |
| FuzzyLocation | 模糊位置表达:精确坐标 / 层级路径 / 区域名 / location_id 四种形态任选 |
| importance | 写入方给的 [0,1] 重要度;参与 L0 排序、forget 留存分、晋升决策 |
| scope / scope_id | 记忆的作用域(项目隔离单元);ScopeFilter 控制检索时包含哪些 scope 及是否含 global |
| global scope | 跨项目共享区;ScopePromoter 把多项目重复模式提升到这里 |
| session | L0 工作记忆的隔离键,一个对话会话一个;close_session 落快照可恢复 |
| 晋升(promotion) | L0→L1 流转:写 temporal_fragments + 向量,再从 L0 移除;阈值默认 800 token |
| promotion_threshold | 晋升 token 阈值;传 0 = 全部直送 L1(benchmark 常用) |
| facts_bypass_l0 | 抽取模式下短事实跳过 L0 直送 L1,保证原子事实立即可检索 |

### 检索

| 名词 | 解释 |
|---|---|
| 双通道 | L1 同时走向量 KNN(语义近似)+ FTS5 BM25(精确关键词)两路 |
| RRF | Reciprocal Rank Fusion,多路排名融合:score(d)=Σ 1/(k+rank_i(d)),k=60;只看名次不看分值,天然可比 |
| KNN | K 近邻向量检索(sqlite-vec),把 query 嵌入后找余弦最近邻 |
| BM25 | 经典词频相关度打分函数,FTS5 内置;补向量检索对专有名词/日期的漏检 |
| FTS5 / trigram | SQLite 全文检索虚拟表;trigram 分词器按 3 字符滑窗,中英文(含 CJK 子串)通吃 |
| CrossEncoder 精排 | 把 (query, doc) 成对送 reranker 模型(bge-reranker-v2-m3)重打分;比双塔准但慢,故只精排前 50 条 |
| max_candidates | 精排每层送评的候选上限(默认 50),保护 recall 延迟预算 |
| hybrid_memory_search | L2 多策略检索:时序/图谱 n 度/空间/因果各出 top-20 再 RRF |
| token budget / 安全边际 | recall 注入上下文的 token 上限(默认 4000),实际装填按 ×0.7(2800)留余量 |
| 贪心装填 | 按优先级逐条尝试:装得下就进,装不下跳过但继续试更小的后续条目 |
| ContextSource / BuiltContext | 检索管线的中间产物(带层标记的候选)与最终产物(text + sources + truncated) |
| MemoryRef | recall 返回的单条引用(id/layer/score/snippet),snippet 截断 120 字符 |

### 写入与抽取

| 名词 | 解释 |
|---|---|
| FactExtractor | 写入时事实抽取 Protocol;PassThrough(默认,原文整块)/ LLM(拆原子事实) |
| 原子事实 / 事实卡 | 一句一事的短事实(保留日期/实体/待办),对齐 mem0 的记忆形成方式;计数/聚合题的关键补强 |
| 降级 | 组件失败时回退到保底行为而非报错(抽取失败→原文,FTS 缺表→纯向量,精排异常→原分数) |
| time_start 锚定 | 晋升时 time_start 取 time_range.exact > approx_start > created_at,历史对话保留真实发生时间 |
| PassThrough / Noop / Hash | 三个 Protocol 的零依赖默认实现的统称——不引入外部模型时的保底行为 |

### 存储

| 名词 | 解释 |
|---|---|
| schema 版本(user_version) | `PRAGMA user_version` 记录库结构版本(现 13),迁移 SQL 按序补跑 |
| temporal_fragments | L1 主体表:content/layer/time_start/scope/importance |
| entities / triples | 图谱节点与三元组;triples 带 valid_from/to(双时态)与 predecessor_id |
| 双时态(bitemporal) | 三元组同时记录"业务有效时间"(valid_from/to)与写入时间,支持时间点回溯 |
| predecessor_id | triples 上的因果链指针:这条事实由哪条先前事实演变而来 |
| causal_chains | 调度器 causal 任务维护的因果链物化表 |
| vector_links | 向量行 ↔ 业务行(fragment/entity/triple)的关联表 |
| fts_fragments | FTS5 external-content 虚拟表(不复制内容,靠 rowid 映射回 temporal_fragments,触发器保同步) |
| sqlite-vec | SQLite 向量扩展,提供 FLOAT[1024] 虚拟表与 KNN |
| R-tree / Haversine | SQLite 内置空间索引(bbox 预过滤)/ 球面距离公式(精算半径内) |
| WAL | SQLite Write-Ahead Logging,读写不互斥,并发读取的基础 |
| ULID | 时间有序的唯一 ID(可排序、字符串友好),替代自增主键 |
| msgpack | 二进制序列化(热路径/断点 cursor 用),比 JSON 紧凑 |
| 归档 / 冷热分层 | 过期数据(triples 365 天 / fragments 180 天)移入归档区、去掉向量;可恢复、可召回(带 [归档] 前缀) |

### 调度与生命周期

| 名词 | 解释 |
|---|---|
| 拉取式触发 | 触发器不推队列,drain() 被调用时才结算到期任务;无后台线程 |
| TimeTrigger / EventTrigger / AdaptiveTrigger | 周期触发 / 事件映射触发 / 规则条件触发(带冷却防抖) |
| 抢占(preempt) | 中断运行中任务的策略族:NONE/IMMEDIATE/GRACEFUL/COOPERATIVE/PRIORITY_INHERITANCE |
| COOPERATIVE / checkpoint() | 协作式让出:任务在安全点主动存断点(进度+步号+msgpack cursor)后停下,可 resume 续跑 |
| consolidate | 核心任务:L1 碎片按 entity/scope 聚合固化成 L2 三元组 |
| forget / 半衰期 | 核心任务:留存分 = importance × 0.5^(age/30 天) × 访问加成(≤3x),age 锚点含最近访问;低于 0.1 删除或降权 |
| summarize | 核心任务:摘要压缩(默认规则式;summarizer=llm 注入 LLM 后端,见 §6) |
| semantic | 核心任务:NetworkX 构实体图、预计算连通分量("语义社区")缓存 |
| bootstrap / 冷启动 | 新项目初始化包:向导问答 + README 解析 + 模板 + 种子注入 + 批量导入;MCP `memory_init_project(project_path=...)` 与 CLI `init --scan` 一键完成"扫描并生成初始记忆"(README/git/markdown/源码),同名项目复用 scope 幂等可重跑 |
| 扫描忽略规则 | markdown 与源码目录扫描跳过 node_modules/.venv/dist 等依赖与构建目录及隐藏目录,`max_files`(默认 500)截断防超大仓库 |
| 源码导入 | 扫描通道之一:`.py` 用 AST 提取模块 docstring/顶层类与函数/内外部依赖(标准库过滤),其余代码扩展名读文件头注释兜底;文件/类/外部库分别落 `file:`/`class:`/`tech:` 实体,内部依赖解析为仓库相对路径 |
| 种子注入(seed) | 预置的领域知识实体/三元组,新项目开箱即有基础记忆 |
| 跨项目克隆(clone_project) | 把既有项目记忆复制为另一 scope(可带过滤器) |

### 并发与质量

| 名词 | 解释 |
|---|---|
| SHARED / EXCLUSIVE / UPDATE 锁 | 读共享 / 写排他 / 中间态(先读后升级写)三档锁 |
| 排序获取(acquire_many) | 多锁按资源名排序获取,从根本上消除循环等待死锁 |
| 乐观版本检测 | 写时携带 expected/base version,CAS 风格比对;不符报 VERSION_STALE / WRITE_WRITE |
| LWW / AUTO_LAST | Last-Write-Wins,冲突默认解法:后写覆盖;主动优化二期起覆盖显式化 — 旧行闭合 valid_to、新行记 predecessor_id(见 §6a 前的写入路径描述) |
| WriteStatus.CONFLICT | 写入被拒(策略为 reject/manual 且检出冲突),不落库 |
| 矛盾检测 | VALUE(逆谓词)/NUMERIC(数值重叠)/TEMPORAL(时间重叠)/CAUSAL(因果环)四类 |
| ScopePromoter | 检测"多项目重复模式"并提升到 global(带最小项目数阈值) |

### 服务与集成

| 名词 | 解释 |
|---|---|
| MCP | Model Context Protocol,Agent 工具接入协议;本项目暴露 memory_recall/write/init_project/graph_query/report_error/stats 六工具 |
| stdio / streamable-http | MCP 两种传输:子进程标准输入输出 / HTTP 常驻端点(/mcp) |
| Web 面板 | 只读管理页(概览/浏览/召回测试);写入统一走 MCP 工具 |
| config.toml | `~/.smilex/config.toml`,服务化配置(db/embedder/reranker/fact_extractor 等) |
| doctor | CLI 环境自检命令 |

### 代码结构与兼容

| 名词 | 解释 |
|---|---|
| contracts | `memory/contracts.py` 的 DTO 契约层(WriteRequest/RecallRequest 等 13 符号);2026-09 自 middlewares/dto.py 下沉,使 bootstrap 不再反向依赖 middlewares |
| shim(纯 re-export 垫片) | 保留旧公共导入路径的兼容模块,内容只有 `from 新位置 import *` + `__all__`;调用方零改动,未来大版本可移除 |
| mixin 拆分 | 把大类按职责拆成多个 mixin 组合回原类(`MemoryMiddleware` = 宿主 + 写入/检索/冷启动三 mixin);方法体不变,模块路径与类名不变 |
| 守卫测试 | `tests/unit/test_architecture.py`:AST 扫描断言依赖方向(memory 不进 middlewares、storage 不进 lifecycle)+ 版本一致性,防架构腐化回归 |
| benchmarks _shared | locomo/longmemeval/mem0_compat 共用的 LLM 接入件(llm_client/judge/answer);各脚本头部一行 sys.path 引导后 `from _shared.xxx import` |

### Benchmark 术语

| 名词 | 解释 |
|---|---|
| LongMemEval / LoCoMo | 两个公开长期记忆评测集(500 题 / 10 长对话 1986 题) |
| 自家口径 vs mem0 口径 | 前者严判分、奖励正确拒答;后者对齐 mem0 官方协议(禁止拒答、宽松判卷),分数可与 mem0 公布值直接比 |
| cutoff | mem0 协议中"取检索结果前 N 条作答"的截断点(10/20/50/200),各自独立作答+判卷 |
| headline | 主指标 = 最大 cutoff(200)的得分 |
| R@10 | 检索命中率:gold 证据是否出现在 top-10 检索结果里(retrieval_baseline,无 LLM) |
| answerer / judge | 作答模型 / 判卷模型(环境变量独立配置,可混搭) |
| LLM-as-judge / rejudge | 用 LLM 对比作答与 gold 判对错;rejudge.py 为离线重判工具(修 judge 空判卷,免重新灌入) |
| BGE-M3 | 多语言嵌入模型(1024 维),本项目默认语义检索底座 |
| GLM / glm-5.3-flash | 智谱大模型;benchmark 默认作答+判卷模型(对应 mem0 的 gpt-5 位置) |

