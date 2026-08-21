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
└── utils/
```

## 设计文档

- [架构整合](docs/design/agent-memory-design.md) — 主架构 spec/contract
- [模块总览](docs/design/modules/00-overview.md) — 12 个模块文档导航
- [Embedding 选型](docs/analyse/embedding-layer.md) — 技术栈选型论证

## 阶段路线图

| 阶段 | 时长 | 范围 |
|------|------|------|
| MVP | 2 周 | 主流程闭环(创建→写入→检索) |
| P0 | 2 周 | 任务调度 + 抢占 |
| P1 | 3 周 | 并发 + 批量导入 + 种子注入 |
| P2 | 2 周 | 知识质量 + 跨项目克隆 |
