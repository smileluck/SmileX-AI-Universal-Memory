# 模块选型与算法优化总览

> **版本**：v1.0
> **更新日期**：2026-06-21
> **配套文档**：[agent-memory-design.md](../agent-memory-design.md)（架构整合）
> **目的**：每个模块的**选型考虑**、**算法优化**、**实施建议**，帮助深入掌握项目细节

---

## 如何使用本文档系列

```
docs/design/
├── agent-memory-design.md         # 架构级整合（先读）
└── modules/                       # 模块级深化（按需读）
    ├── 00-overview.md             # 本文档（总览 + 阅读指南）
    ├── 01-layer0-models.md        # 数据模型：dataclass/UUID/序列化
    ├── 02-layer1-storage.md       # 存储引擎：SQLite/向量/空间
    ├── 03-layer2-lifecycle.md     # 生命周期：缓存/Token Budget/晋升
    ├── 04-layer3-scheduling.md    # 调度+冷启动：触发器/Bootstrap
    ├── 05-layer4-concurrency.md   # 并发控制：锁/冲突/CAS
    ├── 06-layer5-quality.md       # 知识质量：矛盾/提升/归档
    ├── 07-graph-noise-control.md  # 图谱噪声与存储控制（横切关注点）
    ├── 08-graph-category-research.md # 业界图谱品类调研与差距分析
    ├── 09-key-migration-designs.md # 三大可迁移思想深度设计
    ├── 10-personal-growth.md      # 个人学习成长模块（跨项目能力追踪）
    ├── 11-project-export.md       # ★项目记忆导出与共享（Git 友好）
    ├── 12-mvp-execution-plan.md   # ★MVP 执行计划（WBS + 周交付 + 验收）
    ├── 13-server-layer.md         # ★Server 层：MCP 服务 / Web 面板 / 项目注入
    └── EXECUTION_PLAN_GAPS.md     # ★文档缺口评估（决策 D1-D4）
```

## 每个模块文档的统一结构

1. **模块概览**：职责回顾、依赖关系、核心组件清单
2. **技术选型分析**：3-5 个候选方案对比 + 推荐方案 + 选型理由 + 权衡与回退
3. **核心算法与优化**：算法描述 + 复杂度分析 + 优化技巧 + 性能对比
4. **实施建议**：MVP 范围、关键风险、验证方式、与其他模块的接口

## 全局选型矩阵（速查）

| 维度 | 选型 | 关键理由 |
|------|------|---------|
| 主库 | **SQLite 3.40+** | 嵌入式零安装、单文件、WAL 并发 |
| 向量 | **sqlite-vec + ChromaDB** | 嵌入式双轨、HNSW 性能 |
| 空间 | **R-tree**（SQLite 内置）| 原生支持、零扩展 |
| 缓存 | **cachebox** | Cython 加速、~100万 ops/s |
| 数据模型 | **dataclass + Pydantic v2** | 前者内部、后者边界验证 |
| ID 生成 | **ULID**（Python `python-ulid`）| 时间有序、字符串友好 |
| 异步 | **asyncio + aiosqlite** | 单线程并发、零依赖 |
| 调度 | **自研轻量调度器** | 避免 Celery 重依赖 |
| Token 计数 | **tiktoken**（OpenAI 系）+ 本地 fallback | 精确 + 离线兜底 |
| Embedding | **BGE-M3 / E5**（本地）| 免费、多语言、高质量 |
| 序列化 | **MessagePack**（热路径）+ JSON（边界）| 二进制紧凑、跨语言 |
| LLM 提取 | **Function Calling** | 结构化输出、低幻觉 |

## 阶段-模块对应表

| 模块 | MVP 必做 | P1 增强 | P2 优化 |
|------|---------|---------|---------|
| Layer 0 数据模型 | ✅ 完整 dataclass + 枚举 | — | — |
| Layer 1 存储引擎 | ✅ SQLite + sqlite-vec + R-tree | — | 归档 + 量化 |
| Layer 2 生命周期 | ✅ L0 cachebox + L1 ChromaDB + ContextBuilder | L2 混合检索 | L3 语义 |
| Layer 3 调度+冷启动 | ✅ 冷启动最小集（向导+模板+主动学习）| 三重触发 + 抢占 | 跨项目克隆 |
| Layer 4 并发控制 | — | ✅ 锁 + 冲突检测 | 死锁预测 |
| Layer 5 知识质量 | — | ✅ 矛盾检测 | 提升 + 归档 |

## 阅读路径建议

### 路径 A：架构师视角（自顶向下）
1. [agent-memory-design.md](../agent-memory-design.md) §1-§4（架构与原则）
2. 本文档（速查选型）
3. [01-layer0-models.md](01-layer0-models.md) → [02-layer1-storage.md](02-layer1-storage.md)（基础层）
4. [03-layer2-lifecycle.md](03-layer2-lifecycle.md)（核心机制）
5. [04-layer3-scheduling.md](04-layer3-scheduling.md) → [05-layer4-concurrency.md](05-layer4-concurrency.md) → [06-layer5-quality.md](06-layer5-quality.md)（增强层）

### 路径 B：工程师视角（按 MVP 优先级）
1. 本文档（速查）
2. [01-layer0-models.md](01-layer0-models.md)（先写数据模型）
3. [02-layer1-storage.md](02-layer1-storage.md)（搭存储）
4. [03-layer2-lifecycle.md](03-layer2-lifecycle.md)（接生命周期）
5. [04-layer3-scheduling.md](04-layer3-scheduling.md)（冷启动）
6. P1/P2 模块按需读

### 路径 C：性能优化视角
1. 本文档
2. [02-layer1-storage.md](02-layer1-storage.md) §3（存储优化）
3. [03-layer2-lifecycle.md](03-layer2-lifecycle.md) §3（Token Budget 优化）
4. [05-layer4-concurrency.md](05-layer4-concurrency.md) §3（并发优化）

## 通用选型原则

```
┌──────────────────────────────────────────────────────────┐
│  1. 嵌入式优先：能 pip install 就不引入外部服务           │
│                                                          │
│  2. 成熟度优先：能用 5 年+ 项目就不碰 alpha 版本           │
│                                                          │
│  3. 性能可量化：每个选型都有 benchmark 数据支撑            │
│                                                          │
│  4. 可回退：所有关键选型都通过接口抽象，便于替换           │
│                                                          │
│  5. 渐进式：MVP 用最简单方案，后续按需升级                 │
│                                                          │
│  6. 避免过度设计：YAGNI（You Aren't Gonna Need It）       │
└──────────────────────────────────────────────────────────┘
```
