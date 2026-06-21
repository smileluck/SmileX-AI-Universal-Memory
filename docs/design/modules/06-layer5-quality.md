# Layer 5 知识质量层 — 选型与算法优化

> **配套章节**：[agent-memory-design.md §11](../agent-memory-design.md)
> **阶段**：[P2] 双向指针 + 矛盾检测 + 跨项目提升 + 历史归档
> **依赖**：Layer 0/1/3

---

## 1. 模块概览

### 1.1 职责

保证知识库的：
1. **可追溯性**（Traceability）：KG 事实 ↔ 原文证据
2. **一致性**（Consistency）：实体属性 / 时间线 / 原文验证
3. **矛盾可控**（Contradiction）：自动检测 + 解决
4. **跨项目共享**（Scope Promotion）：通用知识自动提升
5. **存储优化**（Archival）：冷热分层 + 量化压缩

### 1.2 核心组件清单

| 组件 | 文件 | 职责 |
|------|------|------|
| `bidirectional_link.py` | 双向指针 | `source_closet` 字段维护 |
| `trace.py` | 追溯引擎 | 正向/反向追溯 |
| `consistency.py` | 一致性检查 | 实体/时间线/原文三维 |
| `contradiction.py` | 矛盾检测解决 | 同步轻量 + 异步重量 |
| `scope_promoter.py` | 跨项目提升 | 共现检测 + 提升 |
| `archival.py` | 历史归档 | 量化 + 分层 + 压缩 |

### 1.3 依赖关系

```
Layer 1 存储
    ▲
    │ 读写
    │
Layer 5 知识质量（本层）
    │
    ▼
Layer 3 调度（提交修复任务）
```

---

## 2. 技术选型分析

### 2.1 双向指针实现：单字段 vs 关联表 vs 图数据库

| 方案 | 查询性能 | 写入复杂度 | 一致性维护 |
|------|---------|-----------|-----------|
| **单字段 `source_closet`** ★推荐 | ⚡⚡⚡ 极快 | 简单 | 双向各存一份 |
| **独立关联表** | ⚡⚡ 中 | 中 | 集中维护 |
| **图数据库（Neo4j）** | ⚡⚡⚡ 极快 | 复杂 | 原生 |

**推荐方案**：**单字段 `source_closet`（保留 MemPalace 命名）**

**选型理由**：
- **实现简单**：两个表各存一个字段
- **查询极快**：B-tree 索引直接定位
- **跨库可行**：ChromaDB 和 SQLite 都支持 metadata

```python
# 写入时双向设置
async def write_with_pointer(triple: Triple, drawer_id: str):
    triple.source_closet = drawer_id
    await db.write_triple(triple)
    await chromadb.update_metadata(drawer_id, {"source_closet": triple.id})
```

---

### 2.2 矛盾检测：规则 vs LLM vs 混合

| 方案 | 准确率 | 速度 | 成本 | 可解释性 |
|------|--------|------|------|---------|
| **纯规则**（字符串/数值比较）| ⭐⭐⭐ | ⚡⚡⚡ 极快 | 免费 | ✅ |
| **纯 LLM** | ⭐⭐⭐⭐⭐ | ❌ 慢 | API 费用 | ❌ |
| **混合（规则优先 + LLM 兜底）** ★推荐 | ⭐⭐⭐⭐ | ⚡⚡ 快 | 中 | ✅ |

**推荐方案**：**混合策略（规则优先 + LLM 兜底）**

**选型理由**：
- **规则快且免费**：数值/属性/时间冲突规则化
- **LLM 处理语义冲突**：因果、上下文矛盾
- **分层触发**：写入时同步规则，整合时异步 LLM

```python
class ContradictionDetector:
    async def detect_sync(self, new: Triple, existing: list[Triple]) -> list[Conflict]:
        """同步规则检测（写入时触发）"""
        conflicts = []
        for old in existing:
            # 规则 1：数值冲突
            if new.predicate == old.predicate and new.subject == old.subject:
                if is_numeric_conflict(new.object_value, old.object_value):
                    conflicts.append(Conflict("numeric", old, new))
                # 规则 2：枚举冲突
                elif is_enum_conflict(new, old):
                    conflicts.append(Conflict("attribute", old, new))
        return conflicts
    
    async def detect_async(self, scope: str) -> list[Conflict]:
        """异步 LLM 检测（整合任务触发）"""
        triples = await db.get_all_triples(scope)
        prompt = build_contradiction_prompt(triples)
        result = await llm.detect_contradictions(prompt)
        return [Conflict(**c) for c in result]
```

---

### 2.3 跨项目共现检测：精确计数 vs 采样估计 vs LSH

| 方案 | 准确率 | 速度 | 内存 |
|------|--------|------|------|
| **精确 SQL GROUP BY** ★推荐 | 100% | 中 | 低 |
| **HyperLogLog**（近似）| 98% | 快 | 极低 |
| **MinHash LSH** | 95% | 快 | 中 |

**推荐方案**：**SQL GROUP BY（数据量可控）**

```sql
-- 跨项目共现检测
SELECT 
    predicate,
    object_value,
    COUNT(DISTINCT scope) AS project_count,
    GROUP_CONCAT(DISTINCT scope) AS projects
FROM triples
WHERE scope LIKE 'project:%'
  AND valid_to IS NULL
GROUP BY predicate, object_value
HAVING COUNT(DISTINCT scope) >= 3
ORDER BY project_count DESC
LIMIT 100;
```

---

### 2.4 历史归档：手动 vs 触发器 vs 定时任务

| 方案 | 自动化 | 性能影响 | 灵活性 |
|------|--------|---------|--------|
| **手动 SQL** | ❌ | 无 | 低 |
| **触发器** | ✅ | 写入 -10% | 低 |
| **定时任务** ★推荐 | ✅ | 无（后台）| 高 |

**推荐方案**：**定时任务（自适应触发）**

```python
class ArchivalScheduler:
    """每周日凌晨 2 点归档"""
    
    @cron("0 2 * * 0")
    async def archive_expired(self):
        retention = RetentionPolicy(
            triple_history_days=365,
            fragment_days=180,
            causal_chain_days=730,
        )
        
        async with db.transaction():
            # 1. 移动到归档库
            moved = await self._move_to_archive(retention)
            # 2. 更新统计
            await self._update_stats(moved)
            # 3. 压缩向量
            await self._quantize_vectors()
```

---

### 2.5 向量量化方案：int8 vs Product Quantization vs 二值化

| 方案 | 压缩比 | 精度损失 | 检索速度 |
|------|--------|---------|---------|
| **float32 → float16** ★MVP | 50% | < 1% | 不变 |
| **int8 标量量化** ★P1 | 75% | < 3% | +10% |
| **Product Quantization (PQ)** | 87.5% | 5-10% | +50% |
| **二值化** | 96.9% | 15-25% | +200% |

**推荐路径**：float16（MVP）→ int8（P1）→ PQ（P2 数据量大时）

---

## 3. 核心算法与优化

### 3.1 双向指针一致性维护

**问题**：ChromaDB 和 SQLite 双向指针如何保证一致？

**算法**：事务 + 补偿

```python
async def write_with_pointer(triple: Triple):
    """写入三元组 + 双向指针"""
    # 1. 先写 ChromaDB（生成 drawer_id）
    drawer_id = await chromadb.add(
        document=triple.content,
        embedding=triple.embedding,
        metadata={"temp": True},
    )
    
    try:
        # 2. 再写 SQLite（含 source_closet）
        triple.source_closet = drawer_id
        await db.write_triple(triple)
        
        # 3. 更新 ChromaDB metadata（移除 temp，加 source_closet）
        await chromadb.update(
            drawer_id,
            metadata={"source_closet": triple.id, "temp": False},
        )
    except Exception:
        # 补偿：删除 ChromaDB 临时记录
        await chromadb.delete(drawer_id)
        raise
```

---

### 3.2 矛盾检测四维算法

**维度 1：时间矛盾**

```python
def detect_time_conflict(new: Triple, old: Triple) -> bool:
    """时间矛盾：同一实体在不同时间被报告不同状态"""
    if not (new.valid_from and old.valid_from):
        return False
    # 时间段重叠
    overlap = time_overlap(
        TimeRange(new.valid_from, new.valid_to),
        TimeRange(old.valid_from, old.valid_to),
    )
    if overlap == 0:
        return False  # 不同时间段，不算矛盾
    # 同一 predicate 但 object 不同
    return new.object_value != old.object_value
```

**维度 2：数量矛盾**

```python
def detect_numeric_conflict(new: Triple, old: Triple) -> bool:
    """数量矛盾：'3 个人' vs '5 个人'"""
    new_num = extract_number(new.object_value)  # "3 个人" → 3
    old_num = extract_number(old.object_value)
    if new_num is None or old_num is None:
        return False
    # 允许 10% 容差
    return abs(new_num - old_num) / max(new_num, old_num) > 0.1
```

**维度 3：属性矛盾**

```python
def detect_attribute_conflict(new: Triple, old: Triple) -> bool:
    """属性矛盾：'红色' vs '蓝色'"""
    if new.predicate != old.predicate:
        return False
    if new.object_value == old.object_value:
        return False
    # 枚举类型属性（颜色/状态/类别）
    if is_enum_attribute(new.predicate):
        return True  # 枚举值互斥
    return False
```

**维度 4：因果矛盾**

```python
async def detect_causal_conflict(new: Triple, old: Triple) -> bool:
    """因果矛盾：'A 导致 B' vs 'B 导致 A'"""
    # 构建因果图，检测反向边
    if new.predicate == "导致" and old.predicate == "导致":
        if new.subject == old.object and new.object == old.subject:
            return True
    return False
```

---

### 3.3 矛盾解决策略

```python
class ContradictionResolver:
    async def resolve(self, conflict: Conflict) -> Resolution:
        # 自动解决
        if conflict.type in ["numeric", "attribute"]:
            return await self._last_write_wins(conflict)
        elif conflict.type == "time":
            return await self._split_by_time(conflict)
        
        # LLM 辅助解决
        if conflict.confidence < 0.7:
            suggestion = await llm.suggest_resolution(conflict)
            if suggestion.confidence > 0.9:
                return Resolution(action="llm_resolved", data=suggestion)
        
        # 提交人工
        return await self._queue_manual(conflict)
    
    async def _split_by_time(self, conflict: Conflict) -> Resolution:
        """时间矛盾：保留两者，标记不同时间段"""
        old, new = conflict.old, conflict.new
        # 旧记录的 valid_to 设为新记录的 valid_from
        await db.update_triple(old.id, valid_to=new.valid_from)
        # 新记录保留
        return Resolution(action="keep_both_with_split")
```

---

### 3.4 跨项目提升算法

**问题**：什么样的模式应该提升为 global？

**算法**：基于支持度 + 置信度

```python
class ScopePromoter:
    MIN_PROJECTS = 3        # 至少 3 个项目共现
    MIN_SUPPORT = 5         # 至少 5 次出现
    MIN_CONFIDENCE = 0.8    # 置信度阈值
    
    async def detect_promotable_patterns(self) -> list[Pattern]:
        """检测可提升的模式"""
        candidates = await db.execute("""
            SELECT predicate, object_value,
                   COUNT(DISTINCT scope) AS project_count,
                   COUNT(*) AS support
            FROM triples
            WHERE scope LIKE 'project:%'
              AND valid_to IS NULL
            GROUP BY predicate, object_value
            HAVING project_count >= ? AND support >= ?
        """, (self.MIN_PROJECTS, self.MIN_SUPPORT))
        
        # 计算置信度
        promotable = []
        for c in candidates:
            confidence = await self._compute_confidence(c)
            if confidence >= self.MIN_CONFIDENCE:
                promotable.append(c)
        
        return promotable
    
    async def _compute_confidence(self, candidate) -> float:
        """置信度 = 1 - 反例比例"""
        positive = candidate.support
        negative = await db.count_negatives(
            candidate.predicate, candidate.object_value
        )
        return positive / (positive + negative)
    
    async def promote_to_global(self, pattern: Pattern):
        """复制到 global scope"""
        triples = await db.get_triples_by_pattern(
            pattern, scope_prefix="project:%"
        )
        async with db.transaction():
            for t in triples:
                new_triple = t.copy(
                    id=str(ULID()),
                    scope=MemoryScope.GLOBAL,
                    source_scope=t.scope,  # 反向追溯
                )
                await db.insert_triple(new_triple)
```

---

### 3.5 历史归档算法

**问题**：如何安全归档而不影响查询？

**算法**：归档库 ATTACH + 透明查询

```python
class ArchivalManager:
    async def archive_expired(self, retention: RetentionPolicy):
        """归档过期数据"""
        # 1. ATTACH 归档库
        await db.execute(f"ATTACH DATABASE 'archive_{year}.db' AS archive")
        
        # 2. 在归档库创建镜像表
        await db.execute("""
            CREATE TABLE archive.triples AS 
            SELECT * FROM triples WHERE 0
        """)
        
        # 3. 移动过期数据
        cutoff = now() - retention.triple_history_days
        await db.execute("""
            INSERT INTO archive.triples 
            SELECT * FROM triples WHERE valid_to < ?
            AND valid_to IS NOT NULL
        """, (cutoff,))
        await db.execute("DELETE FROM triples WHERE valid_to < ? AND valid_to IS NOT NULL", (cutoff,))
        
        # 4. 创建视图透明查询
        await db.execute("""
            CREATE VIEW all_triples AS
            SELECT * FROM triples UNION ALL
            SELECT * FROM archive.triples
        """)
    
    async def query_with_archive(self, filter):
        """查询自动包含归档数据"""
        return await db.execute("SELECT * FROM all_triples WHERE ...")
```

---

### 3.6 向量量化算法

**算法 1**：标量量化（float32 → int8）

```python
def quantize_to_int8(vectors: np.ndarray) -> tuple[np.ndarray, float, float]:
    """标量量化到 int8"""
    vmin, vmax = vectors.min(), vectors.max()
    scale = 255.0 / (vmax - vmin)
    quantized = np.round((vectors - vmin) * scale).astype(np.uint8)
    return quantized, vmin, vmax

def dequantize(quantized: np.ndarray, vmin: float, vmax: float) -> np.ndarray:
    scale = (vmax - vmin) / 255.0
    return quantized.astype(np.float32) * scale + vmin
```

**算法 2**：Product Quantization（PQ）

```python
from sklearn.cluster import KMeans

def train_pq(vectors: np.ndarray, n_subspaces: int = 8, n_clusters: int = 256):
    """训练 PQ 码本"""
    dim = vectors.shape[1]
    sub_dim = dim // n_subspaces
    codebooks = []
    
    for i in range(n_subspaces):
        sub_vectors = vectors[:, i*sub_dim:(i+1)*sub_dim]
        kmeans = KMeans(n_clusters=n_clusters)
        kmeans.fit(sub_vectors)
        codebooks.append(kmeans.cluster_centers_)
    
    return codebooks

def encode_pq(vector: np.ndarray, codebooks: list) -> bytes:
    """编码为 PQ 码（每个子空间 1 字节）"""
    code = bytearray()
    for i, cb in enumerate(codebooks):
        sub_dim = cb.shape[1]
        sub_vec = vector[i*sub_dim:(i+1)*sub_dim]
        # 找最近的聚类中心
        distances = np.linalg.norm(cb - sub_vec, axis=1)
        code.append(np.argmin(distances))
    return bytes(code)
```

**压缩对比**（1536 维）：
| 方案 | 字节 | 压缩比 |
|------|------|--------|
| float32 | 6144 | 1x |
| float16 | 3072 | 2x |
| **int8** ★ | **1536** | 4x |
| PQ（8 子空间）| **8** | **768x** |

---

## 4. 实施建议

### 4.1 MVP 范围

MVP 仅实现双向指针 + 同步矛盾检测（轻量规则）。P1/P2 增强：

```
src/memory/quality/
├── bidirectional_link.py    # ✅ MVP
├── trace.py                 # ✅ MVP
├── consistency.py           # P1
├── contradiction.py         # ⚠️ MVP 仅同步规则
├── scope_promoter.py        # P2
└── archival.py              # P2
```

### 4.2 关键风险

| 风险 | 缓解 |
|------|------|
| 矛盾检测误报 | 阈值 + LLM 兜底 + 人工审核 |
| 提升污染 global | 保守阈值 + 反向追溯 |
| 归档后查询变慢 | ATTACH 透明查询 + 缓存 |
| 向量量化精度损失 | 渐进式（fp16 → int8 → PQ）|

### 4.3 验证方式

```python
async def test_bidirectional_pointer():
    """双向指针一致性"""
    triple = await db.write_triple_with_pointer(...)
    # 正向追溯
    drawer = await trace.kg_to_source(triple.id)
    assert drawer is not None
    # 反向追溯
    triples = await trace.source_to_kg(drawer.id)
    assert triple.id in [t.id for t in triples]

async def test_contradiction_detection():
    """矛盾检测"""
    await db.write_triple(Triple(predicate="color", object_value="red"))
    with pytest.raises(ConflictWarning):
        await db.write_triple(Triple(predicate="color", object_value="blue"))

async def test_scope_promotion():
    """跨项目提升"""
    # 3 个项目共现
    for scope in ["p1", "p2", "p3"]:
        await db.write_triple(Triple(scope=scope, predicate="uses", object_value="redis"))
    
    await promoter.detect_and_promote()
    
    global_triples = await db.get_triples(scope="global")
    assert any(t.object_value == "redis" for t in global_triples)

async def test_archival():
    """归档不影响查询"""
    # 写入 1 年前的数据
    old = Triple(..., valid_to="2025-01-01")
    await db.write_triple(old)
    
    await archival.archive_expired(RetentionPolicy(triple_history_days=365))
    
    # 归档后仍可查询
    results = await db.query_all_triples(filter=...)
    assert old.id in [r.id for r in results]
```

### 4.4 性能基线

| 操作 | 目标延迟 |
|------|---------|
| 双向指针写入 | < 5ms |
| 正向追溯 | < 5ms |
| 反向追溯 | < 5ms |
| 同步矛盾检测（单次）| < 1ms |
| 异步矛盾检测（全库）| < 30s |
| 跨项目共现检测 | < 60s |
| 归档（1 万条）| < 30s |
| 向量量化（10 万）| < 60s |

---

## 5. 后续演进路径

| 阶段 | 增强点 |
|------|--------|
| MVP | 双向指针 + 同步矛盾检测（规则）|
| P1 | 一致性检查 + 异步矛盾（LLM）|
| P2 | 跨项目提升 + 历史归档 |
| P3 | 主动知识图谱演化（自学习）|
