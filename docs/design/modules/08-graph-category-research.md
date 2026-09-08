# 图谱品类深度调研与差距分析

> **配套文档**：[01-layer0-models.md](01-layer0-models.md) ~ [07-graph-noise-control.md](07-graph-noise-control.md)
> **目的**：对比业界 5 大代码/知识图谱品类，识别 SmileX 的不足与优化空间
> **调研方法**：3 个 Agent 并行深度调研（CPG / GraphRAG / AST+KG+企业级），基于官方一手文档

---

## ★ 修订记录(2026-09-08,对照 as-built 复核)

本文写作时(2026-06)的现状快照与成本模型已失真,且部分建议经评审后被
否决或改向。**阅读下文各节时以本节修正为准**:

| 原建议 | as-built 状态(2026-09) | 修订结论 |
|--------|------------------------|---------|
| P0-1 Triple 缺 edge_kind/edge_properties/provenance | **约 70% 伪差距**: triples 已是 15 字段,`relation_type` 7 值 CHECK 就是 EdgeKind 前身(semantic/causal/temporal/spatial 已覆盖,另承担状态跟踪 3 值,见 GAPS D6) | 剩余边类型(REFERENCE/CONTAINS/DERIVED_FROM/CONTRADICTS/SIMILAR_TO)若需要,扩 relation_type CHECK(D6 先例),**不另起 edge_kind 列**;edge_properties/provenance **否决** — certainty 5 值 + confidence 数值 + 07 号文档阈值剪枝已覆盖噪声治理,观察计数已有 fragment 层 access_count 闭环(schema 014) |
| P0-2 增量索引缺失 | **等效落地约 60%**: bulk_importer 以 `text:{sha256[:16]}`/`git:{hash}`/`md:{路径}` 内容寻址 fragment_id,重跑幂等跳过;目录扫描跳过构建目录 + max_files 截断 | §6.1 成本模型前提不成立(导入路径是确定性 AST/git-log 解析,不存在"每周全量 LLM 重提取"基数);`.smilexignore`/git-diff 精细化按需再议 |
| P1 社区检测 | **轻量版已落地**(semantic 任务: nx.connected_components → L3 fragment 缓存);但连通分量是零分辨率 — 记忆图易连成巨分量使输出退化 | **保留但改向**: networkx 内置 `louvain_communities`(不引入 igraph/leidenalg 两个 C 扩展)+ 固定 seed(确定性/幂等要求)+ 摘要复用 Summarizer Protocol(默认规则版零 LLM)+ 结果经 VectorStore 写向量(修复 L3 不进 KNN);保留懒增量触发与懒生成摘要两个设计;Community 数据模型收敛为 EntityCommunity 多对多表(删 member_entity_ids 列表列);"L3.5"命名作废,统一 L3 |
| P1 三模式检索路由(QueryRouter) | 未落地,且设计有 bug:`"?" in query and len(query.split())>10` 对中文永不触发(无空格) | **否决**: 每查询一次 LLM 分类违反"core 零 LLM";Basic/Local 与现有双通道+图谱策略重复;Global 做成 recall 显式 `mode=overview` 参数交给调用方;DRIFT 是 1M token 语料的设计,单库 10K 实体近似多余 |
| P1 Shortcut 边 | 未落地 | **从 P1 删除**(或降至 P3、以 BFS 实测超预算为触发条件): 依赖未落地的 contains 边型与社区检测,150K 实体 + depth≤2 + LIMIT 20 下 CTE BFS 大概率毫秒级 — CPG 技术解决不了的问题在此不存在 |
| P2 声明式 DSL | 未落地 | **否决**: 示例 `where(lambda e: ...)` 自身无法编译为 SQL;relation_type 仅 7 值 + 谓词自由文本撑不起一门语言;2026-09 服务化出口(memory_recall 聚焦参数 + memory_graph_query 结构化工具 + relation_types 过滤)已覆盖其约 80% 真实需求;消费者是 LLM agent,它要参数化工具,不要学私有 DSL |
| P2 SCIP/MCP 适配 | 未做,ADR-018 方向被遵守(源码导入仅 AST,无 CFG/DDG) | 维持 |

ADR-015~018 按上表结论修订后回填主文档 §17(原四个 ADR 从未回填,编号与
11 号文档 ADR-019~022 连续)。§5 路线图与 §8.4 执行顺序已被本节取代。

---

## 0. 执行摘要（先看结论）

### SmileX 当前能力评估

```
┌──────────────────────────────────────────────────────────────┐
│  SmileX 在 5 大品类中的位置                                    │
│                                                              │
│  品类               │ SmileX 能力  │ 关键差距                │
│  ─────────────────────┼──────────────┼────────────────────── │
│  CPG                │ ❌ 无        │ 无代码结构理解（可选）  │
│  AST + KG           │ ❌ 无        │ 无代码符号（可选）      │
│  Graph RAG          │ ⚠️ 部分     │ ★缺社区检测+层次摘要  │
│  Repository KG      │ ✅ 强       │ 已有 scope + 双向指针   │
│  企业级代码图谱     │ ❌ 无        │ 无大规模索引（不需）    │
│  ─────────────────────┼──────────────┼────────────────────── │
│  通用记忆（非代码）  │ ✅ 强       │ 实体/三元组/时空/因果  │
└──────────────────────────────────────────────────────────────┘
```

### 7 个关键不足（按优先级）

> **2026-09 复核**: 下表为 2026-06 视角,其中 3 项 P0 已由 relation_type
> 7 值 + 内容寻址导入**等效落地**(详见顶部修订记录),P1 的
> QueryRouter/Shortcut 边已否决,社区检测改向 networkx Louvain 轻量版。

| 优先级 | 不足 | 来源品类 | 影响 |
|--------|------|---------|------|
| **P0** | Triple 缺 `edge_kind` + `edge_properties` | CPG | 边无类型化，无法区分 AST/因果/引用 |
| **P0** | 关系无 `provenance` 分级（EXTRACTED/INFERRED/AMBIGUOUS）| graphrag-code | 噪声无法精准剪枝 |
| **P0** | 增量索引缺失（SHA256 + git-diff）| graphrag-code/Cursor | 冷启动每次全量提取 |
| **P1** | **无社区检测 + 层次摘要** ★最关键 | GraphRAG | 无法回答宏观问题 |
| **P1** | 检索模式单一（无 Local/Global/DRIFT 路由）| GraphRAG | 查询效率与质量受限 |
| **P1** | 无 shortcut 边（热路径预物化）| CPG | 多跳查询慢 |
| **P2** | 无声明式递归查询 DSL | CodeQL | 复杂查询表达力弱 |

---

## 1. 五大品类核心特征对比

### 1.1 能力矩阵全景

| 能力 | CPG | AST+KG | GraphRAG | RepoKG | 企业级 | **SmileX** |
|------|-----|--------|----------|--------|--------|-----------|
| **多图融合**（共享节点+多边类型）| ✅✅✅ | ⚠️ | ❌ | ❌ | ⚠️ | ❌ |
| **社区检测**（Leiden/Louvain）| ❌ | ❌ | ✅✅✅ | ❌ | ❌ | ❌ |
| **层次摘要**（社区报告）| ❌ | ❌ | ✅✅✅ | ❌ | ❌ | ❌ |
| **检索模式多样**（Local/Global/DRIFT）| ❌ | ❌ | ✅✅✅ | ⚠️ | ⚠️ | ❌ |
| **代码 AST 理解** | ✅✅✅ | ✅✅ | ❌ | ❌ | ✅ | ❌ |
| **控制流/数据流**（CFG/DDG）| ✅✅✅ | ❌ | ❌ | ❌ | ⚠️ | ❌ |
| **符号级精确搜索**（SCIP）| ❌ | ⚠️ | ❌ | ❌ | ✅✅✅ | ❌ |
| **增量索引**（内容寻址+git-diff）| ⚠️ | ✅✅ | ⚠️ | ⚠️ | ✅✅✅ | ❌ |
| **关系置信度分级** | ❌ | ✅✅ | ⚠️ | ❌ | ❌ | ⚠️（仅数值）|
| **实体抽取**（LLM）| ❌ | ⚠️ | ✅✅✅ | ✅✅ | ❌ | ✅✅ |
| **向量检索** | ❌ | ⚠️ | ✅✅ | ❌ | ⚠️ | ✅✅ |
| **跨项目共享**（scope 隔离）| ❌ | ❌ | ❌ | ⚠️ | ⚠️ | ✅✅✅ |
| **因果推理** | ❌ | ❌ | ❌ | ❌ | ❌ | ✅✅✅ |
| **时空索引** | ❌ | ❌ | ❌ | ❌ | ❌ | ✅✅✅ |
| **声明式递归查询** | ✅✅（QL）| ⚠️（Cypher）| ❌ | ❌ | ⚠️ | ⚠️（SQL CTE）|
| **嵌入式零依赖** | ❌（JVM）| ⚠️（Neo4j）| ❌（云端）| ⚠️ | ❌ | ✅✅✅ |

### 1.2 规模与成本对比

| 项目 | 数据规模 | 索引时间 | 内存 | 成本 |
|------|---------|---------|------|------|
| **Joern CPG**（Linux Kernel）| 4754 万节点 | 数小时 | 80-100 GB heap | 高（JVM）|
| **CodeQL**（大型项目）| 数百万 facts | 分钟-小时 | 中 | 商业授权 |
| **Microsoft GraphRAG**（1M token）| ~10K 实体 | 几分钟 | 中 | **$300-700 LLM** |
| **graphrag-code**（中型仓库）| ~10K 节点 | 秒级 | 低 | **零 LLM** |
| **Sourcegraph Cody**（千万行）| 亿级向量 | 增量 | 6 GiB cache | 中 |
| **SmileX**（10 项目/年）| ~150K 实体 | 增量 | < 100 MB | **本地 BGE-M3** |

---

## 2. 详细差距分析

### 2.1 CPG 品类（Joern、CodeQL）

**核心创新**：**多图融合** — AST + CFG + DDG + CDG + CG 共享同一组节点，仅边类型不同。

**Joern 14 层 schema 精华**：
- MetaData / FileSystem / Namespace / Method / Type / Ast / Cfg / Pdg / Dominators / Comment / Finding / TagsAndLocation / Configuration / Binding / Annotation / Base
- 边类型：AST / CFG / CALL / ARGUMENT / RECEIVER / REACHING_DEF / CDG / DOMINATE / CONDITION / INHERITS_FROM / ALIAS_OF / BINDS_TO / REF / CONTAINS / EVAL_TYPE / PARAMETER_LINK

**SmileX 差距**：
| 差距 | 严重度 | 是否需补 |
|------|--------|---------|
| Triple 无 `edge_kind` 枚举 | **高** | ✅ 必补（迁移成本低）|
| Triple 无 `edge_properties` 字典 | **高** | ✅ 必补 |
| 无 shortcut 边（CONTAINS/EVAL_TYPE）| 中 | ✅ P1 补 |
| 无 AST/CFG/DDG（代码结构）| 低 | ❌ 不补（领域错配）|
| 无声明式递归 DSL | 中 | ⚠️ P2 可选 |

**可迁移的核心思想**：
1. **多图融合** → SmileX 可让 Entity 共享节点池，Triple 用 `edge_kind` 区分（语义/因果/时序/引用/包含）
2. **Shortcut 边** → 对"实体→所属会话""实体→最近更新"等高频查询预物化
3. **属性化边** → 因果边带"传播权重"、时序边带"时间戳"、引用边带"上下文位置"

### 2.2 Graph RAG 品类（Microsoft GraphRAG）★最关键差距

**核心创新**：**社区检测 + 层次摘要** 解决"全局宏观问题"。

**Pipeline**：
```
TextUnits → LLM 抽取实体/关系 → 图嵌入 → Leiden 层次聚类 → 社区摘要 → 4 种检索
```

**Leiden 算法三阶段**：
1. **Local moving**：节点按 ∆H 增益移入邻居社区
2. **Refinement**（Louvain 没有）：保证社区 well-connected
3. **Aggregation**：社区聚合成超级节点，回到第 1 步

**复杂度**：近似线性 O(|E|)，百万级边秒级完成。

**4 种检索模式**：
| 模式 | 起点 | 适用场景 |
|------|------|---------|
| **Local Search** | 命中实体 → 邻居 + chunks + claims | "X 的属性？" |
| **Global Search** | 全社区摘要 map-reduce | "整个数据集主题？" |
| **DRIFT Search** | 实体 + 社区混合 → 拆分 sub-query | 既需广度又需深度 |
| **Basic Search** | top-k 向量 | 简单事实查询 |

**SmileX 差距**：
| 差距 | 严重度 | 是否需补 |
|------|--------|---------|
| **无社区检测** | **极高** | ✅ P1 强烈推荐补到 L3 |
| **无层次摘要** | **极高** | ✅ P1 补 |
| **检索模式单一**（仅混合检索）| 高 | ✅ P1 加 Local/Global/DRIFT 路由 |
| **无全局视角查询** | 高 | ✅ P1 补 |

**关键论据**：
> SmileX 当前平铺设计的最大风险是**"无法回答宏观问题"**——例如"用户最近一个月主要在做什么？""这个项目的核心主题是什么？"这类问题，朴素向量+KNN 永远答不全。GraphRAG 论文证明：在 ~1M token 数据集上，对全局问题，GraphRAG 在 comprehensiveness 和 diversity 上显著超越 Baseline RAG。

**成本控制策略**：
- **懒增量社区检测**：实体数增长 ≥10% 才 re-cluster，不要每次写入都跑
- **懒生成社区摘要**：首次被查询时才生成，缓存
- **小模型抽取**：用 Haiku/4o-mini 做抽取与摘要，Opus 只用于最终回答
- **预算估算**：SmileX 单项目 ~10K 实体，社区检测 O(N) 秒级，社区摘要约 50 次 LLM 调用（< $1）

### 2.3 AST + KG 品类（graphrag-code、KGraph）

**核心创新**：
- **graphrag-code**：tree-sitter + Neo4j，**确定性索引（无 LLM）**，边带 `EXTRACTED/INFERRED/AMBIGUOUS` 置信度
- **KGraph**：scip-clang + SQLite + FTS5 + 递归 CTE，证明 SQLite 完全能承载图查询

**graphrag-code 数据模型**：
- 15 节点：Package / File / Class / Method / Function / Interface / Endpoint / Column / GraphQLOperation / Event / Atom / EnvVar / Route / External / EdgeGroup
- 30 边：IMPORTS / DEFINES_CLASS / HAS_METHOD / EXPOSES / EXTENDS / IMPLEMENTS / CALLS / HANDLES / EMITS_EVENT / TESTS / ...

**SmileX 差距**：
| 差距 | 严重度 | 是否需补 |
|------|--------|---------|
| **关系无 provenance 分级** | **高** | ✅ P0 必补 |
| **无增量索引**（SHA256 + git-diff）| **高** | ✅ P0 必补（降冷启动成本）|
| **无代码符号实体类型** | 低 | ⚠️ P2 预留（不实现）|
| **ingest 路径用 LLM**（graphrag-code 避免）| 中 | ⚠️ 通用记忆必须用 LLM |

**可迁移的核心思想**：
1. **关系 provenance 分级** → SmileX 的 Triple 加 `provenance: EXTRACTED/INFERRED/AMBIGUOUS`，LLM 提取的默认 INFERRED，用户确认后晋升 EXTRACTED，清理时优先剪 AMBIGUOUS
2. **增量索引** → 冷启动时对文件计算 SHA256，缓存提取结果，仅对变更文件重新提取
3. **SQLite 完全够用** → KGraph 证明 SQLite + 递归 CTE + FTS5 能承载 N-hop 邻域查询，SmileX 选型正确

### 2.4 企业级代码图谱（Sourcegraph SCIP、Cursor）

**核心创新**：
- **SCIP 协议**：Protobuf 格式，**字符串 Symbol ID**（限制 bug 爆炸半径），87 种 Kind
- **Cursor**：embedding + `.cursorignore` + 多根 workspace
- **Cody 增量 embeddings**：文件级增量，S3/GCS 托管

**SmileX 差距**：
| 差距 | 严重度 | 是否需补 |
|------|--------|---------|
| 无 SCIP 协议支持 | 低 | ❌ 不补（领域错配）|
| 无大规模分布式索引 | 低 | ❌ 不补（个人场景）|
| 无 `.cursorignore` 式配置 | 中 | ⚠️ P1 可加 `.smilexignore` |

**可迁移的核心思想**：
1. **Symbol ID 用带语法的字符串** → SmileX 实体 ID 现在用 ULID，对代码符号可考虑 `smilex py pkg mod Class.method` 格式（限制命名漂移）
2. **增量索引 = 内容寻址 + git-diff** → 文件 SHA256 缓存 + `--since <ref>` 仅处理 diff
3. **避免 ingest 路径引入 LLM** → SmileX 通用记忆必须用 LLM，但代码理解若要做，必须走 tree-sitter / SCIP

### 2.5 Repository KG 品类

**SmileX 已有强项**：
- ✅ `MemoryScope` 字段（project/tenant/global）
- ✅ `source_closet` 双向指针（文档 ↔ 事实）
- ✅ 触发器维护的 `project_current_state`

**差距**：
| 差距 | 严重度 | 是否需补 |
|------|--------|---------|
| 无自动文档生成 | 低 | ⚠️ P2 可选 |
| 无决策追踪（Decision 实体）| 中 | ⚠️ P1 可加 |

---

## 3. 具体优化建议（含数据模型扩展）

### 3.1 P0 必做：Triple 升级为属性多重图（来自 CPG + graphrag-code）

**现状**：
```python
@dataclass
class Triple:
    subject_id: str
    predicate: str              # 仅字符串
    object_id: str
    confidence: float           # 仅数值
    certainty: CertaintyLevel
```

**升级后**：
```python
class EdgeKind(StrEnum):
    """边类型枚举（来自 CPG 多图融合思想）"""
    SEMANTIC = "semantic"           # 语义关系（默认）
    CAUSAL = "causal"               # 因果（A 导致 B）
    TEMPORAL = "temporal"           # 时序（A 先于 B）
    SPATIAL = "spatial"             # 空间（A 位于 B 内）
    REFERENCE = "reference"         # 引用（A 引用 B）
    CONTAINS = "contains"           # 包含（shortcut 边）
    DERIVED_FROM = "derived_from"   # 派生（A 从 B 提取）
    CONTRADICTS = "contradicts"     # 矛盾
    SIMILAR_TO = "similar_to"       # 相似

class Provenance(StrEnum):
    """关系来源分级（来自 graphrag-code）"""
    EXTRACTED = "extracted"     # 用户/工具明确确认
    INFERRED = "inferred"       # LLM 推断（默认）
    AMBIGUOUS = "ambiguous"     # 模糊/低置信度
    OBSERVED = "observed"       # 从行为观察

@dataclass
class Triple:
    # 原有字段
    subject_id: str
    predicate: str
    object_id: str
    confidence: float
    certainty: CertaintyLevel
    
    # ★ 新增字段
    edge_kind: EdgeKind = EdgeKind.SEMANTIC
    edge_properties: dict = field(default_factory=dict)  # 类型化属性
    provenance: Provenance = ProvenANCE.INFERRED
    observation_count: int = 0    # 被观察次数（晋升 EXTRACTED 用）
```

**示例**：
```python
# 因果边带传播权重
Triple(
    subject_id="redis_crash",
    predicate="causes",
    object_id="service_down",
    edge_kind=EdgeKind.CAUSAL,
    edge_properties={"weight": 0.85, "lag_seconds": 30},
    provenance=Provenance.INFERRED,
)

# 引用边带上下文位置
Triple(
    subject_id="meeting_note_001",
    predicate="references",
    object_id="decision_auth",
    edge_kind=EdgeKind.REFERENCE,
    edge_properties={"line": 42, "context": "如前所述"},
    provenance=Provenance.EXTRACTED,
)
```

### 3.2 P0 必做：增量索引（来自 graphrag-code + Cursor）

**现状**：冷启动每次全量提取实体，浪费 LLM 调用。

**升级后**：
```python
@dataclass
class FileIndex:
    file_path: str
    content_hash: str           # SHA256
    last_indexed_at: datetime
    entity_count: int
    triple_count: int

class IncrementalIndexer:
    async def index_project(self, project_id: str, git_ref: str = None):
        """增量索引：只处理变更文件"""
        # 1. 获取文件列表（可选 git-diff）
        if git_ref:
            changed_files = await self._git_diff_files(git_ref)
        else:
            changed_files = await self._all_files(project_id)
        
        # 2. 过滤已索引且未变更的
        to_index = []
        for f in changed_files:
            content = await self._read(f)
            hash = sha256(content)
            existing = await db.get_file_index(f)
            if existing and existing.content_hash == hash:
                continue  # 未变更，跳过
            to_index.append((f, content, hash))
        
        # 3. 批量提取（仅变更文件）
        for f, content, hash in to_index:
            entities, triples = await llm.extract(content)
            await db.write_with_hash(f, hash, entities, triples)
```

**配套：`.smilexignore`**（来自 Cursor 的 `.cursorignore`）：
```
# 跳过大型生成文件
*.min.js
*.map
node_modules/
dist/
build/
__pycache__/

# 跳过二进制
*.png
*.jpg
*.pdf
```

### 3.3 P1 强烈推荐：社区检测 + 层次摘要（来自 GraphRAG）★最关键

**新增 Layer：L3.5 社区层**

```
L0 工作记忆
L1 短时记忆
L2 长时记忆
L3 语义记忆
L3.5 ★新增 社区记忆（层次摘要）  ← GraphRAG 思想
```

**数据模型扩展**：
```python
@dataclass
class Community:
    id: str
    level: int                  # 层次（0=最细，N=最粗）
    parent_id: Optional[str]    # 父社区
    member_entity_ids: List[str]
    summary: str                # LLM 生成的社区摘要
    summary_embedding: Optional[np.ndarray]
    member_count: int
    cohesion_score: float       # 社区内聚度
    created_at: datetime
    updated_at: datetime

@dataclass
class EntityCommunity:
    """实体↔社区 多对多映射"""
    entity_id: str
    community_id: str
```

**算法实现**（用 igraph 或 networkx）：
```python
from igraph import Graph
from leidenalg import find_partition, RBConfigurationVertexPartition

class CommunityDetector:
    MIN_ENTITIES = 50            # 至少 50 实体才检测
    GROWTH_THRESHOLD = 0.10      # 增长 10% 才 re-cluster
    
    async def detect_communities(self, scope: str):
        """懒增量社区检测"""
        # 1. 检查是否需要 re-cluster
        entity_count = await db.count_entities(scope)
        last_clustered = await db.get_last_clustered(scope)
        
        if last_clustered:
            growth = (entity_count - last_clustered.entity_count) / last_clustered.entity_count
            if growth < self.GROWTH_THRESHOLD:
                return  # 未显著增长，跳过
        
        if entity_count < self.MIN_ENTITIES:
            return  # 数据太少，不检测
        
        # 2. 构建图
        edges = await db.get_all_triple_edges(scope)
        g = Graph.TupleList(edges, weights=True)
        
        # 3. Leiden 层次聚类
        partition = find_partition(
            g, RBConfigurationVertexPartition,
            resolution_parameter=1.0,
            n_iterations=-1,  # 收敛为止
        )
        
        # 4. 存储社区（含层次结构）
        await self._store_communities(scope, partition)
    
    async def _store_communities(self, scope, partition):
        """存储层次社区 + 懒生成摘要"""
        for level, comm in enumerate(partition.hierarchical_levels()):
            for comm_id, members in comm.items():
                community = Community(
                    id=str(ULID()),
                    level=level,
                    member_entity_ids=members,
                    summary="",  # 懒生成
                    ...
                )
                await db.insert_community(community)
    
    async def get_or_generate_summary(self, community_id: str) -> str:
        """懒生成社区摘要"""
        community = await db.get_community(community_id)
        if community.summary:
            return community.summary
        
        # 首次访问才生成
        members = await db.get_entities_by_ids(community.member_entity_ids)
        summary = await llm.summarize_community(members)  # 用小模型
        await db.update_community_summary(community_id, summary)
        return summary
```

### 3.4 P1 强烈推荐：三模式检索路由（来自 GraphRAG）

**新增查询路由器**：
```python
class QueryRouter:
    """根据查询类型路由到不同检索模式"""
    
    async def route(self, query: str, scope_filter: ScopeFilter) -> RecallResponse:
        # 1. 分类查询类型
        query_type = await self._classify_query(query)
        
        if query_type == "global":
            # "用户最近主要在做什么？" → 全社区摘要 map-reduce
            return await self._global_search(query, scope_filter)
        elif query_type == "drift":
            # 复杂问题 → 拆分 sub-query + 混合
            return await self._drift_search(query, scope_filter)
        elif query_type == "local":
            # 单实体深挖 → KNN 扩展
            return await self._local_search(query, scope_filter)
        else:
            # simple → 向量检索
            return await self._basic_search(query, scope_filter)
    
    async def _classify_query(self, query: str) -> str:
        """LLM 分类查询类型"""
        # 简单规则 + LLM 兜底
        if any(w in query for w in ["所有", "整体", "主要", "最近", "总结"]):
            return "global"
        if "?" in query and len(query.split()) > 10:
            return "drift"
        # ... 更多规则
        return "local"
    
    async def _global_search(self, query: str, scope: ScopeFilter) -> str:
        """Global Search: 全社区摘要 map-reduce"""
        communities = await db.get_top_level_communities(scope)
        
        # Map: 每个社区生成 partial answer
        partial_answers = await asyncio.gather(*[
            llm.answer_with_context(query, await community.get_summary())
            for community in communities
        ])
        
        # Reduce: 合并
        return await llm.synthesize(query, partial_answers)
```

### 3.5 P1 推荐：Shortcut 边（来自 CPG）

**新增预物化边表**：
```sql
CREATE TABLE shortcut_edges (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,         -- CONTAINS_SESSION, LAST_UPDATED_BY, ...
    weight REAL DEFAULT 1.0,
    created_at TEXT,
    PRIMARY KEY (source_id, target_id, edge_type)
);

-- 由触发器自动维护
CREATE TRIGGER trg_update_shortcut_contains
AFTER INSERT ON triples
WHEN NEW.edge_kind = 'contains'
BEGIN
    INSERT OR REPLACE INTO shortcut_edges (source_id, target_id, edge_type)
    VALUES (NEW.subject_id, NEW.object_id, 'CONTAINS_SESSION');
END;
```

**常用 shortcut 类型**：
- `CONTAINS_SESSION`：实体 → 包含它的会话
- `LAST_UPDATED_BY`：实体 → 最近一次更新的消息
- `CO_OCCURS_WITH`：高频共现实体
- `SAME_COMMUNITY`：同社区实体

### 3.6 P2 可选：代码符号预留（来自 SCIP）

**预留但不实现**：
```python
class EntityType(StrEnum):
    PERSON = "person"
    LOCATION = "location"
    OBJECT = "object"
    CONCEPT = "concept"
    EVENT = "event"
    TECH = "tech"
    CODE_SYMBOL = "code_symbol"  # ★ 预留，P2 实现
```

**未来实施方式**：
- **不自建解析器**
- 通过 **MCP 工具适配器** 消费外部 SCIP 索引（KGraph / graphrag-code）
- 把 `SymbolInformation` 映射为 SmileX 的 `Entity`，`Relationship` 映射为 `Triple`

### 3.7 P2 可选：声明式查询 DSL（来自 CodeQL）

```python
# 类比 CodeQL 的 .ql
class MemoryQuery:
    """声明式递归查询 DSL"""
    
    def __init__(self):
        self._steps = []
    
    @classmethod
    def from_node(cls, node_id: str) -> 'MemoryQuery':
        q = cls()
        q._steps.append(('start', node_id))
        return q
    
    def follow(self, edge_kind: EdgeKind, closure: str = None) -> 'MemoryQuery':
        """
        closure:
          None = 单跳
          '+'  = 一跳或以上（传递闭包）
          '*'  = 零跳或以上
        """
        self._steps.append(('follow', edge_kind, closure))
        return self
    
    def where(self, predicate) -> 'MemoryQuery':
        self._steps.append(('filter', predicate))
        return self
    
    def select(self, fields: list = None) -> list:
        """编译为 SQL（含 WITH RECURSIVE）"""
        sql = self._compile_to_sql()
        return db.execute(sql)

# 使用示例
results = (
    MemoryQuery
    .from_node("redis_crash")
    .follow(EdgeKind.CAUSAL, "+")           # 传递闭包找所有后果
    .where(lambda e: e.certainty >= "high")
    .select()
)
```

---

## 4. 更新后的能力矩阵

| 能力 | 原 SmileX | 优化后 SmileX |
|------|----------|--------------|
| 实体抽取（LLM）| ✅ | ✅ |
| 三元组存储 | ✅ RDF 风格 | ✅ **属性多重图** |
| 关系类型化 | ❌ 仅字符串 | ✅ **EdgeKind 枚举** |
| 关系属性 | ❌ | ✅ **edge_properties** |
| Provenance 分级 | ⚠️ 仅 confidence | ✅ **EXTRACTED/INFERRED/AMBIGUOUS** |
| 增量索引 | ❌ 全量提取 | ✅ **SHA256 + git-diff** |
| 时序检索 | ✅ | ✅ |
| 空间检索 | ✅ R-tree | ✅ |
| 图谱检索 | ✅ 递归 CTE | ✅ |
| 因果检索 | ✅ | ✅ |
| 向量检索 | ✅ ChromaDB | ✅ |
| 混合检索 | ✅ RRF | ✅ |
| **社区检测** | ❌ | ✅ **Leiden（P1）** |
| **层次摘要** | ❌ | ✅ **社区报告（P1）** |
| **多模式检索** | ⚠️ 单一 | ✅ **Local/Global/DRIFT（P1）** |
| **Shortcut 边** | ❌ | ✅ **预物化热路径（P1）** |
| 跨项目共享 | ✅ scope | ✅ |
| 矛盾检测 | ✅ | ✅ |
| 历史归档 | ✅ | ✅ |
| 项目冷启动 | ✅ 六机制 | ✅ + **增量索引** |
| 声明式 DSL | ❌ | ⚠️ **P2 可选** |
| 代码符号 | ❌ | ⚠️ **P2 MCP 适配** |

---

## 5. 实施路线图

> **2026-09 修订**: 原三阶段路线已被顶部修订记录取代 — P0 两项标记
> 等效落地,P1 收敛为"社区检测 Louvain 轻量版"单项,P2 DSL/Shortcut
> 否决。保留下表仅为历史参考。

### P0（MVP 必做，1 周）

| 任务 | 工作量 | 来源 |
|------|--------|------|
| Triple 加 `edge_kind` + `edge_properties` + `provenance` | 2 天 | CPG + graphrag-code |
| 文件 SHA256 缓存 + `.smilexignore` | 2 天 | Cursor |
| 增量索引（仅处理变更文件）| 2 天 | graphrag-code |
| 现有三元组迁移脚本 | 1 天 | — |

### P1（强烈推荐，2 周）

| 任务 | 工作量 | 来源 |
|------|--------|------|
| 集成 igraph + leidenalg | 1 天 | GraphRAG |
| CommunityDetector（懒增量）| 3 天 | GraphRAG |
| 社区摘要懒生成（小模型）| 2 天 | GraphRAG |
| QueryRouter（Local/Global/DRIFT）| 3 天 | GraphRAG |
| Shortcut 边 + 触发器 | 2 天 | CPG |
| 集成测试 | 3 天 | — |

### P2（可选，按需）

| 任务 | 工作量 | 来源 |
|------|--------|------|
| 声明式 DSL（MemoryQuery）| 1 周 | CodeQL |
| MCP 工具适配器（消费 SCIP）| 1 周 | KGraph |
| CodeSymbol 实体类型 | 3 天 | SCIP |

---

## 6. 成本影响分析

### 6.1 P0 增量索引的节省

**场景**：10 项目，每项目 1000 文件，平均每周变更 5%。

| 方案 | LLM 调用/周 | 成本/月（BGE-M3 + Haiku）|
|------|-----------|------------------------|
| 全量提取 | 10,000 | ~$5 |
| **增量索引** ★ | **500** | **~$0.25** |

**节省**：95% LLM 成本。

### 6.2 P1 社区检测的成本

**场景**：单项目 10K 实体。

| 操作 | 频率 | 成本 |
|------|------|------|
| Leiden 聚类 | 增长 10% 触发（~每月）| 0（纯算法）|
| 社区摘要生成 | 懒生成（~100 社区）| ~$0.5（Haiku）|
| QueryRouter 分类 | 每次查询 | ~$0.001（Haiku）|

**总增量成本**：< $1/项目/月。**价值**：解锁"宏观问题"能力。

### 6.3 总体 TCO（10 项目/年）

| 阶段 | 存储成本 | LLM 成本 | 总计 |
|------|---------|---------|------|
| 原 MVP | ~$0（本地）| ~$60 | $60 |
| **+ P0 增量索引** | ~$0 | ~$3 | **$3**（-95%）|
| **+ P1 社区检测** | ~$0 | ~$15 | **$18** |
| **完整版** | ~$0 | ~$18 | **$18** |

---

## 7. 关键决策（ADR 补充）

> **2026-09 修订**: 四个 ADR 按修订结论回填主文档 §17(原文未回填,
> 编号与 11 号文档 ADR-019~022 连续)。各 ADR 末尾的
> "**2026-09 状态**"行以 as-built 为准。

### ADR-015: Triple 升级为属性多重图（借鉴 CPG）

- **选择**：Triple 加 `edge_kind` 枚举 + `edge_properties` 字典 + `provenance` 分级
- **理由**：CPG 的多图融合是验证过的设计；graphrag-code 的 provenance 分级精准降噪
- **权衡**：Triple 表 schema 变复杂，但查询能力大幅提升
- **回退**：保留 `edge_kind='semantic'` 默认值，向后兼容
- **2026-09 状态**: **部分等效落地,余项否决** — relation_type 7 值 CHECK
  已承担 EdgeKind 语义(含状态跟踪扩展,D6 先例);edge_properties/
  provenance 否决(certainty+confidence+阈值剪枝已覆盖);剩余边类型
  按需扩 relation_type CHECK,不另起列

### ADR-016: 引入社区检测（借鉴 GraphRAG）

- **选择**：L3.5 社区层，用 igraph + leidenalg，懒增量 + 懒生成摘要
- **理由**：解决"宏观问题"能力缺失；GraphRAG 论文证明有效性
- **权衡**：增加约 15% 代码复杂度；社区摘要需 LLM 调用
- **缓解**：小模型 + 懒生成 + 增量触发，成本可控（< $1/项目/月）
- **回退**：若社区摘要质量差，关闭自动摘要，仅保留聚类用于检索加速
- **2026-09 状态**: **改向后保留** — 连通分量轻量版已落地(semantic
  任务,L3 fragment 缓存);升级路径改为 networkx 内置
  louvain_communities + 固定 seed(不引入 igraph/leidenalg)+ Summarizer
  Protocol 摘要 + VectorStore 写向量(修 L3 不进 KNN);"L3.5"命名作废

### ADR-017: 增量索引（借鉴 graphrag-code + Cursor）

- **选择**：文件 SHA256 缓存 + `.smilexignore` + 可选 git-diff
- **理由**：减少 95% LLM 调用；冷启动速度提升 10x
- **权衡**：需维护文件哈希表
- **回退**：无（向后兼容，首次全量建立缓存）
- **2026-09 状态**: **等效落地** — bulk_importer 内容寻址 fragment_id
  (text:{sha256} / git:{hash} / md:{路径})重跑幂等;成本模型前提
  (周期性全量 LLM 重提取)不成立,导入路径为确定性解析

### ADR-018: 不自建代码图谱（借鉴 SCIP 生态）

- **选择**：预留 `CodeSymbol` 实体类型，通过 MCP 适配器消费外部 SCIP 索引
- **理由**：SCIP 生态成熟（87 种 Kind、10 种索引器）；自建会爆炸
- **权衡**：依赖外部工具（KGraph/graphrag-code）
- **回退**：若 MCP 生态不成熟，P3 评估自建精简版（仅 tree-sitter）
- **2026-09 状态**: **维持并遵守** — 源码导入仅 AST 产 file:/class:/
  tech: 实体,无 CFG/DDG;MCP 适配器按需再做,"不自建、做消费者"原则
  同样适用于查询语言(否决 DSL)与依赖选型(否决 igraph)

---

## 8. 结论

### 8.1 核心判断

**SmileX 在"通用记忆"赛道定位正确**，但与业界图谱品类对比后发现 **3 个 P0 必补 + 3 个 P1 强烈推荐**的不足。

### 8.2 优化后的定位

```
┌─────────────────────────────────────────────────────────────┐
│  SmileX 优化后的独特价值                                      │
│                                                             │
│  ✅ 通用记忆（非代码专属）                                    │
│  ✅ 嵌入式零依赖（SQLite + cachebox）                        │
│  ✅ 多图融合（Triple 属性多重图）                             │
│  ✅ 社区检测 + 层次摘要（GraphRAG 思想）★新增                │
│  ✅ 多模式检索（Local/Global/DRIFT）★新增                    │
│  ✅ 增量索引（SHA256 + git-diff）★新增                       │
│  ✅ 跨项目共享（scope + 自动提升）                            │
│  ✅ 因果 + 时空 + 向量一体化                                  │
│  ✅ MCP 适配外部代码图谱（不自建）                            │
│                                                             │
│  → 在"个人 Agent 记忆"细分市场无直接竞品                     │
└─────────────────────────────────────────────────────────────┘
```

### 8.3 关键警示

1. **不要试图成为 CPG**：Joern 4754 万节点 + 80GB heap 的路线与 SmileX 嵌入式定位冲突
2. **不要全盘照搬 GraphRAG**：1M token $300-700 的 indexing 成本不可接受，必须用懒增量 + 小模型
3. **不要自建代码图谱**：SCIP 生态已成熟，做消费者即可

### 8.4 推荐执行顺序

> **2026-09 修订版**(原版已被顶部修订记录取代):

```
已完成(2026-09):
  服务化出口 — memory_recall entity/time 聚焦参数 +
  memory_graph_query(path/neighbors/causal + relation_types 过滤),
  L2 图谱/时序策略接出服务层;导入路径内容寻址幂等(增量索引等效)

下一步(若推进,1 周内):
  社区检测轻量版 — semantic 任务从连通分量升级为 networkx
  louvain_communities(固定 seed),摘要走 Summarizer Protocol,
  结果经 VectorStore 写向量(修 L3 不进 KNN) — 这是 P1 里唯一
  站得住的推进理由(巨分量退化是当前轻量版价值有限的根因)

否决/搁置:
  DSL(参数化工具覆盖) · QueryRouter(零 LLM 决策 + 中文规则 bug)
  · Shortcut 边(依赖链不自洽,规模错配) · igraph/leidenalg 依赖
```

---

## 附录：调研来源（一手资料）

### CPG
- Joern CPG 规范：https://cpg.joern.io/
- Joern 文档：https://docs.joern.io/
- CodeQL 文档：https://codeql.github.com/docs/

### Graph RAG
- 论文：arXiv:2404.16130 *From Local to Global*
- 官方文档：https://microsoft.github.io/graphrag/
- Leiden 算法：https://en.wikipedia.org/wiki/Leiden_algorithm

### AST + KG / 企业级
- SCIP schema：https://github.com/sourcegraph/scip/blob/main/scip.proto
- SCIP 设计：https://github.com/sourcegraph/scip/blob/main/docs/DESIGN.md
- Cursor 索引：https://docs.cursor.com/context/codebase-indexing
- Cody code graph：https://docs.sourcegraph.com/cody/explanations/code_graph_context
- graphrag-code：https://github.com/cognitx-leyton/codegraph
- KGraph：https://github.com/ajksunkang-aios/KGraph
