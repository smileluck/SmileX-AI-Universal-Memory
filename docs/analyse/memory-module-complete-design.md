# Harness Agent — 记忆模块完整设计方案

> 版本：v3.0（整合版）
> 更新日期：2026-05-26
> 整合来源：8 份子设计文档

---

## 目录

1. [总览与设计目标](#1-总览与设计目标)
2. [系统架构](#2-系统架构)
3. [数据模型层](#3-数据模型层)
4. [存储引擎层](#4-存储引擎层)
5. [记忆生命周期层](#5-记忆生命周期层)
6. [任务调度层](#6-任务调度层)
7. [抢占与恢复层](#7-抢占与恢复层)
8. [并发控制层](#8-并发控制层)
9. [知识质量管理层](#9-知识质量管理层)
10. [工程目录结构](#10-工程目录结构)
11. [部署与依赖](#11-部署与依赖)

---

## 1. 总览与设计目标

### 1.1 设计理念

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│   "不完美的记忆 > 没有记忆"                                   │
│                                                             │
│   核心原则:                                                  │
│   1. 接受碎片化、不确定的输入                                 │
│   2. 自动推理补全缺失信息                                     │
│   3. 时空图谱因果一体化                                       │
│   4. 自主调度、安全中断、并发安全                              │
│   5. 单一 PostgreSQL 替代多库                                │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 系统能力矩阵

| 能力维度 | 实现方式 | 关键组件 |
|---------|---------|---------|
| **时序记忆** | PostgreSQL tstzrange + GiST | `query_at_time()`, `build_entity_timeline()` |
| **空间记忆** | PostGIS + LTREE | `query_in_area()`, `query_in_location()` |
| **图谱记忆** | 递归 CTE | `find_path()`, `find_n_degree_relations()` |
| **因果记忆** | predecessor_id 链 | `trace_causal_chain()`, `infer_causal_relations()` |
| **语义记忆** | pgvector + ChromaDB | `hybrid_memory_search()` |
| **模糊记忆** | CertaintyLevel + TimeRange | `FuzzyRetrieval`, `MemoryInference` |
| **任务调度** | 三重触发 + 优先级队列 | `MemoryTaskScheduler`, `TaskExecutor` |
| **抢占恢复** | Checkpoint + GRACEFUL | `PreemptiveScheduler`, `InterruptibleTask` |
| **并发控制** | 锁 + 冲突检测 + 解决 | `LockManager`, `ConflictResolver` |
| **知识质量** | 双向指针 + 矛盾检测 | `Traceability`, `ContradictionDetector` |

### 1.3 子文档索引

| # | 文档 | 核心主题 | 在本方案中的位置 |
|---|------|---------|----------------|
| 1 | fuzzy-memory-system.md | 模糊记忆数据模型 | §3 数据模型层 |
| 2 | spatiotemporal-memory-system.md | 时空记忆混合架构 | §4 存储引擎层 |
| 3 | postgresql-spatiotemporal-graph-causal.md | PostgreSQL 一体化 | §4 存储引擎层 |
| 4 | memory-task-scheduler.md | 任务调度 | §6 任务调度层 |
| 5 | task-preemption-recovery.md | 抢占与恢复 | §7 抢占与恢复层 |
| 6 | multi-chain-concurrency-control.md | 并发控制 | §8 并发控制层 |
| 7 | mempalace-bidirectional-pointer.md | 双向指针与矛盾检测 | §9 知识质量管理层 |
| 8 | harness-agent-architecture-plan.md | Harness Agent 总框架 | §2 系统架构 |

---

## 2. 系统架构

### 2.1 分层架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Harness Agent 2.0                            │
│                     (中间件驱动架构)                                  │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  API Gateway (FastAPI + OpenAI Compatible)                     │  │
│  │  /v1/chat/completions  /v1/agent/run  /v1/memory/*            │  │
│  └───────────────────────────┬───────────────────────────────────┘  │
│                              │                                      │
│  ┌───────────────────────────▼───────────────────────────────────┐  │
│  │  Harness Engine 2.0                                           │  │
│  │  ┌─────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐      │  │
│  │  │ Agent   │  │Middleware│  │  Skill   │  │  Tool    │      │  │
│  │  │  Core   │  │ Pipeline │  │ Registry │  │ Registry │      │  │
│  │  └────┬────┘  └────┬─────┘  └──────────┘  └──────────┘      │  │
│  └───────┼────────────┼────────────────────────────────────────┘  │
│          │            │                                             │
│          │    ┌───────▼──────────────────────────────────────┐     │
│          │    │          Memory Middleware                    │     │
│          │    │  (Agent 对记忆系统的唯一入口)                  │     │
│          │    └───────┬──────────────────────────────────────┘     │
│          │            │                                             │
│  ┌───────▼────────────▼────────────────────────────────────────┐  │
│  │                    MEMORY MODULE                             │  │
│  │                                                              │  │
│  │  ┌─────────────────────────────────────────────────────┐    │  │
│  │  │  Layer 5: 知识质量管理                                │    │  │
│  │  │  Traceability │ Consistency │ Contradiction           │    │  │
│  │  └─────────────────────────────────────────────────────┘    │  │
│  │  ┌─────────────────────────────────────────────────────┐    │  │
│  │  │  Layer 4: 并发控制                                    │    │  │
│  │  │  LockManager │ ConflictDetector │ ConflictResolver   │    │  │
│  │  │  TaskChainCoordinator                                │    │  │
│  │  └─────────────────────────────────────────────────────┘    │  │
│  │  ┌─────────────────────────────────────────────────────┐    │  │
│  │  │  Layer 3: 任务调度 + 抢占恢复                         │    │  │
│  │  │  Scheduler │ Triggers │ Executor │ Preemption │ Queue │    │  │
│  │  └─────────────────────────────────────────────────────┘    │  │
│  │  ┌─────────────────────────────────────────────────────┐    │  │
│  │  │  Layer 2: 记忆生命周期 (L0-L3)                        │    │  │
│  │  │  Working │ ShortTerm │ LongTerm │ Semantic            │    │  │
│  │  │  ContextBuilder (Token Budget Control)               │    │  │
│  │  └─────────────────────────────────────────────────────┘    │  │
│  │  ┌─────────────────────────────────────────────────────┐    │  │
│  │  │  Layer 1: 存储引擎                                    │    │  │
│  │  │  PostgreSQL (时序+空间+图谱+因果+向量)                │    │  │
│  │  │  ChromaDB (语义向量)                                  │    │  │
│  │  └─────────────────────────────────────────────────────┘    │  │
│  │  ┌─────────────────────────────────────────────────────┐    │  │
│  │  │  Layer 0: 数据模型                                    │    │  │
│  │  │  FuzzyMemory │ Entity │ Triple │ CausalChain │ ...   │    │  │
│  │  └─────────────────────────────────────────────────────┘    │  │
│  │                                                              │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 数据流

```
用户输入 ──→ Agent Core ──→ Memory Middleware
                                  │
                    ┌─────────────┼─────────────┐
                    ▼             ▼             ▼
              [写入记忆]    [检索记忆]    [推理记忆]
                    │             │             │
                    ▼             ▼             ▼
              ┌──────────────────────────────────┐
              │        Task Scheduler            │
              │  (创建任务 → 优先级队列 → 执行)    │
              └──────────────┬───────────────────┘
                             │
                    ┌────────┼────────┐
                    ▼        ▼        ▼
              [Lock]   [Conflict]  [Checkpoint]
                    │        │        │
                    ▼        ▼        ▼
              ┌──────────────────────────────────┐
              │     PostgreSQL + ChromaDB        │
              │  (时序 + 空间 + 图谱 + 向量)      │
              └──────────────────────────────────┘
```

---

## 3. 数据模型层

### 3.1 核心枚举

```python
class CertaintyLevel(Enum):
    """确定性级别 — 所有模糊数据的基础"""
    EXACT = "exact"       # 精确
    HIGH = "high"         # 高确定性
    MEDIUM = "medium"     # 中等
    LOW = "low"           # 低确定性
    UNKNOWN = "unknown"   # 未知

class MemoryLayer(Enum):
    """记忆层级"""
    L0_WORKING = "L0"     # 工作记忆（当前对话上下文）
    L1_SHORT = "L1"       # 短时记忆（会话级）
    L2_LONG = "L2"        # 长时记忆（持久化）
    L3_SEMANTIC = "L3"    # 语义记忆（知识/概念）

class LockType(Enum):
    """锁类型"""
    SHARED = "shared"              # 读锁，可共享
    EXCLUSIVE = "exclusive"         # 写锁，排他
    UPDATE = "update"               # 更新锁，可升级

class ConflictType(Enum):
    """冲突类型"""
    VERSION_STALE = "version_stale"
    WRITE_WRITE = "write_write"
    READ_WRITE = "read_write"
    CAUSAL_CYCLE = "causal_cycle"
    CAUSAL_CONTRADICTION = "causal_contradiction"

class PreemptionPolicy(Enum):
    """抢占策略"""
    NONE = "none"
    IMMEDIATE = "immediate"
    GRACEFUL = "graceful"
    COOPERATIVE = "cooperative"
    PRIORITY_INHERITANCE = "priority_inheritance"
```

### 3.2 模糊数据模型

```python
@dataclass
class UncertainValue:
    """不确定值 = 值 + 确定性级别"""
    value: Any
    certainty: CertaintyLevel = CertaintyLevel.MEDIUM

@dataclass
class TimeRange:
    """
    时间范围（支持模糊）
    
    三种模式:
    1. 精确: exact_start + exact_end
    2. 近似: approx_start + approx_end
    3. 相对: relative = "昨天下午"
    """
    exact_start: Optional[datetime] = None
    exact_end: Optional[datetime] = None
    approx_start: Optional[datetime] = None
    approx_end: Optional[datetime] = None
    relative: Optional[str] = None
    certainty: CertaintyLevel = CertaintyLevel.MEDIUM

@dataclass
class FuzzyLocation:
    """
    模糊位置
    
    三种模式:
    1. 精确坐标: exact_coords = (lng, lat)
    2. 层级路径: hierarchy = ["家", "客厅"]
    3. 描述性: region = "厨房附近"
    """
    exact_coords: Optional[tuple[float, float]] = None
    hierarchy: List[str] = field(default_factory=list)
    region: Optional[str] = None
    location_id: Optional[UUID] = None
    certainty: CertaintyLevel = CertaintyLevel.MEDIUM

@dataclass
class FuzzyMemory:
    """
    核心记忆单元
    
    所有字段可选，带确定性标注。
    理念: "不完美的记忆 > 没有记忆"
    """
    id: UUID = field(default_factory=uuid4)
    content: Optional[str] = None
    content_embedding: Optional[np.ndarray] = None
    
    # 时空（可选）
    time_range: Optional[TimeRange] = None
    location: Optional[FuzzyLocation] = None
    
    # 关联实体
    entities: List[str] = field(default_factory=list)
    relations: List[str] = field(default_factory=list)
    
    # 元数据
    importance: float = 0.5       # 0-1 重要性
    emotion_weight: float = 0.0   # 0-1 情感权重
    access_count: int = 0         # 访问次数
    layer: MemoryLayer = MemoryLayer.L1_SHORT
    
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None
```

### 3.3 图谱数据模型

```python
@dataclass
class Entity:
    """实体节点"""
    id: UUID
    entity_id: str              # 归一化 ID: "dr._chen"
    entity_type: str            # person, location, object, concept, event
    name: str                   # 原始名称: "Dr. Chen"
    
    # 时空属性
    valid_from: datetime
    valid_to: Optional[datetime] = None  # NULL = 永久有效
    time_certainty: CertaintyLevel = CertaintyLevel.EXACT
    
    # 空间属性
    location: Optional[FuzzyLocation] = None
    
    # 语义向量
    embedding: Optional[np.ndarray] = None
    
    # 双向指针（连接向量数据库）
    source_closet: Optional[str] = None  # → 向量ID / 三元组ID

@dataclass
class Triple:
    """三元组边"""
    id: UUID
    subject_id: UUID
    predicate: str              # 位于, 认识, 导致, 包含...
    object_id: Optional[UUID] = None
    object_value: Optional[str] = None
    
    # 时空
    valid_from: datetime
    valid_to: Optional[datetime] = None
    
    # 因果链
    predecessor_id: Optional[UUID] = None  # 前因
    successor_ids: List[UUID] = field(default_factory=list)  # 后果
    causal_level: int = 0                 # 因果层级
    
    # 置信度
    confidence: float = 1.0
    certainty: CertaintyLevel = CertaintyLevel.EXACT
    
    # 关系类型
    relation_type: str = "semantic"  # causal, spatial, temporal, semantic
    
    # 双向指针
    source_closet: Optional[str] = None  # → 追溯到 Palace 原文

@dataclass
class CausalChain:
    """因果链"""
    id: UUID
    chain_id: str
    node_ids: List[UUID]        # 按因果顺序排列
    chain_type: str             # temporal, spatial, logical, learned
    confidence: float = 1.0
    support_count: int = 1      # 支持该链的记忆数量
```

---

## 4. 存储引擎层

### 4.1 PostgreSQL 一体化 Schema

```sql
-- ============================================
-- 扩展依赖
-- ============================================
CREATE EXTENSION IF NOT EXISTS vector;       -- pgvector
CREATE EXTENSION IF NOT EXISTS postgis;      -- PostGIS
CREATE EXTENSION IF NOT EXISTS ltree;        -- 层级路径

-- ============================================
-- 表1: entities（实体节点）
-- ============================================
CREATE TABLE entities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       VARCHAR(255) NOT NULL,
    entity_type     VARCHAR(50) NOT NULL,      -- person/location/object/concept/event
    name            TEXT NOT NULL,
    name_normalized VARCHAR(255),
    
    -- 时空
    valid_from      TIMESTAMPTZ NOT NULL,
    valid_to        TIMESTAMPTZ,               -- NULL = 永久
    time_certainty  VARCHAR(20) DEFAULT 'exact',
    
    -- 空间
    location        GEOMETRY(POINT, 4326),
    location_fuzzy  JSONB,                     -- {"hierarchy": ["家","客厅"]}
    location_id     VARCHAR(255),
    
    -- 语义
    embedding       vector(1536),
    
    -- 双向指针
    source_closet   VARCHAR(255),
    
    -- 元数据
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    
    CONSTRAINT valid_time CHECK (valid_to IS NULL OR valid_to > valid_from)
);

-- 索引
CREATE INDEX idx_entities_eid ON entities(entity_id);
CREATE INDEX idx_entities_type ON entities(entity_type);
CREATE INDEX idx_entities_loc ON entities USING GIST(location);
CREATE INDEX idx_entities_time ON entities USING GIST(tstzrange(valid_from, valid_to));
CREATE INDEX idx_entities_vec ON entities USING ivfflat(embedding vector_cosine_ops);

-- ============================================
-- 表2: triples（三元组边 + 因果链）
-- ============================================
CREATE TABLE triples (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    triple_id       VARCHAR(255) NOT NULL,
    
    subject_id      UUID NOT NULL REFERENCES entities(id),
    predicate       VARCHAR(100) NOT NULL,
    object_id       UUID REFERENCES entities(id),
    object_value    TEXT,
    
    -- 时空
    valid_from      TIMESTAMPTZ NOT NULL,
    valid_to        TIMESTAMPTZ,
    time_certainty  VARCHAR(20) DEFAULT 'exact',
    
    -- 因果
    predecessor_id  UUID REFERENCES triples(id),
    successor_ids    UUID[] DEFAULT '{}',
    causal_level     INTEGER DEFAULT 0,
    
    -- 置信度
    confidence       DECIMAL(3,2) DEFAULT 1.0,
    certainty        VARCHAR(20) DEFAULT 'exact',
    
    -- 关系
    edge_weight      DECIMAL(5,2) DEFAULT 1.0,
    relation_type    VARCHAR(50) DEFAULT 'semantic',
    
    -- 双向指针
    source_closet   VARCHAR(255),
    
    metadata         JSONB DEFAULT '{}',
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    
    CONSTRAINT valid_time CHECK (valid_to IS NULL OR valid_to > valid_from)
);

CREATE INDEX idx_triples_subj ON triples(subject_id);
CREATE INDEX idx_triples_obj ON triples(object_id);
CREATE INDEX idx_triples_pred ON triples(predicate);
CREATE INDEX idx_triples_pre ON triples(predecessor_id);
CREATE INDEX idx_triples_time ON triples USING GIST(tstzrange(valid_from, valid_to));

-- ============================================
-- 表3: locations（位置层级）
-- ============================================
CREATE TABLE locations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    location_id     VARCHAR(255) NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    location_type   VARCHAR(50),               -- room/building/city/region
    parent_id       UUID REFERENCES locations(id),
    path            LTREE,                      -- "home.living_room"
    coordinates     GEOMETRY(GEOMETRY, 4326),
    coordinates_fuzzy JSONB,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_loc_path ON locations USING GIST(path);
CREATE INDEX idx_loc_coords ON locations USING GIST(coordinates);

-- ============================================
-- 表4: temporal_fragments（时序片段）
-- ============================================
CREATE TABLE temporal_fragments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fragment_id     VARCHAR(255) NOT NULL UNIQUE,
    time_start      TIMESTAMPTZ NOT NULL,
    time_end        TIMESTAMPTZ,
    time_range      TSTZRANGE,
    location_id     UUID REFERENCES locations(id),
    location_fuzzy  JSONB,
    content         TEXT,
    embedding       vector(1536),
    entities        UUID[] DEFAULT '{}',
    related_triples UUID[] DEFAULT '{}',
    fragment_type   VARCHAR(50),
    importance      INTEGER DEFAULT 5,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_frag_time ON temporal_fragments USING GIST(time_range);
CREATE INDEX idx_frag_vec ON temporal_fragments USING ivfflat(embedding vector_cosine_ops);

-- ============================================
-- 表5: causal_chains（因果链）
-- ============================================
CREATE TABLE causal_chains (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chain_id        VARCHAR(255) NOT NULL UNIQUE,
    chain_name      TEXT,
    node_ids        UUID[] NOT NULL,
    chain_length    INTEGER,
    chain_type      VARCHAR(50),
    confidence      DECIMAL(3,2) DEFAULT 1.0,
    support_count   INTEGER DEFAULT 1,
    last_verified   TIMESTAMPTZ,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_chain_nodes ON causal_chains USING GIN(node_ids);

-- ============================================
-- 表6: vector_links（向量关联 / 双向指针）
-- ============================================
CREATE TABLE vector_links (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vector_db       VARCHAR(50) NOT NULL,       -- chromadb/qdrant
    collection_name VARCHAR(255),
    vector_id       VARCHAR(255),
    embedding       vector(1536),
    entity_id       UUID REFERENCES entities(id),
    triple_id       UUID REFERENCES triples(id),
    fragment_id     UUID REFERENCES temporal_fragments(id),
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_vl_entity ON vector_links(entity_id);
CREATE INDEX idx_vl_vec ON vector_links USING ivfflat(embedding vector_cosine_ops);

-- ============================================
-- 表7: checkpoints（任务检查点）
-- ============================================
CREATE TABLE checkpoints (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         UUID NOT NULL,
    progress        DECIMAL(3,2) DEFAULT 0.0,
    step            INTEGER DEFAULT 0,
    state           JSONB DEFAULT '{}',
    cursor          JSONB,
    description     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ
);

CREATE INDEX idx_cp_task ON checkpoints(task_id);
```

### 4.2 检索能力矩阵

| 检索类型 | SQL 技术 | 函数 |
|---------|---------|------|
| 时间点查询 | `tstzrange` + `OVERLAPS` | `query_at_time(time)` |
| 时间范围查询 | `GREATEST/LEAST` | `query_in_range(start, end)` |
| 实体时间线 | `UNION ALL` + 排序 | `build_entity_timeline(entity_id)` |
| 空间半径 | `ST_DWithin` | `query_in_area(lng, lat, radius)` |
| 空间层级 | `LTREE <@` | `query_in_location(path)` |
| 空间关系推理 | `ST_Distance` | `infer_spatial_relations(a, b)` |
| 图谱路径 | `WITH RECURSIVE` | `find_path(start, end)` |
| N度关系 | 递归 CTE + depth | `find_n_degree_relations(entity, n)` |
| 因果追溯 | `predecessor_id` 递归 | `trace_causal_chain(event, direction)` |
| 因果推理 | 时序共现分析 | `infer_causal_relations(entity)` |
| 混合检索 | 条件组合 + 加权评分 | `hybrid_memory_search(...)` |

---

## 5. 记忆生命周期层

### 5.1 四层记忆架构

```
┌─────────────────────────────────────────────────────────────────┐
│                     Token Budget Control                         │
│                     (总预算: ~4000 tokens)                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  L0 Working Memory (工作记忆)                              │  │
│  │  ┌─────────────────────────────────────────────────────┐  │  │
│  │  │ 当前对话上下文 + 注意力焦点                           │  │  │
│  │  │ 容量: ~500 tokens | 生命周期: 单轮对话               │  │  │
│  │  │ 自动更新: 每轮对话刷新                                │  │  │
│  │  └─────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  L1 Short-term Memory (短时记忆)                           │  │
│  │  ┌─────────────────────────────────────────────────────┐  │  │
│  │  │ 会话级记忆 + 临时片段                                │  │  │
│  │  │ 容量: ~1000 tokens | 生命周期: 会话结束              │  │  │
│  │  │ 检索: 向量相似度 (ChromaDB)                          │  │  │
│  │  │ 更新: L0 溢出时自动整合到 L1                         │  │  │
│  │  └─────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  L2 Long-term Memory (长时记忆)                            │  │
│  │  ┌─────────────────────────────────────────────────────┐  │  │
│  │  │ 持久化记忆 + 时空图谱 + 因果链                       │  │  │
│  │  │ 容量: ~2000 tokens (检索后) | 生命周期: 永久         │  │  │
│  │  │ 检索: 混合检索 (语义+时序+空间+图谱)                 │  │  │
│  │  │ 更新: L1 整合时写入 L2 (调度器触发)                   │  │  │
│  │  └─────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  L3 Semantic Memory (语义记忆)                             │  │
│  │  ┌─────────────────────────────────────────────────────┐  │  │
│  │  │ 知识概念 + 模式 + 规则                               │  │  │
│  │  │ 容量: ~500 tokens | 生命周期: 永久                   │  │  │
│  │  │ 检索: 概念匹配 + 图谱遍历                            │  │  │
│  │  │ 更新: 模式发现任务触发 (调度器)                       │  │  │
│  │  └─────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 5.2 记忆流转

```
用户输入
   │
   ▼
┌─────────┐    自动     ┌─────────┐    调度触发    ┌─────────┐    模式发现    ┌─────────┐
│   L0    │ ────────→  │   L1    │ ──────────→  │   L2    │ ──────────→  │   L3    │
│ 工作记忆 │  L0溢出    │ 短时记忆 │  整合任务    │ 长时记忆 │  提取任务    │ 语义记忆 │
└─────────┘            └─────────┘             └─────────┘             └─────────┘
                            │                       │
                       会话结束                  遗忘任务
                            │                       │
                            ▼                       ▼
                        [遗忘]                  [修剪/遗忘]
```

### 5.3 上下文构建器

```python
class ContextBuilder:
    """
    上下文构建器
    
    从 L0-L3 各层加载记忆，控制在 Token Budget 内
    """
    
    TOKEN_BUDGET = {
        "L0": 500,
        "L1": 1000,
        "L2": 2000,
        "L3": 500,
    }
    
    async def build_context(
        self,
        query: str,
        query_embedding: np.ndarray,
        time_range: TimeRange = None,
        location: FuzzyLocation = None,
        entity_filter: str = None
    ) -> str:
        """
        构建上下文
        
        加载策略:
        1. L0: 全量加载（当前对话）
        2. L1: Top-K 向量相似度
        3. L2: 混合检索（语义+时空+图谱）
        4. L3: 概念匹配
        """
        parts = []
        
        # L0: 工作记忆
        l0 = await self._load_l0()
        parts.append(self._format_layer("L0", l0))
        
        # L1: 短时记忆（向量检索）
        l1 = await self._load_l1(query_embedding, top_k=5)
        parts.append(self._format_layer("L1", l1))
        
        # L2: 长时记忆（混合检索）
        l2 = await self._load_l2(
            query_embedding=query_embedding,
            time_range=time_range,
            location=location,
            entity_filter=entity_filter,
            top_k=10
        )
        parts.append(self._format_layer("L2", l2))
        
        # L3: 语义记忆
        l3 = await self._load_l3(query, top_k=3)
        parts.append(self._format_layer("L3", l3))
        
        # Token 预算控制
        return self._trim_to_budget(parts)
```

---

## 6. 任务调度层

### 6.1 三重触发机制

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│  时间触发器   │    │  事件触发器   │    │  自适应触发器  │
│  定时/间隔   │    │  记忆满/新情节│    │  压力/空闲   │
└──────┬──────┘    └──────┬──────┘    └──────┬──────┘
       │                   │                   │
       └───────────────────┼───────────────────┘
                           ▼
              ┌─────────────────────────┐
              │    Priority Queue        │
              │ CRITICAL > HIGH >        │
              │ MEDIUM > LOW > IDLE      │
              └────────────┬────────────┘
                           ▼
              ┌─────────────────────────┐
              │    Task Executor         │
              │ 整合/遗忘/摘要/因果/...   │
              └─────────────────────────┘
```

### 6.2 任务类型与优先级

| 优先级 | 任务类型 | 典型场景 |
|--------|---------|---------|
| **CRITICAL** | 上下文切换、内存告急 | 立即执行，不排队 |
| **HIGH** | 记忆整合、因果推理 | 高优先级队列 |
| **MEDIUM** | 情节摘要、语义提取 | 普通队列 |
| **LOW** | 模式发现、垃圾回收 | 批量执行 |
| **IDLE** | 索引重建、备份 | 空闲时执行 |

### 6.3 自适应规则

| 规则 | 触发条件 | 动作 | 冷却时间 |
|------|---------|------|---------|
| 记忆压力 | L1 使用率 > 80% | 触发整合任务 | 5 分钟 |
| 空闲整合 | 系统空闲 + 有待整合项 | 执行整合 | 1 分钟 |
| 热记忆刷新 | 高频记忆接近遗忘阈值 | 强化权重 | 30 分钟 |
| 模式发现 | 新情节 ≥ 10 条 | 发现模式 | 1 小时 |
| 因果推理 | 存在潜在因果链接 | 推理关联 | 5 分钟 |

---

## 7. 抢占与恢复层

### 7.1 抢占策略

| 策略 | 行为 | 适用场景 |
|------|------|---------|
| **IMMEDIATE** | 立即中断，丢弃进度 | 极端紧急 |
| **GRACEFUL** | 等待检查点，超时强制 | 大多数场景 ✅ |
| **COOPERATIVE** | 任务主动让步 | 长任务 |
| **PRIORITY_INHERITANCE** | 提升低优先级 | 资源竞争 |

### 7.2 检查点机制

```python
@dataclass
class Checkpoint:
    """任务检查点"""
    task_id: UUID
    progress: float = 0.0       # 0.0 - 1.0
    step: int = 0
    state: Dict[str, Any]       # 状态快照
    cursor: Any                  # 游标位置（断点续传）
    description: str = ""

class InterruptibleTask:
    """可中断任务基类"""
    
    def check_interruption(self):
        """在循环中定期调用"""
        if self._should_pause:
            raise InterruptException(
                checkpoint=self.create_checkpoint(),
                can_resume=True
            )
```

### 7.3 状态流转

```
RUNNING ──→ PAUSING ──→ PAUSED ──→ RESUMING ──→ RUNNING
   │                         │
   │              (有检查点)  │
   │                         ▼
   └──────────→ SUSPENDED ──→ RUNNING (恢复)
   │
   │ (无检查点/强制)
   ▼
INTERRUPTED / PREEMPTED
```

---

## 8. 并发控制层

### 8.1 三层防护

```
┌─────────────────────────────────────────────────────────────┐
│ 第1层: 锁管理器 (LockManager)                                 │
│   SHARED(读锁) + EXCLUSIVE(写锁) + UPDATE(更新锁)            │
│   死锁检测 + 等待队列 + 超时释放                              │
├─────────────────────────────────────────────────────────────┤
│ 第2层: 冲突检测器 (ConflictDetector)                          │
│   VERSION_STALE / WRITE_WRITE / CAUSAL_CYCLE                 │
│   操作日志 + 依赖图                                          │
├─────────────────────────────────────────────────────────────┤
│ 第3层: 冲突解决器 (ConflictResolver)                          │
│   AUTO_MERGE / AUTO_LAST / ROLLBACK / MANUAL                │
│   补偿动作（回滚支持）                                        │
└─────────────────────────────────────────────────────────────┘
```

### 8.2 冲突类型与默认策略

| 冲突类型 | 描述 | 默认策略 |
|---------|------|---------|
| VERSION_STALE | 版本过期 | AUTO_MERGE |
| WRITE_WRITE | 写写冲突 | AUTO_LAST |
| READ_WRITE | 读写冲突 | AUTO_LAST |
| CAUSAL_CYCLE | 因果循环 | ROLLBACK_B |
| CAUSAL_CONTRADICTION | 因果矛盾 | MANUAL |

### 8.3 任务链协调

```
任务链A ──→ Lock(X) ──→ 执行 ──→ Unlock(X)
                │                      │
                ▼ 失败                 ▼
任务链B ──→ Wait Queue ──→ Lock释放后唤醒 ──→ 执行
```

---

## 9. 知识质量管理层

### 9.1 双向指针机制

```
┌───────────────────────────────────────────────────────────┐
│  Palace (向量存储)              Knowledge Graph (PG)       │
│  ┌──────────────┐              ┌──────────────┐          │
│  │  drawer_001  │◄────────────►│  triple_001  │          │
│  │  source_     │  source_     │  source_     │          │
│  │  closet:     │  closet:     │  closet:     │          │
│  │  "triple_001"│  "drawer_001"│  "drawer_001"│          │
│  └──────────────┘              └──────────────┘          │
│         │                            │                    │
│         ▼                            ▼                    │
│  正向追溯: KG事实 → Palace原文                              │
│  反向追溯: Palace原文 → KG事实                              │
└───────────────────────────────────────────────────────────┘
```

### 9.2 矛盾检测与解决

```python
class ContradictionDetector:
    """矛盾检测"""
    
    async def detect(self, new_knowledge: str, existing: List[str]) -> List[Contradiction]:
        """
        检测新旧知识矛盾
        
        检测维度:
        1. 时间矛盾: "2024年在北京" vs "2024年在上海"
        2. 数量矛盾: "3个人" vs "5个人"
        3. 属性矛盾: "红色" vs "蓝色"
        4. 因果矛盾: "A导致B" vs "B导致A"
        """

class ContradictionResolver:
    """矛盾解决"""
    
    async def resolve(self, contradiction: Contradiction) -> Resolution:
        """
        解决策略:
        1. accept_new: 接受新知识，失效旧知识
        2. keep_old: 保留旧知识
        3. keep_both: 保留两者（标记为不同时间段）
        4. merge: 合并（如时间范围扩展）
        """
```

### 9.3 一致性检查

| 检查类型 | 说明 | 触发时机 |
|---------|------|---------|
| 实体事实一致性 | 同一实体在不同三元组中的属性是否矛盾 | 写入新三元组时 |
| 时间线一致性 | 实体的时间线是否有重叠矛盾 | 整合任务执行时 |
| 原文验证 | KG 事实是否有 Palace 原文支撑 | 模式发现任务时 |

---

## 10. 工程目录结构

```
src/
├── api/                                # API Gateway
│   ├── app.py                          # FastAPI 入口
│   ├── routes/
│   │   ├── chat.py                     # /v1/chat/completions
│   │   ├── agent.py                    # /v1/agent/run
│   │   ├── memory.py                   # /v1/memory/*
│   │   └── observe.py                  # /v1/observe/*
│   └── schemas/
│
├── core/                               # Agent 核心
│   ├── agent.py                        # Agent 执行循环
│   ├── context.py                      # AgentContext
│   ├── pipeline.py                     # 中间件管道
│   └── session.py                      # 会话管理
│
├── middlewares/                        # 中间件
│   ├── base.py                         # Middleware 基类
│   ├── memory.py                       # 记忆中间件（对接 memory/）
│   ├── planning.py                     # 规划中间件
│   ├── safety.py                       # 安全中间件
│   └── observe.py                      # 可观测性
│
├── memory/                             # ★ 记忆模块（核心）
│   ├── models/                         # 数据模型
│   │   ├── certainty.py               # CertaintyLevel, UncertainValue
│   │   ├── fuzzy.py                   # TimeRange, FuzzyLocation, FuzzyMemory
│   │   ├── graph.py                   # Entity, Triple, CausalChain
│   │   └── task.py                    # Task, TaskChain, Checkpoint
│   │
│   ├── storage/                        # 存储引擎
│   │   ├── postgresql_engine.py       # PostgreSQL 一体化引擎
│   │   ├── schema/                    # SQL Schema
│   │   │   ├── entities.sql
│   │   │   ├── triples.sql
│   │   │   ├── locations.sql
│   │   │   ├── fragments.sql
│   │   │   ├── causal_chains.sql
│   │   │   ├── vector_links.sql
│   │   │   └── checkpoints.sql
│   │   ├── queries/                   # 检索函数
│   │   │   ├── temporal.py            # 时序检索
│   │   │   ├── spatial.py             # 空间检索
│   │   │   ├── graph.py               # 图谱检索（递归 CTE）
│   │   │   ├── causal.py              # 因果追溯
│   │   │   └── hybrid.py              # 混合检索
│   │   └── vector_store.py            # ChromaDB 向量存储
│   │
│   ├── lifecycle/                      # 记忆生命周期
│   │   ├── working.py                 # L0 工作记忆
│   │   ├── shortterm.py               # L1 短时记忆
│   │   ├── longterm.py                # L2 长时记忆
│   │   ├── semantic.py                # L3 语义记忆
│   │   └── context_builder.py         # 上下文构建 + Token 预算
│   │
│   ├── fuzzy/                          # 模糊记忆
│   │   ├── gradual_storage.py         # 渐进式存储
│   │   ├── fuzzy_retrieval.py         # 模糊检索（多维度加权）
│   │   └── memory_inference.py        # 主动推理引擎
│   │
│   ├── quality/                        # 知识质量管理
│   │   ├── bidirectional_link.py      # 双向指针管理
│   │   ├── trace.py                   # 正向/反向追溯
│   │   ├── consistency.py             # 一致性检查
│   │   └── contradiction.py           # 矛盾检测与解决
│   │
│   └── __init__.py                     # MemoryManager 对外接口
│
├── scheduler/                         # 任务调度
│   ├── core.py                         # MemoryTaskScheduler
│   ├── triggers/
│   │   ├── time_trigger.py            # 时间触发
│   │   ├── event_trigger.py           # 事件触发
│   │   └── adaptive_trigger.py        # 自适应触发 + 规则
│   ├── executor/
│   │   ├── task_executor.py           # TaskExecutor（10+ 任务实现）
│   │   ├── interruptible.py           # 可中断任务基类
│   │   ├── batch_task.py              # 批量可中断任务
│   │   └── multi_step_task.py         # 多步骤可中断任务
│   ├── preemption/
│   │   ├── preemption_manager.py      # 抢占管理器
│   │   ├── preemptive_scheduler.py    # 可抢占调度器
│   │   ├── checkpoint.py              # 检查点管理
│   │   └── signal_handler.py          # 信号处理 + 优雅关闭
│   ├── concurrency/
│   │   ├── lock_manager.py            # 锁管理器
│   │   ├── conflict_detector.py       # 冲突检测器
│   │   ├── conflict_resolver.py       # 冲突解决器
│   │   └── chain_coordinator.py       # 任务链协调器
│   ├── queue.py                        # 优先级队列
│   ├── config.py                       # 调度器配置
│   └── monitor.py                      # 监控 + 告警
│
├── tools/                              # 工具系统
├── sandbox/                            # 安全沙箱
├── models/                             # 模型适配层
└── utils/                              # 通用工具
```

---

## 11. 部署与依赖

### 11.1 技术栈

| 组件 | 技术 | 版本 |
|------|------|------|
| 语言 | Python | ≥3.11 |
| Web框架 | FastAPI | ≥0.100 |
| 主数据库 | PostgreSQL | ≥16 |
| 扩展 | pgvector, PostGIS, LTREE | latest |
| 向量数据库 | ChromaDB | ≥0.4 |
| 异步 | asyncio + uvicorn | - |
| ORM | psycopg2 (异步: asyncpg) | - |
| 迁移 | Alembic | - |

### 11.2 Docker Compose

```yaml
version: '3.8'
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: agent_memory
      POSTGRES_USER: agent
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./db/init.sql:/docker-entrypoint-initdb.d/init.sql

  chromadb:
    image: chromadb/chroma:latest
    ports:
      - "8000:8000"
    volumes:
      - chromadata:/chroma/chroma

  agent:
    build: .
    ports:
      - "8001:8001"
    depends_on:
      - postgres
      - chromadb
    environment:
      DATABASE_URL: postgresql://agent:${DB_PASSWORD}@postgres:5432/agent_memory
      CHROMA_HOST: chromadb
      CHROMA_PORT: 8000

volumes:
  pgdata:
  chromadata:
```

### 11.3 配置示例

```yaml
# config/memory.yaml
memory:
  layers:
    L0:
      max_tokens: 500
      auto_update: true
    L1:
      max_tokens: 1000
      vector_top_k: 5
      ttl_seconds: 3600
    L2:
      max_tokens: 2000
      hybrid_top_k: 10
    L3:
      max_tokens: 500
      concept_top_k: 3

  storage:
    postgres:
      host: localhost
      port: 5432
      database: agent_memory
    chromadb:
      host: localhost
      port: 8000
      collection: agent_memory

  scheduler:
    tick_interval: 1.0
    max_concurrent: 4
    default_timeout: 300
    enable_adaptive: true

  preemption:
    policy: graceful
    grace_timeout: 30
    save_checkpoint: true

  concurrency:
    default_timeout: 30
    enable_optimistic: true
    deadlock_check_interval: 5
```

---

## 附录A: 模块间依赖关系

```
                    harness-agent-architecture-plan (总框架)
                              │
              ┌───────────────┼───────────────┐
              │               │               │
        memory/          scheduler/        tools/ + sandbox/
              │               │
    ┌─────────┼─────────┐    │
    │         │         │    │
  fuzzy    spatio-    quality  task-     task-       multi-chain-
  memory    temporal    (双向    scheduler  preemption  concurrency
  system    + pg-graph  指针)    │          │            │
    │         │         │       │          │            │
    └────┬────┘─────────┘       └──────────┴────────────┘
         │
    共享数据模型:
    CertaintyLevel, TimeRange, FuzzyLocation,
    Entity, Triple, CausalChain, Task
```

## 附录B: 关键设计决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| 存储架构 | 单一 PostgreSQL | 降低运维复杂度，pgvector+PostGIS+LTREE 覆盖所有需求 |
| 图数据库 | 不使用 Neo4j | 递归 CTE 足够，避免额外依赖 |
| 向量数据库 | PostgreSQL + ChromaDB | PG 用于精确检索，Chroma 用于大规模语义检索 |
| 锁策略 | 混合（悲观+乐观） | 写操作用悲观锁，读操作用乐观锁 |
| 抢占策略 | GRACEFUL | 平衡及时性和数据安全 |
| 记忆模型 | 模糊优先 | "不完美的记忆 > 没有记忆" |
