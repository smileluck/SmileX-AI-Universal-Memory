# SmileX Agent Memory — 现状架构总览(as-built)

> **版本**: v2.1 · 2026-09-04
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
6. [可插拔组件:三个 Protocol](#6-可插拔组件三个-protocol)
7. [存储层](#7-存储层)
8. [调度与生命周期任务](#8-调度与生命周期任务)
9. [并发控制](#9-并发控制)
10. [知识质量](#10-知识质量)
11. [服务化层](#11-服务化层)
12. [降级矩阵](#12-降级矩阵)
13. [性能注记](#13-性能注记)
14. [扩展指南](#14-扩展指南)
15. [关键设计决策速查](#15-关键设计决策速查)

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
├── middlewares/          # 门面: MemoryMiddleware + DTO
├── memory/
│   ├── models/           # L0 数据: fuzzy.py(FuzzyMemory/TimeRange/FuzzyLocation)
│   │                     #       graph.py(Entity/Triple/CausalChain) scope.py enums.py
│   │                     #       serialization.py(msgpack)
│   ├── storage/          # L1 存储: sqlite_engine / storage_engine / vector_store
│   │   ├── queries/      #   temporal / graph / spatial / causal / fts / hybrid
│   │   └── schema/       #   001..013 迁移 SQL,SCHEMA_VERSION=13
│   ├── lifecycle/        # L2 生命周期: context_builder / promotion / l0_working_memory
│   │                     #   / l0_snapshot / embedder / reranker / extractor / token_counter
│   ├── scheduler/        # L3 调度: scheduler / triggers / tasks / checkpoint
│   │   └── bootstrap/    #   冷启动: 向导/README解析/模板/种子/批量导入/克隆
│   ├── concurrency/      # L4 并发: lock_manager / conflict_detection / resolution / controller
│   └── quality/          # L5 质量: contradiction / archiver / scope_promoter / quantization
├── server/               # 可选: mcp_server / api / app / config / panel/
└── utils/                # ids(ULID) / timeutil(UTC ISO)
```

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
| `archive_expired(policy, scope)` / `restore_archived(...)`                                | 归档与恢复                                                                   |
| `close_session(session_id, persist=True)` / `restore_session(...)`                        | L0 快照落盘(007/009 表)/ 恢复                                                  |

关键 DTO(`middlewares/dto.py`):

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

## 6. 可插拔组件:三个 Protocol

三者同构设计——默认零依赖实现 + 可选强实现,core 永不强制引入
LLM 或重模型;均有 `Config` dataclass + `get_xxx()` 工厂
(`EmbedderConfig`/`RerankerConfig`/`ExtractorConfig`):

| Protocol                                           | 默认(零依赖)                                  | 可选增强                                                                                                               | extra         |
| -------------------------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------ | ------------- |
| `Embedder.dimension / embed / embed_batch`         | `HashEmbedder`(SHA-256 确定性哈希,1024 维,无语义) | `SentenceTransformerEmbedder`(BGE-M3,懒加载,`_sanitize` 剥 `<\|...\|>` 特殊 token 字面量——ST v6 丢弃 tokenizer\_kwargs 后的必须品) | `[embedding]` |
| `Reranker.rerank(query, documents) -> list[float]` | `NoopReranker`(返回 \[])                   | `CrossEncoderReranker`(bge-reranker-v2-m3,懒加载,batch 16 防 MPS 卡死,`asyncio.to_thread` 包裹 predict)                    | `[rerank]`    |
| `FactExtractor.extract(content) -> list[str]`      | `PassThroughExtractor`(\[content])       | `LLMFactExtractor`(OpenAI 兼容,env `SMILEX_EXTRACT_API_KEY/BASE_URL/MODEL`,temperature 0,失败/无 key → \[content])      | `[llm]`       |

server 层 config.toml 对应开关:`embedder` / `reranker` / `fact_extractor`。

## 7. 存储层

单文件 SQLite,`SCHEMA_VERSION = 13`。引擎
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

5 个核心任务(`tasks.py` `CoreTaskRunner`,`register_core_tasks`
配默认触发器):

| 任务          | 行为                                                                                                    | 关键参数                              |
| ----------- | ----------------------------------------------------------------------------------------------------- | --------------------------------- |
| consolidate | L1 fragments 按 entity/scope 分组聚合,固化写入 L2(triples)                                                     | 分组 flush(H1)                      |
| forget      | 留存分 `score = importance × 0.5^(age_days/half_life)`;`score < threshold` → 删除(默认)或降权(importance=score) | half\_life\_days=30,threshold=0.1 |
| summarize   | 规则式摘要(非 LLM),summary\_length 可调                                                                       | <br />                            |
| causal      | 遍历 predecessor\_id 链维护 causal\_chains 表                                                               | <br />                            |
| semantic    | NetworkX 构实体图,预计算连通分量("语义社区")缓存                                                                       | <br />                            |

`bootstrap/` 子包(冷启动):向导问答(`project_bootstrap` +
`onboarding`)、README 解析(`readme_parser`)、项目模板
(`templates/{agent,cli,data,web}.yaml`)、种子注入(`seeds/seed_injector`)、
批量导入(`bulk_importer`,Git/Markdown/文本)、跨项目克隆
(`cross_project_cloner`)、主动学习(`active_learner`)。

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
  session_id, project, top_k, token_budget)` / `memory_write(content,
  session_id, scope, scope_id, entities, relations, importance)` /
  `memory_init_project` / `memory_stats`;Reranker/Extractor 按 config
  工厂构造注入
- **REST**(`api.py`):`GET /stats` `/memories` `/memory/{id}`
  `/tasks` + `POST /recall-test`
- **Web 面板**:只读静态页(概览统计/记忆浏览/召回测试),写入统一走 MCP
- **CLI**(`cli.py` → `smilex-memory`):`serve` / `mcp` / `init`
  (向 Kimi Code / Claude Code 注入工具配置 + 记忆使用约定) / `doctor`;
  三平台自启动注册脚本在 `scripts/register-service-*`

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

