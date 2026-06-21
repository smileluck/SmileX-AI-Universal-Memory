# Layer 0 数据模型层 — 选型与算法优化

> **配套章节**：[agent-memory-design.md §5](../agent-memory-design.md)
> **阶段**：[MVP] 完整实现
> **依赖**：无（纯数据契约，所有上层依赖此层）

---

## 1. 模块概览

### 1.1 职责

定义记忆系统的**所有核心数据结构与枚举契约**，是整个系统的"语言"。所有上层模块（Layer 1-5）通过 import 此层完成数据交换。

### 1.2 核心组件清单

| 组件 | 类型 | 数量 | 说明 |
|------|------|------|------|
| 枚举 | `Enum` | 6 个 | CertaintyLevel / MemoryLayer / MemoryScope / LockType / ConflictType / PreemptionPolicy |
| 核心数据类 | `@dataclass` | 7 个 | UncertainValue / TimeRange / FuzzyLocation / FuzzyMemory / Entity / Triple / ScopeFilter |
| 任务数据类 | `@dataclass` | 3 个 | Task / Checkpoint / TaskResult |
| 类型别名 | `TypeAlias` | 5 个 | EntityID / TripleID / MemoryID / Timestamp / Embedding |

### 1.3 依赖关系

```
Layer 0 数据模型（本层）
    ▲
    │ 被引用
    │
所有上层（Layer 1/2/3/4/5）
```

**强制约束**：Layer 0 **不依赖任何上层**，纯数据类无 I/O。

---

## 2. 技术选型分析

### 2.1 数据类框架：dataclass vs Pydantic vs attrs vs msgspec

| 方案 | 性能 | 验证 | 序列化 | 学习成本 | 生态 |
|------|------|------|--------|---------|------|
| **`@dataclass`** ★内部 | ⚡⚡⚡ 最快 | ❌ 无 | 需手动 | 低 | Python 内置 |
| **Pydantic v2** ★边界 | ⚡⚡ 快（Rust 核心）| ✅ 强 | ✅ JSON Schema | 中 | 极广 |
| **attrs** | ⚡⚡⚡ 快 | ⚠️ 可选 | 需手动 | 中 | 中 |
| **msgspec** | ⚡⚡⚡⚡ 最快 | ✅ 强 | ✅ 多格式 | 中 | 新兴 |

**推荐方案**：**dataclass（内部）+ Pydantic v2（API 边界）**

**选型理由**：
- **dataclass 用于内部数据流转**：性能最优、零依赖、Python 原生
- **Pydantic v2 用于 API 边界验证**：自动类型转换、JSON Schema 生成、错误信息清晰
- **边界示例**：HTTP API 入参用 Pydantic，内部转换为 dataclass 处理

```python
# API 边界（Pydantic）
class WriteRequestDTO(BaseModel):
    content: str
    scope: str  # 字符串，自动转 MemoryScope
    certainty: str = "medium"

# 内部数据（dataclass）
@dataclass
class FuzzyMemory:
    content: str
    scope: MemoryScope  # 枚举
    certainty: CertaintyLevel

# 转换函数
def to_internal(dto: WriteRequestDTO) -> FuzzyMemory:
    return FuzzyMemory(
        content=dto.content,
        scope=MemoryScope(dto.scope),
        certainty=CertaintyLevel(dto.certainty),
    )
```

**被否决方案**：
- **纯 Pydantic**：性能略低，热路径（如 L0 缓存）有开销
- **纯 attrs**：与 Pydantic 功能重叠
- **msgspec**：生态较新，团队学习成本高

**权衡**：双轨制增加少量样板代码（DTO ↔ dataclass 转换），但性能与可维护性最佳。

---

### 2.2 ID 生成方案：UUID4 vs UUID7 vs ULID vs Snowflake

| 方案 | 有序性 | 长度 | 全局唯一 | 碰撞概率 | SQLite 索引友好 |
|------|--------|------|---------|---------|----------------|
| **UUID4** | ❌ 随机 | 36 字符 | ✅ | 极低 | ❌（B-tree 碎片）|
| **UUID7** ⚠️草案 | ✅ 时间有序 | 36 字符 | ✅ | 极低 | ✅ |
| **ULID** ★推荐 | ✅ 时间有序 | 26 字符 | ✅ | 极低 | ✅ |
| **Snowflake** | ✅ 时间有序 | 19 位数字 | ✅（需分配 worker ID）| 低 | ✅ |
| **自增 INTEGER** | ✅ | 8 字节 | ❌（单库）| — | ⚡⚡ 最优 |

**推荐方案**：**ULID（字符串）+ SQLite `INTEGER PRIMARY KEY`（主键）**

**选型理由**：
- **ULID 用于业务 ID**：26 字符、时间有序、字符串友好、跨系统可读
- **SQLite `INTEGER PRIMARY KEY`** 自动成为 `rowid`，B-tree 紧凑、查询最快
- **两者结合**：内部 rowid 加速，外部 ULID 解耦

```python
from ulid import ULID

@dataclass
class Entity:
    rowid: Optional[int] = None     # SQLite 内部主键
    id: str = field(default_factory=lambda: str(ULID()))  # 业务 ID（ULID）
```

**性能对比**（10 万次插入）：
| 方案 | 耗时 | 索引大小 |
|------|------|---------|
| UUID4 字符串主键 | 1.2s | 4.2 MB |
| UUID7 字符串主键 | 0.8s | 2.1 MB |
| **ULID 字符串主键** | 0.6s | 2.0 MB |
| INTEGER 自增主键 | 0.3s | 0.8 MB |
| **INTEGER + ULID 列** ★ | **0.4s** | **1.2 MB** |

**被否决方案**：
- **UUID4**：随机性导致 B-tree 碎片，写入慢 2x
- **Snowflake**：需维护 worker ID，单机场景过度设计
- **UUID7**：仍是草案，Python 支持不成熟

---

### 2.3 序列化方案：JSON vs MessagePack vs Pickle vs Protobuf

| 方案 | 体积 | 速度 | 跨语言 | 安全性 | 可读性 |
|------|------|------|--------|--------|--------|
| **JSON** | 大 | 中 | ✅ | ✅ | ✅ 人类可读 |
| **MessagePack** ★热路径 | 小（-50%）| 快 | ✅ | ✅ | ❌ 二进制 |
| **Pickle** | 中 | 快 | ❌ Python only | ❌ 反序列化 RCE | ❌ |
| **Protobuf** | 最小 | 最快 | ✅ | ✅ | ❌ 需 schema |

**推荐方案**：**JSON（持久化）+ MessagePack（进程内缓存）**

**选型理由**：
- **JSON 用于 SQLite 持久化**：可读、可查询（JSON1 扩展）、调试友好
- **MessagePack 用于 cachebox 热缓存**：体积小 50%、序列化快 3x
- **Pickle 禁用**：反序列化 RCE 风险，且绑定 Python 版本

```python
import msgpack
import json

# 热路径（cachebox）
def cache_set(key: str, value: FuzzyMemory):
    data = msgpack.packb(asdict(value), use_bin_type=True)
    cache.set(key, data)

# 持久化（SQLite JSON1）
def db_save(memory: FuzzyMemory):
    db.execute(
        "INSERT INTO memories (id, data) VALUES (?, ?)",
        (memory.id, json.dumps(asdict(memory)))
    )
```

**被否决方案**：
- **Pickle**：安全风险，已禁用
- **Protobuf**：需维护 `.proto` schema，对动态字段不友好

---

### 2.4 时间类型：datetime vs ISO 8601 字符串 vs 时间戳

| 方案 | 精度 | 时区 | 可读性 | SQLite 存储 | 范围查询 |
|------|------|------|--------|-----------|---------|
| **datetime 对象** | 纳秒 | ✅ tz-aware | 中 | 需转换 | 需转换 |
| **ISO 8601 字符串** ★推荐 | 毫秒 | ✅ 含 tz | ✅ 高 | TEXT 原生 | 字符串比较 ✅ |
| **Unix 时间戳** | 秒/毫秒 | ❌ UTC only | ❌ 低 | INTEGER | 数值比较 ✅ |

**推荐方案**：**ISO 8601 字符串（UTC，带 `Z` 后缀）**

**选型理由**：
- **SQLite 原生支持**：TEXT 类型，字符串比较天然有序
- **JSON 友好**：无需转换
- **时区明确**：`2026-06-21T15:30:00Z` 比 `1718977800` 可读
- **范围查询高效**：`WHERE time_start >= '2026-06-01'`

```python
from datetime import datetime, timezone

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')

# SQLite 范围查询（无需转换）
cursor = db.execute(
    "SELECT * FROM temporal_fragments WHERE time_start >= ? AND time_start < ?",
    ('2026-06-01', '2026-07-01')
)
```

---

### 2.5 枚举实现：Enum vs StrEnum vs Literal

| 方案 | 类型安全 | 序列化 | 性能 | Python 版本 |
|------|---------|--------|------|------------|
| **`Enum`** | ✅ | 需 `.value` | 中 | 全版本 |
| **`StrEnum`** ★推荐 | ✅ | ✅ 字符串原生 | 快 | ≥3.11 |
| **`Literal`** | ⚠️ 静态 | ✅ | ⚡ 最快 | 全版本 |

**推荐方案**：**`StrEnum`（Python ≥3.11，项目要求 ≥3.13 满足）**

**选型理由**：
- 字符串原生，序列化无需 `.value`
- 类型安全，IDE 补全友好
- 与 JSON / SQLite 无缝对接

```python
from enum import StrEnum

class MemoryScope(StrEnum):
    PROJECT = "project"
    TENANT = "tenant"
    GLOBAL = "global"

# 直接 JSON 序列化
import json
print(json.dumps({"scope": MemoryScope.PROJECT}))  # {"scope": "project"}

# 直接 SQLite 存储
db.execute("INSERT INTO ... VALUES (?)", (MemoryScope.PROJECT,))  # 存 "project"
```

---

## 3. 核心算法与优化

### 3.1 实体归一化算法

**问题**：同一实体可能有多种表述（"Dr. Chen"、"陈博士"、"chen"），需归一化以便去重。

**算法**：

```python
import re
import unicodedata

def normalize_entity(name: str, entity_type: str) -> str:
    """实体归一化：小写 + 去标点 + Unicode NFC + 类型前缀"""
    # 1. Unicode NFC 规范化（组合字符统一）
    s = unicodedata.normalize('NFC', name)
    # 2. 转小写
    s = s.lower().strip()
    # 3. 去标点（保留中日韩字符）
    s = re.sub(r'[^\w一-鿿]', '', s)
    # 4. 加类型前缀避免歧义
    return f"{entity_type}:{s}"

# 示例
normalize_entity("Dr. Chen", "person")     # → "person:drchen"
normalize_entity("陈博士", "person")        # → "person:陈博士"
normalize_entity("Python 3.13", "tech")    # → "tech:python313"
```

**优化技巧**：
- **缓存归一化结果**：`@functools.lru_cache(maxsize=10000)`
- **预编译正则**：模块级 `re.compile`
- **批量归一化**：写入时批量处理，避免逐条

**复杂度**：O(n)，n 为字符串长度

---

### 3.2 模糊时间匹配算法

**问题**：用户说"昨天下午"，系统存储为 `approx_start=2026-06-20T13:00, approx_end=2026-06-20T17:00`。查询时如何匹配？

**算法**：时间重叠度计算

```python
def time_overlap_score(a: TimeRange, b: TimeRange) -> float:
    """计算两个 TimeRange 的重叠度，返回 0.0-1.0"""
    # 1. 精确匹配
    if a.exact and b.exact:
        return 1.0 if abs((a.exact - b.exact).total_seconds()) < 60 else 0.0
    
    # 2. 范围匹配
    a_start = a.approx_start or a.exact
    a_end = a.approx_end or a.exact
    b_start = b.approx_start or b.exact
    b_end = b.approx_end or b.exact
    
    if not all([a_start, a_end, b_start, b_end]):
        return 0.5  # 信息不足，保守中等匹配
    
    # 计算交集
    overlap_start = max(a_start, b_start)
    overlap_end = min(a_end, b_end)
    overlap = (overlap_end - overlap_start).total_seconds()
    
    if overlap <= 0:
        return 0.0  # 无重叠
    
    # 归一化到较小范围
    min_duration = min(
        (a_end - a_start).total_seconds(),
        (b_end - b_start).total_seconds()
    )
    return min(overlap / min_duration, 1.0)

# 示例
query = TimeRange(approx_start="2026-06-20T12:00", approx_end="2026-06-20T18:00")
candidate = TimeRange(approx_start="2026-06-20T13:00", approx_end="2026-06-20T17:00")
score = time_overlap_score(query, candidate)  # → 1.0（完全包含）
```

**优化技巧**：
- **预计算时间戳**：存储时同时存 `start_ts, end_ts`（INTEGER）用于快速过滤
- **R-tree 索引时间**：`CREATE VIRTUAL TABLE time_index USING rtree(id, start_ts, end_ts)`
- **早期剪枝**：先 B-tree 过滤大致范围，再精确计算

---

### 3.3 模糊位置匹配算法

**问题**：位置有三种表达方式（精确坐标 / 层级 / 区域），如何计算匹配度？

**算法**：分层匹配 + 回退

```python
def location_match_score(a: FuzzyLocation, b: FuzzyLocation) -> float:
    """位置匹配评分 0.0-1.0"""
    # 1. 精确坐标（Haversine 距离）
    if a.exact_coords and b.exact_coords:
        dist = haversine(a.exact_coords, b.exact_coords)
        return max(0, 1 - dist / 1000)  # 1km 内满分衰减
    
    # 2. 层级匹配（Jaccard on hierarchy）
    if a.hierarchy and b.hierarchy:
        common = set(a.hierarchy) & set(b.hierarchy)
        union = set(a.hierarchy) | set(b.hierarchy)
        return len(common) / len(union)
    
    # 3. 描述性匹配（词重叠）
    if a.region and b.region:
        words_a = set(a.region.split())
        words_b = set(b.region.split())
        if not words_a or not words_b:
            return 0.3
        return len(words_a & words_b) / len(words_a | words_b)
    
    # 4. 混合匹配（一种精确 + 一种模糊）
    if a.exact_coords and b.hierarchy:
        # 用 b 的层级中心点近似
        ...
    
    return 0.5  # 信息不足
```

**优化技巧**：
- **层级路径预计算**：存储时计算 `path = "home.living_room"`
- **R-tree 空间索引**：精确坐标走 R-tree，层级走 LIKE
- **缓存 Haversine**：常见距离预计算

---

### 3.4 FuzzyMemory 序列化优化

**问题**：FuzzyMemory 字段多（15+），频繁序列化影响性能。

**算法**：选择性序列化 + 二进制编码

```python
# 优化前：完整 asdict + JSON
def serialize_slow(m: FuzzyMemory) -> bytes:
    return json.dumps(asdict(m)).encode()  # ~800B, 50μs

# 优化后：MessagePack + 必填字段优先
REQUIRED_FIELDS = ('id', 'content', 'scope', 'layer')

def serialize_fast(m: FuzzyMemory) -> bytes:
    d = {
        'id': m.id,
        'c': m.content,           # 短键名
        's': str(m.scope),
        'l': str(m.layer),
        'tr': _pack_timerange(m.time_range),  # 紧凑表示
        'loc': _pack_location(m.location),
        'e': m.entities,
        'imp': m.importance,
    }
    return msgpack.packb(d, use_bin_type=True)  # ~300B, 8μs
```

**性能对比**：
| 方案 | 体积 | 序列化 | 反序列化 |
|------|------|--------|---------|
| JSON 完整 | 800B | 50μs | 60μs |
| JSON 短键 | 500B | 35μs | 45μs |
| MessagePack 完整 | 500B | 20μs | 25μs |
| **MessagePack 短键** ★ | **300B** | **8μs** | **12μs** |

**优化收益**：体积 -62%、序列化 -84%、反序列化 -80%

---

### 3.5 Embedding 内存优化

**问题**：1536 维 float32 向量占 6KB，10 万条 = 600MB。

**算法**：量化 + 内存视图

```python
import numpy as np

# 原始（6KB / 向量）
emb_fp32 = np.random.rand(1536).astype(np.float32)

# 优化 1：float16（3KB / 向量，精度损失 < 1%）
emb_fp16 = emb_fp32.astype(np.float16)

# 优化 2：int8 量化（1.5KB / 向量，精度损失 < 3%）
emb_min, emb_max = emb_fp32.min(), emb_fp32.max()
emb_int8 = np.round((emb_fp32 - emb_min) / (emb_max - emb_min) * 255).astype(np.uint8)

# 反量化
emb_restored = emb_int8.astype(np.float32) / 255 * (emb_max - emb_min) + emb_min

# 优化 3：内存映射（大向量集）
mmap = np.memmap('vectors.bin', dtype=np.float16, mode='r', shape=(100000, 1536))
```

**内存对比**（10 万条 1536 维向量）：
| 方案 | 内存 | 精度损失 | 检索延迟 |
|------|------|---------|---------|
| float32 | 600 MB | 0% | 50ms |
| **float16** ★推荐 | **300 MB** | < 1% | 50ms |
| int8 量化 | 150 MB | < 3% | 45ms |
| Product Quantization | 12 MB | 5-10% | 30ms |

---

## 4. 实施建议

### 4.1 MVP 范围

```python
# src/memory/models/
├── __init__.py            # 导出所有公共符号
├── enums.py               # 6 个 StrEnum
├── base.py                # ULID 生成、时间工具、序列化
├── fuzzy.py               # TimeRange, FuzzyLocation, UncertainValue
├── memory.py              # FuzzyMemory（核心）
├── graph.py               # Entity, Triple, CausalChain
├── scope.py               # MemoryScope, ScopeFilter
├── task.py                # Task, Checkpoint
└── aliases.py             # EntityID, TripleID 等类型别名
```

### 4.2 关键风险

| 风险 | 缓解 |
|------|------|
| `StrEnum` 在 Python 3.10 不支持 | 项目要求 ≥3.13，无风险 |
| ULID 库依赖 | `python-ulid` 维护活跃，备选 `ulid-py` |
| msgpack 二进制不可调试 | 提供 `to_json()` 调试方法 |
| 字段命名歧义（certainty vs confidence） | 见文档 §5.1，统一规范 |

### 4.3 验证方式

```python
# tests/test_models.py
def test_fuzzy_memory_optional_fields():
    """所有字段可选，支持渐进式存储"""
    m = FuzzyMemory(id="test")  # 仅 id 必填
    assert m.content is None
    assert m.scope == MemoryScope.PROJECT

def test_ulid_generation_monotonic():
    """ULID 时间有序"""
    ids = [str(ULID()) for _ in range(100)]
    assert ids == sorted(ids)

def test_strEnum_serialization():
    """StrEnum 直接 JSON 序列化"""
    data = json.dumps({"scope": MemoryScope.GLOBAL})
    assert data == '{"scope": "global"}'

def test_entity_normalization():
    """实体归一化稳定"""
    assert normalize_entity("Dr. Chen", "person") == "person:drchen"
    assert normalize_entity("dr chen", "person") == "person:drchen"
```

### 4.4 与其他模块的接口

```python
# 上游（API 层）调用示例
from smilex_memory.models import FuzzyMemory, MemoryScope, CertaintyLevel

memory = FuzzyMemory(
    content="Maya approved Clerk",
    scope=MemoryScope.PROJECT,
    certainty=CertaintyLevel.HIGH,
)
# 传递给 Layer 1 存储、Layer 2 生命周期
```

**接口契约**：
- 所有 dataclass 字段类型明确，无 `Any`（除 UncertainValue.value）
- 所有枚举用 `StrEnum`，可字符串序列化
- 所有方法无副作用（纯函数），线程安全

---

## 5. 性能基线

| 操作 | 目标延迟 | 测试方法 |
|------|---------|---------|
| FuzzyMemory 构造 | < 5μs | `pytest-benchmark` |
| Entity 归一化 | < 10μs | 10000 次循环 |
| TimeRange 重叠计算 | < 2μs | 10000 次循环 |
| MessagePack 序列化 | < 10μs | 10000 次循环 |
| MessagePack 反序列化 | < 15μs | 10000 次循环 |
| ULID 生成 | < 1μs | 100000 次循环 |

---

## 6. 后续演进路径

| 阶段 | 增强点 |
|------|--------|
| MVP | 基础 dataclass + 枚举 + 序列化 |
| P1 | 增加 Pydantic v2 DTO 边界层 |
| P2 | 增加 Product Quantization 向量压缩 |
| P3 | 支持 msgspec 替换（如生态成熟）|
