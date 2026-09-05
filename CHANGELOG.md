# Changelog

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/),
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
生产发布流程见 [docs/release-guide.md](docs/release-guide.md)。

## [Unreleased]

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
