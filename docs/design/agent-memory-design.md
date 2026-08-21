# SmileX Agent Memory System — 整合设计文档

> **版本**：v1.0（全新整合版）
> **更新日期**：2026-06-21
> **整合来源**：`docs/analyse/` 下 11 份分析文档（约 13000+ 行）
> **目标读者**：实施工程师、架构 review 人员
> **核心理念**：「不完美的记忆 > 没有记忆」

---

## §0 文档契约与阅读指南

### 0.1 本文档是什么

一份**面向工程师的实施蓝图**（spec/contract），不是研究笔记。它整合了 11 份分析文档的精华，去除了冗余与重复，明确了：

- **做什么**：分层架构 + 模块边界 + 公共 API 契约
- **为什么**：14 个 ADR 决策记录（含权衡与回退方案）
- **怎么做**：6 大关键算法伪代码 + 工程路线图
- **何时做**：MVP → P0 → P1 → P2 四阶段拆解，每阶段含退出标准

### 0.2 阶段标签图例

| 标签 | 含义 | 工程师行动 |
|------|------|-----------|
| `[MVP]` | 最小可用产品必做 | 优先实现，2 周内交付 |
| `[P1]` | 第一阶段增强 | MVP 后跟进 |
| `[P2]` | 第二阶段优化 | 视需求推动 |

### 0.3 引用规则

| 内容 | 处理方式 |
|------|---------|
| 枚举、关键 dataclass 签名、SQL 函数签名、算法伪代码 | **内联到本文档** |
| 完整 SQL DDL、完整类实现、配置 YAML | **链接到 `src/` 对应文件** |
| 11 份分析文档的详细论述 | **见 §20 交叉引用表** |

### 0.4 核心技术栈速览

| 组件 | 选型 | 备注 |
|------|------|------|
| 主库 | **SQLite 3.40+**（Python 自带）| 嵌入式零安装 |
| 向量索引 | **sqlite-vec**（HNSW）+ ChromaDB | 双轨制 |
| 空间索引 | **R-tree 虚拟表** | SQLite 内置 |
| 进程内缓存 | **cachebox**（Cython 加速）| 替代 Redis |
| Python 版本 | ≥3.13 | — |
| 部署模式 | 单机/嵌入式 | `pip install` 即用 |

---

## §1 问题陈述与设计原则

### 1.1 问题陈述

构建一个面向 AI Agent 的**高级记忆系统**，解决以下核心问题：

1. **碎片化输入**：用户/Agent 提供的信息不完整（时间未知、地点模糊、内容残缺）
2. **时空图谱因果一体化**：传统方案需要 4 个独立数据库（TimescaleDB + PostGIS + Neo4j + 向量库），运维复杂
3. **项目隔离与跨项目共享**：项目专属记忆要隔离，通用架构经验要复用
4. **冷启动体验差**：新项目零历史，首次对话召回率低
5. **嵌入式分发难**：现有方案多依赖 PostgreSQL/Redis，个人电脑安装门槛高

### 1.2 五条核心原则

```
┌─────────────────────────────────────────────────────────────┐
│  1. 不完美的记忆 > 没有记忆                                    │
│     接受碎片化、不确定输入，渐进式补全                          │
│                                                              │
│  2. 单库一体化 > 多库拼装                                      │
│     SQLite + 扩展替代 TimescaleDB + PostGIS + Neo4j            │
│                                                              │
│  3. 嵌入式优先 > 服务端优先                                    │
│     pip install 即用，零外部服务依赖                            │
│                                                              │
│  4. 项目隔离 + 跨项目共享                                      │
│     scope 字段 + 自动提升机制                                  │
│                                                              │
│  5. 强契约 + 弱耦合                                            │
│     6 层架构，禁止反向依赖，并行开发无冲突                       │
└─────────────────────────────────────────────────────────────┘
```

### 1.3 系统能力矩阵

| 能力维度 | 实现方式 | 关键组件 | 阶段 |
|---------|---------|---------|------|
| 时序记忆 | SQLite `valid_from/valid_to` + B-tree | `query_at_time()` | MVP |
| 空间记忆 | R-tree 虚拟表 | `query_in_area()` | MVP |
| 图谱记忆 | `WITH RECURSIVE` CTE | `find_path()` | MVP |
| 因果记忆 | `predecessor_id` 链 | `trace_causal_chain()` | P1 |
| 语义记忆 | sqlite-vec + ChromaDB | `hybrid_memory_search()` | MVP |
| 模糊记忆 | `CertaintyLevel` + `TimeRange` | `FuzzyRetrieval` | P2 |
| 任务调度 | 三重触发 + 优先级队列 | `MemoryTaskScheduler` | P1 |
| 抢占恢复 | Checkpoint + GRACEFUL | `PreemptiveScheduler` | P1 |
| 并发控制 | 锁 + 冲突检测 + 解决 | `LockManager` | P1 |
| 知识质量 | 双向指针 + 矛盾检测 + 跨项目提升 | `ScopePromoter` | P2 |
| 项目冷启动 | 六机制组合 | `ProjectBootstrap` | MVP→P2 |

---

## §2 系统上下文

### 2.1 系统在 Agent 中的位置

```
┌────────────────────────────────────────────────────────────┐
│                  外部 Agent（Claude/Codex/自定义）              │
└────────────────────────────┬───────────────────────────────┘
                             │ HTTP API / Python SDK / MCP（待定）
                             ▼
┌────────────────────────────────────────────────────────────┐
│                   SmileX Memory Service                     │
│  ┌────────────────────────────────────────────────────────┐│
│  │            Memory Middleware（唯一入口）                ││
│  │   write(event)  recall(query)  infer(question)  trace  ││
│  └────────────────────────┬───────────────────────────────┘│
│                           ▼                                  │
│  ┌────────────────────────────────────────────────────────┐│
│  │              Memory Module（6 层架构）                  ││
│  │  L5: 知识质量  │  L4: 并发控制  │  L3: 调度+冷启动        ││
│  │  L2: 生命周期  │  L1: 存储引擎  │  L0: 数据模型           ││
│  └────────────────────────────────────────────────────────┘│
│                           ▼                                  │
│  ┌────────────────────────────────────────────────────────┐│
│  │       SQLite + ChromaDB + cachebox（全嵌入式）           ││
│  └────────────────────────────────────────────────────────┘│
└────────────────────────────────────────────────────────────┘
                             │
                             ▼
                    ~/.smilex/data.db
                    ~/.smilex/vectors/
```

### 2.2 核心数据流

```
外部 Agent 请求
       │
       ▼
Memory Middleware（CRUD + 检索）
       │
       ├─→ [写入] Layer 4 并发控制 → Layer 1 SQLite → Layer 5 矛盾检测
       │
       └─→ [检索] Layer 2 生命周期 → Layer 1 混合检索 → Token Budget 裁剪
                                                          │
                                                          ▼
                                                   返回上下文 < 4000 tokens
```

### 2.3 与现有分析文档的关系

本文档**替代** `memory-module-complete-design.md`（v3.0 整合版）作为工程师的 main reference。原 v3.0 与 8 份子设计文档作为**详细参考**保留在 `docs/analyse/`，通过 §20 交叉引用表关联。

---

## §3 公共 API 契约 [MVP]

### 3.1 MemoryMiddleware 接口

`MemoryMiddleware` 是 Agent 访问记忆系统的**唯一入口**。所有外部调用必须经过此层。

```python
class MemoryMiddleware:
    """Agent 访问记忆系统的唯一入口"""

    async def initialize_project(
        self, request: ProjectInitRequest
    ) -> ProjectInitResponse:
        """新项目冷启动初始化（见 §9.5）"""

    async def write(self, event: WriteRequest) -> WriteResponse:
        """写入记忆事件（实体/三元组/片段）"""

    async def recall(self, query: RecallRequest) -> RecallResponse:
        """检索记忆（混合检索 + Token 预算裁剪）"""

    async def infer(self, question: InferRequest) -> InferResponse:
        """基于记忆推理（图谱遍历 + 因果追溯）"""

    async def trace(self, request: TraceRequest) -> TraceResponse:
        """追溯记忆来源（双向指针）"""

    async def promote_to_global(
        self, memory_id: str, scope: MemoryScope
    ) -> PromoteResponse:
        """手动提升记忆作用域（见 §11）"""
```

### 3.2 核心 DTO 定义

```python
@dataclass
class WriteRequest:
    scope: MemoryScope                  # project/tenant/global
    content: str                        # 记忆内容
    entities: List[str] = field(default_factory=list)
    relations: List[TripleInput] = field(default_factory=list)
    time_range: Optional[TimeRange] = None
    location: Optional[FuzzyLocation] = None
    certainty: CertaintyLevel = CertaintyLevel.MEDIUM
    importance: float = 0.5

@dataclass
class RecallRequest:
    query: str                          # 自然语言查询
    scope_filter: ScopeFilter           # 作用域过滤
    time_range: Optional[TimeRange] = None
    location: Optional[FuzzyLocation] = None
    entity_filter: Optional[str] = None
    top_k: int = 10
    token_budget: int = 4000

@dataclass
class RecallResponse:
    context: str                        # 压缩到预算内的上下文
    sources: List[MemoryRef]            # 引用的记忆 ID 列表
    layers_used: List[MemoryLayer]      # 命中的层级
    token_count: int                    # 实际 token 数

@dataclass
class ProjectInitRequest:
    name: str
    description: str
    tech_stack: List[str]
    readme_content: Optional[str] = None
    key_files: Dict[str, str] = field(default_factory=dict)
    template: Optional[str] = None      # web/cli/data/agent
```

### 3.3 错误码

```python
class ErrorCode(Enum):
    # 写入相关
    WRITE_CONFLICT = "write_conflict"           # 并发冲突
    WRITE_VALIDATION_FAILED = "validation_failed"
    SCOPE_VIOLATION = "scope_violation"          # 跨作用域写入被拒
    
    # 检索相关
    RECALL_EMPTY = "recall_empty"
    RECALL_TIMEOUT = "recall_timeout"
    
    # 项目相关
    PROJECT_EXISTS = "project_exists"
    PROJECT_NOT_FOUND = "project_not_found"
    BOOTSTRAP_INCOMPLETE = "bootstrap_incomplete"
    
    # 存储相关
    STORAGE_FULL = "storage_full"
    CORRUPTION_DETECTED = "corruption_detected"
    
    # 通用
    INTERNAL_ERROR = "internal_error"
    INVALID_ARGUMENT = "invalid_argument"
```

### 3.4 SLA 契约

| API | MVP P99 延迟 | P1 P99 延迟 | 备注 |
|-----|-------------|-------------|------|
| `write` | < 50ms | < 20ms | 异步索引构建 |
| `recall` | < 500ms | < 200ms | 含 Token Budget 裁剪 |
| `infer` | < 1s | < 500ms | 图谱遍历 + 因果追溯 |
| `trace` | < 100ms | < 50ms | 单跳双向指针 |
| `initialize_project` | < 30s | < 10s | 不含批量导入 |

---

## §4 分层架构总览

### 4.1 六层架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                    Memory Middleware（对外入口）                   │
└────────────────────────────┬────────────────────────────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
┌───────────────┐  ┌───────────────────┐  ┌───────────────┐
│  Layer 5 [P2] │  │   Layer 3 [P1]    │  │  Layer 2 [MVP]│
│  知识质量      │  │  调度+抢占+冷启动   │  │   生命周期     │
│  Traceability │◄─│  Scheduler        │◄─│  L0/L1/L2/L3  │
│  Promoter     │  │  Bootstrap        │  │  ContextBuild │
│  Archival     │  │  Preemptor        │  │               │
└───────┬───────┘  └─────────┬─────────┘  └───────┬───────┘
        │                    │                    │
        │           ┌────────┴────────┐           │
        │           ▼                 ▼           │
        │  ┌──────────────┐  ┌──────────────┐    │
        │  │  Layer 4 [P1]│  │              │    │
        │  │  并发控制     │◄─┤              │    │
        │  │  LockManager │  │              │    │
        │  │  ConflictDet │  │              │    │
        │  └──────┬───────┘  │              │    │
        │         │          │              │    │
        ▼         ▼          ▼              ▼    ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Layer 1 [MVP]: 存储引擎                       │
│          SQLite + sqlite-vec + ChromaDB + R-tree                │
└────────────────────────────┬────────────────────────────────────┘
                             │
                ┌────────────┴────────────┐
                ▼                         ▼
      ┌──────────────────┐      ┌──────────────────┐
      │ Layer 0 [MVP]    │      │                  │
      │ 数据模型          │      │   纯数据契约       │
      │ FuzzyMemory      │      │   无 I/O          │
      │ Entity/Triple    │      │                  │
      │ Scope            │      │                  │
      └──────────────────┘      └──────────────────┘
```

### 4.2 各层职责一句话概括

| Layer | 职责 | 阶段 |
|-------|------|------|
| 0 数据模型 | 定义所有核心数据结构与枚举契约 | MVP |
| 1 存储引擎 | SQLite 一体化存储 + 五维检索 | MVP |
| 2 生命周期 | L0-L3 四层记忆管理 + Token Budget | MVP→P1 |
| 3 调度+冷启动 | 三重触发任务调度 + 项目冷启动 | P1（冷启动 MVP） |
| 4 并发控制 | 锁 + 冲突检测 + 解决三层防护 | P1 |
| 5 知识质量 | 双向指针 + 矛盾检测 + 跨项目提升 + 归档 | P2 |

### 4.3 强制约束（依赖方向）

```
✅ 允许的依赖方向：
  Layer N → Layer N-1（向下依赖）
  Layer 5 → Layer 3（回调，提交修复任务）

❌ 禁止的依赖：
  反向依赖（Layer 1 → Layer 3）
  跨层跳级（Layer 5 → Layer 1，必须经 Layer 3/4）
  Layer 0 依赖任何上层（纯数据契约）
```

**约束目的**：保证 6 层可以**并行开发**，模块边界清晰，合并冲突最小化。

---

## §5 数据模型层（Layer 0）[MVP]

### 5.1 核心枚举

```python
class CertaintyLevel(Enum):
    """确定性级别 — 所有模糊数据的基础"""
    EXACT = "exact"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"

class MemoryLayer(Enum):
    """记忆层级"""
    L0_WORKING = "L0"
    L1_SHORT = "L1"
    L2_LONG = "L2"
    L3_SEMANTIC = "L3"

class MemoryScope(Enum):
    """记忆作用域 — 解决项目隔离与跨项目共享"""
    PROJECT = "project"   # 项目专属
    TENANT = "tenant"     # 租户专属
    GLOBAL = "global"     # 全局通用

class LockType(Enum):
    SHARED = "shared"
    EXCLUSIVE = "exclusive"
    UPDATE = "update"

class ConflictType(Enum):
    VERSION_STALE = "version_stale"
    WRITE_WRITE = "write_write"
    READ_WRITE = "read_write"
    CAUSAL_CYCLE = "causal_cycle"
    CAUSAL_CONTRADICTION = "causal_contradiction"

class PreemptionPolicy(Enum):
    NONE = "none"
    IMMEDIATE = "immediate"
    GRACEFUL = "graceful"               # 默认
    COOPERATIVE = "cooperative"
    PRIORITY_INHERITANCE = "priority_inheritance"
```

### 5.2 核心 Dataclass 签名

> 仅展示字段签名，完整实现见 `src/memory/models/`
>
> **存储备注（决策 D6）**：存储层 `scope` 列存**全路径**字符串（`"global"` / `"project:<id>"` / `"tenant:<id>"`)，而非 `MemoryScope` 枚举的简单字符串；Layer 0 ↔ Layer 1 之间由 StorageEngine 适配器互转，Layer 0 的 `MemoryScope` 枚举保持不变（Python API 简洁）。

```python
@dataclass
class UncertainValue:
    value: Any
    certainty: CertaintyLevel = CertaintyLevel.MEDIUM
    note: Optional[str] = None

@dataclass
class TimeRange:
    """三种模式：精确 / 近似 / 相对"""
    exact: Optional[datetime] = None
    approx_start: Optional[datetime] = None
    approx_end: Optional[datetime] = None
    relative: Optional[str] = None       # "昨天下午"、"刚才"
    certainty: CertaintyLevel = CertaintyLevel.MEDIUM

@dataclass
class FuzzyLocation:
    """三种模式：精确坐标 / 层级 / 区域"""
    exact_coords: Optional[tuple[float, float]] = None
    hierarchy: List[str] = field(default_factory=list)  # ["家","客厅"]
    region: Optional[str] = None
    location_id: Optional[str] = None
    certainty: CertaintyLevel = CertaintyLevel.MEDIUM

@dataclass
class FuzzyMemory:
    """核心记忆单元 — 所有字段可选，强制渐进式存储"""
    id: str
    content: Optional[str] = None
    content_embedding: Optional[np.ndarray] = None
    time_range: Optional[TimeRange] = None
    location: Optional[FuzzyLocation] = None
    entities: List[str] = field(default_factory=list)
    relations: List[str] = field(default_factory=list)
    scope: MemoryScope = MemoryScope.PROJECT
    importance: float = 0.5
    emotion_weight: float = 0.0
    access_count: int = 0
    layer: MemoryLayer = MemoryLayer.L1_SHORT
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None

@dataclass
class Entity:
    """实体节点"""
    id: str
    entity_id: str                       # 归一化 ID
    entity_type: str                     # person/location/object/concept/event
    name: str
    scope: MemoryScope
    valid_from: datetime
    valid_to: Optional[datetime] = None
    location: Optional[FuzzyLocation] = None
    embedding: Optional[np.ndarray] = None
    source_closet: Optional[str] = None  # 双向指针

@dataclass
class Triple:
    """三元组边 + 因果链"""
    id: str
    triple_id: str
    subject_id: str
    predicate: str
    object_id: Optional[str] = None
    object_value: Optional[str] = None
    scope: MemoryScope
    valid_from: datetime
    valid_to: Optional[datetime] = None
    predecessor_id: Optional[str] = None # 因果链前驱
    causal_level: int = 0
    confidence: float = 1.0
    certainty: CertaintyLevel = CertaintyLevel.EXACT
    relation_type: str = "semantic"      # causal/spatial/temporal/semantic
    source_closet: Optional[str] = None

@dataclass
class ScopeFilter:
    """查询时的作用域过滤"""
    include_project: Optional[str] = None
    include_tenant: Optional[str] = None
    include_global: bool = True          # 默认召回通用经验
    include_all_projects: bool = False   # 跨项目复盘场景
```

### 5.3 典型查询场景

| 场景 | ScopeFilter | 语义 |
|------|-------------|------|
| 项目内对话 | `include_project=当前, include_global=True` | 本项目 + 通用经验 |
| 跨项目复盘 | `include_all_projects=True, include_global=True` | 所有人踩过的坑 |
| 架构决策 | `include_global=True`（其余 False） | 纯架构经验 |
| 租户隔离 | `include_tenant=当前, include_global=True` | 多租户 SaaS |

---

## §6 存储引擎层（Layer 1）[MVP]

### 6.1 七张核心表摘要矩阵

> 完整 DDL 见 `src/memory/storage/schema/*.sql`

| 表名 | 职责 | 关键列 | 索引策略 |
|------|------|-------|---------|
| `entities` | 实体节点 | `id, entity_id, entity_type, name, scope, valid_from, valid_to, embedding, source_closet` | B-tree on `(scope, entity_id)` + B-tree on `valid_from` |
| `triples` | 三元组 + 因果链 | `id, triple_id, subject_id, predicate, object_id, scope, valid_from, valid_to, predecessor_id, confidence, source_closet` | B-tree on `(subject_id, predicate)` + B-tree on `predecessor_id` |
| `locations` | 位置层级 | `id, location_id, name, location_type, parent_id, path, coordinates` | B-tree on `path`（替代 LTREE） |
| `temporal_fragments` | 时序片段 | `id, fragment_id, time_start, time_end, location_id, content, embedding, entities` | B-tree on `time_start` + B-tree on `time_end` |
| `causal_chains` | 因果链 | `id, chain_id, node_ids, chain_type, confidence, support_count` | (JSON1 索引 node_ids) |
| `vector_links` | 向量关联 | `id, vector_db, vector_id, embedding, entity_id, triple_id, fragment_id` | sqlite-vec on `embedding` |
| `checkpoints` | 任务检查点 | `id, task_id, progress, step, state, cursor` | B-tree on `task_id` |
| `project_current_state` ★ | 项目当前状态汇总 | `scope, subject_id, predicate, object_id, object_value, valid_from, confidence` | PRIMARY KEY `(scope, subject_id, predicate)` |
| `memory_l0_snapshot` ★ | L0 工作记忆快照 | `session_id, data, updated_at, expires_at` | PRIMARY KEY `session_id` |
| `triples_archive` ★(P2,schema 011) | 历史三元组归档(冷热分层) | 与 `triples` 同构 + `archived_at` | B-tree on `(scope, valid_from)` + `valid_to` |
| `temporal_fragments_archive` ★(P2,schema 011) | 历史时序片段归档 | 与 `temporal_fragments` 同构 + `archived_at`(embedding 置 NULL,不保留向量) | B-tree on `(scope, time_start)` + `updated_at` |
| `predicate_dict` ★(P2,schema 011) | predicate 字典编码 | `code INTEGER PK, predicate UNIQUE`;`triples.predicate_code` 冗余编码列(读取仍走 TEXT) | UNIQUE on `predicate` |

### 6.2 SQLite 类型映射

| 逻辑类型 | SQLite 物理类型 | 说明 |
|---------|---------------|------|
| UUID | `TEXT` | 36 字符标准格式 |
| TIMESTAMPTZ | `TEXT` | ISO 8601 字符串 |
| JSONB | `TEXT` | JSON1 扩展解析 |
| UUID[] | `TEXT`（JSON 数组）| JSON1 查询 |
| vector(1024) | `FLOAT[1024]`（sqlite-vec）| 虚拟表（维度按决策 D5 统一为 1024，对齐 BGE-M3）|
| GEOMETRY(POINT) | R-tree 虚拟表 | `min_lng, max_lng, min_lat, max_lat` |

### 6.3 项目记忆当前状态机制（SQLite 适配）

SQLite 不支持物化视图，用**触发器维护的汇总表**等效实现：

```sql
CREATE TABLE project_current_state (
    scope        TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    predicate    TEXT NOT NULL,
    object_id    TEXT,
    object_value TEXT,
    valid_from   TEXT NOT NULL,
    confidence   REAL DEFAULT 1.0,
    PRIMARY KEY (scope, subject_id, predicate)
);

CREATE TRIGGER trg_update_current_state
AFTER INSERT ON triples
WHEN NEW.relation_type IN ('project_state', 'task_status', 'config')
   AND NEW.scope LIKE 'project:%'
BEGIN
    UPDATE triples SET valid_to = NEW.valid_from
    WHERE subject_id = NEW.subject_id
      AND predicate = NEW.predicate
      AND scope = NEW.scope
      AND valid_to IS NULL
      AND id != NEW.id;
    
    INSERT INTO project_current_state 
        (scope, subject_id, predicate, object_id, object_value, valid_from, confidence)
    VALUES 
        (NEW.scope, NEW.subject_id, NEW.predicate, NEW.object_id, NEW.object_value, NEW.valid_from, NEW.confidence)
    ON CONFLICT(scope, subject_id, predicate) DO UPDATE SET
        object_id = excluded.object_id,
        object_value = excluded.object_value,
        valid_from = excluded.valid_from,
        confidence = excluded.confidence;
END;
```

### 6.4 向量与空间索引

```sql
-- sqlite-vec 向量虚拟表
CREATE VIRTUAL TABLE memory_vectors USING vec0(
    memory_id TEXT PRIMARY KEY,
    embedding FLOAT[1024]
);

-- 维度说明（决策 D5）：统一为 1024（BGE-M3 维度）。MVP 默认 HashEmbedder
-- （零依赖、确定性输出 1024 维），可选后端 SentenceTransformerEmbedder
-- 经 embedding extra 安装。

-- KNN 检索
SELECT memory_id, distance FROM memory_vectors
WHERE embedding MATCH ? ORDER BY distance LIMIT 10;

-- R-tree 空间虚拟表
CREATE VIRTUAL TABLE memory_locations USING rtree(
    id, min_lng, max_lng, min_lat, max_lat
);
```

### 6.5 检索能力矩阵

| 检索类型 | 实现方式 | 函数签名 | 延迟目标 |
|---------|---------|---------|---------|
| 时间点查询 | `valid_from <= t AND (valid_to IS NULL OR valid_to > t)` | `query_at_time(t) → rows` | < 5ms |
| 时间范围查询 | `valid_from <= end AND (valid_to IS NULL OR valid_to >= start)` | `query_in_range(start, end) → rows` | < 10ms |
| 实体时间线 | `UNION ALL` + 排序 | `build_entity_timeline(entity_id) → rows` | < 20ms |
| 空间半径 | R-tree bbox + 精确过滤 | `query_in_area(lng, lat, radius) → rows` | < 20ms |
| 空间层级 | `path LIKE 'home.%'` | `query_in_location(path) → rows` | < 10ms |
| 图谱路径 | `WITH RECURSIVE` CTE | `find_path(src, dst, max_depth) → path` | < 50ms |
| N 度关系 | 递归 CTE + depth | `find_n_degree_relations(entity, n) → rows` | < 100ms |
| 因果追溯 | `predecessor_id` 递归 | `trace_causal_chain(triple_id) → chain` | < 50ms |
| 因果推理 | 时序共现分析 | `infer_causal_relations(entity) → relations` | < 200ms |
| 当前状态 | `project_current_state` 表 | `get_current_state(subject_id) → row` | < 5ms |
| 混合检索 | RRF 加权融合 | `hybrid_memory_search(query, scope_filter, top_k) → rows` | < 200ms |

### 6.6 对外契约

```python
class StorageEngine:
    async def read(self, filter: QueryFilter) -> List[Row]: ...
    async def write(self, record: Record) -> WriteResult: ...
    async def hybrid_search(
        self, query: str, scope_filter: ScopeFilter, top_k: int = 10
    ) -> List[RankedRow]: ...
    async def get_current_state(self, subject_id: str, scope: str) -> Optional[Row]: ...
    async def query_at_time(self, t: datetime, scope_filter: ScopeFilter) -> List[Row]: ...
    async def find_path(self, src: str, dst: str, max_depth: int = 5) -> List[str]: ...
    async def trace_causal_chain(self, triple_id: str) -> List[Triple]: ...
```

---

## §7 记忆生命周期层（Layer 2）[MVP→P1]

### 7.1 四层记忆架构

```
┌─────────────────────────────────────────────────────────────┐
│                Token Budget 总预算: ~4000                      │
├─────────────────────────────────────────────────────────────┤
│ L0 Working Memory (工作记忆)    ~500 tokens  单轮对话          │
│   实现：cachebox 进程内 + SQLite 快照                          │
│   策略：全量加载                                                │
├─────────────────────────────────────────────────────────────┤
│ L1 Short-term Memory (短时记忆) ~1000 tokens 会话级            │
│   实现：ChromaDB 向量检索                                      │
│   策略：Top-K=5 向量相似度                                      │
├─────────────────────────────────────────────────────────────┤
│ L2 Long-term Memory (长时记忆)  ~2000 tokens 永久              │
│   实现：SQLite 混合检索                                         │
│   策略：Top-K=10 RRF 融合（时序+空间+图谱+语义）                │
├─────────────────────────────────────────────────────────────┤
│ L3 Semantic Memory (语义记忆)   ~500 tokens  永久              │
│   实现：SQLite 图谱遍历                                         │
│   策略：Top-K=3 概念匹配                                        │
└─────────────────────────────────────────────────────────────┘
```

### 7.2 L0 工作记忆缓存架构（核心决策）

```python
from cachebox import LRUCache

class L0WorkingMemory:
    """cachebox 6.x 真实 API（决策 D4）：纯 LRU,不启用 TTL。

    会话过期由应用层主动清理(MemoryMiddleware.close_session →
    clear_session / 进程退出钩子 → clear),持久化由 SQLite 快照负责。
    """

    def __init__(self, sqlite_conn):
        self.hot = LRUCache(maxsize=1000)   # 最多 1000 个活跃会话
        self.db = sqlite_conn

    async def get(self, session_id: str) -> Optional[bytes]:
        data = self.hot.get(session_id)      # 亚微秒
        if data is not None:
            return data
        cursor = await self.db.execute(
            "SELECT data FROM memory_l0_snapshot WHERE session_id=?",
            (session_id,)
        )
        if row := await cursor.fetchone():   # 毫秒
            self.hot.insert(session_id, row[0])
            return row[0]
        return None

    async def set(self, session_id: str, data: bytes):
        self.hot.insert(session_id, data)
        await self._dump_to_sqlite(session_id, data)  # 异步每 5s

    def clear_session(self, session_id: str) -> None:
        """会话结束/项目卸载时由上层主动调用(决策 D4)."""
        self.hot.pop(session_id, None)

    def clear(self) -> None:
        """进程退出钩子:清空所有会话."""
        self.hot.clear()
```

> **cachebox 6.x API 漂移说明**（决策 D4 / EXECUTION_PLAN_GAPS §3.9）:cachebox 不提供
> `TTLPrefixCache`,TTL 与 LRU 不可合并;写入方法是 `insert()` 而非 `set()`。
> 因此 L0 采用纯 `LRUCache(maxsize=1000)`,过期语义交给应用层清理。

### 7.3 记忆流转（晋升规则）

```
L0 工作记忆 ──[溢出]──→ L1 短时记忆 ──[调度器整合任务]──→ L2 长时记忆 ──[模式发现]──→ L3 语义记忆
                          │                              │
                       会话结束                       遗忘任务
                          │                              │
                       [遗忘]                       [修剪/归档]
```

### 7.4 Token Budget 算法（见 §12 算法 1）

```python
TOKEN_BUDGET = {"L0": 500, "L1": 1000, "L2": 2000, "L3": 500}  # 总 4000
```

### 7.5 对外契约

```python
class ContextBuilder:
    async def build_context(
        self, query: str, query_embedding: np.ndarray,
        time_range: TimeRange = None, location: FuzzyLocation = None,
        entity_filter: str = None, scope_filter: ScopeFilter = None,
        token_budget: int = 4000
    ) -> str: ...
```

---

## §8 任务调度层（Layer 3a）[P1]

### 8.1 三重触发机制

| 触发器 | 触发条件 | 示例 |
|--------|---------|------|
| **时间触发** | cron 表达式或固定间隔 | 每日 02:00 整合任务 |
| **事件触发** | 记忆满/新情节/访问模式 | L1 满 80% → 触发整合 |
| **自适应触发** | 记忆压力/空闲/热记忆/模式/因果 | 带 5min-1h 冷却时间 |

### 8.2 任务类型与优先级

| 优先级 | 任务类型 | 典型场景 |
|--------|---------|---------|
| **CRITICAL** | 上下文切换、内存告急、冷启动写入 | 立即执行，跳过队列 |
| **HIGH** | 记忆整合、因果推理、矛盾修复 | 高优先级队列 |
| **MEDIUM** | 情节摘要、语义提取、强化 | 普通队列 |
| **LOW** | 模式发现、垃圾回收、统计更新 | 批量执行 |
| **IDLE** | 索引重建、备份、归档 | 空闲时执行 |

### 8.3 自适应规则（带冷却）

| 规则 | 触发条件 | 动作 | 冷却 |
|------|---------|------|------|
| 记忆压力 | L1 使用率 > 80% | 触发整合任务 | 5 分钟 |
| 空闲整合 | 系统空闲 + 有待整合项 | 执行整合 | 1 分钟 |
| 热记忆刷新 | 高频记忆接近遗忘阈值 | 强化权重 | 30 分钟 |
| 模式发现 | 新情节 ≥ 10 条 | 发现模式 | 1 小时 |
| 因果推理 | 存在潜在因果链接 | 推理关联 | 5 分钟 |

### 8.4 对外契约

```python
class MemoryTaskScheduler:
    async def submit(self, task: Task) -> str: ...
    async def preempt(self, task_id: str, policy: PreemptionPolicy) -> None: ...
    async def resume(self, task_id: str) -> None: ...
    async def cancel(self, task_id: str) -> None: ...
```

---

## §9 抢占与恢复层（Layer 3b）[P1]

### 9.1 抢占策略

| 策略 | 行为 | 适用场景 |
|------|------|---------|
| **IMMEDIATE** | 立即中断，丢弃进度 | 极端紧急 |
| **GRACEFUL** ✅默认 | 等待检查点，超时强制 | 大多数场景 |
| **COOPERATIVE** | 任务主动让步 | 长任务 |
| **PRIORITY_INHERITANCE** | 提升低优先级 | 资源竞争 |

### 9.2 状态机

```
RUNNING ──preempt──→ PAUSING ──checkpoint──→ PAUSED ──resume──→ RESUMING ──→ RUNNING
                         │                       │
                         │ (超时)                │ (无检查点)
                         ▼                       ▼
                      INTERRUPTED             PREEMPTED
```

### 9.3 检查点机制

```python
@dataclass
class Checkpoint:
    task_id: str
    progress: float = 0.0           # 0.0 - 1.0
    step: int = 0
    state: Dict[str, Any]           # 状态快照
    cursor: Any                     # 游标位置（断点续传）
    description: str = ""
```

### 9.4 检查点 schema 引用

完整 DDL 见 `src/memory/storage/schema/checkpoints.sql`。关键字段：`task_id`、`progress`、`step`、`state`（JSON）、`cursor`（JSON）、`expires_at`。

### 9.5 抢占协议（见 §12 算法 5）

`InterruptibleTask` 基类：循环中定期 `check_interruption()`，抛出 `InterruptException` 时自动保存检查点。

---

## §9.5 项目冷启动机制 [MVP→P2]

### 9.5.1 六大机制

针对"新项目如何快速建立工作记忆"的核心设计：

| 机制 | 阶段 | 实现 | 作用 |
|------|------|------|------|
| **初始化向导** | MVP | `bootstrap/onboarding.py` | 从 README/元信息提取首批实体 |
| **项目模板** | MVP | `bootstrap/templates.py` | Web/CLI/Data/Agent 预置骨架 |
| **主动学习** | MVP | `bootstrap/active_learner.py` | Agent 引导式提问补充 |
| **批量导入** | P1 | `bootstrap/bulk_importer.py` | Git/Markdown/Jira/OpenAPI |
| **种子注入** | P1 | `bootstrap/seed_injector.py` | 从 global 召回相关经验 |
| **跨项目克隆** | P2 | `bootstrap/cross_project_cloner.py` | 从相似项目复制通用部分 |

### 9.5.2 冷启动完整流程

```
新项目创建
   ↓
[1] 初始化向导（用户提供元信息）              < 30s
   - 解析 README 提取实体（人名/技术/组织）
   - 建立 scope = project:{id}
   - 写入首批 project_state 三元组
   ↓
[2] 选择项目模板（建立骨架实体）              < 1s
   - 根据技术栈匹配模板
   - 注入骨架实体（HTTP/DB/Auth/Tool 等概念）
   ↓
[3] 批量导入（可选：Git/文档/对话历史）       < 60s
   - Git log → 事件三元组
   - Markdown → 实体提取
   - OpenAPI → API 实体
   ↓
[4] 种子注入（从 global 召回相关经验）        < 5s
   - 按技术栈召回（Python 最佳实践、FastAPI 陷阱）
   - 复制到 project scope（不污染 global）
   ↓
[5] 主动学习（Agent 引导对话补充）            < 3min
   - 首轮对话主动提问：
     "主要目标？" / "核心技术？" / "团队结构？"
   - 用户回答 → 立即写入 L1/L2
   ↓
[6] 首轮完整检索（混合检索 + RRF 融合）       < 500ms
   - 此时已有：元信息 + 骨架 + 批量 + 种子 + 主动学习
   - Top-5 召回率 > 60%
   ↓
工作记忆就绪 ✅  （总计 < 5 分钟）
```

### 9.5.3 关键 API

```python
class ProjectBootstrap:
    async def initialize(
        self, name: str, description: str, tech_stack: List[str],
        readme_content: Optional[str] = None,
        key_files: Dict[str, str] = None,
        template: Optional[str] = None,
    ) -> ProjectContext: ...

    async def bulk_import(
        self, project_id: str, source: ImportSource, config: dict
    ) -> ImportResult: ...

    async def clone_from(
        self, src_scope: str, dst_scope: str, clone_filter: CloneFilter
    ) -> CloneResult: ...
```

### 9.5.4 冷启动退出标准

| 标准 | 阈值 |
|------|------|
| 实体数量 | ≥ 50 个 |
| 三元组数量 | ≥ 200 个 |
| 首次混合检索 Top-5 召回率 | > 60% |
| 用户主动学习交互 | ≥ 3 轮 |
| 工作记忆可用时间 | < 5 分钟 |

---

## §10 并发控制层（Layer 4）[P1]

### 10.1 三层防护

```
┌─────────────────────────────────────────────────────────────┐
│ 第1层: 锁管理器 (LockManager)                                 │
│   SHARED(读锁) + EXCLUSIVE(写锁) + UPDATE(更新锁)            │
│   死锁检测 + 等待队列 + 超时释放                              │
├─────────────────────────────────────────────────────────────┤
│ 第2层: 冲突检测器 (ConflictDetector)                          │
│   VERSION_STALE / WRITE_WRITE / READ_WRITE                   │
│   CAUSAL_CYCLE / CAUSAL_CONTRADICTION                        │
│   操作日志 + 依赖图                                          │
├─────────────────────────────────────────────────────────────┤
│ 第3层: 冲突解决器 (ConflictResolver)                          │
│   AUTO_MERGE / AUTO_LAST / ROLLBACK / MANUAL                │
│   补偿动作（回滚支持）                                        │
└─────────────────────────────────────────────────────────────┘
```

### 10.2 锁粒度

| 粒度 | 适用场景 | 默认 |
|------|---------|------|
| MEMORY_ITEM | 单个记忆项 | — |
| MEMORY_CHUNK | 一组相关记忆 | — |
| MEMORY_LAYER | 整个记忆层 | — |
| **ENTITY** ★默认 | 单个实体 | ✅ |
| RELATION | 单条三元组 | — |
| CAUSAL_CHAIN | 整条因果链 | — |

### 10.3 冲突类型与默认解决策略

| 冲突类型 | 描述 | 默认策略 |
|---------|------|---------|
| VERSION_STALE | 版本过期 | AUTO_MERGE |
| WRITE_WRITE | 写写冲突 | AUTO_LAST |
| READ_WRITE | 读写冲突 | AUTO_LAST |
| CAUSAL_CYCLE | 因果循环 | ROLLBACK_B |
| CAUSAL_CONTRADICTION | 因果矛盾 | MANUAL |

### 10.4 任务链协调

```
任务链A ──→ Lock(X) ──→ 执行 ──→ Unlock(X)
                │                      │
                ▼ 失败                 ▼
任务链B ──→ Wait Queue ──→ Lock释放后唤醒 ──→ 执行
```

### 10.5 对外契约

```python
class LockManager:
    async def acquire(
        self, resource: str, lock_type: LockType, timeout: float = 30.0
    ) -> LockHandle: ...
    async def release(self, handle: LockHandle) -> None: ...

class ConflictDetector:
    async def check(self, op: Operation) -> Optional[Conflict]: ...

class ConflictResolver:
    async def resolve(self, conflict: Conflict) -> Resolution: ...
```

---

## §11 知识质量层（Layer 5）[P2]

### 11.1 双向指针机制

```
┌───────────────────────────────────────────────────────────┐
│  Palace (ChromaDB)              Knowledge Graph (SQLite)   │
│  ┌──────────────┐              ┌──────────────┐           │
│  │  drawer_001  │◄────────────►│  triple_001  │           │
│  │  source_     │  source_     │  source_     │           │
│  │  closet:     │  closet:     │  closet:     │           │
│  │  "triple_001"│  "drawer_001"│  "drawer_001"│           │
│  └──────────────┘              └──────────────┘           │
│         │                            │                     │
│         ▼                            ▼                     │
│  正向追溯: KG事实 → Palace原文                              │
│  反向追溯: Palace原文 → KG事实                              │
└───────────────────────────────────────────────────────────┘
```

### 11.2 矛盾检测（同步轻量 + 异步重量）

```python
class ContradictionDetector:
    async def detect_sync(
        self, new_triple: Triple, existing: List[Triple]
    ) -> List[Contradiction]:
        """写入时同步触发：实体属性冲突"""

    async def detect_async(
        self, scope: str
    ) -> List[Contradiction]:
        """整合任务异步触发：因果链矛盾"""

class ContradictionResolver:
    async def resolve(
        self, contradiction: Contradiction
    ) -> Resolution:
        """策略：accept_new / keep_old / keep_both / merge"""
```

检测维度：时间矛盾、数量矛盾、属性矛盾、因果矛盾（详见 §12 算法 4）。

### 11.3 跨项目知识提升（核心扩展）

```python
class ScopePromoter:
    async def detect_cross_project_patterns(self) -> List[Pattern]:
        """查找在 >= 3 个项目中都出现的关系模式"""
        # 例如：Redis + 缓存雪崩 + 失败模式
    
    async def promote_to_global(self, pattern: Pattern) -> str:
        """复制相关三元组，scope 改为 global"""
    
    async def manual_promote(
        self, memory_id: str, scope: MemoryScope
    ) -> None: ...
```

### 11.4 一致性检查

| 检查类型 | 说明 | 触发时机 |
|---------|------|---------|
| 实体事实一致性 | 同一实体属性是否矛盾 | 写入新三元组时 |
| 时间线一致性 | 实体时间线是否重叠矛盾 | 整合任务执行时 |
| 原文验证 | KG 事实是否有 Palace 原文支撑 | 模式发现任务时 |

### 11.5 历史存储优化策略

| 优化方案 | 节省比例 | 代价 | 阶段 |
|---------|---------|------|------|
| **向量量化**（float32 → int8）| 75% | 检索精度损失 < 2% | MVP |
| **冷热分层**（1 年前归档）| 60% | 冷数据访问延迟 +秒级 | P1 |
| **predicate 字典编码** | 40% | 写入复杂度增加 | P1 |
| **抽样遗忘**（importance < 0.3）| 30% | 部分细节丢失 | P2 |
| **重复检测合并**（RRF > 0.95）| 20% | 偶尔误合并 | P2 |
| **因果链压缩** | 50% | 中间节点不可追溯 | P2 |

**综合效果**：10 项目/年从 ~11 GB → ~3 GB（节省 ~70%）。

### 11.6 历史保留策略

| 数据类型 | 默认保留 | 归档策略 |
|---------|---------|---------|
| 当前状态三元组（valid_to IS NULL）| 永久 | 不归档 |
| 历史状态三元组 | 1 年 | 1 年后归档到 `triples_archive` |
| 时序片段 | 6 个月 | 6 个月后归档 |
| 因果链（已完结）| 2 年 | 2 年后压缩为首尾摘要 |
| 矛盾记录（已解决）| 1 年 | 1 年后仅保留结论 |

### 11.7 对外契约

```python
class Traceability:
    async def trace_kg_to_source(self, triple_id: str) -> Optional[Drawer]: ...
    async def trace_source_to_kg(self, drawer_id: str) -> List[Triple]: ...

class Archival:
    async def archive_expired(self, retention: RetentionPolicy) -> ArchiveResult: ...
```

---

## §12 关键算法摘要

### 12.1 算法 1：Token Budget 上下文构建

```
function build_context(query, budget=4000):
    parts = []
    l0 = load_l0()
    parts.append(("L0", l0, count_tokens(l0)))
    
    l1 = vector_search(query, top_k=5)
    parts.append(("L1", l1, count_tokens(l1)))
    
    l2 = hybrid_memory_search(query, time_range, location, top_k=10)
    parts.append(("L2", l2, count_tokens(l2)))
    
    l3 = concept_match(query, top_k=3)
    parts.append(("L3", l3, count_tokens(l3)))
    
    # 按优先级 L0 > L1 > L2 > L3 裁剪到预算
    return trim_to_budget(parts, budget)
```

### 12.2 算法 2：混合检索（Reciprocal Rank Fusion）

```
function hybrid_memory_search(query, filters, top_k=10):
    vec_results = vector_search(query, top_k=50)      # 语义
    time_results = query_in_range(filters.time)        # 时序
    spatial_results = query_in_area(filters.location)  # 空间
    graph_results = find_path(filters.entity, depth=2) # 图谱
    
    # RRF 融合: score(d) = Σ 1/(k + rank_i(d)), k=60
    fused = {}
    for result_set in [vec, time, spatial, graph]:
        for rank, doc in enumerate(result_set, 1):
            fused[doc.id] += 1 / (60 + rank)
    
    return sorted(fused, by=score, desc)[:top_k]
```

### 12.3 算法 3：模糊匹配加权评分

```
function fuzzy_match(query_mem, candidate_mem):
    score = 0
    weights = {"time": 0.3, "location": 0.2, "content": 0.4, "entity": 0.1}
    
    if query_mem.time_range and candidate_mem.time_range:
        score += weights.time * time_overlap(query.time, candidate.time)
    if query_mem.location and candidate_mem.location:
        score += weights.location * location_match(query.loc, candidate.loc)
    
    score += weights.content * cosine_similarity(query.emb, candidate.emb)
    score += weights.entity * jaccard(query.entities, candidate.entities)
    
    certainty_factor = {EXACT:1.0, HIGH:0.8, MEDIUM:0.6, LOW:0.4, UNKNOWN:0.2}
    return score * certainty_factor[candidate.time_range.certainty]
```

### 12.4 算法 4：矛盾检测（写入时同步）

```
function detect_contradiction(new_triple, existing_triples):
    contradictions = []
    for t in existing_triples:
        if t.subject != new_triple.subject: continue
        if t.predicate != new_triple.predicate: continue
        if time_overlap(t.valid, new_triple.valid) == 0: continue
        
        if t.object != new_triple.object:
            contradictions.append(Contradiction(
                type=infer_type(t, new_triple),  # 数量/属性/时间/因果
                old=t, new=new_triple
            ))
    return contradictions
```

### 12.5 算法 5：抢占与检查点协议

```
class InterruptibleTask:
    async def run(self):
        try:
            for item in self.cursor.iter():
                self.check_interruption()  # 抛 InterruptException
                self.process(item)
                if self.step % CHECKPOINT_INTERVAL == 0:  # 默认 100 步
                    await self.save_checkpoint()
        except InterruptException as e:
            await self.save_checkpoint(can_resume=True)
            raise
    
    async def resume(self, checkpoint):
        self.cursor = checkpoint.cursor
        self.state = checkpoint.state
        await self.run()
```

### 12.6 算法 6：项目冷启动 Pipeline

```
async function initialize_project(meta, template=None):
    # [1] 创建 scope
    scope = f"project:{slugify(meta.name)}"
    await db.execute("INSERT INTO scopes ...", scope)
    
    # [2] 从 README 提取首批实体
    entities = await llm.extract_entities(meta.readme_content)
    await db.batch_insert_entities(entities, scope)
    
    # [3] 应用模板（骨架）
    if template:
        skeleton = load_template(template, meta.tech_stack)
        await db.batch_insert_entities(skeleton, scope)
    
    # [4] 种子注入（从 global 召回）
    seeds = await db.hybrid_search(
        query=meta.description,
        scope_filter=ScopeFilter(include_global=True, include_project=False),
        top_k=50
    )
    await db.clone_to_scope(seeds, scope)
    
    # [5] 标记为"待主动学习"
    await db.set_flag(scope, "needs_active_learning", True)
    
    return ProjectContext(scope=scope, stage="ready_for_interaction")
```

---

## §13 模块边界与依赖图 [MVP]

### 13.1 6 层模块接口契约

```
┌─────────────────────────────────────────────────────────────────┐
│                  Memory Middleware（对外入口）                    │
└────────────────────────────┬────────────────────────────────────┘
                             │ MemoryMiddleware 接口（§3）
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
┌───────────────┐  ┌───────────────────┐  ┌───────────────┐
│  Layer 5      │  │   Layer 3         │  │  Layer 2      │
│  质量接口:     │  │   调度接口:        │  │  生命周期接口:  │
│  Traceability │◄─│   Scheduler       │◄─│  ContextBuild │
│  Promoter     │  │   Bootstrap       │  │  L0/L1/L2/L3  │
│  Archival     │  │   Preemptor       │  │               │
└───────┬───────┘  └─────────┬─────────┘  └───────┬───────┘
        │                    │                    │
        │           ┌────────┴────────┐           │
        │           ▼                 ▼           │
        │  ┌──────────────┐                    │
        │  │  Layer 4     │                    │
        │  │  并发接口:    │◄───────────────────┤
        │  │  Lock/Conflict│                    │
        │  └──────┬───────┘                    │
        │         │                              │
        ▼         ▼                              ▼
┌─────────────────────────────────────────────────────────────────┐
│              Layer 1: StorageEngine 接口                         │
│              read / write / hybrid_search / get_current_state    │
└────────────────────────────┬────────────────────────────────────┘
                             │
                ┌────────────┴────────────┐
                ▼                         ▼
      ┌──────────────────┐
      │ Layer 0          │
      │ 纯数据契约        │
      │ FuzzyMemory      │
      │ Entity/Triple    │
      └──────────────────┘
```

### 13.2 接口契约清单

| 接口 | 提供方 | 调用方 | 关键方法 |
|------|--------|--------|---------|
| `DataModels` | Layer 0 | 所有层 | dataclass + 枚举 |
| `StorageEngine` | Layer 1 | Layer 2/3/4/5 | `read/write/hybrid_search` |
| `ContextBuilder` | Layer 2 | Middleware | `build_context` |
| `L0WorkingMemory` | Layer 2 | Layer 2 | `get/set`（cachebox） |
| `MemoryTaskScheduler` | Layer 3 | Middleware, Layer 5 | `submit/preempt/resume` |
| `ProjectBootstrap` | Layer 3 | Middleware | `initialize/bulk_import/clone_from` |
| `LockManager` | Layer 4 | Layer 1/2/3 | `acquire/release` |
| `ConflictDetector` | Layer 4 | Layer 1/3 | `check` |
| `Traceability` | Layer 5 | Middleware | `trace_kg_to_source` |
| `ScopePromoter` | Layer 5 | Layer 3（调度） | `promote_to_global` |

### 13.3 依赖方向（强制）

```
✅ 允许：
  Layer 0 ← 任何层（被引用）
  Layer N → Layer N-1（向下）
  Layer 5 → Layer 3（提交修复任务）

❌ 禁止：
  Layer 1 → Layer 2/3/4/5（反向）
  Layer N → Layer N+2（跨级跳）
  Layer 0 → 任何上层
```

### 13.4 反向依赖禁止规则

- Layer 1（存储）不得调用 Layer 3（调度）— 否则循环依赖
- Layer 4（并发）不得直接读 Layer 2（生命周期）— 必须经 Layer 1
- Layer 5（质量）修改 Layer 1 数据时，必须通过 Layer 3 任务（保证锁与冲突检测）

---

## §14 工程路线图

### 14.1 四阶段拆解

| 阶段 | 时长 | 范围 | 退出标准 |
|------|------|------|---------|
| **MVP** | 2 周 | 主流程闭环 | 创建项目→写入→检索 可用 |
| **P0** | 2 周 | 任务调度 + 抢占 | 整合/遗忘任务自动执行 |
| **P1** | 3 周 | 并发 + 批量导入 + 种子注入 | 多任务链并发安全 |
| **P2** | 2 周 | 知识质量 + 跨项目克隆 | 自动提升 + 归档可用 |

### 14.2 MVP 阶段（2 周）

**范围**：
- Layer 0 完整数据模型（6 dataclass + 6 枚举）
- Layer 1 SQLite schema（7 张核心表 + 触发器）
- Layer 1 sqlite-vec 集成（向量检索）
- Layer 2 L0（cachebox）+ L1（sqlite-vec，决策 D1）+ ContextBuilder
- Layer 3 冷启动最小集（向导 + 模板 + 主动学习）
- MemoryMiddleware 最小接口（`initialize_project` + `write` + `recall`）

**退出标准**（实测 2026-08-21，全部达标 ✅）：
- ✅ `pip install smilex-memory` 可用 — `uv build` 生成 wheel + sdist，临时干净 venv 安装后 `import smilex; smilex.__version__` 正常（0.1.0）
- ✅ 新项目 < 5 分钟可用 — 实测 engine 初始化 + `initialize_project` ≈ 13ms，首次 recall ≈ 0.03ms
- ✅ 写入 P99 < 50ms，检索 P99 < 500ms — 实测写入 P99 ≈ 2.9ms（1000 样本），检索 P99 ≈ 6.6ms（100 样本，token budget 4000）
- ✅ 冷启动 Top-5 召回率 > 60% — 实测 8/8 = 100%（确定性关键词 + 图通道）
- ✅ 全量测试 433 passed；E2E（创建→写入→检索）通过

**阻塞性依赖**（已在 Windows 验证 ✅）：
- sqlite-vec Python 绑定可用性 — ✅ Windows 可用（烟雾测试通过）
- cachebox Windows 兼容性验证 — ✅ 通过（注意 API 漂移，见 ADR-012 / 决策 D4）

### 14.3 P0 阶段（2 周）

**范围**：
- Layer 3 三重触发调度器
- Layer 3 GRACEFUL 抢占 + Checkpoint
- 5 类核心任务（整合/遗忘/摘要/因果/语义）

**退出标准**（实测 2026-08-21，全部达标 ✅，覆盖于 `tests/unit/test_core_tasks.py`）：
- ✅ 整合任务自动 L1→L2 流转 — 实测：3 条同实体 L1 片段经整合任务聚合为 1 条 L2（importance 取组内最大、实体并集），源片段删除；不足 min_group 的单条保留 L1，重跑幂等
- ✅ 长任务可被抢占 + 续传 — 实测：12 条片段分 12 批整合，第 3 批检查点后 GRACEFUL 抢占 → PAUSED 且 checkpoint 落库（cursor=已扫描游标），resume 后从断点续扫，正确产出 4 条 L2 且不重复
- ✅ 自适应规则触发冷却正确 — 实测：条件持续为真时两次触发间隔 ≥ cooldown，冷却期内（0.2s < 0.4s）不重复触发

### 14.4 P1 阶段（3 周）

**范围**：
- Layer 4 锁管理器 + 冲突检测 + 解决
- Layer 3 批量导入（Git/Markdown）
- Layer 3 种子注入
- Layer 5 矛盾检测（同步）

**退出标准**：
- ✅ 多任务链并发无 lost update（实测 2026-08-21，P1-a 达标，覆盖于 `tests/unit/test_lock_manager.py` / `tests/unit/test_conflict.py`）：① 两条并发链对同一资源读-改-写，EXCLUSIVE 锁序列化 50×2 次递增无丢失（计数器终值=100）；② 两条链基于同一版本并发更新，后者检出 VERSION_STALE 经 AUTO_MERGE 三路合并保留双方修改（版本检查生效）；③ middleware 层两会话 `asyncio.gather` 并发覆写同一实体属性，EXCLUSIVE 锁序列化 + WRITE_WRITE 经 AUTO_LAST 放行，两条 triple 均落库无异常；另验证 MANUAL 策略下覆写返回 `WriteStatus.CONFLICT + ConflictInfo`、锁超时抛 `LockTimeoutError`
- ✅ Git 历史可批量导入（实测 2026-08-21,P1-b 达标，覆盖于 `tests/unit/test_importer.py`):`BulkImporter`(`scheduler/bootstrap/bulk_importer.py`)支持 git/markdown/text 三类 `ImportSource`;git 走 CLI 子进程解析 `git log`（零新依赖，git 不可用抛 RuntimeError),3 提交临时仓库实测 → 3 条 L1 时序记忆（`fragment_id="git:repo:{hash}"` 幂等，锚定提交时间）+ 作者 person 实体 + 文件 object 实体 + contributes_to/contains_file 三元组；重复导入 memory_count=0（幂等）；分批 + CheckpointStore 断点续传（预置 cursor 跳过已处理提交实测 resumed=True)；markdown 目录批量（大文件按行分块）每文件一条 L1 记忆，文本批次按内容 sha256 幂等
- ✅ 矛盾检测准确率 > 90%（实测 2026-08-21,P1-b 达标，覆盖于 `tests/unit/test_contradiction.py`):`ContradictionDetector`(`memory/quality/contradiction.py`）规则版四维检测（VALUE 枚举/单值谓词取值冲突、NUMERIC 数量超 10% 容差、TEMPORAL 半开区间重叠取值不同、CAUSAL 同键因果 + 反向因果），复用 ConflictType(VALUE/NUMERIC/TEMPORAL→WRITE_WRITE,CAUSAL→CAUSAL_CONTRADICTION);27 条标注样本（13 正例 + 13 负例 + 1 复合）实测准确率 **27/27 = 100%**

### 14.5 P2 阶段（2 周）

**范围**：
- Layer 5 跨项目自动提升
- Layer 5 历史归档（冷热分层）
- Layer 3 跨项目克隆
- 性能优化（向量量化 + predicate 编码）

**退出标准**：
- ✅ 跨 3 项目共现自动提升准确率 > 90% — 实测 24/24 = 100%(2026-08-21,标注集 12 正例 + 12 负例,见 `tests/unit/test_cross_project_promotion.py`;规则版 ScopePromoter,归一化键共现判定,无向量)
- ✅ 10 项目/年存储 < 3 GB — 实测 2026-08-21(`tests/benchmarks/storage_size.py`,抽样外推:实测 1 项目 × 1 月 = 500 实体 + 2000 三元组 + 2000 片段含 1024 维向量,×120 线性外推到 10 项目/年):未优化 **1.18 GiB** / P2 优化(归档清向量 + VACUUM + predicate 字典编码)后 **1.17 GiB**,均 < 3 GB。注:归档清向量的体积收益受 sqlite-vec vec0 分块存储粒度限制(约 1000 条/chunk,碎片化删除不释放未清空 chunk);int8 量化工具(`quality/quantization.py`,4x 压缩,往返误差有界)已就绪,生产应用留待后续
- ✅ 归档数据可按需召回 — 实测 2026-08-21(`tests/unit/test_archiver.py`):`Archiver`(quality/archiver.py,schema 011 同库 `triples_archive`/`temporal_fragments_archive` 归档表)按 §11.6 规则(历史三元组 365 天 / 片段 180 天 + 低重要度,当前状态三元组永不归档)迁移冷数据;recall 默认只查热数据,`recall(include_archived=True)` 经 `query_archived`(非向量通道:scope + 关键词)并入归档结果,或 `restore_archived`/`Archiver.restore` 显式回迁(三元组回迁临时摘除 §6.3 触发器防 LWW 污染当前状态);幂等 + 调度器挂接(register_archive_task)+ GRACEFUL 抢占断点续传实测通过

### 14.6 工作量估算（人周）

| 模块 | MVP | P0 | P1 | P2 | 合计 |
|------|-----|----|----|----|------|
| Layer 0 数据模型 | 1 | — | — | — | 1 |
| Layer 1 存储引擎 | 3 | — | 1 | 1 | 5 |
| Layer 2 生命周期 | 2 | 1 | — | — | 3 |
| Layer 3 调度+冷启动 | 1 | 2 | 2 | 1 | 6 |
| Layer 4 并发控制 | — | — | 3 | — | 3 |
| Layer 5 知识质量 | — | — | 1 | 3 | 4 |
| MemoryMiddleware | 1 | 1 | — | — | 2 |
| **合计** | **8** | **4** | **7** | **5** | **24** |

---

## §15 优化建议（六维度）

### 15.1 性能优化

| 建议 | 措施 | 预期收益 |
|------|------|---------|
| **WAL 模式** | `PRAGMA journal_mode=WAL; synchronous=NORMAL` | 写入吞吐 3x |
| **cachebox 热缓存** | L0 + 高频 L1 走进程内 | 读取命中率 > 70% |
| **sqlite-vec HNSW** | 替代暴力检索 | 向量 P99 < 30ms |
| **异步索引构建** | 写入返回 ID，后台批量 | 写入延迟降低 80% |
| **复合索引** | `(scope, valid_from, entity_id)` | 时序查询加速 3x |
| **prepared statement** | 复用执行计划 | 查询延迟降低 30% |

### 15.2 可扩展性优化（SQLite 单写者瓶颈缓解）

| 建议 | 措施 | 适用阶段 |
|------|------|---------|
| **写入批量化** | 应用层累积，每 100ms 批量提交 | 写 QPS 提升 5x |
| **时间分区表** | 按季度分表 `fragments_2026q2` | 单表 > 10M 行 |
| **项目分库** | 每个 scope 一个 `.db` 文件 | 多项目场景 |
| **冷热分层** | 冷数据移到归档 `.db`（ATTACH 查询）| 数据 > 1 年 |
| **ChromaDB 分 collection** | 按 scope 切分向量集合 | 向量 > 500K |

### 15.3 可观测性优化

| 建议 | 措施 |
|------|------|
| **指标埋点** | Prometheus：`memory_write_latency`、`memory_search_recall`、`task_queue_depth` |
| **分布式追踪** | OpenTelemetry 跟踪 `write/recall/infer` 链路 |
| **审计日志** | 所有写入记录 who/what/when/source_closet |
| **健康检查** | `/health` 端点检查 SQLite + ChromaDB 状态 |
| **SQLite 监控** | `PRAGMA integrity_check` 定期运行 |
| **告警规则** | 任务失败率 > 5%、队列积压 > 1000、SQLite > 10GB |

### 15.4 安全优化

| 建议 | 措施 |
|------|------|
| **作用域隔离** | 所有查询强制带 `scope_filter` |
| **PII 脱敏** | 写入前对敏感字段哈希/加密 |
| **文件加密** | SQLCipher 扩展加密 `.db`（可选）|
| **API 鉴权** | API Key + JWT（开放 HTTP API 时）|
| **SQL 注入防护** | 全部使用参数化查询（`?` 占位符）|
| **向量隔离** | ChromaDB collection 按 scope 隔离 |

### 15.5 成本优化

| 建议 | 措施 | 节省 |
|------|------|------|
| **Embedding 本地化** | BGE/E5 替代 OpenAI | API 成本 90% |
| **向量量化** | float32 → int8 | 内存 75% |
| **冷数据归档** | 1 年前移归档库 | 存储 60% |
| **任务批量化** | LOW/IDLE 批量执行 | CPU 2x |
| **Lite 模式** | 无 ChromaDB，纯 sqlite-vec | 内存 30% |

### 15.6 工程化优化

| 建议 | 措施 |
|------|------|
| **Schema 版本化** | 自定义迁移 + `PRAGMA user_version` |
| **测试覆盖** | Layer 0/1 单元，Layer 2/3/4 集成，性能压测 |
| **CI/CD** | GitHub Actions：lint + test + 发布 PyPI |
| **配置中心** | Pydantic Settings + 环境变量分离 |
| **接口版本化** | `/v1/memory/*` + SemVer |
| **嵌入式打包** | PyInstaller / Nuitka 单可执行文件 |

---

## §16 性能指标与 SLA

### 16.1 核心指标（SQLite 基准）

| 指标 | MVP P99 | P1 P99 | P2 P99 |
|------|---------|--------|--------|
| 写入延迟 | < 50ms | < 20ms | < 10ms |
| 单查询延迟 | < 5ms | < 2ms | < 1ms |
| 向量检索 | < 50ms | < 30ms | < 20ms |
| 混合检索 | < 200ms | < 100ms | < 50ms |
| 上下文构建 | < 500ms | < 200ms | < 100ms |
| 并发写入（WAL）| 200 QPS | 1000 QPS | 3000 QPS |
| 并发读取 | 1000 QPS | 5000 QPS | 10000 QPS |

### 16.2 容量规划（SQLite 单库）

| 资源 | MVP 容量 | 上限 | 说明 |
|------|---------|------|------|
| 实体数量 | 100K | 10M | 单库推荐上限 |
| 三元组数量 | 1M | 100M | 配合归档 |
| 向量数量 | 500K | 50M | sqlite-vec 上限 |
| 单库数据量 | 5 GB | 1 TB | 个人场景够用 |
| 项目数量 | 10 | 200 | 按 scope 隔离 |

### 16.3 项目记忆专属指标

| 指标 | 目标 | 说明 |
|------|------|------|
| 当前状态查询 | < 5ms | `project_current_state` 表 |
| 强实时查询 | < 2ms | `valid_to IS NULL` |
| 跨项目共现检测 | 1 小时周期 | 自适应触发 |
| 自动提升准确率 | > 90% | 监控误判率 |
| 历史归档节省 | 70% | 11GB → 3GB |

### 16.4 存储估算（10 项目/年）

| 优化阶段 | 存储量 | 节省 |
|---------|--------|------|
| 未优化 | 11 GB | — |
| + 向量量化 | 6.5 GB | 41% |
| + 冷热分层 | 3.5 GB | 68% |
| + predicate 编码 | 3 GB | 73% |

### 16.5 可用性（嵌入式场景）

| 维度 | 目标 | 说明 |
|------|------|------|
| 进程崩溃恢复 | < 1 秒 | SQLite WAL 回放 |
| 数据持久性 | 99.99% | WAL + 定期备份 |
| 单次备份大小 | ~3 GB | 10 项目/年优化后 |
| 备份方式 | 拷贝 `.db` | 或 `.backup` 在线 |
| 灾难恢复 | 文件还原 | 无需复杂流程 |

---

## §17 风险与权衡（ADR）

### ADR-001: 主库选型 — SQLite vs PostgreSQL

- **选择**：SQLite + sqlite-vec + R-tree + ChromaDB 全嵌入式
- **理由**：个人电脑零安装、单文件分发、跨平台一致、进程内 < 1ms 延迟
- **权衡**：SQLite 单写者模型，高并发写需排队（WAL 缓解）
- **回退**：通过 `StorageEngine` 接口抽象，未来可加 PG 适配器

### ADR-002: 向量存储 — sqlite-vec（ChromaDB 不再纳入）

- **选择**：MVP 用 **sqlite-vec**（决策 D1 修正，原结论"MVP 用 ChromaDB"作废）
- **理由**：符合 §1.2「单库一体化 > 多库拼装」原则——向量索引与主库同文件、同事务、零额外服务；ChromaDB 独立存储与之本质冲突。依据 `docs/analyse/embedding-layer.md`「方案1: SQLite + sqlite-vec + NetworkX」(sqlite-vec 适配度 10/10)
- **权衡**：sqlite-vec 为暴力 KNN（无 HNSW），向量规模 > 500K 时需评估性能
- **回退**：`VectorStore` 接口抽象，SaaS 阶段（P5）可切换 Qdrant

### ADR-003: 锁机制

- **选择**：默认乐观锁（version 字段），因果链用悲观锁
- **理由**：读多写少，乐观锁吞吐高
- **权衡**：高并发写时重试成本高
- **回退**：监控重试率 > 20% 切悲观锁

### ADR-004: 索引构建

- **选择**：异步索引（写入返回 ID，后台批量）
- **理由**：写入延迟对体验影响大
- **权衡**：最终一致性窗口（几秒）
- **缓解**：同步构建 entity 索引（快速过滤）

### ADR-005: 抢占策略

- **选择**：默认 GRACEFUL，紧急 IMMEDIATE
- **理由**：断点续传成本远低于重做
- **权衡**：响应慢（等检查点）
- **回退**：检查点间隔可配（默认 100 步）

### ADR-006: 矛盾检测时机

- **选择**：轻量同步 + 重量异步
- **理由**：保证强一致又不拖慢写入
- **权衡**：异步检测延迟几分钟
- **缓解**：发现矛盾立即提交 HIGH 任务

### ADR-007: 记忆层级

- **选择**：L0/L1/L2/L3 四层
- **理由**：与认知科学对齐，Token 预算清晰
- **权衡**：L3 与 L2 边界模糊
- **缓解**：L3 仅存抽象概念，L2 存具体事件

### ADR-008: 双向指针

- **选择**：单一 `source_closet` 字段
- **理由**：实现简单、查询快
- **权衡**：一对多需拆多条记录
- **回退**：一对多 > 30% 时迁移到独立表

### ADR-009: 项目记忆当前状态

- **选择**：触发器维护汇总表 + 自动失效旧记录
- **理由**：SQLite 不支持物化视图，触发器等效
- **权衡**：写入成本增加 10-15%
- **回退**：应用层显式 UPSERT

### ADR-010: 项目隔离与共享

- **选择**：`MemoryScope` 字段 + 自动/手动提升
- **理由**：兼顾隔离与共享
- **权衡**：查询加 5ms 过滤成本
- **回退**：自动误判率 > 10% 关闭，仅手动

### ADR-011: 历史保留策略

- **选择**：完整历史 + 分层归档优化
- **理由**：支持任意时间点回溯，归档后仅 3GB/10项目/年
- **权衡**：存储比"仅当前"高 3x
- **回退**：超预算时切"滚动 N 版本"

### ADR-012: 缓存选型

- **选择**：cachebox + SQLite 双层
- **理由**：进程内亚微秒 + 复用主库
- **权衡**：崩溃丢失最近 5s 数据
- **回退**：cachebox 维护停滞时切 `functools.lru_cache`
- **API 漂移说明（决策 D4）**：本文档原假设的 `TTLPrefixCache` / `.set()` 在 cachebox 6.x 中不存在（TTL 与 LRU 不可合并，写入方法为 `.insert()`）。L0 实际使用纯 `LRUCache(maxsize=1000)`，不启用 TTL；会话过期由应用层主动清理（`close_session` → `pop()`，进程退出钩子 → `clear()`)。详见 EXECUTION_PLAN_GAPS §3.9

### ADR-013: 服务暴露方式

- **当前状态**：暂缓，下一版本细化
- **候选**：HTTP API / Python SDK / MCP Server
- **决策时机**：MVP 完成后基于实际接入需求

### ADR-014: 项目冷启动策略

- **选择**：六机制组合（向导 + 模板 + 批量 + 种子 + 主动学习 + 克隆）
- **理由**：综合召回率 > 60%
- **权衡**：增加 15% 代码复杂度
- **回退**：主动学习反感时提供"跳过向导"

---

## §18 工程目录结构 [MVP]

```
src/
├── api/                         # API Gateway（待 ADR-013 细化）
├── middlewares/                 # 中间件
│   └── memory.py                # MemoryMiddleware 实现
├── memory/                      # ★ 记忆模块（核心）
│   ├── models/                  # Layer 0: 数据模型
│   │   ├── certainty.py        # CertaintyLevel
│   │   ├── fuzzy.py            # TimeRange, FuzzyLocation, FuzzyMemory
│   │   ├── graph.py            # Entity, Triple, CausalChain
│   │   ├── scope.py            # MemoryScope, ScopeFilter
│   │   └── task.py             # Task, Checkpoint
│   ├── storage/                 # Layer 1: 存储引擎
│   │   ├── sqlite_engine.py    # SQLite 连接 + 扩展加载
│   │   ├── schema/             # SQL DDL（7 + 2 张表）
│   │   ├── queries/            # 检索函数
│   │   │   ├── temporal.py / spatial.py / graph.py / causal.py / hybrid.py / scope.py
│   │   └── vector_store.py     # ChromaDB 适配
│   ├── lifecycle/               # Layer 2: 生命周期
│   │   ├── working.py          # L0 (cachebox + SQLite)
│   │   ├── shortterm.py        # L1 (ChromaDB)
│   │   ├── longterm.py         # L2 (SQLite)
│   │   ├── semantic.py         # L3 (图谱)
│   │   └── context_builder.py  # Token Budget
│   ├── scheduler/               # Layer 3: 调度 + 冷启动
│   │   ├── triggers/           # 时间/事件/自适应触发
│   │   ├── executor/           # 任务执行器
│   │   ├── preemption/         # 抢占 + Checkpoint
│   │   └── bootstrap/          # 冷启动六机制
│   ├── concurrency/             # Layer 4: 并发控制
│   │   ├── lock_manager.py / conflict_detector.py / conflict_resolver.py
│   └── quality/                 # Layer 5: 知识质量
│       ├── bidirectional_link.py / trace.py / consistency.py
│       ├── contradiction.py / scope_promoter.py / archival.py
└── utils/                       # 通用工具
```

---

## §19 部署与技术栈 [MVP]

### 19.1 技术栈表

| 层次 | 技术 | 版本 | 用途 |
|------|------|------|------|
| 语言 | Python | ≥3.13 | 主语言 |
| 主库 | SQLite | 3.40+ | 主存储（Python 自带）|
| 向量 | sqlite-vec | ≥0.1.6 | 向量索引 |
| ~~向量~~ | ~~ChromaDB~~ | — | ~~语义检索~~（决策 D1：不再纳入，SaaS 阶段 P5 备选 Qdrant）|
| Embedding | HashEmbedder（默认，零依赖）/ BGE-M3 + sentence-transformers（可选 `embedding` extra）| ≥3.0 | 文本向量化，1024 维（决策 D5）|
| 空间 | R-tree | SQLite 内置 | 空间索引 |
| 缓存 | cachebox | latest | 进程内 LRU+TTL |
| 异步 | asyncio | Python 内置 | 异步 I/O |
| 配置 | Pydantic | v2 | 数据验证 + Settings |
| 测试 | pytest | latest | 单元/集成测试 |
| Lint | ruff | latest | 代码规范 |

### 19.2 安装方式

```bash
# 用户安装（最终目标）
pip install smilex-memory

# 开发安装
git clone https://github.com/smilename/smilex-memory.git
cd smilex-memory
pip install -e ".[dev]"
```

### 19.3 数据库文件布局

```
~/.smilex/
├── data.db              # 主 SQLite 数据库
├── vectors/             # ChromaDB 向量数据
│   ├── chroma.sqlite3   # ChromaDB 元数据
│   └── collections/
├── archive/             # 冷数据归档
│   └── 2025.db
├── logs/                # 日志
└── config.yaml          # 用户配置
```

### 19.4 首次启动行为

```python
# 伪代码
def first_run_setup():
    if not os.path.exists("~/.smilex/data.db"):
        initialize_sqlite_schema()       # 创建 7+2 张表
        load_extensions()                 # sqlite-vec, R-tree
        create_default_global_scope()     # 注入通用知识种子
    if not os.path.exists("~/.smilex/vectors"):
        chromadb.PersistentClient("~/.smilex/vectors")
```

### 19.5 配置示例（`~/.smilex/config.yaml`）

```yaml
database:
  path: ~/.smilex/data.db
  pragmas:
    journal_mode: WAL
    synchronous: NORMAL
    cache_size: -64000           # 64MB

vector:
  backend: sqlite_vec            # 决策 D1:ChromaDB 不再纳入
  dimension: 1024                # 决策 D5:BGE-M3 维度

cache:
  backend: cachebox
  l0_maxsize: 1000
  l0_ttl: 3600

bootstrap:
  template_dir: ~/.smilex/templates/
  enable_active_learning: true
```

---

## §20 交叉引用

### 20.1 新文档章节 ↔ 分析文档映射

| 本文档章节 | 来源分析文档 | 复用程度 |
|-----------|-------------|---------|
| §1 设计原则 | `memory-module-complete-design.md` §1 | 压缩 |
| §2 系统上下文 | `harness-agent-architecture-plan.md` | 精简 |
| §3 公共 API 契约 | 新增（参考 harness §3） | 全新 |
| §4 分层架构 | `memory-module-complete-design.md` §2 | 重构 |
| §5 数据模型 | `fuzzy-memory-system.md` + `memory-module-complete-design.md` §3 | 整合 |
| §6 存储引擎 | `postgresql-spatiotemporal-graph-causal.md` + `spatiotemporal-memory-system.md` | SQLite 改造 |
| §7 生命周期 | `memory-module-complete-design.md` §5 | 压缩 |
| §8 任务调度 | `memory-task-scheduler.md` | 摘要 |
| §9 抢占恢复 | `task-preemption-recovery.md` | 摘要 |
| §9.5 冷启动 | 新增 | 全新 |
| §10 并发控制 | `multi-chain-concurrency-control.md` | 摘要 |
| §11 知识质量 | `mempalace-bidirectional-pointer.md` + `mempalace-optimization-roadmap.md` | 整合 + 扩展 |
| §12 关键算法 | 跨多文档提炼 | 全新 |
| §13 模块边界 | 新增 | 全新 |
| §14 路线图 | 新增 | 全新 |
| §15 优化建议 | `mempalace-optimization-roadmap.md` | SQLite 适配 |
| §16 SLA | 新增 | 全新 |
| §17 ADR | 跨多文档 + 新增 | 全新 |
| §18 目录结构 | `memory-module-complete-design.md` §10 | 压缩 |
| §19 部署 | `harness-agent-architecture-plan.md` §技术选型 | 嵌入式改造 |

### 20.2 11 份分析文档清单

| # | 文档 | 主要贡献 |
|---|------|---------|
| 1 | `memory-module-complete-design.md` | v3.0 整合版（本文档前身）|
| 2 | `harness-agent-architecture-plan.md` | Harness Agent 总框架 |
| 3 | `fuzzy-memory-system.md` | 模糊记忆数据模型 |
| 4 | `spatiotemporal-memory-system.md` | 时空记忆混合架构 |
| 5 | `postgresql-spatiotemporal-graph-causal.md` | PG 一体化设计（已 SQLite 化）|
| 6 | `memory-task-scheduler.md` | 三重触发调度 |
| 7 | `task-preemption-recovery.md` | 抢占恢复机制 |
| 8 | `multi-chain-concurrency-control.md` | 多链并发控制 |
| 9 | `mempalace-bidirectional-pointer.md` | 双向指针机制 |
| 10 | `mempalace-optimization-roadmap.md` | 优化路线图 |
| 11 | `litellm-vllm-comparison.md` | 模型对比（暂未引用）|

---

**文档结束**

> 本文档为 SmileX Agent Memory System 的 v1.0 整合设计。后续模块细化设计见 `docs/design/modules/`（阶段 B 启动）。
