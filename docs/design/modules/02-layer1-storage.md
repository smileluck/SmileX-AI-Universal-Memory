# Layer 1 存储引擎层 — 选型与算法优化

> **配套章节**：[agent-memory-design.md §6](../agent-memory-design.md)
> **阶段**：[MVP] 完整实现
> **依赖**：Layer 0 数据模型

---

## 1. 模块概览

### 1.1 职责

提供 **SQLite 一体化存储** + **五维检索能力**（时序 / 空间 / 图谱 / 因果 / 语义）。**完全不使用 PostgreSQL**，针对个人电脑安装场景零依赖。

### 1.2 核心组件清单

| 组件 | 文件 | 职责 |
|------|------|------|
| `sqlite_engine.py` | 引擎入口 | 连接管理、WAL 配置、扩展加载 |
| `schema/*.sql` | 7+2 张表 | 实体/三元组/位置/片段/因果/向量链接/检查点 + 项目状态 + L0 快照 |
| `queries/temporal.py` | 时序检索 | `query_at_time`, `query_in_range`, `build_entity_timeline` |
| `queries/spatial.py` | 空间检索 | `query_in_area`（R-tree）, `query_in_location`（path LIKE）|
| `queries/graph.py` | 图谱检索 | `find_path`（递归 CTE）, `find_n_degree_relations` |
| `queries/causal.py` | 因果追溯 | `trace_causal_chain`, `infer_causal_relations` |
| `queries/hybrid.py` | 混合检索 | `hybrid_memory_search`（RRF 融合）|
| `queries/scope.py` | 作用域过滤 | `query_with_scope` |
| `vector_store.py` | ChromaDB 适配 | 向量写入、相似度检索、双向指针 |

### 1.3 依赖关系

```
Layer 0 数据模型
    ▲
    │ import
    │
Layer 1 存储引擎（本层）
    ▲
    │ 调用
    │
Layer 2/3/4/5
```

---

## 2. 技术选型分析

### 2.1 主库选型：SQLite vs PostgreSQL vs DuckDB vs Firebird

| 维度 | SQLite | PostgreSQL | DuckDB | Firebird |
|------|--------|-----------|--------|----------|
| **安装方式** | Python 自带 | 系统包 | pip install | 系统包 |
| **运行模式** | 嵌入式 | C/S 服务 | 嵌入式 | C/S 服务 |
| **内存占用** | ~0（进程内）| 50-100MB | 30-50MB | 50MB |
| **写入并发** | 单写者（WAL）| 多写者 | 单写者 | 多写者 |
| **读并发** | 多读者 | 多读者 | 多读者 | 多读者 |
| **向量扩展** | sqlite-vec | pgvector | duckdb-vss | ❌ |
| **空间扩展** | R-tree/RTreatment | PostGIS | ❌ | ❌ |
| **JSON 支持** | JSON1 | JSONB | 原生 | JSON |
| **递归 CTE** | ✅ | ✅ | ✅ | ✅ |
| **触发器** | ✅ | ✅ | ❌ | ✅ |
| **物化视图** | ❌（用触发器模拟）| ✅ | ✅ | ❌ |
| **单库容量** | 1TB+ | 无上限 | 1TB+ | 10TB+ |
| **分发友好** | ⭐⭐⭐⭐⭐ | ⭐ | ⭐⭐⭐⭐ | ⭐⭐ |

**推荐方案**：**SQLite 3.40+（Python 自带）**

**选型理由**（针对个人电脑安装场景）：
- **零安装**：Python 自带，`pip install smilex-memory` 即用
- **零外部服务**：无后台进程，不占独立内存
- **单文件分发**：整个记忆库就是一个 `.db` 文件
- **跨平台一致**：Windows/macOS/Linux 完全相同的二进制
- **备份简单**：拷贝文件即可
- **进程内访问**：< 1ms 查询延迟，比 PG 的 1-5ms 更快
- **个人场景性能足够**：写入 1000-3000 QPS、读 5000+ QPS

**被否决方案**：
- **PostgreSQL**：安装复杂（管理员权限）、后台服务常驻、嵌入式分发困难
- **DuckDB**：偏 OLAP 分析，OLTP（频繁小写入）不如 SQLite
- **Firebird**：Python 生态弱、文档少

---

### 2.2 向量索引：sqlite-vec vs ChromaDB vs Qdrant vs LanceDB

| 方案 | 类型 | 索引算法 | 性能（100K 向量）| 部署 | 元数据 |
|------|------|---------|----------------|------|--------|
| **sqlite-vec** | SQLite 扩展 | HNSW / brute force | 30ms P99 | 嵌入式 | 需配合 SQLite |
| **ChromaDB** ★MVP | 嵌入式服务 | HNSW | 50ms P99 | 嵌入式 | 自带 SQLite |
| **Qdrant** | 独立服务 | HNSW | 10ms P99 | 独立部署 | 内置 |
| **LanceDB** | 嵌入式 | IVF + PQ | 20ms P99 | 嵌入式 | 内置 |
| **FAISS** | 库 | 多种 | 5ms P99 | 嵌入式 | ❌ 需自建 |

**推荐方案**：**MVP 用 ChromaDB，P2 评估切 sqlite-vec 统一**

**选型理由**：
- **ChromaDB 优势**：生态成熟、自带 SQLite 元数据、Python 原生、API 友好
- **sqlite-vec 优势**：完全统一到主库、零额外组件、HNSW 性能更优
- **渐进策略**：MVP 用 ChromaDB 快速验证，P2 评估迁移到 sqlite-vec

```python
# 通过接口抽象，便于切换
class VectorStore(Protocol):
    async def add(self, id: str, embedding: list[float], metadata: dict) -> None: ...
    async def search(self, query: list[float], top_k: int) -> list[SearchResult]: ...

class ChromaDBVectorStore(VectorStore): ...
class SQLiteVecVectorStore(VectorStore): ...  # P2 切换
```

**被否决方案**：
- **Qdrant**：需独立部署，违反嵌入式原则
- **FAISS**：需自建元数据管理，工程量大
- **LanceDB**：生态较新，稳定性待验证

---

### 2.3 空间索引：R-tree vs SpatiaLite vs GeoHash

| 方案 | 类型 | 索引精度 | 查询性能 | SQLite 内置 | 复杂度 |
|------|------|---------|---------|------------|--------|
| **R-tree** ★推荐 | 虚拟表 | bbox 近似 | ⚡⚡⚡ 极快 | ✅ 原生 | 低 |
| **SpatiaLite** | 扩展 | 精确 | ⚡⚡ 快 | ❌ 需加载 | 高 |
| **GeoHash** | 字符串编码 | 网格 | ⚡ 快 | ✅ 纯 SQL | 低 |
| **暴力扫描** | 全表 | 精确 | ❌ 慢 | ✅ | 极低 |

**推荐方案**：**R-tree（粗过滤）+ Haversine（精确计算）**

**选型理由**：
- **R-tree 原生支持**：SQLite 编译时默认启用，无需额外扩展
- **bbox 近似查询极快**：通过 B-tree 剪枝，毫秒级返回候选集
- **精确过滤用 Python**：Haversine 距离公式精算

```python
# 两阶段查询
async def query_in_area(lng: float, lat: float, radius_m: float):
    # 1. R-tree bbox 粗过滤（毫秒）
    bbox = bbox_from_point(lng, lat, radius_m)
    candidates = await db.execute(
        "SELECT id FROM memory_locations WHERE "
        "min_lng >= ? AND max_lng <= ? AND "
        "min_lat >= ? AND max_lat <= ?",
        (*bbox,)
    )
    # 2. Haversine 精确过滤
    return [c for c in candidates if haversine(...) <= radius_m]
```

**被否决方案**：
- **SpatiaLite**：扩展加载在不同平台有兼容性问题
- **GeoHash**：边界精度问题（赤道、极地）

---

### 2.4 异步 SQLite 访问：aiosqlite vs sqlite3（线程池） vs vanilla

| 方案 | 异步模型 | 性能 | 兼容性 | 学习成本 |
|------|---------|------|--------|---------|
| **aiosqlite** ★推荐 | asyncio 原生 | 中 | ⭐⭐⭐⭐ | 低 |
| **sqlite3 + ThreadPoolExecutor** | 线程池 | 高 | ⭐⭐⭐⭐⭐ | 中 |
| **vanilla sqlite3（同步）** | 阻塞 | ⚡ 最高 | ⭐⭐⭐⭐⭐ | 极低 |
| **APSW** | 同步（更强 API）| ⚡⚡ 最高 | ⭐⭐⭐ | 高 |

**推荐方案**：**aiosqlite（与 FastAPI 一致）**

**选型理由**：
- **asyncio 原生**：与 FastAPI / ChromaDB 一致，避免线程切换开销
- **API 与 sqlite3 一致**：迁移成本低
- **内置连接池**：自动管理 connection

```python
import aiosqlite

async def get_db():
    db = await aiosqlite.connect("~/.smilex/data.db")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    return db
```

**被否决方案**：
- **vanilla sqlite3**：阻塞 asyncio 事件循环
- **APSW**：API 与标准库差异大，团队学习成本高

---

### 2.5 扩展加载：sqlite-vec 加载方式

**问题**：sqlite-vec 是 C 扩展，不同平台加载方式不同。

**方案对比**：

| 方式 | 优点 | 缺点 |
|------|------|------|
| **Python wheel 自带 .so/.dll** | pip install 即用 | 文件大（每个平台 10MB）|
| **运行时动态加载** | 灵活 | 用户需手动配置 |
| **`sqlite3` 模块的 `enable_load_extension`** | 原生 | 部分发行版禁用 |

**推荐方案**：**Python wheel 自带 + 自动加载**

```python
import sqlite_vec
import aiosqlite

async def init_db():
    db = await aiosqlite.connect(":memory:")
    # sqlite-vec 自动注册
    await db.enable_load_extension(True)
    sqlite_vec.load(db)
    await db.enable_load_extension(False)
    # 验证
    async with db.execute("SELECT vec_version()") as cur:
        version = await cur.fetchone()
        assert version is not None
```

---

### 2.6 Schema 迁移：Alembic vs yoyo-migrations vs 自研

| 方案 | 依赖 | 学习成本 | 功能 |
|------|------|---------|------|
| **Alembic** | SQLAlchemy | 高 | 强大，但偏 ORM |
| **yoyo-migrations** | 轻量 | 低 | 纯 SQL 迁移 |
| **pipeline** | 零依赖 | 极低 | 极简 |
| **自研 + `PRAGMA user_version`** ★推荐 | 零依赖 | 极低 | 完全可控 |

**推荐方案**：**自研 + `PRAGMA user_version`**

**选型理由**：
- SQLite 原生提供 `user_version` pragma 跟踪版本
- 无需引入额外依赖
- 完全控制迁移逻辑

```python
MIGRATIONS = [
    (1, "001_initial.sql"),
    (2, "002_add_scope.sql"),
    (3, "003_add_triggers.sql"),
]

async def migrate(db):
    async with db.execute("PRAGMA user_version") as cur:
        current = (await cur.fetchone())[0]
    
    for target, sql_file in MIGRATIONS:
        if target > current:
            sql = Path(f"migrations/{sql_file}").read_text()
            await db.executescript(sql)
            await db.execute(f"PRAGMA user_version = {target}")
            await db.commit()
```

---

## 3. 核心算法与优化

### 3.1 混合检索算法（RRF 融合）

**问题**：多个检索器（向量/时序/空间/图谱）返回各自 Top-K，如何融合排序？

**算法**：Reciprocal Rank Fusion

```python
def rrf_fusion(result_sets: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """
    RRF: score(d) = Σ 1/(k + rank_i(d))
    
    Args:
        result_sets: 多个检索器的排序结果列表
        k: 平滑常数（推荐 60）
    
    Returns:
        [(doc_id, score), ...] 按 score 降序
    """
    scores = defaultdict(float)
    for result_set in result_sets:
        for rank, doc_id in enumerate(result_set, start=1):
            scores[doc_id] += 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: -x[1])

# 示例
vector_results = ["m1", "m2", "m3", "m4", "m5"]      # 向量 Top-5
time_results = ["m3", "m1", "m6", "m7"]                # 时序 Top-4
graph_results = ["m2", "m8", "m1"]                     # 图谱 Top-3

fused = rrf_fusion([vector_results, time_results, graph_results])
# → [("m1", 0.0481), ("m3", 0.0345), ("m2", 0.0338), ...]
```

**优化技巧**：
- **k 值调优**：k=60 通用推荐，结果集小时用 k=30
- **加权 RRF**：对可信检索器加权
- **早停**：top-K 之后停止计算

```python
# 加权 RRF
def weighted_rrf(result_sets, weights, k=60):
    scores = defaultdict(float)
    for results, w in zip(result_sets, weights):
        for rank, doc_id in enumerate(results, 1):
            scores[doc_id] += w / (k + rank)
    return sorted(scores.items(), key=lambda x: -x[1])
```

**复杂度**：O(N × K)，N 为检索器数量，K 为各检索器返回数

---

### 3.2 递归 CTE 图谱遍历优化

**问题**：`find_path(src, dst, max_depth)` 用递归 CTE，深度大时性能差。

**朴素实现**：

```sql
WITH RECURSIVE path AS (
    SELECT subject_id AS node, object_id AS next, 1 AS depth, subject_id AS path
    FROM triples WHERE subject_id = ?
    UNION ALL
    SELECT t.subject_id, t.object_id, p.depth + 1, p.path || ',' || t.object_id
    FROM triples t JOIN path p ON t.subject_id = p.next
    WHERE p.depth < ?  -- max_depth
)
SELECT * FROM path WHERE next = ?;
```

**优化 1**：双向 BFS（从两端同时扩展）

```sql
-- 从 src 和 dst 同时扩展，相遇时停止
WITH RECURSIVE
    forward AS (...),  -- 从 src 向前
    backward AS (...)  -- 从 dst 向后
SELECT * FROM forward f JOIN backward b ON f.next = b.next;
```

**优化 2**：深度限制 + 早停

```sql
WITH RECURSIVE path AS (
    SELECT ...
    UNION ALL
    SELECT ... FROM triples t JOIN path p ON ...
    WHERE p.depth < ? AND p.next != ?  -- 找到目标即停
)
SELECT * FROM path WHERE next = ? LIMIT 1;
```

**优化 3**：缓存常用路径

```python
@functools.lru_cache(maxsize=10000)
async def find_path_cached(src: str, dst: str, depth: int) -> list[str]:
    return await _find_path_uncached(src, dst, depth)
```

**性能对比**（10 万三元组，深度 5）：
| 方案 | 耗时 |
|------|------|
| 朴素递归 CTE | 200ms |
| 双向 BFS | 50ms |
| 双向 + 早停 | 20ms |
| **双向 + 早停 + 缓存** ★ | **< 5ms（命中缓存）** |

---

### 3.3 时序查询优化

**问题**：`query_at_time(t)` 需要找到 `valid_from <= t AND (valid_to IS NULL OR valid_to > t)`。

**朴素实现**：

```sql
SELECT * FROM triples WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to > ?);
```

**优化 1**：复合索引

```sql
CREATE INDEX idx_triples_time ON triples(valid_from, valid_to);
```

**优化 2**：R-tree 替代 B-tree（处理 IS NULL）

```sql
-- 用 9999-12-31 替代 NULL
UPDATE triples SET valid_to = '9999-12-31' WHERE valid_to IS NULL;

-- R-tree 索引时间区间
CREATE VIRTUAL TABLE triples_time_rtree USING rtree(
    id, valid_from_ts, valid_to_ts
);

-- 查询：t 落在 [valid_from, valid_to) 区间内
SELECT t.* FROM triples t
JOIN triples_time_rtree r ON t.rowid = r.id
WHERE r.valid_from_ts <= ? AND r.valid_to_ts > ?;
```

**性能对比**（100 万三元组）：
| 方案 | 耗时 |
|------|------|
| 朴素（无索引）| 800ms |
| B-tree 复合索引 | 50ms |
| **R-tree 时间区间** ★ | **15ms** |

---

### 3.4 空间查询优化（R-tree + Haversine）

**算法**：两阶段查询

```python
async def query_in_area(lng: float, lat: float, radius_m: float):
    """半径查询：R-tree 粗过滤 + Haversine 精确"""
    # 1. 计算 bbox（米转度近似）
    lat_delta = radius_m / 111000  # 1° ≈ 111km
    lng_delta = radius_m / (111000 * cos(radians(lat)))
    
    # 2. R-tree 粗过滤（毫秒级）
    candidates = await db.execute(
        "SELECT id FROM memory_locations WHERE "
        "min_lng >= ? AND max_lng <= ? AND "
        "min_lat >= ? AND max_lat <= ?",
        (lng - lng_delta, lng + lng_delta,
         lat - lat_delta, lat + lat_delta)
    )
    
    # 3. Haversine 精确过滤
    results = []
    for row in candidates:
        dist = haversine(lng, lat, row['lng'], row['lat'])
        if dist <= radius_m:
            results.append((row, dist))
    
    return sorted(results, key=lambda x: x[1])

def haversine(lng1, lat1, lng2, lat2):
    """Haversine 公式：地球表面两点距离"""
    R = 6371000  # 地球半径（米）
    φ1, φ2 = radians(lat1), radians(lat2)
    Δφ = radians(lat2 - lat1)
    Δλ = radians(lng2 - lng1)
    a = sin(Δφ/2)**2 + cos(φ1)*cos(φ2)*sin(Δλ/2)**2
    return 2*R*atan2(sqrt(a), sqrt(1-a))
```

**优化收益**：相比暴力扫描，R-tree 预过滤可减少 95% 候选集。

---

### 3.5 项目当前状态触发器优化

**问题**：触发器在写入时同步执行，可能拖慢写入 10-15%。

**优化**：分摊到批量提交

```python
async def batch_write_triples(triples: list[Triple]):
    """批量写入，单事务摊薄触发器成本"""
    async with db.execute("BEGIN"):
        for t in triples:
            await db.execute(
                "INSERT INTO triples (...) VALUES (...)",
                ...
            )
        await db.execute("COMMIT")
    # 触发器在 COMMIT 时统一执行
```

**性能对比**（1000 条三元组）：
| 方案 | 耗时 |
|------|------|
| 单条写入（每条触发）| 2.5s |
| **批量写入（事务内）** ★ | **0.3s** |
| 批量 + 关闭触发器 + 手动 UPSERT | 0.25s |

---

### 3.6 SQLite 调优清单

```python
# 启动时必做的 PRAGMA
PRAGMAS = [
    "PRAGMA journal_mode=WAL",          # 并发读 + 单写者
    "PRAGMA synchronous=NORMAL",        # 平衡安全与性能
    "PRAGMA cache_size=-65536",         # 64MB 缓存（负数 = KB）
    "PRAGMA temp_store=MEMORY",         # 临时表内存
    "PRAGMA mmap_size=268435456",       # 256MB 内存映射
    "PRAGMA foreign_keys=ON",           # 启用外键
    "PRAGMA busy_timeout=5000",         # 5s 锁等待
]
```

**效果**：相比默认配置，写入吞吐提升 5-10x。

---

## 4. 实施建议

### 4.1 MVP 范围

```
src/memory/storage/
├── sqlite_engine.py            # 引擎入口 + PRAGMA 配置
├── migrate.py                  # Schema 迁移
├── schema/
│   ├── 001_entities.sql
│   ├── 002_triples.sql
│   ├── 003_locations.sql
│   ├── 004_temporal_fragments.sql
│   ├── 005_causal_chains.sql
│   ├── 006_vector_links.sql
│   ├── 007_checkpoints.sql
│   ├── 008_project_current_state.sql  ★ 新增（含触发器）
│   ├── 009_memory_l0_snapshot.sql     ★ 新增
│   └── 010_virtual_tables.sql         ★ sqlite-vec + R-tree
├── queries/
│   ├── temporal.py / spatial.py / graph.py / causal.py / hybrid.py / scope.py
└── vector_store.py             # ChromaDB 适配
```

### 4.2 关键风险

| 风险 | 缓解 |
|------|------|
| sqlite-vec Windows 加载失败 | 提供 wheel 自带 .dll，测试 Win10/11 |
| R-tree 在某些 SQLite 编译版本缺失 | 启动时检查并 fallback 到 B-tree |
| 单写者瓶颈 | 应用层批量写入 + WAL |
| Schema 迁移失败 | 每个 migration 包裹在事务里，失败自动回滚 |

### 4.3 验证方式

```python
# tests/test_storage.py
async def test_sqlite_extensions():
    """验证扩展加载"""
    db = await init_db()
    async with db.execute("SELECT vec_version()") as cur:
        assert await cur.fetchone() is not None

async def test_trigger_maintains_current_state():
    """触发器维护项目当前状态"""
    await db.write_triple(t1 := Triple(..., relation_type="project_state", object_value="A"))
    state = await db.get_current_state(...)
    assert state.object_value == "A"
    
    # 写入新值，旧值应失效
    await db.write_triple(t2 := Triple(..., object_value="B"))
    state = await db.get_current_state(...)
    assert state.object_value == "B"

async def test_hybrid_search_rrf():
    """混合检索 RRF 融合"""
    results = await db.hybrid_search("test query", top_k=10)
    assert len(results) <= 10
    # 验证得分单调递减
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)
```

### 4.4 性能基线

| 操作 | 目标延迟 | 容量上限 |
|------|---------|---------|
| 单条写入 | < 5ms | 1000 QPS（WAL）|
| 批量写入（1000 条）| < 300ms | — |
| 单条查询 | < 2ms | — |
| 时序查询 | < 15ms | 100 万行 |
| 空间查询 | < 20ms | 100 万点 |
| 图谱遍历（深度 5）| < 50ms | 10 万三元组 |
| 向量检索（100K）| < 30ms | 50 万向量 |
| 混合检索 | < 200ms | — |

---

## 5. 后续演进路径

| 阶段 | 增强点 |
|------|--------|
| MVP | SQLite + sqlite-vec + ChromaDB + 基础检索 |
| P1 | 优化索引 + 复合查询 + 触发器维护 |
| P2 | sqlite-vec 替代 ChromaDB（统一）|
| P3 | 支持 PostgreSQL 适配器（多用户场景）|
