# 三大可迁移思想 — 深度设计文档

> **配套文档**：[08-graph-category-research.md](08-graph-category-research.md)
> **目的**：把三个最值得迁移的思想落到可执行的数据模型、算法、代码骨架
> **核心理念**：**数据模型严谨（预留扩展点），实现极简（YAGNI），按需触发**

---

## 总览：三个思想的协同关系

```
┌──────────────────────────────────────────────────────────────┐
│                    SmileX 知识图谱三层增强                      │
│                                                              │
│  ┌── 层 1: 数据结构层（属性多重图）─────────────────────┐    │
│  │  Triple + EdgeKind + edge_properties + provenance    │    │
│  │  ↓ 解决：边无类型化、噪声无法精准剪枝                  │    │
│  └─────────────────────────────────────────────────────┘    │
│                         ↓                                    │
│  ┌── 层 2: 知识结构层（社区检测）────────────────────────┐    │
│  │  Leiden 层次聚类 + 社区摘要懒生成                     │    │
│  │  ↓ 解决：无法回答宏观问题、检索效率低                  │    │
│  └─────────────────────────────────────────────────────┘    │
│                         ↓                                    │
│  ┌── 层 3: 数据质量层（provenance + 增量索引）────────────┐    │
│  │  EXTRACTED/INFERRED/AMBIGUOUS 分级 + SHA256 缓存      │    │
│  │  ↓ 解决：噪声控制、LLM 成本（节省 95%）                │    │
│  └─────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
```

**协同逻辑**：
- 层 1 提供"边的类型化"，让层 2 的社区检测可以选择性聚类（如只对 `SEMANTIC` 边聚类）
- 层 2 的社区摘要用层 3 的 `EXTRACTED` 优先（高质量摘要）
- 层 3 的清理策略依赖层 1 的 `edge_kind`（如只清理 `AMBIGUOUS + semantic`）

---

## 思想 1：多图融合 — 属性多重图设计

### 1.1 量化好处

| 场景 | RDF 风格（predicate 仅字符串）| 属性多重图（edge_kind 枚举）|
|------|---------------------------|--------------------------|
| 查询"导致 X 的事件" | `WHERE predicate='causes'`（全表扫描）| `WHERE edge_kind='causal'`（索引命中）|
| 新增"引用"关系 | 改 schema + 迁移数据 | 加 `EdgeKind.REFERENCE` 枚举 |
| 清理"弱关联" | 无法区分关系强度 | `WHERE edge_kind='semantic' AND confidence<0.5` |
| 多跳遍历 | 全表递归 | 按 `edge_kind` 分区剪枝，快 5-10x |
| 表达"A 在 T 时刻引用了 B" | 需额外表存元数据 | `edge_properties={"position":42,"time":T}` |

### 1.2 数据模型设计

#### 1.2.1 EdgeKind 枚举（克制版，MVP 只 5 种）

```python
# src/smilex/models/edge_kind.py
from enum import StrEnum

class EdgeKind(StrEnum):
    """边类型枚举
    
    设计原则：
    1. 克制：MVP 只 5 种，按需扩展
    2. 互斥：每条边只能有一种 kind（避免歧义）
    3. 可查询：每种 kind 有独立索引
    """
    SEMANTIC = "semantic"       # 语义关系（默认，LLM 提取）
    CAUSAL = "causal"           # 因果（A 导致 B）
    TEMPORAL = "temporal"       # 时序（A 先于 B）
    REFERENCE = "reference"     # 引用（A 引用/提及 B）
    CONTAINS = "contains"       # 包含（shortcut 边，A 包含 B）
    
    # P2 扩展（预留，不实现）：
    # SPATIAL = "spatial"       # 空间（A 位于 B 内）
    # DERIVED_FROM = "derived_from"  # 派生（A 从 B 提取）
    # CONTRADICTS = "contradicts"    # 矛盾
    # SIMILAR_TO = "similar_to"      # 相似
```

#### 1.2.2 edge_properties 的 schema 约束

```python
# src/smilex/models/edge_schemas.py
from typing import TypedDict

class CausalProps(TypedDict, total=False):
    """CAUSAL 边的属性 schema"""
    weight: float               # 因果强度 0.0-1.0
    lag_seconds: int            # 延迟（A 发生后多久 B 发生）
    mechanism: str              # 因果机制描述（"通过...导致"）

class TemporalProps(TypedDict, total=False):
    """TEMPORAL 边的属性 schema"""
    interval_seconds: int       # A 与 B 的时间间隔
    is_sequential: bool         # 是否严格顺序（不允许乱序）
    is_repeating: bool          # 是否周期性

class ReferenceProps(TypedDict, total=False):
    """REFERENCE 边的属性 schema"""
    position: int               # 引用位置（行号/字符偏移）
    context: str                # 引用上下文（前后 50 字）
    reference_type: str         # quote/paraphrase/link

class ContainsProps(TypedDict, total=False):
    """CONTAINS 边的属性 schema（shortcut 边）"""
    depth: int                  # 嵌套深度
    is_direct: bool             # 直接包含 vs 间接包含

# Schema 注册表（运行时校验）
EDGE_PROPERTY_SCHEMAS = {
    EdgeKind.CAUSAL: CausalProps,
    EdgeKind.TEMPORAL: TemporalProps,
    EdgeKind.REFERENCE: ReferenceProps,
    EdgeKind.CONTAINS: ContainsProps,
    EdgeKind.SEMANTIC: dict,  # 语义边无固定 schema
}
```

#### 1.2.3 Triple 升级后的完整定义

```python
# src/smilex/models/triple.py
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

@dataclass
class Triple:
    """属性多重图的三元组（升级版）"""
    id: str                              # ULID
    subject_id: str
    predicate: str                       # 具体谓词（如 "causes", "references"）
    object_id: Optional[str] = None      # 实体 ID（可空，用 object_value）
    object_value: Optional[str] = None   # 字面值
    
    # ★ 新增：类型化字段
    edge_kind: EdgeKind = EdgeKind.SEMANTIC
    edge_properties: dict = field(default_factory=dict)
    
    # 元数据
    scope: str = "project:default"
    confidence: float = 0.7
    certainty: CertaintyLevel = CertaintyLevel.MEDIUM
    
    # 时空（保留原有设计）
    valid_from: str = field(default_factory=lambda: now_iso())
    valid_to: Optional[str] = None
    
    # 因果链（仅 CAUSAL 边用）
    predecessor_id: Optional[str] = None
    causal_level: int = 0
    
    # 双向指针（保留 MemPalace 命名）
    source_closet: Optional[str] = None
    
    # 审计
    created_at: str = field(default_factory=lambda: now_iso())
    updated_at: str = field(default_factory=lambda: now_iso())
    access_count: int = 0
    
    def validate_edge_properties(self) -> bool:
        """校验 edge_properties 是否符合 edge_kind 的 schema"""
        schema = EDGE_PROPERTY_SCHEMAS.get(self.edge_kind)
        if schema is None or schema is dict:
            return True  # 无约束
        # 简单校验：所有 key 必须在 schema 中
        return all(k in schema.__annotations__ for k in self.edge_properties)
```

### 1.3 SQLite Schema 设计

```sql
-- src/smilex/storage/schema/002_triples_v2.sql

CREATE TABLE triples (
    -- 标识
    id              TEXT PRIMARY KEY,
    triple_id       TEXT NOT NULL,              -- 业务 ID（ULID）
    
    -- 三元组核心
    subject_id      TEXT NOT NULL,
    predicate       TEXT NOT NULL,
    object_id       TEXT,
    object_value    TEXT,
    
    -- ★ 类型化边
    edge_kind       TEXT NOT NULL DEFAULT 'semantic',
    edge_properties TEXT DEFAULT '{}',          -- JSON
    
    -- 作用域
    scope           TEXT NOT NULL DEFAULT 'project:default',
    
    -- 质量
    confidence      REAL DEFAULT 0.7,
    certainty       TEXT DEFAULT 'medium',
    provenance      TEXT DEFAULT 'inferred',    -- ★ 思想 3
    
    -- 时空
    valid_from      TEXT NOT NULL,
    valid_to        TEXT,
    
    -- 因果（仅 edge_kind='causal' 时有意义）
    predecessor_id  TEXT,
    causal_level    INTEGER DEFAULT 0,
    
    -- 双向指针
    source_closet   TEXT,
    
    -- 审计
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now')),
    access_count    INTEGER DEFAULT 0,
    observation_count INTEGER DEFAULT 0,        -- ★ 思想 3
    
    -- 约束
    CHECK (edge_kind IN ('semantic','causal','temporal','reference','contains')),
    CHECK (provenance IN ('extracted','inferred','observed','ambiguous')),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);

-- ★ 关键索引：复合索引按 edge_kind 分区
CREATE INDEX idx_triples_subj_kind ON triples(subject_id, edge_kind);
CREATE INDEX idx_triples_obj_kind ON triples(object_id, edge_kind);
CREATE INDEX idx_triples_scope_kind ON triples(scope, edge_kind);
CREATE INDEX idx_triples_pred ON triples(predicate);
CREATE INDEX idx_triples_predecessor ON triples(predecessor_id) 
    WHERE edge_kind = 'causal';
CREATE INDEX idx_triples_time ON triples(valid_from, valid_to);

-- JSON 索引（SQLite JSON1）
CREATE INDEX idx_triples_props ON triples(json_extract(edge_properties, '$.weight'))
    WHERE edge_kind = 'causal';
```

### 1.4 查询接口设计

```python
# src/smilex/storage/queries/graph.py

class GraphQueries:
    async def find_outgoing(
        self, entity_id: str,
        edge_kinds: list[EdgeKind] = None,    # 过滤边类型
        predicate: str = None,
        scope: str = None,
        valid_at: datetime = None,            # 时序过滤
        min_confidence: float = 0.0,
    ) -> list[Triple]:
        """查询节点的出边（支持按 edge_kind 过滤）"""
        sql = "SELECT * FROM triples WHERE subject_id = ?"
        params = [entity_id]
        
        if edge_kinds:
            placeholders = ",".join("?" * len(edge_kinds))
            sql += f" AND edge_kind IN ({placeholders})"
            params.extend(edge_kinds)
        
        if predicate:
            sql += " AND predicate = ?"
            params.append(predicate)
        
        if scope:
            sql += " AND scope = ?"
            params.append(scope)
        
        if valid_at:
            sql += " AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
            params.extend([valid_at, valid_at])
        
        if min_confidence > 0:
            sql += " AND confidence >= ?"
            params.append(min_confidence)
        
        return await self.db.fetchall(sql, params)
    
    async def trace_causal_chain(
        self, start_id: str, max_depth: int = 5
    ) -> list[Triple]:
        """因果链追溯（仅 edge_kind='causal'）"""
        sql = """
        WITH RECURSIVE chain AS (
            SELECT *, 1 AS depth FROM triples
            WHERE subject_id = ? AND edge_kind = 'causal'
            UNION ALL
            SELECT t.*, c.depth + 1 FROM triples t
            JOIN chain c ON t.subject_id = c.object_id
            WHERE t.edge_kind = 'causal' AND c.depth < ?
        )
        SELECT * FROM chain ORDER BY depth
        """
        return await self.db.fetchall(sql, [start_id, max_depth])
    
    async def find_shortcut_contains(
        self, parent_id: str, max_depth: int = 3
    ) -> list[str]:
        """CONTAINS shortcut 边的快速遍历"""
        sql = """
        WITH RECURSIVE contained AS (
            SELECT object_id, 1 AS depth FROM triples
            WHERE subject_id = ? AND edge_kind = 'contains'
            UNION ALL
            SELECT t.object_id, c.depth + 1 FROM triples t
            JOIN contained c ON t.subject_id = c.object_id
            WHERE t.edge_kind = 'contains' AND c.depth < ?
        )
        SELECT DISTINCT object_id FROM contained
        """
        return [r[0] for r in await self.db.fetchall(sql, [parent_id, max_depth])]
```

### 1.5 实施步骤

| 步骤 | 工作量 | 产出 |
|------|--------|------|
| 1. 定义 EdgeKind 枚举 + edge_properties schema | 0.5 天 | `models/edge_kind.py` |
| 2. 升级 Triple dataclass | 0.5 天 | `models/triple.py` |
| 3. 写 SQLite schema（带索引）| 0.5 天 | `schema/002_triples_v2.sql` |
| 4. 实现 GraphQueries（含递归 CTE）| 1 天 | `queries/graph.py` |
| 5. 写迁移脚本（v1 → v2）| 0.5 天 | `migrations/001_upgrade_triples.py` |
| 6. 单元测试 | 1 天 | `tests/test_graph_queries.py` |
| **合计** | **4 天** | — |

---

## 思想 2：社区检测 + 层次摘要设计

### 2.1 量化好处

**解锁"宏观问题"能力**：

| 问题 | 朴素向量 RAG | + 社区检测 |
|------|------------|-----------|
| "用户最近主要做什么？" | ❌ 召回散乱 | ✅ Global Search 答全 |
| "这个项目的核心主题？" | ❌ 无法概括 | ✅ 社区摘要直接答 |
| "A 和 B 什么关系？" | ⚠️ 单跳 | ✅ 同社区深挖 |
| 上下文压缩 | 10 实体 = 10 条记忆 | **1 社区摘要 ≈ 100 实体** |

**性能提升**：
- 检索加速：先找社区（10 个）→ 深入命中社区，剪掉 90% 无关节点
- Token 节省：Global Search 用摘要而非全实体，节省 80% tokens

### 2.2 数据模型设计

#### 2.2.1 Community 表

```sql
-- src/smilex/storage/schema/006_communities.sql

CREATE TABLE communities (
    id              TEXT PRIMARY KEY,
    scope           TEXT NOT NULL,
    
    -- 层次结构
    level           INTEGER NOT NULL,          -- 0=最细，N=最粗
    parent_id       TEXT,                     -- 父社区（上一层级）
    
    -- 成员
    member_count    INTEGER NOT NULL,
    member_ids      TEXT NOT NULL,             -- JSON 数组
    
    -- 摘要（懒生成）
    summary         TEXT,                      -- LLM 生成的社区摘要
    summary_embedding BLOB,                   -- 摘要向量（用于 Global Search）
    summary_model   TEXT,                      -- 生成摘要的模型（版本追溯）
    summary_at      TEXT,
    
    -- 质量
    cohesion_score  REAL,                      -- 社区内聚度（Leiden 输出）
    quality_score   REAL,                      -- 摘要质量评分
    
    -- 审计
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now')),
    
    FOREIGN KEY (parent_id) REFERENCES communities(id)
);

CREATE INDEX idx_comm_scope_level ON communities(scope, level);
CREATE INDEX idx_comm_parent ON communities(parent_id);
```

#### 2.2.2 Entity-Community 映射表

```sql
CREATE TABLE entity_communities (
    entity_id       TEXT NOT NULL,
    community_id    TEXT NOT NULL,
    scope           TEXT NOT NULL,
    level           INTEGER NOT NULL,
    PRIMARY KEY (entity_id, community_id, level)
);

CREATE INDEX idx_ec_community ON entity_communities(community_id);
CREATE INDEX idx_ec_scope_level ON entity_communities(scope, level);
```

#### 2.2.3 重新聚类日志表（追踪触发）

```sql
CREATE TABLE cluster_runs (
    id              TEXT PRIMARY KEY,
    scope           TEXT NOT NULL,
    triggered_by    TEXT NOT NULL,             -- growth/manual/scheduled
    entity_count    INTEGER,
    edge_count      INTEGER,
    community_count INTEGER,
    duration_ms     INTEGER,
    ran_at          TEXT DEFAULT (datetime('now'))
);
```

### 2.3 Leiden 算法实现

```python
# src/smilex/quality/community_detector.py
import igraph as ig
from leidenalg import (
    find_partition,
    RBConfigurationVertexPartition,
)
from dataclasses import dataclass

@dataclass
class LeidenConfig:
    """Leiden 算法配置（实战经验值）"""
    resolution: float = 1.0           # 默认；增大→社区更小
    min_community_size: int = 5       # 小于 5 个实体的社区丢弃
    n_iterations: int = -1            # -1 = 收敛为止
    max_levels: int = 3               # 层次深度
    edge_kinds: list = None           # 仅对哪些边类型聚类（默认 semantic + causal）
    weight_attr: str = "weight"       # 用 confidence 作为权重


class CommunityDetector:
    """社区检测器（懒增量 + 层次聚类）"""
    
    GROWTH_THRESHOLD = 0.15           # 实体增长 15% 才 re-cluster
    MIN_ENTITIES = 100                # 至少 100 实体才跑
    
    def __init__(self, db, llm, config: LeidenConfig = None):
        self.db = db
        self.llm = llm
        self.config = config or LeidenConfig()
    
    async def maybe_recluster(self, scope: str) -> Optional[str]:
        """懒增量触发：检查是否需要 re-cluster"""
        stats = await self.db.get_scope_stats(scope)
        
        # 条件 1: 数据量不足
        if stats.entity_count < self.MIN_ENTITIES:
            return None
        
        # 条件 2: 增长不够
        last_run = await self.db.get_last_cluster_run(scope)
        if last_run:
            growth = (stats.entity_count - last_run.entity_count) / last_run.entity_count
            if growth < self.GROWTH_THRESHOLD:
                return None
        
        # 触发聚类
        return await self._run_leiden(scope, triggered_by="growth")
    
    async def _run_leiden(self, scope: str, triggered_by: str) -> str:
        """执行 Leiden 层次聚类"""
        start_time = time.time()
        
        # 1. 构建图
        edges = await self._load_edges(scope)
        if len(edges) < 10:
            return None  # 边太少
        
        vertices = await self._load_vertices(scope, edges)
        graph = ig.Graph.TupleList(
            edges, directed=True, vertices=vertices,
            edge_attrs=["weight"]
        )
        
        # 2. Leiden 层次聚类
        partition = find_partition(
            graph,
            RBConfigurationVertexPartition,
            resolution_parameter=self.config.resolution,
            n_iterations=self.config.n_iterations,
            weights="weight",
        )
        
        # 3. 存储社区
        run_id = str(ULID())
        comm_count = await self._store_communities(
            scope, graph, partition, run_id
        )
        
        # 4. 记录运行日志
        await self.db.insert_cluster_run(ClusterRun(
            id=run_id,
            scope=scope,
            triggered_by=triggered_by,
            entity_count=len(vertices),
            edge_count=len(edges),
            community_count=comm_count,
            duration_ms=int((time.time() - start_time) * 1000),
        ))
        
        return run_id
    
    async def _load_edges(self, scope: str) -> list[tuple]:
        """加载边（按 edge_kind 过滤）"""
        edge_kinds = self.config.edge_kinds or [
            EdgeKind.SEMANTIC, EdgeKind.CAUSAL
        ]
        placeholders = ",".join("?" * len(edge_kinds))
        rows = await self.db.fetchall(f"""
            SELECT subject_id, object_id, confidence
            FROM triples
            WHERE scope = ? 
              AND edge_kind IN ({placeholders})
              AND object_id IS NOT NULL
              AND valid_to IS NULL
        """, [scope, *edge_kinds])
        return [(r[0], r[1], r[2]) for r in rows]
    
    async def _store_communities(
        self, scope, graph, partition, run_id
    ) -> int:
        """存储层次社区结构"""
        count = 0
        
        # Leiden 的层次结构：partition 在不同 level 给出不同划分
        # 简化版：只存 level 0（最细），P2 再加层次聚合
        for comm_id_str, member_indices in enumerate(partition):
            if len(member_indices) < self.config.min_community_size:
                continue
            
            member_ids = [graph.vs[i]["name"] for i in member_indices]
            
            comm = Community(
                id=str(ULID()),
                scope=scope,
                level=0,
                parent_id=None,
                member_count=len(member_ids),
                member_ids=json.dumps(member_ids),
                summary="",  # 懒生成
                cohesion_score=self._compute_cohesion(graph, member_indices),
            )
            await self.db.insert_community(comm)
            
            # 建立 entity → community 映射
            for eid in member_ids:
                await self.db.insert_entity_community(
                    entity_id=eid, community_id=comm.id, scope=scope, level=0
                )
            
            count += 1
        
        return count
    
    def _compute_cohesion(self, graph, member_indices) -> float:
        """计算社区内聚度（内部边数 / 最大可能边数）"""
        n = len(member_indices)
        if n < 2:
            return 0.0
        sub = graph.subgraph(member_indices)
        actual = sub.ecount()
        max_possible = n * (n - 1)  # 有向图
        return actual / max_possible if max_possible > 0 else 0.0
```

### 2.4 社区摘要懒生成

```python
# src/smilex/quality/community_summarizer.py

class CommunitySummarizer:
    """社区摘要生成（懒触发 + 小模型 + 缓存）"""
    
    SMALL_MODEL = "claude-haiku-4-5"  # 或 gpt-4o-mini
    MAX_MEMBERS_PER_SUMMARY = 30       # 单次摘要最多 30 个实体
    
    async def get_or_generate(self, community_id: str) -> str:
        """获取摘要，不存在则懒生成"""
        comm = await self.db.get_community(community_id)
        
        if comm.summary:
            return comm.summary
        
        # 生成
        summary = await self._generate_summary(comm)
        
        # 缓存
        await self.db.update_community_summary(
            community_id,
            summary=summary,
            summary_embedding=await self.embedder.embed(summary),
            summary_model=self.SMALL_MODEL,
            summary_at=now_iso(),
        )
        
        return summary
    
    async def _generate_summary(self, comm: Community) -> str:
        """用小模型生成社区摘要"""
        member_ids = json.loads(comm.member_ids)[:self.MAX_MEMBERS_PER_SUMMARY]
        members = await self.db.get_entities_by_ids(member_ids)
        
        # 获取社区内部的关键三元组（增强摘要信息）
        internal_triples = await self.db.get_internal_triples(
            member_ids, limit=20
        )
        
        prompt = f"""请总结以下实体群组的共同主题和关键关系。

## 实体列表（{len(members)} 个）
{self._format_entities(members)}

## 关键关系（社区内部）
{self._format_triples(internal_triples)}

## 输出要求
- 一段话，不超过 200 字
- 突出主题、关键人物/技术、主要活动
- 中文输出

## 主题摘要："""
        
        return await self.llm.complete(
            model=self.SMALL_MODEL,
            prompt=prompt,
            max_tokens=300,
        )
```

### 2.5 查询路由与三模式检索

```python
# src/smilex/lifecycle/query_router.py

class QueryRouter:
    """查询路由：根据查询类型选择检索模式"""
    
    GLOBAL_KEYWORDS = {
        "所有", "整体", "主要", "最近", "总结", "都在", "每个",
        "全部", "概括", "综述", "概览", "all", "overall", "summary"
    }
    
    DRIFT_INDICATORS = {"比较", "对比", "关系", "影响", "vs", "compare"}
    
    async def route(
        self, query: str, scope_filter: ScopeFilter
    ) -> RecallResponse:
        """主路由方法"""
        mode = self._classify(query)
        
        if mode == "global":
            return await self._global_search(query, scope_filter)
        elif mode == "drift":
            return await self._drift_search(query, scope_filter)
        else:
            return await self._local_search(query, scope_filter)
    
    def _classify(self, query: str) -> str:
        """规则分类（不调 LLM，省成本）"""
        q_lower = query.lower()
        
        # Global: 含全局关键词
        if any(k in q_lower for k in self.GLOBAL_KEYWORDS):
            return "global"
        
        # Drift: 含对比关键词 + 长查询
        if any(k in q_lower for k in self.DRIFT_INDICATORS):
            if len(query.split()) > 8:
                return "drift"
        
        # Local: 默认
        return "local"
    
    async def _global_search(
        self, query: str, scope_filter: ScopeFilter
    ) -> RecallResponse:
        """Global Search: 社区摘要 map-reduce"""
        # 1. 获取顶层社区
        communities = await self.db.get_top_communities(
            scope_filter, level=0
        )
        
        # 2. 并行：每个社区用摘要回答 partial
        partials = await asyncio.gather(*[
            self._community_partial(query, comm)
            for comm in communities[:20]  # 限制最多 20 个社区
        ])
        
        # 3. Reduce: 合并所有 partial
        final = await self.llm.complete(
            model="claude-haiku-4-5",  # 小模型合并
            prompt=f"""基于以下各社区的局部回答，综合回答用户问题。

问题：{query}

各社区局部回答：
{self._format_partials(partials)}

综合回答："""
        )
        
        return RecallResponse(
            context=final,
            sources=[comm.id for comm in communities[:20]],
            layers_used=[MemoryLayer.L3_5_COMMUNITY],
            mode="global",
        )
    
    async def _community_partial(
        self, query: str, comm: Community
    ) -> str:
        """单社区的 partial answer"""
        summary = await self.summarizer.get_or_generate(comm.id)
        return await self.llm.complete(
            model="claude-haiku-4-5",
            prompt=f"""基于这个社区摘要，回答问题。

社区摘要：{summary}

问题：{query}

如果社区内容与问题无关，回复"无关"。否则简要回答（不超过 100 字）。

回答："""
        )
    
    async def _local_search(
        self, query: str, scope_filter: ScopeFilter
    ) -> RecallResponse:
        """Local Search: 命中实体 → 邻居扩展"""
        # 1. 向量检索找种子实体
        query_emb = await self.embedder.embed(query)
        seeds = await self.vector_store.search(query_emb, top_k=5)
        
        # 2. 邻居扩展（按 edge_kind 过滤）
        context_entities = set(seeds)
        for seed in seeds:
            neighbors = await self.graph.find_outgoing(
                seed.id,
                edge_kinds=[EdgeKind.SEMANTIC, EdgeKind.CAUSAL],
                min_confidence=0.5,
            )
            context_entities.update(n.object_id for n in neighbors if n.object_id)
        
        # 3. 构建 context
        entities = await self.db.get_entities_by_ids(list(context_entities))
        return RecallResponse(
            context=self._format_entities(entities),
            sources=[e.id for e in entities],
            layers_used=[MemoryLayer.L2_LONG],
            mode="local",
        )
    
    async def _drift_search(
        self, query: str, scope_filter: ScopeFilter
    ) -> RecallResponse:
        """DRIFT Search: Global + Local 混合"""
        # 1. 先用 Global 找相关社区
        communities = await self.db.search_communities_by_query(
            query, scope_filter, top_k=3
        )
        
        # 2. 每个社区内做 Local Search
        all_results = []
        for comm in communities:
            local_result = await self._local_in_community(
                query, comm
            )
            all_results.extend(local_result)
        
        # 3. 合并去重
        return RecallResponse(
            context=self._merge_results(all_results),
            sources=[r.id for r in all_results],
            layers_used=[MemoryLayer.L3_5_COMMUNITY, MemoryLayer.L2_LONG],
            mode="drift",
        )
```

### 2.6 成本控制策略

| 策略 | 实现 | 效果 |
|------|------|------|
| **懒增量聚类** | 增长 15% 才 re-cluster | 减少 90% Leiden 调用 |
| **懒生成摘要** | 首次访问才算 | 减 少 70% LLM 调用 |
| **小模型优先** | Haiku/4o-mini | 成本仅 Opus 的 1/30 |
| **缓存命中** | summary + embedding 持久化 | 二次查询零成本 |
| **社区上限** | Global 最多 20 社区 | 控制单次查询成本 |

**单项目/月成本估算**：
- Leiden 聚类：0（纯算法）
- 摘要生成：~50 社区 × Haiku ≈ $0.25
- Global Search：5-10 次 Haiku ≈ $0.05/次
- **总计：< $1/月**

---

## 思想 3：确定性 + Provenance + 增量索引

### 3.1 量化好处

**Provenance 分级降噪**：

| 场景 | 无 provenance | 有 provenance |
|------|-------------|--------------|
| LLM 幻觉错误三元组 | 混入正常知识 | 标记 `AMBIGUOUS`，不污染检索 |
| 清理低质量数据 | 全表扫描 + 启发式 | `DELETE WHERE provenance='ambiguous'` 精准 |
| 用户确认的知识 | 与推测混在一起 | `EXTRACTED` 永不清理 |
| 知识可信度排序 | 无法区分 | `EXTRACTED` 优先召回 |

**增量索引降本**：

| 场景 | 全量提取 | 增量索引 | 节省 |
|------|---------|---------|------|
| 10 项目 × 1000 文件，每周 5% 变更 | 10000 次 LLM | 500 次 | **95%** |
| 月成本（Haiku）| ~$5 | ~$0.25 | **$4.75** |
| 冷启动时间（10K 文件）| 30 分钟 | **2 分钟**（5% 变更）| **93%** |

### 3.2 Provenance 设计

#### 3.2.1 枚举与语义

```python
# src/smilex/models/provenance.py
from enum import StrEnum

class Provenance(StrEnum):
    """知识来源分级（来自 graphrag-code 的置信度标签）"""
    
    EXTRACTED = "extracted"       # 用户明确确认 / 工具抽取 / 代码静态分析
    INFERRED = "inferred"         # LLM 推断（默认）
    OBSERVED = "observed"         # 从行为观察（如访问日志、点击流）
    AMBIGUOUS = "ambiguous"       # 低置信度 / 矛盾未解决 / 过期候选


PROVENANCE_PRIORITY = {
    Provenance.EXTRACTED: 1.0,    # 最高优先级
    Provenance.OBSERVED: 0.8,
    Provenance.INFERRED: 0.6,     # 默认
    Provenance.AMBIGUOUS: 0.2,    # 最低，优先清理
}

PROVENANCE_RETENTION = {
    Provenance.EXTRACTED: float('inf'),  # 永不清理
    Provenance.OBSERVED: 365,             # 1 年
    Provenance.INFERRED: 180,             # 半年
    Provenance.AMBIGUOUS: 7,              # 7 天后清理
}
```

#### 3.2.2 自动晋升规则

```python
# src/smilex/quality/provenance_promoter.py

class ProvenancePromoter:
    """provenance 自动晋升与降级"""
    
    async def process_all(self, scope: str):
        """定期处理（由调度器触发）"""
        await self._promote_inferred(scope)
        await self._demote_to_ambiguous(scope)
        await self._cleanup_ambiguous(scope)
    
    async def _promote_inferred(self, scope: str):
        """INFERRED → EXTRACTED 的晋升"""
        # 规则 1: 被多次观察（observation_count >= 3）
        await self.db.execute("""
            UPDATE triples 
            SET provenance = 'extracted', updated_at = datetime('now')
            WHERE scope = ? 
              AND provenance = 'inferred'
              AND observation_count >= 3
        """, (scope,))
        
        # 规则 2: 被用户显式确认
        await self.db.execute("""
            UPDATE triples 
            SET provenance = 'extracted', updated_at = datetime('now')
            WHERE scope = ? 
              AND provenance = 'inferred'
              AND user_confirmed = 1
        """, (scope,))
    
    async def _demote_to_ambiguous(self, scope: str):
        """INFERRED → AMBIGUOUS 的降级"""
        # 规则: 多次矛盾未解决
        await self.db.execute("""
            UPDATE triples 
            SET provenance = 'ambiguous', updated_at = datetime('now')
            WHERE scope = ? 
              AND provenance = 'inferred'
              AND id IN (
                  SELECT triple_id FROM contradictions
                  WHERE resolved = 0
                  GROUP BY triple_id
                  HAVING COUNT(*) >= 2
              )
        """, (scope,))
    
    async def _cleanup_ambiguous(self, scope: str):
        """清理超期的 AMBIGUOUS"""
        retention_days = PROVENANCE_RETENTION[Provenance.AMBIGUOUS]
        await self.db.execute("""
            DELETE FROM triples
            WHERE scope = ?
              AND provenance = 'ambiguous'
              AND datetime(updated_at) < datetime('now', ?)
        """, (scope, f'-{retention_days} days'))
```

#### 3.2.3 检索时的 Provenance 加权

```python
# 在混合检索中，按 provenance 调整得分
def provenance_weighted_score(
    base_score: float, provenance: Provenance
) -> float:
    """根据 provenance 调整检索得分"""
    return base_score * PROVENANCE_PRIORITY[provenance]

# 检索时
async def hybrid_search(query: str, top_k: int = 10):
    raw_results = await rrf_fusion(...)
    weighted = [
        (r, provenance_weighted_score(r.score, r.provenance))
        for r in raw_results
    ]
    weighted.sort(key=lambda x: -x[1])
    return [r for r, _ in weighted[:top_k]]
```

### 3.3 增量索引设计

#### 3.3.1 FileIndex 表

```sql
-- src/smilex/storage/schema/007_file_index.sql

CREATE TABLE file_index (
    file_path       TEXT PRIMARY KEY,
    scope           TEXT NOT NULL,
    content_hash    TEXT NOT NULL,              -- SHA256
    file_size       INTEGER,
    last_modified   TEXT,                       -- 文件系统时间
    last_indexed_at TEXT DEFAULT (datetime('now')),
    
    -- 索引统计
    entity_count    INTEGER DEFAULT 0,
    triple_count    INTEGER DEFAULT 0,
    
    -- 错误追踪
    last_error      TEXT,
    last_error_at   TEXT
);

CREATE INDEX idx_file_scope ON file_index(scope);
CREATE INDEX idx_file_hash ON file_index(content_hash);
```

#### 3.3.2 IncrementalIndexer 实现

```python
# src/smilex/bootstrap/incremental_indexer.py
import hashlib
from pathlib import Path

class IncrementalIndexer:
    """增量索引：SHA256 + git-diff 触发"""
    
    CHUNK_SIZE = 50                # 批量处理 50 文件
    MAX_FILE_SIZE = 1024 * 1024    # 1MB 以上跳过
    
    def __init__(self, db, llm, ignore_patterns: list = None):
        self.db = db
        self.llm = llm
        self.ignore = ignore_patterns or self._default_ignore()
    
    async def index_scope(
        self, scope: str, root: Path, git_ref: str = None
    ) -> IndexStats:
        """索引整个 scope（增量）"""
        # 1. 获取待处理文件列表
        if git_ref:
            files = await self._git_diff_files(root, git_ref)
        else:
            files = await self._scan_files(root)
        
        # 2. 过滤 ignore
        files = [f for f in files if not self._should_ignore(f)]
        
        # 3. 批量处理
        stats = IndexStats()
        for batch in self._batch(files, self.CHUNK_SIZE):
            result = await self._process_batch(scope, root, batch)
            stats.merge(result)
        
        return stats
    
    async def _process_batch(
        self, scope: str, root: Path, batch: list[Path]
    ) -> IndexStats:
        """处理一批文件"""
        stats = IndexStats()
        
        for path in batch:
            rel_path = str(path.relative_to(root))
            
            # 1. 读文件
            try:
                content = path.read_text(encoding='utf-8')
            except Exception as e:
                await self._record_error(rel_path, str(e))
                stats.errors += 1
                continue
            
            # 2. 大小检查
            if len(content) > self.MAX_FILE_SIZE:
                stats.skipped_large += 1
                continue
            
            # 3. 计算哈希
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            
            # 4. 查缓存
            cached = await self.db.get_file_index(rel_path)
            if cached and cached.content_hash == content_hash:
                stats.skipped_unchanged += 1
                continue
            
            # 5. 提取实体与三元组
            try:
                entities, triples = await self.llm.extract(
                    content, 
                    source_file=rel_path,
                    scope=scope,
                )
            except Exception as e:
                await self._record_error(rel_path, str(e))
                stats.errors += 1
                continue
            
            # 6. 写入（事务）
            async with self.db.transaction():
                # 删除旧数据（文件已变更）
                if cached:
                    await self.db.delete_by_source_file(rel_path)
                
                # 写入新数据
                await self.db.batch_insert_entities(entities)
                await self.db.batch_insert_triples(triples)
                
                # 更新文件索引
                await self.db.upsert_file_index(FileIndex(
                    file_path=rel_path,
                    scope=scope,
                    content_hash=content_hash,
                    file_size=len(content),
                    last_modified=datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
                    entity_count=len(entities),
                    triple_count=len(triples),
                ))
            
            stats.indexed += 1
            stats.entities += len(entities)
            stats.triples += len(triples)
        
        return stats
    
    def _default_ignore(self) -> list:
        """默认 .smilexignore 规则"""
        return [
            "node_modules/", "__pycache__/", ".venv/", "venv/",
            "dist/", "build/", "target/", "*.egg-info/",
            "*.min.js", "*.min.css", "*.map",
            "*.png", "*.jpg", "*.jpeg", "*.gif", "*.pdf",
            "*.zip", "*.tar.gz",
            "package-lock.json", "yarn.lock", "Pipfile.lock",
            "*.log", "logs/",
            ".git/", ".smilex/",
        ]
    
    def _should_ignore(self, path: Path) -> bool:
        """检查文件是否匹配 ignore 规则"""
        s = str(path)
        for pattern in self.ignore:
            if pattern.endswith('/'):
                if pattern[:-1] in s:
                    return True
            elif pattern.startswith('*.'):
                if path.suffix == pattern[1:]:
                    return True
            elif pattern in s:
                return True
        return False
```

#### 3.3.3 git-diff 集成（可选增强）

```python
async def _git_diff_files(
    self, root: Path, since_ref: str
) -> list[Path]:
    """仅处理 git diff 变更的文件"""
    result = await asyncio.create_subprocess_exec(
        'git', 'diff', '--name-only', since_ref, 'HEAD',
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await result.communicate()
    
    files = []
    for line in stdout.decode().splitlines():
        path = root / line
        if path.exists():
            files.append(path)
    
    return files
```

### 3.4 Triple 表的 source_file 字段

```sql
-- 给 triples 表加 source_file 字段（用于增量删除）
ALTER TABLE triples ADD COLUMN source_file TEXT;
CREATE INDEX idx_triples_source_file ON triples(source_file);

-- 删除某文件的所有三元组
DELETE FROM triples WHERE source_file = ?;
```

---

## 4. 综合实施路线图

### Phase A：MVP 必做（1 周）

| 任务 | 工作量 | 关键产出 |
|------|--------|---------|
| Triple 加 `edge_kind` + `edge_properties` + `provenance` + `source_file` | 2 天 | 升级 schema + dataclass |
| EdgeKind 枚举 + edge_properties schema | 0.5 天 | `models/edge_kind.py` |
| Provenance 枚举 + 晋升规则 | 1 天 | `quality/provenance_promoter.py` |
| FileIndex 表 + IncrementalIndexer | 2 天 | `bootstrap/incremental_indexer.py` |
| `.smilexignore` 默认规则 | 0.5 天 | 配置文件 |
| 迁移脚本（旧数据 → 新 schema）| 1 天 | `migrations/` |

**立竿见影的效果**：
- ✅ 数据模型严谨（预留所有扩展点）
- ✅ 增量索引节省 95% LLM 成本
- ✅ Provenance 精准降噪

### Phase B：MVP 后推荐（2 周）

| 任务 | 工作量 | 关键产出 |
|------|--------|---------|
| 集成 igraph + leidenalg | 0.5 天 | 依赖 |
| CommunityDetector（懒增量）| 2 天 | `quality/community_detector.py` |
| CommunitySummarizer（懒生成 + Haiku）| 1.5 天 | `quality/community_summarizer.py` |
| QueryRouter（Local/Global/DRIFT）| 3 天 | `lifecycle/query_router.py` |
| ProvenancePromoter 定期任务 | 1 天 | `scheduler/` |
| 集成测试（端到端）| 2 天 | `tests/` |

**解锁的能力**：
- ✅ 回答宏观问题
- ✅ 检索效率提升（社区剪枝）
- ✅ 上下文压缩（社区摘要）

### Phase C：按需增强

| 任务 | 触发条件 |
|------|---------|
| Shortcut 边（CONTAINS）| 多跳查询慢时 |
| 层次聚类（multi-level）| Global Search 结果粗糙时 |
| DRIFT Search 优化 | 复杂查询准确率不足时 |
| 声明式 DSL | 查询表达力不足时 |

---

## 5. 关键决策清单

### 5.1 数据模型决策（不可逆，必做对）

| 决策 | 选项 | 推荐 |
|------|------|------|
| Triple 是否有 edge_kind | 字符串 predicate vs 枚举 | **枚举（预留扩展）** |
| edge_properties 是否有 schema | 任意 dict vs TypedDict | **TypedDict（类型安全）** |
| provenance 是否分 4 级 | 二值 vs 四值 | **四值（精准降噪）** |
| Triple 是否有 source_file | 无 vs 有 | **有（支持增量删除）** |
| Triple 是否有 observation_count | 无 vs 有 | **有（自动晋升）** |

### 5.2 算法决策（可调）

| 决策 | 选项 | 推荐 |
|------|------|------|
| Leiden 分辨率 | 0.5 / 1.0 / 2.0 | **1.0（默认）** |
| 最小社区大小 | 3 / 5 / 10 | **5（平衡粒度）** |
| Re-cluster 触发 | 实时 / 增长 15% / 定时 | **增长 15%（性价比高）** |
| 摘要模型 | Opus / Sonnet / Haiku | **Haiku（成本优先）** |
| Global Search 社区上限 | 5 / 20 / 50 | **20（平衡覆盖与成本）** |

### 5.3 工程决策（可逆）

| 决策 | 选项 | 推荐 |
|------|------|------|
| 图算法库 | networkx / igraph | **igraph（性能优 10x）** |
| Leiden 实现 | leidenalg / graspologic | **leidenalg（更活跃）** |
| 查询路由分类 | LLM / 规则 / 混合 | **规则（省成本）** |
| `.smilexignore` 格式 | gitignore 风格 / YAML | **gitignore（用户熟悉）** |

---

## 6. 风险与缓解

### 6.1 数据模型风险

| 风险 | 缓解 |
|------|------|
| EdgeKind 枚举不够用 | MVP 克制（5 种），P2 按需扩展 |
| edge_properties 类型错误 | `validate_edge_properties()` 写入前校验 |
| provenance 误降级 | AMBIGUOUS 7 天宽限期，可恢复 |

### 6.2 算法风险

| 风险 | 缓解 |
|------|------|
| Leiden 聚类质量差 | 监控 `cohesion_score`，< 0.3 时调 resolution |
| 社区摘要幻觉 | 用 EXTRACTED 优先 + 多次确认 |
| Global Search 慢 | 限制 20 社区 + 并行 + Haiku |

### 6.3 成本风险

| 风险 | 缓解 |
|------|------|
| 社区摘要烧钱 | 懒生成 + Haiku + 缓存 |
| Leiden 频繁触发 | 增长 15% 阈值 + 最小 100 实体 |
| 查询路由误判 | 规则分类（不调 LLM）|

---

## 7. 度量与监控

### 7.1 关键指标

```python
@dataclass
class GraphMetrics:
    # 数据质量
    ambiguous_ratio: float           # AMBIGUOUS 占比（目标 < 10%）
    extracted_ratio: float           # EXTRACTED 占比（目标 > 30%）
    
    # 社区健康
    avg_community_size: float        # 平均社区大小（目标 10-50）
    avg_cohesion: float              # 平均内聚度（目标 > 0.3）
    orphan_entities: int             # 不属于任何社区的实体（目标 < 5%）
    
    # 索引效率
    incremental_skip_rate: float     # 增量跳过率（目标 > 90%）
    avg_index_time_ms: float         # 平均索引时间（目标 < 1s/文件）
    
    # 查询效率
    global_search_usage: float       # Global Search 使用率
    avg_query_latency_ms: float      # 平均查询延迟（目标 < 500ms）
```

### 7.2 告警规则

```yaml
alerts:
  - name: ambiguous_ratio_high
    condition: "ambiguous_ratio > 0.2"
    action: "trigger_cleanup_now"
    
  - name: low_cohesion
    condition: "avg_cohesion < 0.2"
    action: "tune_leiden_resolution"
    
  - name: low_incremental_skip
    condition: "incremental_skip_rate < 0.7"
    action: "review_file_changes"
    
  - name: global_search_slow
    condition: "avg_query_latency_ms > 2000"
    action: "reduce_community_limit"
```

---

## 8. 结论

三个思想的协同价值：

```
┌───────────────────────────────────────────────────────────┐
│  思想 1（属性多重图）                                       │
│  → 让数据模型严谨，查询表达力强，扩展性好                   │
│  → 成本：4 天工作量                                        │
│                                                           │
│  思想 2（社区检测）                                        │
│  → 解锁宏观问题能力，检索效率提升                          │
│  → 成本：2 周工作量 + <$1/月运营                           │
│                                                           │
│  思想 3（provenance + 增量索引）                           │
│  → 精准降噪 + 节省 95% LLM 成本                           │
│  → 成本：3 天工作量                                        │
│                                                           │
│  综合效果：                                                │
│  ✅ 数据模型严谨（预留扩展点，避免迁移痛苦）               │
│  ✅ 宏观问题能力（社区检测 + Global Search）              │
│  ✅ 成本可控（增量索引 + 小模型 + 懒生成）                │
│  ✅ 噪声可控（provenance 精准清理）                       │
└───────────────────────────────────────────────────────────┘
```

**实施建议**：
- **Phase A（MVP 必做）**：思想 1 + 思想 3 → 立即见效（降本 + 数据严谨）
- **Phase B（MVP 后）**：思想 2 → 解锁新能力（宏观问题）
- **Phase C（按需）**：层次聚类、shortcut 边、声明式 DSL
