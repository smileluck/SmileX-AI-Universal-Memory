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

# 方式一: 全局常驻(推荐) — 单库 ~/.smilex/memory.db,scope 隔离多项目
smilex-memory serve          # MCP: http://127.0.0.1:8765/mcp,面板: http://127.0.0.1:8765/
smilex-memory init <项目目录> --guide   # 一键注入 MCP 配置 + 记忆使用约定

# 方式二: 项目级 stdio — 独立库 <项目>/.smilex/memory.db
smilex-memory init <项目目录> --stdio

# 方式三: 植入工具全局(--scope user,HTTP) — 一次配置,所有项目生效
smilex-memory init --scope user                      # codex/cursor/zcode/trae/claude(kimi 仅项目级)
smilex-memory init --scope user --tool codex zcode   # 只植入指定工具(--tool 可多值)

# 指定工具注入项目: --tool 取 kimi|claude|codex|cursor|zcode|trae|workbuddy|all
# Codex 为 TOML 配置(文本级手术合并,保留原有内容/注释),ZCode 写嵌套 mcp.servers

# 初始化即扫描: 冷启动 + 自动读 README + 导入 git 历史/markdown 文档为初始记忆
# (幂等可重跑;跳过 node_modules/.venv 等目录,stdio 模式写入项目内 .smilex/memory.db)
smilex-memory init <项目目录> --stdio --scan
# agent 侧等价: MCP 工具 memory_init_project(name, project_path="<项目根>")
# (stdio 模式 project_path 可省略,自动按 db 路径推断项目根)

# 环境自检
smilex-memory doctor
```

常驻自启动注册脚本(登录后自动 `smilex-memory serve`):

- Windows: `powershell -File scripts/register-service-windows.ps1`(`-Unregister` 卸载)
- Linux: `scripts/register-service-linux.sh`(`--uninstall` 卸载,systemd --user)
- macOS: `scripts/register-service-macos.sh`(`--uninstall` 卸载,launchd)

Web 面板(`http://127.0.0.1:8765/`)为只读,零依赖纯静态、可离线:
概览(统计卡 / L0-L3 分层叠条 / scope 分布 / 调度任务进度,15s 自动刷新)、
记忆浏览(FTS5 关键词搜索 + 类型/层/scope 过滤,行点击看全字段详情)、
召回测试(可调 top_k / session_id / token_budget,来源含分数条与片段);
另提供 `/api/health` 健康检查。写入统一走 MCP 工具
(`memory_recall` / `memory_write` / `memory_init_project` / `memory_stats`)。
详见 [Server 层设计](docs/design/modules/13-server-layer.md)。

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
