# Layer 2 记忆生命周期层 — 选型与算法优化

> **配套章节**：[agent-memory-design.md §7](../agent-memory-design.md)
> **阶段**：[MVP→P1] L0/L1 核心，L2/L3 增强
> **依赖**：Layer 0 数据模型、Layer 1 存储引擎

---

## 1. 模块概览

### 1.1 职责

管理 **L0-L3 四层记忆**的存储、流转、遗忘，控制 **Token 预算**（~4000 tokens），构建 Agent 上下文。

### 1.2 核心组件清单

| 组件 | 文件 | 职责 |
|------|------|------|
| `working.py` (L0) | 工作记忆 | cachebox 进程内 + SQLite 快照 |
| `shortterm.py` (L1) | 短时记忆 | ChromaDB 向量检索 |
| `longterm.py` (L2) | 长时记忆 | SQLite 混合检索 |
| `semantic.py` (L3) | 语义记忆 | 图谱遍历 |
| `context_builder.py` | 上下文构建 | Token Budget 算法、各层加载、裁剪 |

### 1.3 依赖关系

```
Layer 0/1
    ▲
    │
Layer 2 生命周期（本层）
    ▲
    │ 调用 build_context
    │
Memory Middleware
```

---

## 2. 技术选型分析

### 2.1 L0 缓存：cachebox vs cachetools vs Redis vs DiskCache

| 方案 | 性能 | 持久化 | 部署 | 依赖 |
|------|------|--------|------|------|
| **cachebox** ★推荐 | ⚡⚡⚡ 100万 ops/s（Cython）| ❌ 进程内 | 嵌入式 | 零 |
| **cachetools** | ⚡ 10万 ops/s（纯 Python）| ❌ | 嵌入式 | 零 |
| **functools.lru_cache** | ⚡⚡ 标准 | ❌ | 嵌入式 | Python 自带 |
| **Redis** | ⚡⚡ 10万 ops/s | ✅ AOF/RDB | 独立服务 | 需部署 |
| **DiskCache** | ⚡ 1万 ops/s | ✅ SQLite | 嵌入式 | 零 |

**推荐方案**：**cachebox（进程内）+ SQLite 快照（持久化）**

**选型理由**：
- **cachebox 性能最优**：Cython 加速，比 cachetools 快 10x
- **进程内亚微秒延迟**：L0 高频访问每轮对话多次读写
- **SQLite 复用主库**：无需额外服务（与 DiskCache 冗余）
- **崩溃恢复**：cachebox 丢失时从 SQLite 快照恢复（< 50ms）

```python
from cachebox import TTLPrefixCache

class L0WorkingMemory:
    def __init__(self, db):
        self.hot = TTLPrefixCache(
            maxsize=1000,       # 1000 个活跃会话
            ttl=3600,           # 1 小时未访问过期
            policy="lru",
        )
        self.db = db
```

**被否决方案**：
- **Redis**：嵌入式场景过度设计，违反零外部依赖原则
- **DiskCache**：与 SQLite 冗余，多一层 SQLite 无收益
- **cachetools**：纯 Python，热路径性能不足
- **functools.lru_cache**：不支持 TTL，灵活性差

---

### 2.2 Token 计数：tiktoken vs HuggingFace vs 估算

| 方案 | 精度 | 速度 | 离线 | 模型覆盖 |
|------|------|------|------|---------|
| **tiktoken** ★OpenAI 系 | 精确（BPE）| ⚡⚡⚡ 快 | ✅ | GPT 系列 |
| **transformers tokenizer** | 精确 | ⚡ 中 | ✅ | 所有 HF 模型 |
| **粗略估算**（chars/4）| ±20% | ⚡⚡⚡⚡ 极快 | ✅ | 通用 |
| **API 远程计数** | 精确 | ❌ 慢 | ❌ | 取决于 API |

**推荐方案**：**tiktoken 主方案 + 粗略估算 fallback**

**选型理由**：
- **tiktoken 性能最优**：Rust 实现，100 万 tokens/s
- **覆盖主流模型**：GPT-3.5/4，可扩展到其他 BPE 模型
- **fallback 机制**：tiktoken 加载失败时用 `chars/4` 估算

```python
import tiktoken

class TokenCounter:
    def __init__(self):
        try:
            self.enc = tiktoken.encoding_for_model("gpt-4")
            self._mode = "tiktoken"
        except:
            self._mode = "estimate"
    
    def count(self, text: str) -> int:
        if self._mode == "tiktoken":
            return len(self.enc.encode(text))
        return len(text) // 4  # 粗略估算
```

---

### 2.3 上下文构建策略：贪心 vs 动态规划 vs 优先级

| 策略 | 复杂度 | Token 利用率 | 实现难度 |
|------|--------|------------|---------|
| **贪心（按优先级填充）** ★推荐 | O(N) | 高 | 低 |
| **动态规划（背包）** | O(N×B) | 最高 | 高 |
| **比例分配（固定比例）** | O(N) | 中 | 极低 |
| **强化学习（自适应）** | — | 高 | 极高 |

**推荐方案**：**贪心策略（按 L0 > L1 > L2 > L3 优先级）**

**选型理由**：
- **实现简单**：无需复杂数据结构
- **效果接近最优**：动态规划仅多利用 5-10% tokens
- **可解释**：用户能看到每层占用多少 tokens

---

### 2.4 Embedding 模型：OpenAI vs BGE vs E5 vs Cohere

| 方案 | 维度 | 质量（MTEB）| 成本 | 离线 | 多语言 |
|------|------|-----------|------|------|--------|
| **OpenAI text-embedding-3-large** | 3072 | 64.6 | $0.13/M | ❌ | ✅ |
| **BGE-M3** ★推荐 | 1024 | 64.7 | 免费 | ✅ | ✅ |
| **E5-mistral-7b** | 4096 | 66.6 | 免费（自部署）| ✅ | ✅ |
| **Cohere embed-v3** | 1024 | 64.5 | $0.10/M | ❌ | ✅ |
| **本地小模型 all-MiniLM** | 384 | 56.5 | 免费 | ✅ | ⚠️ |

**推荐方案**：**BGE-M3（本地，1024 维）**

**选型理由**：
- **免费 + 离线**：完全本地运行，无 API 成本
- **质量接近 OpenAI**：MTEB 得分 64.7 vs 64.6
- **多语言**：中英混合场景表现优
- **1024 维平衡**：比 3072 维节省 66% 存储

```python
from FlagEmbedding import BGEM3FlagModel

class LocalEmbedder:
    def __init__(self):
        self.model = BGEM3FlagModel('BAAI/bge-m3', use_fp16=True)
    
    def embed(self, text: str) -> list[float]:
        return self.model.encode(text)['dense_vecs'].tolist()
```

**被否决方案**：
- **OpenAI**：API 成本 + 离线不可用
- **E5-mistral-7b**：模型大（14GB），加载慢
- **all-MiniLM**：质量不足（仅 56.5）

---

## 3. 核心算法与优化

### 3.1 Token Budget 分配算法（贪心 + 优先级）

```python
TOKEN_BUDGET = {"L0": 500, "L1": 1000, "L2": 2000, "L3": 500}
SAFETY_MARGIN = 0.7  # 实际预算 = 总预算 × 0.7

async def build_context(query: RecallRequest) -> str:
    budget = int(query.token_budget * SAFETY_MARGIN)  # 4000 × 0.7 = 2800
    
    parts = []  # [(layer, content, tokens)]
    
    # L0: 全量加载（最高优先级）
    l0 = await load_l0_session(query.session_id)
    if l0:
        parts.append(("L0", l0, count_tokens(l0)))
    
    # L1: Top-K 向量检索
    l1 = await vector_search(query.query, top_k=5)
    parts.append(("L1", format_l1(l1), count_tokens(format_l1(l1))))
    
    # L2: 混合检索 Top-K
    l2 = await hybrid_memory_search(query, top_k=10)
    parts.append(("L2", format_l2(l2), count_tokens(format_l2(l2))))
    
    # L3: 概念匹配
    l3 = await concept_match(query.query, top_k=3)
    parts.append(("L3", format_l3(l3), count_tokens(format_l3(l3))))
    
    # 贪心裁剪
    return greedy_trim(parts, budget)

def greedy_trim(parts: list, budget: int) -> str:
    """按优先级 L0 > L1 > L2 > L3 贪心填充"""
    result = []
    used = 0
    for layer, content, tokens in parts:  # 已按优先级排序
        if used + tokens <= budget:
            result.append(content)
            used += tokens
        else:
            # 超预算，部分填充（保留前 N 条）
            remaining = budget - used
            partial = truncate_to_tokens(content, remaining)
            if partial:
                result.append(partial)
            break
    return "\n\n".join(result)
```

**优化技巧**：
- **预算预留**：总预算 × 0.7 留作安全边际
- **早停**：预算满即停，不再查询下层
- **部分填充**：超预算时保留前 N 条而非整体丢弃

---

### 3.2 L0 缓存淘汰策略（LRU + TTL + 优先级）

**问题**：cachebox 满了如何淘汰？纯 LRU 可能淘汰活跃会话。

**算法**：优先级 LRU

```python
from cachebox import TTLPrefixCache

class PriorityLRU:
    def __init__(self, maxsize=1000):
        self.cache = TTLPrefixCache(maxsize=maxsize, ttl=3600)
        self.priorities = {}  # session_id → priority
    
    def get(self, session_id: str):
        self.priorities[session_id] = self.priorities.get(session_id, 0) + 1
        return self.cache.get(session_id)
    
    def set(self, session_id: str, data: bytes):
        # 高优先级会话不被淘汰
        if self.cache.full() and session_id not in self.cache:
            self._evict_low_priority()
        self.cache.set(session_id, data)
    
    def _evict_low_priority(self):
        """淘汰最低优先级的会话"""
        if not self.priorities:
            return
        victim = min(self.priorities, key=self.priorities.get)
        self.cache.delete(victim)
        del self.priorities[victim]
```

---

### 3.3 L0→L1 溢出策略

**问题**：L0 工作记忆满了，如何溢出到 L1 短时记忆？

**算法**：基于 token 阈值的溢出 + 摘要压缩

```python
L0_OVERFLOW_THRESHOLD = 800  # L0 超 800 tokens 触发溢出

async def maybe_overflow_l0(session_id: str):
    content = await l0.get(session_id)
    if count_tokens(content) <= L0_OVERFLOW_THRESHOLD:
        return
    
    # 1. 切分 L0 为"保留段"和"溢出段"
    keep, overflow = split_by_tokens(content, keep_tokens=300)
    
    # 2. 溢出段写入 L1（向量检索）
    embedding = await embedder.embed(overflow)
    await l1.add(
        id=str(ULID()),
        document=overflow,
        embedding=embedding,
        metadata={"session_id": session_id, "layer": "L1"}
    )
    
    # 3. 保留段更新 L0
    await l0.set(session_id, keep)
```

---

### 3.4 L1→L2 整合算法

**问题**：会话结束时，L1 短时记忆如何整合到 L2 长时记忆？

**算法**：提取实体 + 三元组 + 写入 SQLite

```python
async def consolidate_l1_to_l2(session_id: str):
    """会话结束时，L1 整合到 L2"""
    # 1. 提取 L1 所有片段
    fragments = await l1.get_by_session(session_id)
    
    # 2. LLM 提取实体与关系
    extraction = await llm.extract(
        prompt=EXTRACT_PROMPT,
        content="\n".join(fragments),
    )
    
    # 3. 写入 L2 SQLite（实体 + 三元组）
    async with db.transaction():
        for entity in extraction.entities:
            await db.upsert_entity(entity)
        for triple in extraction.triples:
            await db.upsert_triple(triple)
    
    # 4. 清理 L1（可选）
    await l1.delete_by_session(session_id)
```

**优化**：
- **批量写入**：所有实体/三元组在一个事务内
- **去重**：upsert 而非 insert
- **异步执行**：会话结束后台整合，不阻塞用户

---

### 3.5 上下文压缩算法

**问题**：检索结果超出 token 预算，如何压缩？

**算法 1**：摘要压缩

```python
async def compress_with_summary(content: str, target_tokens: int) -> str:
    if count_tokens(content) <= target_tokens:
        return content
    # LLM 摘要
    return await llm.summarize(content, max_tokens=target_tokens)
```

**算法 2**：截断 + 关键句

```python
async def compress_with_keywords(content: str, target_tokens: int) -> str:
    """基于关键词重要性的截断"""
    sentences = split_sentences(content)
    scored = [(s, keyword_score(s, query_keywords)) for s in sentences]
    scored.sort(key=lambda x: -x[1])
    
    result = []
    used = 0
    for sentence, _ in scored:
        tokens = count_tokens(sentence)
        if used + tokens > target_tokens:
            continue
        result.append(sentence)
        used += tokens
    return " ".join(result)
```

**性能对比**：
| 方法 | 压缩比 | 信息保留 | 延迟 |
|------|--------|---------|------|
| LLM 摘要 | 高 | ⭐⭐⭐⭐⭐ | 慢（500ms+）|
| **关键句截断** ★ | 中 | ⭐⭐⭐⭐ | 快（< 10ms）|
| 朴素截断 | 低 | ⭐⭐ | 极快（< 1ms）|

**推荐**：MVP 用关键句截断，P2 评估 LLM 摘要。

---

### 3.6 L3 语义记忆概念匹配

**问题**：L3 存"抽象概念"，如何匹配查询？

**算法**：概念图遍历 + 同义词扩展

```python
async def concept_match(query: str, top_k: int = 3) -> list[Concept]:
    """L3 语义检索"""
    # 1. 提取查询中的概念
    query_concepts = await llm.extract_concepts(query)
    
    # 2. 同义词扩展
    expanded = set()
    for c in query_concepts:
        expanded.add(c)
        expanded.update(await get_synonyms(c))
    
    # 3. 图谱遍历找相关概念
    candidates = []
    for concept in expanded:
        related = await graph.find_n_degree_relations(concept, depth=2)
        candidates.extend(related)
    
    # 4. 按相关度排序
    return sorted(candidates, key=lambda x: -x.relevance)[:top_k]
```

---

## 4. 实施建议

### 4.1 MVP 范围

```
src/memory/lifecycle/
├── working.py             # L0 cachebox + SQLite 快照
├── shortterm.py           # L1 ChromaDB
├── context_builder.py     # Token Budget
├── counter.py             # TokenCounter
└── embedder.py            # LocalEmbedder（BGE-M3）
```

L2/L3 暂用 SQLite + 图谱检索的最小实现，P1 增强。

### 4.2 关键风险

| 风险 | 缓解 |
|------|------|
| cachebox Windows 兼容性 | 测试 Win10/11，备选 cachetools |
| BGE-M3 模型大（2GB） | 首次启动下载，缓存到 `~/.cache/` |
| Token 计数不准 | tiktoken + fallback estimate |
| 整合任务拖慢响应 | 异步执行，不阻塞用户 |

### 4.3 验证方式

```python
async def test_token_budget():
    """Token Budget 不超限"""
    ctx = await context_builder.build_context(query="...", token_budget=4000)
    assert count_tokens(ctx) <= 4000

async def test_l0_overflow():
    """L0 溢出触发"""
    # 写入大量内容
    for i in range(100):
        await l0.set(session_id, l0.get(session_id, "") + f" message {i}")
    # 验证溢出到 L1
    assert await l1.count(session_id) > 0

async def test_priority_lru():
    """优先级 LRU 淘汰"""
    # 高频访问的会话不被淘汰
    for _ in range(1000):
        await l0.get("active_session")
    await l0.set("new_session", "...")  # 触发淘汰
    assert "active_session" in l0.cache
```

### 4.4 性能基线

| 操作 | 目标延迟 |
|------|---------|
| L0 读取（cachebox 命中）| < 1μs |
| L0 读取（SQLite fallback）| < 1ms |
| L0 写入 | < 1μs |
| L1 向量检索（Top-5）| < 50ms |
| L2 混合检索（Top-10）| < 200ms |
| L3 概念匹配（Top-3）| < 100ms |
| 上下文构建（4000 tokens）| < 500ms |
| Token 计数（1000 字符）| < 1ms |

---

## 5. 后续演进路径

| 阶段 | 增强点 |
|------|--------|
| MVP | L0/L1 + 基础 ContextBuilder |
| P1 | L2 混合检索 + L1→L2 整合任务 |
| P2 | L3 语义 + LLM 摘要压缩 |
| P3 | 反馈驱动启发式 Token 分配**[2026-09 降级: 原"强化学习"否决]**
  — 单用户库 RL 不可训练/不可测,与确定性贪心装填的测试体系冲突;
  改为 schema 014 的 access_count/留存分信号加权装填排序,确定性拿到
  90% 价值 |
