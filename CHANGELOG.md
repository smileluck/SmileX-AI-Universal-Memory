# Changelog

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/),
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
生产发布流程见 [docs/release-guide.md](docs/release-guide.md)。

## [Unreleased]

### Added

- **记忆主动优化一期(检索反馈闭环 + 近重复合并,参照 DeepSeek Harness
  dsh-agent-memory 治理模式;确定性规则,零 LLM 零新增依赖)**:
  - schema 014: temporal_fragments 新增 `access_count`/`last_accessed_at`
    (ALTER,既有库升级无损;FTS 触发器只在 UPDATE OF content 联动,计数不扰索引)
  - 检索反馈闭环(默认开启,`access_tracking: false` 可关): recall 命中的
    fragment 自动累加访问计数 — source id 为混合命名空间(L1 向量通道含
    实体/三元组 id),先 IN 过滤出真实 fragment 行再批量 UPDATE,统计写失败
    只降级不抛;`MemoryMiddleware(track_access=)` / ServerConfig 透传
  - forget 任务公式扩展: **使用即续命**(衰减锚点从 updated_at 改为
    max(updated_at, last_accessed_at))+ **常用即升值**
    (`× min(3, 1+log10(1+access_count))`),无访问数据时行为与历史一致
  - 第 7 个核心任务 `dedup`(每日 LOW): 同 scope/layer 近重复合并 —
    FTS trigram 短语找候选 + 字符 bigram Jaccard ≥ 0.7(DSH 中文校准值)
    确认;幸存者 = 较早 created_at,吸收访问计数(求和)/时间区间(并集)/
    实体(保序并集)/importance(max);重复行先清向量(vector_links FK
    引用热表)再删除;keyset 分页 + checkpoint 断点续传,幂等可重跑
- 面板零成本可见: `/api/memory/{id}` 详情自动展示新列(access_count/
  last_accessed_at)

- **LLM 摘要压缩(P3 演进路径,默认规则版)**: 新可选组件
  `memory/summarizer.py`(与 embedder/extractor 同款 Protocol 模式)—
  `Summarizer` Protocol + `RuleSummarizer`(原任务内联规则原样迁入,行为
  逐字一致)+ `LLMSummarizer`(OpenAI 兼容,env
  `SMILEX_SUMMARIZE_API_KEY/BASE_URL/MODEL`,temperature 0,GLM 系模型
  自动附 `{"thinking":{"type":"disabled"}}` 防推理吃 token);降级语义:
  无 key/缺包/调用失败一律回退规则摘要,绝不抛(调度器会删 FAILED 任务的
  checkpoint,摘要任务必须自愈)。`summarize` 调度任务接受后端注入,
  幂等键 `:summary`/字段继承/checkpoint 机制不变;
  `ServerConfig.summarizer = rule | llm`,app lifespan 装配
- **面板运行告警可视化**: 概览页新增「运行告警」区(告警 badge 按严重度
  着色,空时显示"无告警";附队列深度/执行中任务/巡检状态元信息行)—
  消费 `/api/health` 已有的 alerts/scheduler/db 字段,auth 裁剪载荷下
  安全跳过

- **HTTP API Key 鉴权(§15.4,默认关闭)**: `server/auth.py` 中间件挂主 app
  最外层,单点覆盖 MCP / /api/* / /metrics;双方案取 key
  (`Authorization: Bearer` 或 `X-API-Key`),`hmac.compare_digest` 恒时比较;
  豁免面板静态资源与 `/api/health`(无 key 时 health 只返回 daemon 契约
  字段 + `auth: "required"`,不暴露配置摘要)。配置 `api_key` 键,环境变量
  `SMILEX_API_KEY` 优先(密钥不落盘首选);`doctor` 输出认证状态行
- **工具配置 headers 自动注入**: `smilex-memory init` 在启用鉴权时为
  kimi/claude/cursor 的 HTTP 条目自动追加 `"headers": {"X-API-Key": ...}`
  (格式官方支持);codex/zcode/trae/workbuddy 保守不注入(键名漂移/严格
  schema 静默丢条目),改打印手动指引
- **面板鉴权支持**: 右上角 API Key 输入框(localStorage `smilex.apikey`),
  单一 fetch 出口统一附 `X-API-Key`;401 时健康丸显示"未授权"引导输入
- **SQLCipher 库文件加密(§15.4,实验性,默认关闭)**: `SQLiteEngine
  (encryption_key=)` — `PRAGMA key` 为连接首条语句,随即校验
  `PRAGMA cipher_version`,非 SQLCipher 构建立即报错中止(**绝不静默写
  明文库**);`storage/sqlcipher.py` pysqlcipher3 shim(自动尝试);可选
  extra `[encryption]`(需系统 libsqlcipher);配置 `db_encryption_key` 键,
  环境变量 `SMILEX_DB_KEY` 优先;`build_middleware` 经既有 engine 注入口
  组装(MemoryMiddleware 零改动)
- ServerConfig 新增 2 键: `api_key` / `db_encryption_key`(example.yaml
  以注释形态展示并加守卫测试,不引导密钥落盘)
- pragma 字符串值改单引号包裹转义(`PRAGMA {key}='{value}'`,防运算符
  配置中的引号/分号破坏语句 — 安全审计发现的唯一裸拼接点加固)

### 安全核查结论(§15.4 收尾,写入 asbuilt 安全注记)

- SQL 注入: 全库审计无用户可控字符串进入 SQL 文本(FTS MATCH 短语转义后
  参数绑定、scope 全程 `?` 绑定、表名白名单/字面量)
- 作用域隔离: `build_scope_clause` 全库强制参数化 scope 过滤,达标
- 向量隔离: ChromaDB 路线已废弃(决策 D1),sqlite-vec 单库行级 scope
  过滤天然隔离,原条目标注不适用


- **可观测性四件套(§15.3,零新增核心依赖)**:
  - `GET /metrics` 指标端点(Prometheus 文本格式,`memory/observability/
    metrics.py` 手写 Counter/Gauge/Histogram 注册表,Gauge 支持回调惰性
    求值): write/recall 计数与延迟直方图、`smilex_task_total{name,status}`
    任务终态、队列深度、库体积、巡检结果;`/api/*` 请求计数(路径模板化
    防 label 基数爆炸);告警规则样例 `docs/observability-alerts.yaml`
    (队列>1000 / 库>10GB / 失败率>5%,与 `/api/health` 的 alerts 同阈值)
  - 审计日志(JSONL 追加写,默认库文件旁 `audit-<db名>.jsonl`): 变更事件
    记录 session/scope/memory_ids/状态/耗时/内容指纹 — **不落原文**,
    审计文件自身不含 PII;`audit_reads` 可选记录 recall
  - `/api/health` 升级: 新增 db 连接态/体积/巡检结果、调度器队列深度、
    alerts 告警数组;`status`/`started_at`/`uptime_s` 契约字段不变
    (daemon status 依赖)
  - 第 6 个核心调度任务 `db_integrity`(LOW,每日 `PRAGMA quick_check`
    + 页统计 → `smilex_db_integrity_ok` gauge + health)
  - 结构化日志: structlog(核心依赖但从未使用)正式启用,库内统一
    `get_logger()` JSON→stderr;调度任务失败与事实抽取降级不再静默
  - `TelemetryMemoryMiddleware`(组合包装,核心 write/recall 零改动):
    指标/审计/span 三合一;`Telemetry.from_config` 按 ServerConfig 组装
- **PII 脱敏(§15.4,默认 noop 一键开启)**: `memory/pii.py` 可选组件
  (与 reranker/extractor 同模式): `RegexPIIMasker` 高置信度内置类别 —
  email / 手机号 / 身份证(GB11643 校验位)/ 银行卡(Luhn)/ IPv4(段值
  校验)/ API key(sk-/AKIA/ghp_/JWT/Bearer);每类独立策略 redact(默认)/
  hash(sha256 前 8 位,等值保持可连接)/ mask(部分保留);`extra_patterns`
  自定义类别;`MemoryMiddleware(pii_masker=...)` 于 write() 入口对
  content/实体名/三元组自由文本统一脱敏(幂等;默认 noop 直通,记忆系统
  常需记住用户联系方式)
- **OpenTelemetry 分布式追踪(可选 `[tracing]` extras)**: `Tracer` Protocol
  + NoopTracer 默认零开销;`tracing: otel` 后 write/recall/bootstrap 产生
  真实 span(默认 ConsoleSpanExporter,宿主自配 OTLP provider 时优先尊重)
- ServerConfig 新增 6 键: `metrics`/`audit`/`audit_path`/`audit_reads`/
  `tracing`/`pii_masker`(全字段注释入 config.example.yaml,守卫测试同步)
- `MemoryTaskScheduler` 公共 `queue_depth`/`current_task`/`recent_tasks`
  属性与 `observer` 终态回调(任务计数与失败告警的挂钩点);
  `StorageEngine.db_path` 属性
- `/api/stats` 与 MCP `memory_stats` 重复的计数 SQL 统一为
  `server/stats.py collect_stats()`(memory_stats 返回字段随之补齐对齐)

### Fixed

- 事实抽取器异常静默降级现在会打 warning 日志(此前完全无痕)
- 调度任务失败(_run_guarded)现在有 error 结构化日志(此前仅内存
  history 可查)

### Changed

- **项目结构重构(零 API 破坏,全部公共导入路径经 shim/门面保留)**:
  - `middlewares/dto.py` 内容下沉为 `memory/contracts.py`(它只依赖
    models+utils,本属契约层),旧路径变纯 re-export shim;断开
    `scheduler/bootstrap → middlewares` 反向依赖,循环依赖不复存在
  - `memory/lifecycle/{embedder,reranker,extractor}.py` 上移为
    `memory/{embedder,reranker,extractor}.py`(基础设施客户端无生命周期
    语义);修复 `storage → lifecycle` 倒挂边,旧路径留 shim
  - 四个大文件拆分(方法体不变,模块路径与类名不变):
    `scheduler/tasks.py` → `tasks/` 包(5 任务模块 + `_common`);
    `bulk_importer.py` 纯解析函数 → `bootstrap/import_parsing.py`;
    `middlewares/memory.py`(915 行)→ 宿主 + `_write_path`/`_recall_path`/
    `_bootstrap_facade` 三个 mixin(242 行);
    `cli.py` → `cli/` 包(`adapters`/`inject` + 子命令,入口点不变)
  - benchmarks: locomo/longmemeval 逐字节相同的 `llm_client/judge/answer`
    提公为 `benchmarks/_shared/`;mem0_compat 与 retrieval_baseline 的
    importlib 文件加载 hack 改为标准包导入;README 全部调用方式不变

### Added

- **后台静默运行 + 端口指定 + 指定配置文件启动**:
  - 新增 `smilex-memory start / stop / status` 三件套(`server/daemon.py`,
    全 stdlib 零新增依赖): start 后台脱离终端(POSIX `start_new_session` /
    Windows `DETACHED_PROCESS`),pidfile `~/.smilex/serve-{端口}.pid`、日志
    `~/.smilex/logs/serve-{端口}.log` 按端口隔离多实例;轮询 `/api/health`
    确认就绪(30s 预算兼容嵌入模型慢加载),子进程早夭打印日志尾部;
    stop 走 SIGTERM 优雅退出 → 10s 超时 SIGKILL 兜底(`--force` 直接强杀);
    重复 start 幂等,stale pidfile 自动清理;`cli/__main__.py` 支持
    `python -m smilex.cli` 再执行
  - 全子命令新增 `--config <路径>` 指定配置文件,`.toml` 与 `.yaml/.yml`
    均可(按扩展名识别,PyYAML 已是核心依赖);显式路径不存在时报错退出
  - `serve`/`start` 新增 `--log-level debug|info|warning|error`(透传 uvicorn),
    `start` 另支持 `--log-file`;`doctor` 输出服务状态行(daemon 运行中/
    非 daemon 启动/未运行)
  - 常驻注册脚本支持 `SMILEX_SERVE_ARGS` 环境变量向 `serve` 传参
    (如 `--config ~/.smilex/config.yaml --port 9000`)
- 仓库根新增 `config.example.yaml`(全字段中文注释示例:格式/优先级/多实例/
  embedder/reranker/fact_extractor 的 extras 依赖与 `SMILEX_EXTRACT_*` 环境变量);
  README 新增「配置文件参数」小节(字段表 + 环境变量说明);
  守卫测试确保示例文件始终可加载且与默认值一致
- 结构守卫测试(`tests/unit/test_architecture.py`): AST 断言
  `memory/**` 不 import `smilex.middlewares`、`storage/**` 不 import
  `memory.lifecycle`、pyproject version == `smilex.__version__`
- benchmarks 纳入 ruff CI(`mem0_prompts.py` 的 prompt 长文本行豁免 E501)

### Chore

- `__version__` 0.1.0 → 0.1.2 对齐 pyproject(由守卫测试防再漂移)
- git 产物清理: mem0_compat 7 个 ckpt(可 `--resume` 重建)与
  locomo 数据集 jsonl(download_dataset.py 可重下)移出版本库,
  补 .gitignore 规则;人工报告 md/json 保留

### Added(待发布功能的既有条目)

- **初始化扫描新增源码通道**: `memory_init_project` / `init --scan` /
  `bootstrap_project(scan_code=True)` 扫描项目源码结构生成初始记忆——
  `.py` 用 AST 提取模块 docstring、顶层类与函数、内外部依赖
  (标准库过滤,绝对导入按本目录/src 布局/仓库根顺序解析为仓库内
  相对路径),其余代码扩展名
  (.ts/.go/.rs 等)读文件头注释兜底;文件/类/外部库分别落
  `file:`/`class:`/`tech:` 实体(与 git 通道同命名约定,跨通道去重),
  复用 markdown 的目录忽略规则与 `max_files` 上限,幂等可重跑

## [0.1.2] - 2026-09-04

### Added

- **FTS5 BM25 关键词双通道检索**(schema v13): L1 检索在向量 KNN 之外
  新增 BM25 关键词通道,RRF(k=60)融合——修复专有名词/日期等精确词漏检,
  LoCoMo 采样 R@10 较裸向量 +10pp
- **Cross-Encoder 精排**(可选 `[rerank]` extra,默认 noop):
  RRF 融合后对 L1/L2 候选按层精排(bge-reranker-v2-m3,每层上限 50 条
  候选保护延迟);全量 1536 题 R@10 95.3% → 97.4%
- **写入时 LLM 事实抽取**(可选 `[llm]` extra,默认 PassThrough,
  core 保持零 LLM): `FactExtractor` Protocol + `LLMFactExtractor`,
  长块拆原子事实逐条入库,失败降级回原文;抽取模式下事实可跳过 L0
  直送 L1(`facts_bypass_l0`)
- **初始化即扫描生成初始记忆**: `MemoryMiddleware.bootstrap_project()`
  编排冷启动 + 扫描(README 自动读取 + git 历史 + markdown 文档 →
  L1 记忆与实体/三元组种子);MCP `memory_init_project` 新增
  `project_path/scan_git/scan_markdown/max_commits` 参数(stdio 模式
  project_path 可按 db 路径自动推断),CLI `smilex-memory init --scan`
  一键完成(stdio 写项目内库,HTTP 直写全局库)
- **冷启动 scope 按项目名复用**: 同名项目重复 `initialize_project`
  不再新建 scope(按稳定 entity_id 查回),重复初始化/扫描幂等
- **主流编码工具植入**: `smilex-memory init` 新增 Codex(TOML 文本级手术
  合并,保留原有内容/注释)/ Cursor / ZCode(嵌套 `mcp.servers`,条目
  最小化)/ Trae 适配器与 WorkBuddy 手动接入指引;`--scope user` 写工具
  全局配置(`~/.codex/config.toml`、`~/.cursor/mcp.json`、
  `~/.zcode/cli/config.json`、`~/.claude.json` 等,仅 HTTP 模式);
  `--tool` 支持多值;stdio 条目自动解析绝对路径(GUI 工具无 shell PATH)
- markdown 目录扫描忽略规则: 跳过 node_modules/.venv/dist 等依赖与
  构建目录及隐藏目录;`ImportSource.max_files`(默认 500)截断
- Benchmark 工具链四套: LongMemEval / LoCoMo(自家严口径)、
  retrieval_baseline(纯检索 R@10,无需 LLM key)、mem0_compat
  (mem0 官方协议对齐口径,含离线 rejudge 工具)
- 服务化配置支持 `reranker` / `fact_extractor` 开关(config.toml)
- CI: lint + Python 3.11-3.14 测试矩阵;发布流水线增加测试门禁、
  tag/版本一致性校验、provenance attestation
- 文档: as-built 架构总览(含名词解释表)、README 成绩速览

### Fixed

- promotion 时间锚定: `time_start` 取 `time_range.exact > approx_start >
  created_at`(此前一律用墙钟 created_at,历史对话灌入后真实时间丢失)
- embedder 对 `<|endoftext|>` 等特殊 token 字面量的净化
  (sentence-transformers v6 丢弃 tokenizer_kwargs 后的兼容修复)

### 记忆质量成绩(mem0 兼容口径,glm-5.3-flash 作答+判卷)

- LongMemEval: 96.7%(mem0 官方 94.4,gpt-5)
- LoCoMo: 98.9%(mem0 官方 92.5,gpt-5)

## [0.1.1] - 2026-08-28

- MIT License + PyPI 发布 workflow(Trusted Publishing/OIDC)
- `requires-python >=3.11`

## [0.1.0] - 2026-08

首个发布版本: MVP 全量(Layer 0-5)。

- 数据模型(FuzzyMemory/图谱/因果)+ SQLite 存储引擎
  (sqlite-vec 向量 / R-tree 空间 / 递归 CTE 图查询 / 时态查询)
- L0 工作记忆(LRU + 快照)、Token Budget 上下文构建、L0→L1 晋升
- 三重触发调度器 + GRACEFUL 抢占断点续跑;5 类核心任务
  (整合/遗忘/摘要/因果/语义);项目冷启动 bootstrap
- Layer 4 锁管理器 + 冲突检测;Layer 5 矛盾检测 / 归档冷热分层 /
  跨项目共现提升 global
- MCP server + Web 面板 + 三平台自启动注册脚本
