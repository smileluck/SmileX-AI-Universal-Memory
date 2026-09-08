# SmileX Agent Memory System

面向 AI Agent 的嵌入式高级记忆系统 — 时序、空间、图谱、因果、语义五维一体化。

## 核心理念

> 「不完美的记忆 > 没有记忆」

- **单库一体化**: SQLite + sqlite-vec + R-tree + NetworkX,单文件部署
- **嵌入式优先**: `pip install` 即用,零外部服务依赖
- **项目隔离 + 跨项目共享**: `scope` 字段 + 自动提升机制
- **强契约 + 弱耦合**: 6 层架构,禁止反向依赖

## 技术栈

| 层次 | 技术 | 用途 |
|------|------|------|
| 主库 | SQLite 3.40+ | 主存储(Python 自带) |
| 向量 | sqlite-vec | HNSW 向量索引 |
| 空间 | R-tree | SQLite 内置空间索引 |
| 图谱 | NetworkX | L3 语义层算法 |
| 缓存 | cachebox | 进程内 LRU+TTL |
| 异步 | asyncio + aiosqlite | 异步 I/O |
| 数据模型 | dataclass + Pydantic v2 | 内部 + 边界验证 |

## Benchmark 成绩速览

记忆质量均以 glm-5.3-flash 作答+判卷(mem0 官方用 gpt-5,本项目成绩非同模型对比,略吃亏):
详细口径与逐项数字见 [Benchmark 一节](#benchmark)。

| 评测 | 口径 | 本项目 | mem0 官方(gpt-5) |
|---|---|---|---|
| LongMemEval(30 题分层采样) | mem0 兼容,headline | **96.7%** | 94.4% |
| LoCoMo(90 题采样) | mem0 兼容,headline | **98.9%** | 92.5% |
| LoCoMo 检索 R@10(全量 1536 题) | 裸 BGE-M3 KNN 基线 | 95.3% → **97.4%**(精排后) | — |

关键结论:

- **写入时事实抽取**是最大增益项:LongMemEval headline 93.3→96.7,
  且四个 cutoff 完全一致(小上下文即可装下答案要点,不受长 prompt 干扰);
  LoCoMo headline 持平 98.9%、cutoff=10 97.8→98.9,只赚不赔
- **Cross-Encoder 精排**全量 +2.1pp(四题型全涨),只改排序不改召回集
- 剩余失败几乎全部为数据集标注噪声 / answerer 推理上限,非检索或记忆形成缺陷
  (逐题归因见 [实测结果](#实测结果汇总))

## 快速开始

```bash
# 安装依赖(开发模式)
uv sync --extra dev

# 运行烟雾测试(验证 Windows 兼容性)
uv run pytest tests/smoke/ -v

# 代码检查
uv run ruff check src/ tests/
```

## 项目结构

```
src/smilex/
├── middlewares/         # MemoryMiddleware(Agent 访问入口)
├── memory/
│   ├── models/          # Layer 0: 数据模型
│   ├── storage/         # Layer 1: 存储引擎(schema/ 迁移 SQL、queries/ 查询模块)
│   ├── lifecycle/       # Layer 2: 生命周期(L0 工作记忆、Embedder、晋升)
│   ├── scheduler/       # Layer 3: 调度 + 冷启动(bootstrap/ 项目初始化)
│   ├── concurrency/     # Layer 4: 并发控制(P1)
│   └── quality/         # Layer 5: 知识质量(P2)
├── server/              # Server 层: MCP 服务 + Web 面板(可选 [server] extras)
└── utils/
```

## 服务化使用(MCP + Web 面板)

把记忆库作为 MCP 服务接入主流 Agent 工具:Kimi / Claude Code / Codex / Cursor /
ZCode / Trae 一键注入配置,WorkBuddy 输出手动接入指引(MCP 由其客户端 UI 管理)。

```bash
# 安装(核心库零新依赖,server 相关依赖按需装)
pip install 'smilex-ai-memory[server]'

# 后台静默运行(推荐) — 脱离终端,pidfile 管理,日志 ~/.smilex/logs/serve-{端口}.log
smilex-memory start          # 默认 127.0.0.1:8765;重复执行幂等
smilex-memory status         # 查看运行状态(pid / 健康 / 日志;退出码 0=运行中)
smilex-memory stop           # 优雅停止(SIGTERM → 超时 SIGKILL,--force 直接强杀)

# 自定义端口 / 配置文件(.toml 与 .yaml 均可,CLI 旗标 > 配置文件 > 默认值)
smilex-memory start --port 9000 --config ~/.smilex/config.yaml
smilex-memory status --port 9000 && smilex-memory stop --port 9000   # 多实例按端口隔离

# 前台运行(调试用) — serve 与 start 接受相同旗标,另有 --log-level debug|info|warning|error
smilex-memory serve --port 9000

# 方式一: 全局常驻(推荐) — 单库 ~/.smilex/memory.db,scope 隔离多项目
smilex-memory init <项目目录> --guide   # 一键注入 MCP 配置 + 记忆使用约定

# 方式二: 项目级 stdio — 独立库 <项目>/.smilex/memory.db
smilex-memory init <项目目录> --stdio

# 方式三: 植入工具全局(--scope user,HTTP) — 一次配置,所有项目生效
smilex-memory init --scope user                      # codex/cursor/zcode/trae/claude(kimi 仅项目级)
smilex-memory init --scope user --tool codex zcode   # 只植入指定工具(--tool 可多值)

# 指定工具注入项目: --tool 取 kimi|claude|codex|cursor|zcode|trae|workbuddy|all
# Codex 为 TOML 配置(文本级手术合并,保留原有内容/注释),ZCode 写嵌套 mcp.servers

# 初始化即扫描: 冷启动 + 自动读 README + 导入 git 历史/markdown 文档/源码结构为初始记忆
# (源码:.py 走 AST 提取模块文档/顶层定义/依赖,其余扩展名读文件头注释;幂等可重跑,
#  跳过 node_modules/.venv 等目录,stdio 模式写入项目内 .smilex/memory.db)
smilex-memory init <项目目录> --stdio --scan
# agent 侧等价: MCP 工具 memory_init_project(name, project_path="<项目根>")
# (stdio 模式 project_path 可省略,自动按 db 路径推断项目根)

# 环境自检(配置 / db / extras / 服务状态)
smilex-memory doctor
```

### 配置文件参数

配置默认读 `~/.smilex/config.toml`(首次 serve 自动生成精简模板);
`--config` 可指定任意路径,`.toml` 与 `.yaml/.yml` 均可(字段名相同,见仓库根
[config.example.yaml](config.example.yaml) 全字段注释示例)。
优先级:CLI 旗标 > 配置文件 > 默认值;多实例用不同端口(pidfile/日志按端口隔离)。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `db_path` | `~/.smilex/memory.db` | SQLite 数据库路径(支持 `~` 展开,目录自动创建) |
| `host` | `127.0.0.1` | 监听地址;`0.0.0.0` 监听所有网卡(注意暴露风险) |
| `port` | `8765` | 监听端口,可被 `--port` 覆盖 |
| `embedder` | `hash` | 嵌入器:`hash` 零依赖本地 / `sentence-transformers` 语义检索([embedding] extras,首启下载模型) |
| `reranker` | `noop` | 重排序器:`noop` / `cross-encoder` 精排([rerank] extras) |
| `fact_extractor` | `passthrough` | 事实抽取:`passthrough` 原文入库 / `llm` 写入时抽结构化事实([llm] extras,见下方环境变量) |
| `summarizer` | `rule` | 摘要压缩:`rule` 规则截取 / `llm` 调度任务生成 LLM 摘要([llm] extras + `SMILEX_SUMMARIZE_*`,失败自动降级规则) |
| `access_tracking` | `true` | 检索反馈闭环:recall 命中即累加访问计数(见「记忆主动优化」) |
| `max_records_per_scope` | `0` | 每 scope fragment 配额,forget 每日修剪超额(0=关闭;受保护行豁免) |
| `protect_importance` | `0.9` | 删除守卫:importance ≥ 此值豁免 forget/dedup 删除(confirm_protected 可越过) |
| `token_budget` | `4000` | 召回 token 预算(`memory_recall` 按此裁剪返回内容) |
| `enable_scheduler` | `true` | serve 进程内核心调度任务(遗忘衰减 / 语义 / 摘要 / 因果 / 巩固 + 每日 SQLite 巡检);stdio 模式不适用 |
| `metrics` | `true` | `GET /metrics` 指标端点(Prometheus 文本格式,零依赖) |
| `audit` | `true` | 变更事件 JSONL 审计(默认库文件旁 `audit-<db名>.jsonl`,不落原文) |
| `audit_path` | 空 | 审计文件显式路径(空 = 按库文件推导;`":memory:"` 库默认禁用) |
| `audit_reads` | `false` | 是否审计 recall 读事件(量大,默认只记变更) |
| `tracing` | `noop` | 分布式追踪:`noop` / `otel`([tracing] extras) |
| `pii_masker` | `noop` | 写入前 PII 脱敏:`noop` 直通 / `regex` 高置信度正则替换 |
| `api_key` | 空 | HTTP API Key 鉴权(推荐环境变量 `SMILEX_API_KEY`,见下) |
| `db_encryption_key` | 空 | SQLCipher 库加密(推荐环境变量 `SMILEX_DB_KEY`,实验性) |

`fact_extractor: llm` 需设置环境变量(放在 shell / 服务环境,不放配置文件):
`SMILEX_EXTRACT_API_KEY`(必填,缺失时抽取静默降级)、
`SMILEX_EXTRACT_BASE_URL`(可选,OpenAI 兼容代理 / 私有网关)、
`SMILEX_EXTRACT_MODEL`(模型名,默认 `glm-4.5-flash`)。
`summarizer: llm` 同理用 `SMILEX_SUMMARIZE_API_KEY / _BASE_URL / _MODEL`
(与 EXTRACT 三件套同形状,可用同一网关不同模型)。

常驻自启动注册脚本(登录后自动 `smilex-memory serve`,支持
`SMILEX_SERVE_ARGS="--config ~/.smilex/config.yaml --port 9000"` 传参):

- Windows: `powershell -File scripts/register-service-windows.ps1`(`-Unregister` 卸载)
- Linux: `scripts/register-service-linux.sh`(`--uninstall` 卸载,systemd --user)
- macOS: `scripts/register-service-macos.sh`(`--uninstall` 卸载,launchd)

Web 面板(`http://127.0.0.1:8765/`)为只读,零依赖纯静态、可离线:
概览(统计卡 / L0-L3 分层叠条 / scope 分布 / 运行告警 / 调度任务进度,15s 自动刷新)、
记忆浏览(FTS5 关键词搜索 + 类型/层/scope 过滤,行点击看全字段详情)、
召回测试(可调 top_k / session_id / token_budget,来源含分数条与片段);
另提供 `/api/health` 健康检查。写入统一走 MCP 工具
(`memory_recall` / `memory_write` / `memory_init_project` / `memory_stats`)。
详见 [Server 层设计](docs/design/modules/13-server-layer.md)。

### 记忆主动优化(检索反馈闭环 + 近重复合并)

参照 [DeepSeek Harness dsh-agent-memory](https://github.com/deepseek-ai/deepseek-harness/discussions/1448)
的治理模式:**确定性规则 + 使用反馈**,零 LLM 零新增依赖,默认开启。

- **使用即续命**:recall 命中的 fragment 自动累加 `access_count`/`last_accessed_at`
  (schema 014),遗忘任务的衰减锚点从"最后更新时间"改为"更新与最近访问的较新者"——
  被用过的记忆从使用时刻重新衰减,不再被时间冲走
- **常用即升值**:保留分乘 `min(3, 1 + log10(1 + access_count))`,高频记忆最多
  3 倍存活加成
- **近重复合并**(第 7 个核心调度任务 `dedup`,每日 LOW):同 scope 同层的近重复
  条目自动合并——FTS trigram 短语找候选 + 字符 bigram Jaccard ≥ 0.7(DSH 中文
  校准值)确认;幸存者为较早创建者,吸收重复行的访问计数(求和)/时间区间
  (并集)/实体(并集)/重要度(max),重复行连同向量删除。写得越多重复越多的
  问题由库自己收敛
- 关闭反馈:`access_tracking: false`(recall 回到纯只读);dedup 阈值可经
  `scheduler.submit("dedup", payload={"threshold": 0.8})` 调整
- **superseded 显式化**(主动优化二期): 同键 LWW 覆盖不再是静默双行 —
  旧行闭合(`valid_to = 新行 valid_from`)、新行记 `predecessor_id`(因果链
  任务自动接续;归档在 365 天后自动收纳;图策略召回只取当前有效边,
  被覆盖的旧值不再漏进上下文);相同值重复断言不触发覆盖
- **容量治理与删除守卫**(主动优化二期): `max_records_per_scope`(0=关闭)
  设定每 scope 配额,forget 每日把超额 scope 按留存分升序修剪;
  `protect_importance`(默认 0.9)以上的高价值记忆豁免 forget/dedup 的一切
  删除(显式 `confirm_protected: true` 可越过)— 对齐 DSH
  "importance=3 需 confirm" 的删除守卫语义
- 后续路线(参照 DSH 差距分析):lessonize 教训写入协议、错误指纹闭环、
  WRITE_WRITE 之外冲突类型接入写入路径

### API Key 鉴权与库文件加密(§15.4)

**API Key 鉴权**(默认关闭;`0.0.0.0` 对外监听或远程 agent 接入时建议开启):

```bash
export SMILEX_API_KEY="$(openssl rand -hex 24)"   # 环境变量优先于配置文件 api_key
smilex-memory start
```

- 生效后所有请求(MCP `/mcp`、`/api/*`、`/metrics`)需带
  `Authorization: Bearer <key>` 或 `X-API-Key: <key>`(恒时比较,防时序侧信道)
- 豁免: 面板静态资源与 `/api/health`(无 key 时 health 只返回运行状态与
  `auth: "required"`,不暴露配置摘要;daemon 三契约字段不受影响)
- 面板右上角有 API Key 输入框(存浏览器 localStorage,自动附在所有请求上)
- 工具接入(headers 支持):**kimi / claude / cursor** 由
  `smilex-memory init` 自动注入 `"headers": {"X-API-Key": ...}`;
  **codex / zcode / trae / workbuddy** 配置格式不支持或不安全(键名随版本
  漂移 / 严格 schema 会静默丢条目),init 会打印手动配置指引,
  手动写法:`{"url": "...", "headers": {"X-API-Key": "<key>"}}`
- stdio 模式(`smilex-memory mcp`)是本地管道,无需鉴权
- `doctor` 输出认证状态行;JWT/OAuth 留待多租户需求(mcp SDK 的
  TokenVerifier 通道已预留)

**库文件加密**(SQLCipher,实验性,默认关闭):

```bash
brew install sqlcipher                    # 或 apt install libsqlcipher-dev
pip install 'smilex-ai-memory[encryption]'
export SMILEX_DB_KEY="强随机密钥"          # 环境变量优先于配置文件
smilex-memory start
```

- 原理:`PRAGMA key` 必须是连接首条语句,随即校验 `PRAGMA cipher_version` —
  **非 SQLCipher 构建会立刻报错中止,绝不静默写出明文库**
- aiosqlite 绑定标准库 sqlite3,SQLCipher 需经 pysqlcipher3 shim
  (自动尝试)或 sqlcipher 构建的解释器;见 `storage/sqlcipher.py` 注释
- 注意:外部直连库文件的脚本(如 mem0_compat benchmark 的
  `sqlite3.connect`)对加密库需同样应用 key;加密 + sqlite-vec 向量扩展
  的组合取决于构建,启用前请小规模验证
- 密钥丢失 = 数据不可恢复,请妥善保管(可用 `openssl rand -hex 32` 生成)

### 可观测性与安全

四件套默认开启(除 tracing/PII 脱敏为可选后端),全部零新增核心依赖:

- **指标** — `GET /metrics`(Prometheus 文本格式,手写零依赖):write/recall
  计数与延迟直方图、调度任务终态(`smilex_task_total{name,status}`)、队列深度、
  库体积、每日巡检结果。接 Prometheus 直接抓取即可,告警规则样例见
  [docs/observability-alerts.yaml](docs/observability-alerts.yaml)
  (队列积压 >1000 / 库体积 >10GB / 任务失败率 >5%,与 `/api/health` 的
  `alerts` 字段同阈值 — 本地自包含告警,不依赖 Prometheus 也能看到)。
- **审计日志** — 变更事件 JSONL 追加写(默认库文件旁 `audit-<db名>.jsonl`):
  记录 session/scope/memory_ids/状态/耗时/**内容指纹**(`content_sha256`,
  不落原文 → 审计文件自身不含 PII)。`audit_reads: true` 可补记 recall。
- **健康检查增强** — `/api/health` 在 uptime/配置摘要之外新增 db 连接态、
  库体积、巡检结果(`db_integrity` 调度任务每日跑 `PRAGMA quick_check`)、
  调度器队列深度与 `alerts` 告警数组;原字段形状不变,daemon 依赖的
  `status`/`started_at`/`uptime_s` 契约保持。
- **PII 脱敏**(`pii_masker: regex` 开启)— 写入前对 content/实体名/三元组
  自由文本做高置信度替换:email / 手机号 / 身份证(GB11643 校验位)/ 银行卡
  (Luhn)/ IPv4 / API key(sk-/AKIA/ghp_/JWT/Bearer)。每类可独立选策略:
  `redact`(默认,`[EMAIL]`)/ `hash`(`[EMAIL:9f86d081]`,等值保持可连接)/
  `mask`(`138****5678`);支持 `extra_patterns` 自定义类别。库级用法:
  `MemoryMiddleware(..., pii_masker=get_pii_masker(PIIConfig(backend="regex")))`。
  注意:agent 记忆常常就是要记住用户的联系方式,故默认 `noop` 直通。
- **结构化日志** — structlog JSON → stderr(库内统一 `get_logger()`,
  默认 WARNING 级安静,`--log-level` 提级);调度任务失败、事实抽取降级等
  此前静默的路径现在都有日志。
- **分布式追踪**(可选 `[tracing]` extras)— `tracing: otel` 后
  write/recall/bootstrap 产生 OpenTelemetry span;默认 ConsoleSpanExporter
  打到 stderr,接 OTLP/Jaeger 在入口自配 provider 即可被尊重:

  ```python
  from opentelemetry.sdk.trace import TracerProvider
  from opentelemetry.sdk.trace.export import BatchSpanProcessor, OtlpGrpcSpanExporter
  provider = TracerProvider()
  provider.add_span_processor(BatchSpanProcessor(OtlpGrpcSpanExporter()))
  opentelemetry.trace.set_tracer_provider(provider)  # 先于服务启动执行
  ```

## Benchmark

三套评测,环境与模型配置通用:

| 套件 | 目录 | 回答的问题 | 需要 LLM key |
|---|---|---|---|
| 自家口径 LongMemEval | `benchmarks/longmemeval/` | 长期记忆问答质量(严判分、奖励拒答) | 是 |
| 自家口径 LoCoMo | `benchmarks/locomo/` | 超长多 session 对话问答(含 adversarial 拒答) | 是 |
| 检索基线对比 | `benchmarks/retrieval_baseline/` | 记忆管线 vs 裸向量 KNN 的 R@10 | **否** |
| mem0 兼容口径 | `benchmarks/mem0_compat/` | 与 mem0 公布分数直接对比 | 是 |

### 通用 LLM 配置(所有需要 key 的 benchmark 共用)

换模型只需改环境变量,代码零改动;生成与判卷模型独立配置,可混搭:

| 环境变量 | 作用 | 不设置时默认 |
|---|---|---|
| `OPENAI_API_KEY` | API key(必填) | 缺失则报错退出 |
| `OPENAI_BASE_URL` | API 网关地址 | OpenAI 官方 |
| `SMILEX_BENCH_ANSWER_MODEL` | 生成答案的模型 | `gpt-4o-mini` |
| `SMILEX_BENCH_JUDGE_MODEL` | 判卷的模型 | `gpt-4o-mini` |
| `SMILEX_EXTRACT_API_KEY/BASE_URL/MODEL` | 写入时事实抽取(`--fact-extraction`) | 复用 OPENAI_* ,模型默认 glm-4.5-flash |

常见配置示例:

```bash
# 智谱 GLM(免费额度大,推荐试跑)
export OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4
export SMILEX_BENCH_ANSWER_MODEL=glm-5.3-flash
export SMILEX_BENCH_JUDGE_MODEL=glm-5.3-flash

# DeepSeek
export OPENAI_BASE_URL=https://api.deepseek.com/v1
export SMILEX_BENCH_ANSWER_MODEL=deepseek-chat
export SMILEX_BENCH_JUDGE_MODEL=deepseek-chat

# OpenAI 官方(无需 BASE_URL)
export SMILEX_BENCH_ANSWER_MODEL=gpt-4o-mini
```

也可单行内联,不污染 shell 环境:

```bash
OPENAI_API_KEY=你的key \
OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4 \
SMILEX_BENCH_ANSWER_MODEL=glm-5.3-flash \
SMILEX_BENCH_JUDGE_MODEL=glm-5.3-flash \
uv run python benchmarks/longmemeval/run_benchmark.py --limit 10
```

注意事项(GLM/智谱实测经验):

- `SMILEX_BENCH_ANSWER_MODEL` 等变量务必作为命令行前缀或确认已 export
  (跑完看报告头部 `answerer=` 行核对,环境变量未生效是最常见的坑)
- checkpoint 文件名不含模型名,换模型需把 `results/ckpt_*` 移开再用 `--resume`
- judge 偶发把思考文本耗尽 max_tokens 导致空判卷;`rejudge.py`
  (longmemeval/ 与 mem0_compat/ 各一份)可离线重判,不必重新灌入

### 自家口径:LongMemEval

使用 [LongMemEval](https://huggingface.co/datasets/xiaowu0162/longmemeval)
(longmemeval_s,500 题):对话历史灌入 → `recall()` 构建上下文 → LLM 生成答案
→ LLM-as-judge 对比 gold,输出总分 / 各题型准确率 / 注入 token 数 / 检索延迟。
判分从严:语义等价 + 奖励正确拒答。

```bash
uv sync --extra embedding --extra benchmark
uv run python benchmarks/longmemeval/download_dataset.py   # ~270MB,缓存到 data/

uv run python benchmarks/longmemeval/run_benchmark.py --limit 50  # 小规模试跑
uv run python benchmarks/longmemeval/run_benchmark.py             # 全量 500 题
```

其他参数:`--embedder sentence-transformers|hash`、`--token-budget 4000`、
`--top-k 10`、`--dump-context`(调试)、`--config longmemeval_s`。
每样本独立临时 db,全程走 `MemoryMiddleware` 公开 API(write / recall);
结果存 `benchmarks/longmemeval/results/`;无 key 可用
`_smoke_stub.py` 冒烟验证链路。

### 自家口径:LoCoMo

使用 [LoCoMo](https://github.com/snap-research/locomo)(locomo10,10 个长对话 /
1986 题),环境变量同上。与 LongMemEval 的差异:

- **数据形态**:每样本是单人双 speaker 超长对话(19~32 个 session /
  419~689 轮);同一对话的所有题共享一次灌入(checkpoint 键 `conv_idx + q_idx`)
- **题型**:single-hop / multi-hop / temporal / open-domain / adversarial,
  按题型分组报告;**adversarial** 语义为"对话中不可回答",gold 视作
  "I don't know.",模型拒答才得分

```bash
uv run python benchmarks/locomo/download_dataset.py   # ~2.7MB
uv run python benchmarks/locomo/run_benchmark.py --limit 1 --max-qa 5
uv run python benchmarks/locomo/run_benchmark.py --resume   # 全量
```

### 检索基线对比(裸 BGE-M3 vs 记忆管线)

量化记忆算法相对 naive RAG 的检索增益:同一份数据、同一种切块,对比
`raw`(BGE-M3 余弦 KNN)与 `smilex`(向量 KNN + BM25/FTS5 关键词通道 RRF 融合)
的 R@10。纯本地检索评测,无需 LLM key。

```bash
uv sync --extra embedding
uv run python benchmarks/retrieval_baseline/run_baseline.py --dataset locomo --limit 1
uv run python benchmarks/retrieval_baseline/run_baseline.py --dataset longmemeval --limit 10
uv run python benchmarks/retrieval_baseline/run_baseline.py --dataset locomo --rerank --skip-raw   # 加精排
```

| 通道 | 含义 |
|---|---|
| `raw_bge_m3` | 裸 BGE-M3 KNN,严格 top-10 |
| `smilex_recall` | 融合排名前 10 个 source(与 raw 同口径) |
| `smilex_recall_all` | recall 实际返回的全部 source(双通道去重后 ~2×10) |

命中判定:LongMemEval 按 gold `answer_session_ids`,LoCoMo 按 gold
`evidence`(dia_id 粒度;adversarial 不计入)。LoCoMo category 映射以 mem0
实现为准(与论文编号不同):1=multi-hop 2=temporal 3=open-domain
4=single-hop 5=adversarial。

注意:macOS MPS 连跑多个 LongMemEval 样本时可能触发 Metal 命令挂起,
可用 `--device cpu` 绕过(慢但稳)。

### mem0 兼容口径

为使分数可直接与 mem0 公布的 LoCoMo 92.5 / LongMemEval 94.4 对比,按
[mem0ai/memory-benchmarks](https://github.com/mem0ai/memory-benchmarks) 协议
逐项对齐实现的独立 runner。与自家口径的根本差异是**判分哲学**:mem0
禁止拒答 + 判卷宽松;自家口径奖励拒答 + 严格语义等价。两套并存,报告时注明口径。

```bash
# LoCoMo(categories 1-4 全量,adversarial 不计分)
uv run python benchmarks/mem0_compat/locomo.py --conversations 0 --max-qa 2  # 冒烟
uv run python benchmarks/mem0_compat/locomo.py --resume                      # 全量

# LongMemEval(默认每题型 5 题分层采样 seed=42,共 30 题)
uv run python benchmarks/mem0_compat/longmemeval.py --per-type 1   # 冒烟 6 题
uv run python benchmarks/mem0_compat/longmemeval.py                # mem0 默认 30 题

# 可选开关(两者可叠加)
#   --fact-extraction   写入时 LLM 事实抽取(ckpt 加 _fx 后缀)
#   --source cleaned    LongMemEval 社区修正版数据(默认 original)
#   --rerank            检索精排(ckpt 加 _rr 后缀)

# 离线重判(修 judge 空判卷/误判,不重新灌入)
uv run python benchmarks/mem0_compat/rejudge.py <结果json> [--all] [--dry-run]
```

与 mem0 协议的对齐项:

| 项 | mem0 官方 | 本 runner |
|---|---|---|
| LoCoMo 题目范围 | categories 1-4(排除 adversarial) | 相同 |
| LoCoMo gold 预处理 | category 3 取分号前第一段 | 相同 |
| 采样(LME) | 每题型 5 题 seed=42 | 相同(`--per-type/--seed`) |
| 检索 | top-200 一次取全 | 相同(`recall top_k=200`) |
| 截断点 | cutoffs 10/20/50/200 各评一次 | 相同 |
| 记忆呈现 | 时间序、不显示 score | 相同(session 日期锚定 time_range) |
| 作答/判卷 prompt | mem0 官方原文 | 逐字移植(`mem0_prompts.py`) |
| headline 指标 | 最大 cutoff(200) | 相同 |
| 作答/判卷模型 | gpt-5 / gpt-5 | GLM 环境变量约定(比较时注意差异) |
| 记忆形成 | LLM 抽取事实卡片 | 原文分层存储(默认)/ `--fact-extraction` 对齐 |

### 可选增强:精排与事实抽取

两项增强均默认关闭(零依赖行为不变),经 benchmark 定位弱点后引入:

**Cross-Encoder 精排**(`[rerank]` extra,bge-reranker-v2-m3)

- 位置: 多路召回 RRF 融合之后、token 裁剪之前,对 L1/L2 候选按层精排
  (L0 工作记忆不动);每层上限 50 条候选保护 recall 延迟预算
- 接入: `MemoryMiddleware(reranker=get_reranker(RerankerConfig(backend="cross-encoder")))`
  或 config.toml `reranker = "cross-encoder"`
- 实测(LoCoMo 全量 1536 题):R@10 95.3% → **97.4%**(+2.1pp,四题型全涨);
  全返回口径不变 98.5%(只改排序不改召回集)。代价:本机(MPS)约 4.4s/查询,
  生产低延迟场景可减小 max_candidates 或换 reranker-base

**写入时 LLM 事实抽取**(`[llm]` extra,OpenAI 兼容 API,core 保持零 LLM)

- `FactExtractor` Protocol,默认 `PassThroughExtractor`(原 content 整块写入);
  `LLMFactExtractor` 把长块抽取为原子事实(一句一事、保留日期/实体/待办),
  每条事实独立入库并可检索——对齐 mem0 的事实卡记忆形成方式,
  是计数 / 跨 session 整合类问题的关键补强
- 失败语义: 未配置 key / 调用失败一律降级回原文整块,write 永不因抽取崩
- 接入: 构造 `LLMFactExtractor()`(env `SMILEX_EXTRACT_*`)
  或 config.toml `fact_extractor = "llm"`

**顺带修复**: `WriteRequest.time_range` 现在会锚定到晋升后的
`temporal_fragments.time_start`(此前用墙钟 created_at,历史对话时间丢失)。

### 实测结果汇总

除注明外,answerer/judge 均为 glm-5.3-flash(mem0 官方为 gpt-5)。

**LongMemEval 四格对照**(30 题分层采样,headline = cutoff 200):

| | original | cleaned |
|---|---|---|
| 无抽取 | 93.3% | 93.3% |
| + 事实抽取 | **96.7%**(四 cutoff 全同) | **96.7%**(multi-session 满分) |

- 事实抽取使各 cutoff 完全一致(10 条记忆的小上下文即可装下答案要点,
  不再依赖大 cutoff 凑证据、也不受长 prompt 干扰),超过 mem0 官方 94.4(gpt-5)
- original 版唯一失败为标注歧义计数题(gold 把"从亲友处取回衣物"计入
  "店内取/退",模型答"2 items"其实合理);cleaned 版唯一失败为
  knowledge-update 新旧值冲突题(原文明确说过 "six times now",answerer
  抱住了被强调两次的旧值)——answerer 推理上限,非记忆缺陷

**LoCoMo**(对话 0-2 各取前 30 题,共 90 题):

| 配置 | cutoff 10 | 20 | 50 | **200(headline)** |
|---|---|---|---|---|
| 无抽取 | 97.8% | 97.8% | 98.9% | **98.9%** |
| + 事实抽取 | 98.9% | 97.8% | 98.9% | **98.9%** |

分题型(headline):multi-hop 36/36、temporal 45/45、single-hop 2/2、
open-domain 5/7。两版剩余失败完全重合,均为 open-domain 推断题的标注噪声
(如 "John 的学位专业",gold 期望从弱线索推出 Political science,而对话
文本通篇指向 mechanical engineering)。注意:此为采样口径(题型分布偏
temporal/multi-hop,与 mem0 全量 ~1540 题有差异),仅供参考;全量可
`--resume` 续跑。

**检索 R@10**(retrieval_baseline,LoCoMo 全量 10 对话 1536 题):

| 配置 | multi_hop | open_domain | single_hop | temporal | Overall |
|---|---|---|---|---|---|
| 裸 BGE-M3 KNN | — | — | — | — | 86.7%(conv-26 采样) |
| smilex 无精排 | 94.7% | 79.3% | 97.7% | 94.1% | 95.3% |
| smilex + 精排 | 96.5% | 83.7% | 99.0% | 97.8% | **97.4%** |

全返回口径(recall 实际返回的全部 source)精排前后均为 98.5%。
当前最弱题型为 open_domain(世界知识长尾,切块内容本身是天花板)。

## 设计文档

- [现状架构总览](docs/design/architecture-asbuilt.md) — **as-built**:已实现系统的架构/数据流/设计决策(先读)
- [架构整合](docs/design/agent-memory-design.md) — 设计时 spec/contract(部分选型已演进,以 as-built 为准)
- [模块总览](docs/design/modules/00-overview.md) — 13 个模块文档导航
- [Embedding 选型](docs/analyse/embedding-layer.md) — 技术栈选型论证
- [发布说明](docs/release-guide.md) — TestPyPI(测试)/ PyPI(生产)发布流程
- [更新日志](CHANGELOG.md) — 各版本变更记录

## 阶段路线图

| 阶段 | 时长 | 范围 |
|------|------|------|
| MVP | 2 周 | 主流程闭环(创建→写入→检索) |
| P0 | 2 周 | 任务调度 + 抢占 |
| P1 | 3 周 | 并发 + 批量导入 + 种子注入 |
| P2 | 2 周 | 知识质量 + 跨项目克隆 |
