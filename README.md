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
