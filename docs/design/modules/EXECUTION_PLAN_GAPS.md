# SmileX MVP 执行计划缺口评估

> **产出阶段**: 第一阶段(项目骨架 + 依赖验证)
> **更新日期**: 2026-06-21
> **目的**: 系统性识别设计文档中的实施级缺口,为后续详细执行计划提供决策依据

---

## 0. 决策记录(本会话产出)

### D1: 向量存储用 sqlite-vec(方案一,修正 ADR-002)

**决策**: MVP 用 **sqlite-vec**,不装 ChromaDB。

**权威依据**: [`docs/analyse/embedding-layer.md`](../../analyse/embedding-layer.md)「方案1: SQLite + sqlite-vec + NetworkX」是第一梯队推荐,sqlite-vec 适配度 10/10。该文档**修正了 ADR-002**(ADR-002 写"MVP 用 ChromaDB")。

**待办**:
- [x] 主文档 §17 ADR-002 应更新为「MVP 用 sqlite-vec,ChromaDB 不再纳入」— 已更新(2026-08-21)
- [x] §14.2 MVP 阻塞性依赖条目应明确为「sqlite-vec Windows 可用性」,删除 cachebox 之外的项 — 已标注 Windows 验证通过(2026-08-21)
- [x] §19.1 技术栈表应把 ChromaDB 移除或降级为 P5 备选 — 已降级并删除行(2026-08-21)

### D2: NetworkX 锁定为 L3 语义层选型(方案一)

**决策**: MVP 起即纳入依赖清单,提前在 Windows 上验证,但实际接入发生在 P2。

**权威依据**: embedding-layer.md 给 NetworkX 10/10 适配度,与 sqlite-vec 并列。

### D3: 技术演进路线锁定

| 阶段 | 技术栈 |
|------|--------|
| MVP-P1 | SQLite + sqlite-vec + NetworkX |
| P2 | 增加 Community + Pattern(基于 NetworkX 算法) |
| P3 | 引入 DuckDB 做分析层(OLTP→SQLite, OLAP→DuckDB) |
| P4 | 代码图谱暴涨时引入 Kuzu |
| P5 | 做 SaaS 时引入 Qdrant |

### D4: L0 工作记忆缓存用 LRUCache(放弃 TTL)

**决策**: L0 缓存使用 `cachebox.LRUCache`,**不启用 TTL**。会话过期由应用层主动调用 `cache.clear()` 或 `cache.pop(session_id)` 清理。

**理由**:
- API 简单(无 `global_ttl` 参数干扰)
- LRU 淘汰策略与"工作记忆保留最近活跃会话"语义一致
- TTL 过期在 Agent 场景不刚需(会话通常显式开始/结束)
- 应用层清理点明确:`MemoryMiddleware.close_session(session_id)` / 进程退出钩子
- 性能最优(cachebox Cython 加速 + 纯 LRU 复杂度 O(1))

**实施影响**:
- Layer 2 L0 实现用 `LRUCache(maxsize=1000)`
- 会话结束/项目卸载时由上层主动调用 `pop()` / `clear()`
- 文档 §7.2 代码示例需重写

**待办**:
- [x] 主文档 §7.2 更新代码示例 — 已改为 LRUCache 真实 API(2026-08-21)
- [x] §17 ADR-012 标注此偏离 — 已补充 API 漂移说明(2026-08-21)
- [x] Layer 2 实施时使用 LRUCache — 已落实(`src/smilex/memory/lifecycle/l0_working_memory.py`)

### D6: scope 字段存"全路径" + relation_type 扩展状态跟踪值(L1.1 决策)

**决策**:
1. **scope 列存全路径**: `"project:abc"` / `"tenant:xyz"` / `"global"`,不是 §5.2 MemoryScope 枚举的简单字符串
2. **relation_type 扩展**: 在 §5.2 四值基础上,加入 §6.3 触发器需要的 `'project_state'/'task_status'/'config'`

**理由**:
- §6.3 触发器语义是 `NEW.scope LIKE 'project:%'`,需要在同一列携带类型 + ID
- §6.3 触发器 WHEN 子句使用 `relation_type IN ('project_state', 'task_status', 'config')`,与 §5.2 的 4 值冲突
- MVP 阶段允许 relation_type 并存(图谱语义 + 状态跟踪),P1 重构时考虑引入独立的 `predicate_category` 列

**实施影响**:
- DDL: 所有含 scope 列的表 CHECK 改为 `scope = 'global' OR scope LIKE 'project:%' OR scope LIKE 'tenant:%'`
- DDL: triples 表 relation_type CHECK 扩展为 7 值
- Layer 1 StorageEngine(L1.7): 需实现 Layer 0 enum ↔ Layer 1 全路径互转的适配器
- Layer 0 MemoryScope enum 不变(保持 Python API 简洁)
- Layer 0 ScopeFilter.matches() 已支持 scope_id 参数,适配 OK

**待办**:
- [x] 主文档 §6.3 触发器与新 DDL 对齐(已对齐,文档无需改)
- [x] 主文档 §5.2 备注 scope 列存全路径,Layer 0 ↔ Layer 1 转换说明 — 已补充(2026-08-21)
- [x] P1 阶段重构:引入 predicate_category 列(技术债)— **关闭为 wontfix(2026-09-08)**:
      7 值合并的 relation_type 已稳定运行(全库查询按集合过滤,如
      `relation_type IN ('project_state','task_status','config')`),拆列收益
      仅语义纯度,代价是 schema 迁移 + 触发器/查询/测试全链改动。
      ADR-015(主文档 §17)采纳同一结论:剩余边类型按需扩 CHECK,不另起列。
      若未来状态跟踪值继续膨胀再重开此项。

---

## 1. 总体结论

| 文档分区 | 完整度 | 是否可立即实施 |
|---------|--------|--------------|
| §1-§2 问题陈述 / 系统上下文 | ✅ 完整 | 可立即实施 |
| §3 公共 API 契约 | ⚠️ 部分缺失 | 需补全 DTO |
| §4 分层架构总览 | ✅ 完整 | 可立即实施 |
| §5 Layer 0 数据模型 | ✅ 完整 | 可立即实施(以主文档 §5 为准) |
| §6 Layer 1 存储引擎 | ⚠️ DDL 不完整 | 需融合主文档字段 + 模块文档 DDL |
| §7 Layer 2 生命周期 | ⚠️ 配置细节模糊 | 需明确 cachebox 参数与 ChromaDB 替换方案 |
| §8-§9 Layer 3 调度 | ✅ MVP 范围清晰 | 可立即实施 |
| §9.5 项目冷启动 | ⚠️ 类型未定义 | 需补 ProjectContext/ImportResult 等 |
| §10-§11 Layer 4/5 | — | 不在 MVP 范围 |
| §12 关键算法 | ✅ 6 个算法齐全 | 可立即实施 |
| §13 模块边界 | ✅ 清晰 | 可立即实施 |
| §14 路线图 | ✅ 清晰 | 但 MVP 子任务粒度过粗 |
| §17 ADR | ⚠️ ADR-002 已被 D1 修正 | 需更新 |

---

## 2. 接口层缺口 (§3)

主文档 §3.2 只列出 5 个完整 DTO,以下未定义。MVP 范围内必需的优先补全:

### 2.1 必需(MVP 阻塞)

```python
@dataclass
class WriteResponse:
    memory_id: str
    status: WriteStatus              # 枚举未定义:saved/conflict/partial
    layers_affected: List[MemoryLayer]
    conflict: Optional[ConflictInfo]  # ConflictInfo 类型未定义
    written_at: datetime

@dataclass
class ProjectInitResponse:
    scope: str
    entity_count: int
    triple_count: int
    stage: BootstrapStage            # 枚举未定义:onboarding/template/active_learning/ready
    elapsed_ms: int

@dataclass
class MemoryRef:
    id: str
    layer: MemoryLayer
    score: float
    scope: MemoryScope
    snippet: Optional[str]           # 检索结果片段

@dataclass
class TripleInput:
    """用户输入版 Triple(省略服务端填充字段)"""
    subject_id: Optional[str]        # 可空,允许服务端从 content 提取
    subject_name: Optional[str]
    predicate: str
    object_id: Optional[str]
    object_value: Optional[str]
    certainty: CertaintyLevel = CertaintyLevel.MEDIUM
```

### 2.2 可推迟到 P1

- `InferRequest / InferResponse`
- `TraceRequest / TraceResponse`
- `PromoteResponse`
- `ConflictInfo`(并发冲突细节,Layer 4 才用)
- `WriteStatus`、`BootstrapStage` 枚举

---

## 3. 实施层缺口

### 3.1 DDL 不完整 (Layer 1)

主文档 §6.1 仅给字段摘要,模块文档 02 提供了 DDL 但字段命名与主文档不一致。**需要融合冻结后才能实施**。

| 表名 | 主文档 §6.1 字段 | 模块文档 02 字段 | 缺口 |
|------|----------------|-----------------|------|
| `entities` | `id, entity_id, entity_type, name, scope, valid_from, valid_to, embedding, source_closet` | `id, name, entity_type, scope, properties, source_scope, created_at` | 字段命名冲突:`entity_id` vs 无;`embedding` vs `properties`;`source_closet` vs `source_scope`。需统一以主文档为准 |
| `triples` | `id, triple_id, subject_id, predicate, object_id, scope, valid_from, valid_to, predecessor_id, confidence, source_closet` + 14 个枚举/类型字段 | `id, subject_id, predicate, object_id, object_value, scope, valid_from, valid_to, certainty, created_at` | 模块文档缺 `triple_id, predecessor_id, causal_level, relation_type, source_closet`。需以主文档为准 |
| `locations` | `id, location_id, name, location_type, parent_id, path, coordinates` | `id, lng, lat, scope, memory_id`(改名 `memory_locations`) | 表名冲突;字段定义完全不同。需以主文档为准 + R-tree 字段 |
| `temporal_fragments` | `id, fragment_id, time_start, time_end, location_id, content, embedding, entities` | `id, time_start, time_end, content, scope, layer, created_at` | 模块文档缺 `fragment_id, location_id, embedding, entities` |
| `causal_chains` | `id, chain_id, node_ids, chain_type, confidence, support_count` | `id, cause_id, effect_id, strength, scope, created_at` | 数据模型完全不同(链式 vs 二元关系)。需以主文档为准 |
| `vector_links` | `id, vector_db, vector_id, embedding, entity_id, triple_id, fragment_id` | `memory_id, collection_name, created_at` | 字段完全不同。**重要**: 由于 D1 改用 sqlite-vec,该表应改为存 `sqlite-vec` rowid 映射,而非 ChromaDB collection |
| `checkpoints` | `id, task_id, progress, step, state, cursor` | 一致 | 无缺口 |
| `project_current_state` ★ | 主文档 §6.3 完整 DDL + 触发器 | — | 无缺口 |
| `memory_l0_snapshot` ★ | `session_id, data, updated_at, expires_at` | `session_id, data BLOB, last_updated` | 字段名差异:`updated_at` vs `last_updated`;主文档有 `expires_at` |

**建议行动**: 创建 `src/smilex/memory/storage/schema/001_*.sql` ~ `009_*.sql` 时,**以主文档 §6.1 + §6.3 字段为准**,模块文档 02 仅作参考。

### 3.2 数据模型冲突 (Layer 0)

主文档 §5 vs 模块文档 01-layer0-models.md:

| 类型 | 主文档 §5 | 模块文档 01 | 推荐 |
|------|---------|-----------|------|
| `LockType` 值 | SHARED / EXCLUSIVE / UPDATE | READ / WRITE | 用主文档(Layer 4 才会用到,MVP 可暂不实现) |
| `ConflictType` 值 | VERSION_STALE / WRITE_WRITE / READ_WRITE / CAUSAL_CYCLE / CAUSAL_CONTRADICTION | CONCURRENT_UPDATE / SCHEMA_MISMATCH / VERSION_CONFLICT | 用主文档(Layer 4 才会用到) |
| `PreemptionPolicy` 值 | NONE / IMMEDIATE / GRACEFUL / COOPERATIVE / PRIORITY_INHERITANCE | IMMEDIATE / GRACEFUL / COOPERATIVE | 用主文档 |
| `Entity` 字段 | `id, entity_id, entity_type, name, scope, valid_from, valid_to, location, embedding, source_closet` | `id, name, entity_type, scope, properties, source_scope, created_at` | 用主文档 |
| `Triple` 字段 | 14 个字段(含 `triple_id, predecessor_id, causal_level, certainty, relation_type, source_closet`) | 10 个字段 | 用主文档 |
| `FuzzyMemory` 字段 | `id, content, content_embedding, time_range, location, entities, relations, scope, importance, emotion_weight, access_count, layer, created_at, updated_at, expires_at` | `id, content, scope, layer, time_range, location, entities, importance, certainty, created_at` | 用主文档(更完整) |

**建议行动**: 实施 Layer 0 时,**以主文档 §5 为唯一权威**,模块文档 01 仅作技术选型参考(StrEnum、ULID、MessagePack 等推荐采纳)。

### 3.3 向量存储选型(ADR-002 已被 D1 修正)

**问题**: ADR-002 写"MVP 用 ChromaDB,未来评估切 sqlite-vec",但 §1.2 原则 2「单库一体化 > 多库拼装」与 ChromaDB 独立存储的本质冲突。

**决策**: 见 D1 — 修正 ADR-002 为「MVP 用 sqlite-vec」。

**影响**:
- `vector_links` 表的 `vector_db` 字段含义变更(从 "chromadb" 变为 "sqlite-vec")
- `VectorStore` 接口实现简化(无需双写)
- §7 Layer 2 的 "L1 ChromaDB" 改为 "L1 sqlite-vec"

### 3.4 ProjectBootstrap 辅助类型未定义 (§9.5)

```python
# 以下类型在主文档 §9.5 出现但未定义
class ImportSource: ...        # 输入源:Git/Markdown/对话历史
class CloneFilter: ...         # 跨项目克隆过滤
class ProjectContext: ...      # 项目上下文(含 scope/stage 等)
class ImportResult: ...        # 导入结果
class CloneResult: ...         # 克隆结果
```

**建议**: MVP 必需 `ProjectContext`,其余可推迟到 P1(批量导入)/ P2(跨项目克隆)。

**状态(2026-08-21)**: `ProjectContext` 已于 MVP 落地(middlewares/dto.py);`ImportSource`/`ImportResult` 已于 P1-b 落地(`scheduler/bootstrap/bulk_importer.py`,支持 git/markdown/text);`CloneFilter`/`CloneResult` 已于 P2-a 落地(`scheduler/bootstrap/cross_project_cloner.py`,实体类型/层/时间过滤 + ID 重映射 + 溯源)。

### 3.5 Embedding 方案未指定

**问题**: sqlite-vec 不自带 embedding,需要外部模型把文本转向量。

**缺口**: 主文档 §19 技术栈表写 "Embedding: BGE-M3 / E5(本地)",但未明确:
- 用哪个 Python 库?(`sentence-transformers`?`FlagEmbedding`?ONNX runtime?)
- 模型下载策略?(首次启动从 HuggingFace 拉?)
- 维度(§6.4 写 `FLOAT[1536]` 是 OpenAI 维度,BGE-M3 是 1024 维 — 冲突)

**建议**: 在 Layer 2 实施前,先决策:
- 维度统一为 1024(BGE-M3)
- 库:`sentence-transformers`(成熟度最高)
- 模型下载:首次启动从 HF Hub 拉,缓存到 `~/.smilex/models/`

### 3.6 LLM provider 未指定(冷启动/主动学习)

**问题**: §9.5 主动学习需要 LLM 提取实体/三元组,但未指定 provider。

**建议**:
- 接口抽象(`LLMProvider` Protocol),MVP 可用关键词提取兜底(零 LLM 依赖)
- 实际 LLM 接入留到 P1,通过 OpenAI 兼容 API

### 3.7 SLA 验收方法学未定义

**问题**: §14.2 写"Top-5 召回率 > 60%",但没说怎么测。

**缺口**:
- 没有标注数据集
- 没有 benchmark 脚本
- 没有 metric 定义

**建议**: 创建 `tests/benchmarks/recall_at_k.py`,用 5-10 个典型项目作为 golden set,人工标注 Top-K 应该召回的记忆。

### 3.8 ADR-013 服务暴露方式明确暂缓

**影响**: §2.1 系统上下文图标注"HTTP API / Python SDK / MCP(待定)",MVP 阶段不暴露 HTTP,仅作为 Python SDK 使用。

### 3.9 cachebox API 漂移(★ 实测发现,文档需修正)

**问题**: 主文档 §7.2 引用 `TTLPrefixCache(maxsize=1000, ttl=3600, policy="lru").set(k, v).get(k)`,但 cachebox 6.1.2 实际 API 完全不同:

| 文档假设 | cachebox 6.1.2 实际 |
|---------|-------------------|
| `TTLPrefixCache` 类 | ❌ 不存在 |
| TTL+LRU 合并类 | ❌ 不提供(只有独立的 `TTLCache` / `LRUCache`) |
| `.set(k, v)` 写入 | ❌ 应为 `.insert(k, v)` |
| `ttl=` 关键字参数 | ❌ `TTLCache` 用 `global_ttl=`,`VTTLCache` 用 `ttl=`(per-key) |

**实际可用 API**:
```python
from cachebox import TTLCache, LRUCache, VTTLCache, FIFOCache, LFUCache, RRCache

# 全局 TTL 缓存(所有 key 同一 TTL)
cache = TTLCache(maxsize=1000, global_ttl=3600)
cache.insert("k", "v")
cache.get("k")

# 纯 LRU 缓存(无 TTL)
cache = LRUCache(maxsize=1000)
cache.insert("k", "v")

# 可变 TTL(每个 key 单独 TTL)
cache = VTTLache(maxsize=1000)
cache.insert("k", "v", ttl=60)
```

**对 §7.2 L0 工作记忆架构的影响**:
- 原 L0 设计假设的"TTL+LRU 合并"无法直接实现
- 四个备选方案(详见决策 D4 已选 B):
  - **方案 A**: 用 `TTLCache` 放弃 LRU 淘汰(简单,但 maxsize 行为可能不符 LRU 预期)
  - **方案 B** ✅ **(已选)**: 用 `LRUCache` 放弃 TTL(会话不过期,需应用层主动清理)
  - **方案 C**: 自定义 `TLRUCache(TTLCache)` 子类,在 `insert` 时维护 LRU 链(开发成本高)
  - **方案 D**: 切换到 `cachetools.TTLCache`(API 更接近文档假设,但 cachebox 的 Cython 加速优势消失)

**待办**:
- [x] 主文档 §7.2 代码示例改为真实 API(LRUCache + 应用层清理)— 已更新(2026-08-21)
- [x] §17 ADR-012 补充 cachebox 6.x API 漂移说明 — 已补充(2026-08-21)
- [x] 决策 L0 用哪个方案 → **B(见 D4)**

---

## 4. 是否需要详细执行计划文档?

**结论**: **需要**。建议创建 [`docs/design/modules/12-mvp-execution-plan.md`](12-mvp-execution-plan.md)。

### 4.1 现有文档的不足

- 主文档 §14.2 MVP 范围只列了 6 个高层项,没有细化到任务级别
- 缺乏任务依赖图(哪个先做哪个后做)
- 缺乏每周交付物定义
- 缺乏风险应对 playbook(Windows 兼容性问题、模型下载失败等)

### 4.2 建议的 12-mvp-execution-plan.md 结构

```markdown
# MVP 执行计划

## 1. 任务分解(WBS)
[列出 30-50 个原子任务,每个任务 0.5-2 天]

## 2. 任务依赖图
[Mermaid Gantt 图]

## 3. 周交付物
- Week 1: Layer 0 + Layer 1 schema
- Week 2: Layer 1 查询 + Layer 2 + MemoryMiddleware

## 4. 风险与应对
- sqlite-vec Windows 兼容性
- cachebox Cython 编译
- 模型下载失败

## 5. 验收 checklist
[对应 §14.2 退出标准的具体测试用例]
```

---

## 5. 建议的下一步

按优先级排序:

### 5.1 立即可做(无阻塞)

1. ✅ **Layer 0 数据模型实现**(以主文档 §5 为准,StrEnum 化 + ULID + MessagePack 序列化)— **2026-06-21 完成**
   - 6 StrEnums + 7 dataclasses + msgpack 序列化
   - 58 个单元测试全过
   - 详见 `src/smilex/memory/models/` 与 `tests/unit/`
2. **Layer 1 完整 DDL 编写**(以主文档 §6.1 字段为准,融合模块文档 02 的 DDL 语法)— **下一阶段**
3. **更新 ADR-002**(修正为 sqlite-vec)— **待文档侧处理**

### 5.2 需先补文档

1. **Layer 2 实施前**: 决策 Embedding 方案(见 §3.5)
2. **Layer 3 冷启动前**: 定义 `ProjectContext` 类型(见 §3.4)
3. **验收前**: 创建 benchmark 数据集(见 §3.7)

### 5.3 建议下一会话做

1. **创建 `12-mvp-execution-plan.md`**(详细 WBS + Gantt)
2. **更新主文档**(ADR-002 修正 + §19.1 技术栈表 + §14.2 阻塞性依赖)
3. **开始 Layer 0 实施**(可独立完成,不依赖其他层)

---

## 6. Windows 依赖验证结果

**测试时间**: 2026-06-21
**Python**: CPython 3.13.11 (uv managed)
**uv**: 0.9.18
**平台**: Windows 11 Home China 10.0.22631

### 6.1 依赖安装

`uv sync --extra dev` 成功安装 39 个包(耗时 ~5 分钟,主要是 numpy/mypy/ruff 下载)。

| 包 | 版本 | 状态 |
|---|------|------|
| python-ulid | 3.1.0 | ✅ |
| msgpack | 1.2.1 | ✅ |
| pydantic | 2.13.4 | ✅ |
| pydantic-settings | 2.14.2 | ✅ |
| aiosqlite | 0.22.1 | ✅ |
| **sqlite-vec** ★ | 0.1.9 | ✅ |
| **cachebox** ★ | 6.1.2 | ✅(但 API 漂移,见 §3.9) |
| tiktoken | 0.13.0 | ✅ |
| numpy | 2.4.6 | ✅ |
| networkx | 3.6.1 | ✅ |
| pyyaml | 6.0.3 | ✅ |
| structlog | 26.1.0 | ✅ |

### 6.2 烟雾测试结果

```
============================= 38 passed in 2.19s ==============================
```

**全部 38 个测试通过**(10 个测试文件,覆盖所有 12 个 runtime 依赖 + smilex 包结构)。

特别确认的阻塞性项:
- ✅ `test_sqlite_vec.py::test_sqlite_vec_load` — C 扩展加载成功
- ✅ `test_sqlite_vec.py::test_sqlite_vec_knn` — vec0 虚拟表 + KNN 查询正确返回近邻
- ✅ `test_rtree.py` — R-tree 创建 + bbox 空间查询正确
- ✅ `test_cachebox.py` — TTL 过期 + LRU 淘汰均生效(使用真实 API,见 §3.9)

### 6.3 Ruff lint

`uv run ruff check src/ tests/` — **All checks passed!**

### 6.4 结论

**MVP 阻塞性依赖在 Windows 上全部可用**,可以放心进入 Layer 0 实施。

cachebox API 漂移问题(§3.9)已通过 **决策 D4(方案 B:LRUCache)** 解决,Layer 2 实施无阻塞。Layer 0 / Layer 1 / Layer 2 / Layer 3 均可推进。

---
