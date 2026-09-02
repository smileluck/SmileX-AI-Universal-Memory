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

把记忆库作为 MCP 服务接入 Kimi Code / Claude Code 等 Agent 工具:

```bash
# 安装(核心库零新依赖,server 相关依赖按需装)
pip install 'smilex-ai-memory[server]'

# 方式一: 全局常驻(推荐) — 单库 ~/.smilex/memory.db,scope 隔离多项目
smilex-memory serve          # MCP: http://127.0.0.1:8765/mcp,面板: http://127.0.0.1:8765/
smilex-memory init <项目目录> --guide   # 一键注入 MCP 配置 + 记忆使用约定

# 方式二: 项目级 stdio — 独立库 <项目>/.smilex/memory.db
smilex-memory init <项目目录> --stdio

# 环境自检
smilex-memory doctor
```

常驻自启动注册脚本(登录后自动 `smilex-memory serve`):

- Windows: `powershell -File scripts/register-service-windows.ps1`(`-Unregister` 卸载)
- Linux: `scripts/register-service-linux.sh`(`--uninstall` 卸载,systemd --user)
- macOS: `scripts/register-service-macos.sh`(`--uninstall` 卸载,launchd)

Web 面板为只读(概览统计 / 记忆浏览 / 召回测试);写入统一走 MCP 工具
(`memory_recall` / `memory_write` / `memory_init_project` / `memory_stats`)。
详见 [Server 层设计](docs/design/modules/13-server-layer.md)。

## 记忆质量 Benchmark(LongMemEval)

对标 mem0 的记忆评测,使用 [LongMemEval](https://huggingface.co/datasets/xiaowu0162/longmemeval)
(longmemeval_s,500 题)评估长期记忆问答质量:对话历史灌入 → `recall()` 构建上下文
→ LLM 生成答案 → LLM-as-judge 对比 gold,输出总分 / 各题型准确率 / 注入 token 数 / 检索延迟。

```bash
# 1. 安装依赖(embedding + benchmark extras)
uv sync --extra embedding --extra benchmark

# 2. 下载数据集(~270MB,缓存到 benchmarks/longmemeval/data/)
uv run python benchmarks/longmemeval/download_dataset.py

# 3. 配置 LLM(OpenAI 兼容 API,用于答案生成与判卷)
export OPENAI_API_KEY=sk-...
# 可选: OPENAI_BASE_URL / SMILEX_BENCH_ANSWER_MODEL / SMILEX_BENCH_JUDGE_MODEL(默认 gpt-4o-mini)

# 4. 运行(先小规模试跑)
uv run python benchmarks/longmemeval/run_benchmark.py --limit 50
uv run python benchmarks/longmemeval/run_benchmark.py           # 全量 500 题
```

### 指定模型(任一 OpenAI 兼容服务)

换模型只需改环境变量,代码零改动;生成与判卷模型独立配置,可混搭
(如答案用 `glm-4.5-flash`、判卷用更稳的 `glm-4.5`):

| 环境变量 | 作用 | 不设置时默认 |
|---|---|---|
| `OPENAI_API_KEY` | API key(必填) | 缺失则报错退出 |
| `OPENAI_BASE_URL` | API 网关地址 | OpenAI 官方 |
| `SMILEX_BENCH_ANSWER_MODEL` | 生成答案的模型 | `gpt-4o-mini` |
| `SMILEX_BENCH_JUDGE_MODEL` | 判卷的模型 | `gpt-4o-mini` |

常见配置示例:

```bash
# 智谱 GLM(免费额度大,推荐试跑)
export OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4
export SMILEX_BENCH_ANSWER_MODEL=glm-4.5-flash
export SMILEX_BENCH_JUDGE_MODEL=glm-4.5-flash

# DeepSeek
export OPENAI_BASE_URL=https://api.deepseek.com/v1
export SMILEX_BENCH_ANSWER_MODEL=deepseek-chat
export SMILEX_BENCH_JUDGE_MODEL=deepseek-chat

# OpenAI 官方(无需 BASE_URL)
export SMILEX_BENCH_ANSWER_MODEL=gpt-4o-mini
```

也可以单行内联,不污染 shell 环境:

```bash
OPENAI_API_KEY=你的key \
OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4 \
SMILEX_BENCH_ANSWER_MODEL=glm-4.5-flash \
SMILEX_BENCH_JUDGE_MODEL=glm-4.5-flash \
uv run python benchmarks/longmemeval/run_benchmark.py --limit 10
```

### 其他运行参数

```bash
--embedder sentence-transformers  # 检索后端(默认 BGE-M3;--embedder hash 对比零依赖配置)
--token-budget 4000               # recall 注入的上下文 token 预算
--top-k 10                        # 检索条数
--dump-context                    # 调试: 打印每题实际检索到的上下文
--config longmemeval_s            # 数据集配置
```

- 默认 BGE-M3 语义检索(`--embedder hash` 可对比零依赖配置)
- 每样本独立临时 db,全程走 `MemoryMiddleware` 公开 API(write / recall)
- 结果存 `benchmarks/longmemeval/results/`(JSON 明细 + Markdown 摘要)
- 无 API key 时可用 `uv run python benchmarks/longmemeval/_smoke_stub.py` 冒烟验证链路

## 记忆质量 Benchmark(LoCoMo)

对标 mem0 / Zep 的记忆评测,使用
[LoCoMo](https://github.com/snap-research/locomo)(locomo10,10 个长对话 /
1986 题)评估超长多 session 对话的记忆问答质量。与 LongMemEval 共用同一套
作答 / 判卷 / LLM 客户端约定(环境变量完全一致,见上文模型配置表)。

```bash
# 1. 安装依赖(与 LongMemEval 相同)
uv sync --extra embedding --extra benchmark

# 2. 下载数据集(~2.7MB,缓存到 benchmarks/locomo/data/)
uv run python benchmarks/locomo/download_dataset.py

# 3. 配置 LLM(同 LongMemEval: OPENAI_API_KEY 等)

# 4. 运行(先小规模试跑)
uv run python benchmarks/locomo/run_benchmark.py --limit 1 --max-qa 5
uv run python benchmarks/locomo/run_benchmark.py --resume   # 全量 10 对话 1986 题
```

与 LongMemEval 的差异:

- **数据形态**:每个样本是单人双 speaker 超长对话(19~32 个 session /
  419~689 轮),question 挂在对话上;同一对话的所有题共享一次灌入
  (checkpoint 键 `conv_idx + q_idx`),全量灌入仅 10 次
- **题型**:single-hop / multi-hop / temporal / open-domain / adversarial,
  按题型分组报告
- **adversarial 判分**:该题型语义为"对话中不可回答",gold 视作
  "I don't know.",模型拒答才得分(与 LongMemEval abstention 语义统一)
- 结果存 `benchmarks/locomo/results/`(JSON 明细 + Markdown 摘要)

## 检索基线对比(裸 BGE-M3 vs 记忆管线)

量化记忆算法相对 naive RAG 的检索增益:同一份数据、同一种切块,对比两条通道的
R@10 —— `raw`(切块 → BGE-M3 向量 → 纯余弦 KNN,即"直接用 embedding 模型做
向量库检索"的基线)与 `smilex`(向量 KNN + BM25/FTS5 关键词通道 RRF 融合)。
不需要 LLM API key,纯本地检索评测。

```bash
uv sync --extra embedding   # 只需 embedding,无需 LLM
uv run python benchmarks/retrieval_baseline/run_baseline.py --dataset locomo --limit 1
uv run python benchmarks/retrieval_baseline/run_baseline.py --dataset longmemeval --limit 10
```

三个口径:

| 通道 | 含义 |
|---|---|
| `raw_bge_m3` | 裸 BGE-M3 KNN,严格 top-10 |
| `smilex_recall` | 融合排名前 10 个 source(与 raw 同口径) |
| `smilex_recall_all` | recall 实际返回的全部 source(双通道去重后 ~2×10) |

LoCoMo conv-26(152 题)实测:

```
raw_bge_m3      Overall 130/150 = 86.7 %
smilex_recall   Overall 145/150 = 96.7 %   (top-10 同口径,+10.0pt)
smilex_recall_all Overall 147/150 = 98.0 %
```

(分题型 raw→smilex top-10:multi_hop 78.1→90.6,temporal 91.9→100.0,
open_domain 81.8→90.9,single_hop 88.6→98.6;category 映射 1=multi_hop 2=temporal
3=open_domain 4=single_hop,以 mem0 实现为准)

命中判定:LongMemEval 按 gold `answer_session_ids`(检索结果含任一 gold
session 的块即命中),LoCoMo 按 gold `evidence`(dia_id 粒度;adversarial
题语义为"不可回答",不计入)。结果存
`benchmarks/retrieval_baseline/results/`。

注意:macOS MPS 连跑多个 LongMemEval 样本时可能触发 Metal 命令挂起
(卡在 Loading weights 之后),可用 `--device cpu` 绕过(慢但稳)。

## mem0 兼容口径 benchmark(`benchmarks/mem0_compat/`)

为使分数可直接与 mem0 公布的 LoCoMo 92.5 / LongMemEval 94.4 对比,
按 [mem0ai/memory-benchmarks](https://github.com/mem0ai/memory-benchmarks)
的评测协议逐项对齐实现了一套独立 runner。与自家严口径 benchmark 的根本
差异是**判分哲学**:mem0 禁止拒答 + 判卷宽松(部分正确即对、语义改写即对、
日期 ±14 天容忍);自家口径奖励拒答 + 严格语义等价。两套并存,报告时注明口径。

```bash
export OPENAI_API_KEY=...  # 同 longmemeval 约定(默认 GLM 环境变量体系)

# LoCoMo mem0 口径(categories 1-4 全量,adversarial 不计分)
uv run python benchmarks/mem0_compat/locomo.py --conversations 0 --max-qa 2  # 冒烟
uv run python benchmarks/mem0_compat/locomo.py --resume                      # 全量

# LongMemEval mem0 口径(默认每题型 5 题分层采样 seed=42,共 30 题)
uv run python benchmarks/mem0_compat/longmemeval.py --per-type 1   # 冒烟 6 题
uv run python benchmarks/mem0_compat/longmemeval.py                # mem0 默认 30 题
uv run python benchmarks/mem0_compat/longmemeval.py --all-questions --source cleaned
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
| 记忆形成 | LLM 抽取事实卡片 | 原文分层存储(BGE-M3+FTS5,被测系统本身) |

### LongMemEval 实测(mem0 口径,2026-09-01)

`--per-type 5 --seed 42`(30 题,mem0 默认采样),answerer/judge = glm-5.3-flash:

| cutoff | 10 | 20 | 50 | **200(headline)** |
|---|---|---|---|---|
| Overall | 90.0% | 96.7% | 86.7% | **93.3%** |

mem0 官方(gpt-5 作答+判卷)headline 94.4%。本项目以 flash 级模型达到 93.3%,
分题型仅 multi-session/knowledge-update 各 1 题未对。已知误差源:
LongMemEval original 版部分题目 gold 标注歧义(如 multi-session 计数题把
"从亲友处取回衣物"计入"店内取/退"),可用 `--source cleaned` 对照。

**开启写入时事实抽取后(2026-09-02,同采样同模型,`--fact-extraction`)**:
四个 cutoff 全部 **96.7%**(29/30,唯一失败为上述标注歧义题),且各 cutoff
完全一致——原子事实让 10 条记忆的小上下文即可装下答案要点,不再依赖大
cutoff 凑证据、也不受长 prompt 干扰。判卷经 rejudge.py 离线修正(judge
思考文本截断曾误记 0 分)。此成绩超过 mem0 官方 94.4(gpt-5)。

### LoCoMo 实测(mem0 口径,2026-09-01)

对话 0-2 各取前 30 题(共 90 题,answerer/judge = glm-5.3-flash):

| cutoff | 10 | 20 | 50 | **200(headline)** |
|---|---|---|---|---|
| Overall | 97.8% | 97.8% | 98.9% | **98.9%** |

分题型(headline):multi-hop 36/36、temporal 45/45、single-hop 2/2,
仅 open-domain 5/7(85.7%)。mem0 官方(gpt-5)92.5%。
注意:此为每对话前 30 题的采样(题型分布偏 temporal/multi-hop,
single-hop 仅 2 题),与 mem0 全量(~1540 题)口径有差异,仅供参考;
全量可 `uv run python benchmarks/mem0_compat/locomo.py --resume` 续跑。

### 检索与记忆形成优化(2026-09-02)

两项可选增强,均默认关闭(零依赖行为不变),经 benchmark 定位弱点后引入:

**Cross-Encoder 精排**(`[rerank]` extra,bge-reranker-v2-m3)
- 位置: 多路召回 RRF 融合之后、token 裁剪之前,对 L1/L2 候选按层精排
  (L0 工作记忆不动);每层上限 50 条候选保护 recall 延迟预算
- 接入: `MemoryMiddleware(reranker=get_reranker(RerankerConfig(backend="cross-encoder")))`
  或 config.toml `reranker = "cross-encoder"`
- 对比验证: `uv run python benchmarks/retrieval_baseline/run_baseline.py --dataset locomo --rerank --skip-raw`
- 实测(LoCoMo 全量 10 对话 1536 题,2026-09-02):
  R@10 无精排 95.3% → 精排 **97.4%**(+2.1pp,四题型全涨:
  multi_hop 94.7→96.5 / open_domain 79.3→83.7 / single_hop 97.7→99.0 /
  temporal 94.1→97.8);全返回口径不变(98.5%,精排只改排序不改召回集)。
  代价: 本机(MPS)约 4.4s/查询(50 候选),超出 recall P99 预算——
  生产低延迟场景可减小 max_candidates 或换 reranker-base

**写入时 LLM 事实抽取**(`[llm]` extra,OpenAI 兼容 API,core 保持零 LLM)
- `FactExtractor` Protocol,默认 `PassThroughExtractor`(原 content 整块写入);
  `LLMFactExtractor` 把长块抽取为原子事实(一句一事、保留日期/实体/待办),
  每条事实独立入库并可检索——对齐 mem0 的事实卡记忆形成方式,
  是计数/跨 session 整合类问题的关键补强
- 失败语义: 未配置 key / 调用失败一律降级回原文整块,write 永不因抽取崩
- 接入: 构造 `LLMFactExtractor()`(env `SMILEX_EXTRACT_API_KEY/BASE_URL/MODEL`)
  或 config.toml `fact_extractor = "llm"`
- 对比验证: `uv run python benchmarks/mem0_compat/longmemeval.py --per-type 1 --fact-extraction`

**顺带修复**: `WriteRequest.time_range` 现在会锚定到晋升后的
`temporal_fragments.time_start`(此前用墙钟 created_at,历史对话时间丢失,
benchmark 曾被迫按文本匹配回填)。

其他说明:

- LongMemEval 数据源 `--source original`(默认,与历史连续)/ `cleaned`
  (mem0 用的 `longmemeval-cleaned` 社区修正版,自动下载)
- LoCoMo category 映射以 mem0 实现为准(与论文编号不同):
  1=multi-hop 2=temporal 3=open-domain 4=single-hop 5=adversarial
- checkpoint 支持 `--resume`(LoCoMo 按 conv+qidx,LME 按 question_id)
- 结果存 `benchmarks/mem0_compat/results/`

## 设计文档

- [架构整合](docs/design/agent-memory-design.md) — 主架构 spec/contract
- [模块总览](docs/design/modules/00-overview.md) — 13 个模块文档导航
- [Embedding 选型](docs/analyse/embedding-layer.md) — 技术栈选型论证

## 阶段路线图

| 阶段 | 时长 | 范围 |
|------|------|------|
| MVP | 2 周 | 主流程闭环(创建→写入→检索) |
| P0 | 2 周 | 任务调度 + 抢占 |
| P1 | 3 周 | 并发 + 批量导入 + 种子注入 |
| P2 | 2 周 | 知识质量 + 跨项目克隆 |
