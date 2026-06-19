# MemPalace 双向指针机制详解

> 创建日期：2026-05-25
> 基于：MemPalace 源码分析
> 目标：深入理解 Palace 与 Knowledge Graph 的双向指针设计

---

## 一、核心机制概述

### 1.1 什么是双向指针

MemPalace 采用**双存储架构**，通过 **source_closet** 字段建立双向可追溯的链接：

```
┌─────────────────────────────────────────────────────────────────┐
│              MemPalace 双存储系统的连接机制                         │
│                                                                 │
│   Palace (ChromaDB)              Knowledge Graph (SQLite)       │
│   ┌─────────────────┐            ┌─────────────────┐          │
│   │ Drawer 001      │            │ Triple          │          │
│   │                 │            │                 │          │
│   │ content: "Maya  │            │ subject: Maya   │          │
│   │ approved Clerk" │            │ predicate: works │          │
│   │                 │ ←────────→ │ object: Orion   │          │
│   │ source_closet:  │   双向指针  │                 │          │
│   │   "triple_001"  │            │ source_closet:  │          │
│   │                 │            │   "drawer_001"  │          │
│   └─────────────────┘            └─────────────────┘          │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 设计目标

| 目标 | 说明 |
|------|------|
| **证据追溯** | 从 KG 事实 → 追溯到原文证据 |
| **一致性保证** | 确保 KG 中的事实与 Palace 中的原文对应 |
| **矛盾检测** | 检测新旧知识的冲突 |
| **来源追踪** | 知道每条知识来自哪条原始记录 |

---

## 二、数据结构详解

### 2.1 Palace (ChromaDB) 中的 Drawer

```python
# 每个 drawer 存入 ChromaDB 时的 metadata

drawer_metadata = {
    # ===== 空间结构（宫殿隐喻）=====
    "wing": "driftwood",              # 项目/人员（如项目名、人名）
    "room": "auth-migration",        # 话题（如 auth-migration）
    "hall": "decisions",             # 类型（facts/events/decisions/discoveries/preferences/advice）
    
    # ===== 追溯指针（核心！）=====
    "source_closet": "triple_001",   # ← 指向 KG 中的三元组 ID
    
    # ===== 附加信息 =====
    "created_at": "2026-01-15T14:30:00",
    "updated_at": "2026-01-15T14:30:00",
    "importance": 0.9,               # 重要性（用于 L1 层筛选）
    "source_file": "meeting_2026_01_15.jsonl",  # 原始来源
    "chunk_index": 3,                # 在源文件中的位置
    "hash": "abc123def456",          # 内容哈希（去重用）
}

# drawer 的完整存储
drawer = {
    "id": "drawer_001",
    "document": "Maya 批准了 Clerk 而非 Auth0，因为 Clerk 的 React 集成更好",
    "embedding": [0.023, -0.051, ...],  # 语义向量
    "metadata": drawer_metadata,
}
```

### 2.2 Knowledge Graph (SQLite) 中的 Triple

```sql
-- triples 表结构

CREATE TABLE triples (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,          -- 实体（如 Maya、team）
    predicate TEXT NOT NULL,         -- 关系（如 works_on、decided）
    object TEXT NOT NULL,            -- 对象（如 Orion、Clerk）
    
    -- ===== 追溯指针（核心！）=====
    source_closet TEXT,             -- ← 指向 Palace 中的 drawer ID
    
    -- ===== 时间有效性 =====
    valid_from TEXT,                -- 生效时间（如 "2026-01-15"）
    valid_to TEXT,                  -- 失效时间（NULL 表示未失效）
    
    -- ===== 元数据 =====
    confidence REAL DEFAULT 1.0,    -- 置信度
    source_type TEXT,               -- 来源类型（meeting/chat/manual）
    
    -- ===== 审计字段 =====
    created_at TEXT,
    updated_at TEXT,
    
    -- 索引
    UNIQUE(subject, predicate, object, valid_from)
);
```

### 2.3 关系示例数据

```
triples 表内容：

┌────────────┬──────────┬───────────┬──────────┬──────────────┬─────────────┐
│ id         │ subject  │ predicate │ object   │ source_closet│ valid_from  │
├────────────┼──────────┼───────────┼──────────┼──────────────┼─────────────┤
│ triple_001 │ Maya     │ works_on  │ Orion    │ drawer_001   │ 2026-01-15 │
│ triple_002 │ Kai      │ debug     │ OAuth    │ drawer_002   │ 2026-01-16 │
│ triple_003 │ team     │ decided   │ Clerk    │ drawer_003   │ 2026-01-17 │
│ triple_004 │ Maya     │ approved  │ Clerk    │ drawer_004   │ 2026-01-17 │
│ triple_005 │ team     │ migrated  │ GraphQL  │ drawer_005   │ 2026-02-01 │
└────────────┴──────────┴───────────┴──────────┴──────────────┴─────────────┘

palace.drawers 表内容：

┌────────────┬──────────────────────────────────────────────────┬──────────────┐
│ id         │ document                                         │ source_closet│
├────────────┼──────────────────────────────────────────────────┼──────────────┤
│ drawer_001 │ Maya approved Clerk over Auth0...                │ triple_001   │
│ drawer_002 │ Kai debugging OAuth token refresh issue...      │ triple_002   │
│ drawer_003 │ Team meeting: decided to use Clerk...           │ triple_003   │
│ drawer_004 │ Maya reviewed the integration options...         │ triple_004   │
│ drawer_005 │ Migration completed from REST to GraphQL...      │ triple_005   │
└────────────┴──────────────────────────────────────────────────┴──────────────┘
```

---

## 三、核心操作实现

### 3.1 写入操作

```python
# palace/graph.py

class KnowledgeGraph:
    """Knowledge Graph 操作"""
    
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.palace = Palace()  # Palace 实例
    
    def add_triple(
        self,
        subject: str,
        predicate: str,
        object: str,
        valid_from: str = None,
        source_closet: str = None,  # Palace drawer ID
        **kwargs
    ) -> str:
        """
        添加三元组，同时更新 Palace 端的双向指针
        
        Args:
            subject: 实体
            predicate: 关系
            object: 对象
            valid_from: 生效时间
            source_closet: 来源 drawer ID（如果有）
        """
        triple_id = f"triple_{uuid.uuid4().hex[:8]}"
        now = datetime.now().isoformat()
        
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO triples 
            (id, subject, predicate, object, source_closet, 
             valid_from, valid_to, confidence, source_type, 
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, 
            triple_id,
            subject,
            predicate,
            object,
            source_closet,
            valid_from or now,
            kwargs.get("valid_to"),
            kwargs.get("confidence", 1.0),
            kwargs.get("source_type", "manual"),
            now,
            now,
        )
        
        self.conn.commit()
        
        # ===== 关键：更新 Palace 端的 source_closet =====
        if source_closet:
            self.palace.update_source_closet(source_closet, triple_id)
        
        return triple_id
    
    def update_source_closet(self, triple_id: str, drawer_id: str):
        """
        更新三元组的 source_closet 字段
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE triples SET source_closet = ? WHERE id = ?
        """, drawer_id, triple_id)
        self.conn.commit()


# palace/palace.py

class Palace:
    """Palace 操作"""
    
    def update_source_closet(self, drawer_id: str, triple_id: str):
        """
        更新 Palace drawer 的 source_closet 字段
        
        实际实现：在 ChromaDB 中更新 metadata
        """
        self.collection.update(
            ids=[drawer_id],
            metadatas=[{
                "source_closet": triple_id,  # ← 建立指向
            }]
        )
```

### 3.2 读取操作

```python
# palace/graph.py

class KnowledgeGraph:
    """Knowledge Graph 查询"""
    
    def get_triple(self, triple_id: str) -> Optional[dict]:
        """获取单个三元组"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM triples WHERE id = ?
        """, (triple_id,))
        
        row = cursor.fetchone()
        if row:
            return self._row_to_dict(row)
        return None
    
    def get_entity_triples(
        self, 
        subject: str = None,
        predicate: str = None,
        object: str = None,
        as_of: str = None,  # 时间点查询
    ) -> list[dict]:
        """
        获取实体相关的三元组
        
        Args:
            subject: 实体名
            predicate: 关系名
            object: 对象
            as_of: 查询某时间点的状态
        """
        cursor = self.conn.cursor()
        
        query = "SELECT * FROM triples WHERE 1=1"
        params = []
        
        if subject:
            query += " AND subject = ?"
            params.append(subject)
        if predicate:
            query += " AND predicate = ?"
            params.append(predicate)
        if object:
            query += " AND object = ?"
            params.append(object)
        
        # 时间有效性过滤
        if as_of:
            query += " AND (valid_from IS NULL OR valid_from <= ?)"
            params.append(as_of)
            query += " AND (valid_to IS NULL OR valid_to > ?)"
            params.append(as_of)
        
        query += " ORDER BY valid_from DESC"
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        return [self._row_to_dict(row) for row in rows]
    
    def timeline(self, entity: str) -> list[dict]:
        """
        获取实体的完整时间线
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM triples 
            WHERE subject = ? OR object = ?
            ORDER BY valid_from ASC
        """, (entity, entity))
        
        rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]
```

### 3.3 关系操作

```python
class MemoryRelations:
    """记忆关系操作"""
    
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.kg = KnowledgeGraph(db_path)
        self.palace = Palace()
    
    def link_drawer_to_triple(self, drawer_id: str, triple_id: str):
        """
        建立 Drawer 和 Triple 的双向链接
        
        Drawer.source_closet = triple_id
        Triple.source_closet = drawer_id
        """
        # 1. 更新 Palace 端
        self.palace.update_metadata(drawer_id, {
            "source_closet": triple_id
        })
        
        # 2. 更新 KG 端
        self.kg.update_source_closet(triple_id, drawer_id)
    
    def unlink(self, drawer_id: str):
        """
        解除链接（删除时调用）
        """
        # 获取 triple ID
        drawer = self.palace.get_drawer(drawer_id)
        if drawer and drawer.get("metadata", {}).get("source_closet"):
            triple_id = drawer["metadata"]["source_closet"]
            
            # 清除 Palace 端
            self.palace.update_metadata(drawer_id, {
                "source_closet": None
            })
            
            # 清除 KG 端
            self.kg.update_source_closet(triple_id, None)
```

---

## 四、来源追溯实现

### 4.1 从 KG 追溯到 Palace（原文）

```python
# palace/traceability.py

class Traceability:
    """来源追溯：从 KG 事实 → Palace 原文"""
    
    def __init__(self, kg: KnowledgeGraph, palace: Palace):
        self.kg = kg
        self.palace = palace
    
    def trace_triple_to_original(self, triple_id: str) -> dict:
        """
        从三元组追溯到原文
        
        场景：用户问" Maya 负责什么项目？"
        """
        # 1. 获取三元组
        triple = self.kg.get_triple(triple_id)
        if not triple:
            return {"error": "Triple not found"}
        
        # 2. 读取 source_closet
        drawer_id = triple.get("source_closet")
        
        # 3. 获取原文
        if drawer_id:
            drawer = self.palace.get_drawer(drawer_id)
        else:
            drawer = None
        
        return {
            "triple": triple,
            "drawer_id": drawer_id,
            "original_text": drawer["document"] if drawer else None,
            "traceable": drawer_id is not None,
        }
    
    def trace_entity_to_evidence(self, entity: str) -> list[dict]:
        """
        从实体追溯所有证据
        
        场景：用户问"关于 Maya 的所有信息"
        """
        # 1. 获取实体的所有三元组
        triples = self.kg.get_entity_triples(subject=entity)
        
        # 2. 追溯每个三元组的原文
        evidence = []
        for triple in triples:
            trace = self.trace_triple_to_original(triple["id"])
            evidence.append({
                "triple": triple,
                "original_text": trace["original_text"],
                "drawer_id": trace["drawer_id"],
                "confidence": triple.get("confidence", 1.0),
            })
        
        return evidence
    
    def get_complete_context(self, triple_id: str) -> dict:
        """
        获取完整上下文（事实 + 原文 + 时间线）
        """
        triple = self.kg.get_triple(triple_id)
        trace = self.trace_triple_to_original(triple_id)
        
        # 获取实体的完整时间线
        timeline = self.kg.timeline(triple["subject"])
        
        return {
            "fact": {
                "subject": triple["subject"],
                "predicate": triple["predicate"],
                "object": triple["object"],
                "valid_from": triple["valid_from"],
                "valid_to": triple["valid_to"],
            },
            "evidence": {
                "original_text": trace["original_text"],
                "drawer_id": trace["drawer_id"],
                "source_type": triple.get("source_type"),
            },
            "timeline": [
                {
                    "date": t["valid_from"],
                    "fact": f"{t['subject']} {t['predicate']} {t['object']}"
                }
                for t in timeline
            ],
        }
```

### 4.2 从 Palace 追溯到 KG（事实）

```python
class ReverseTraceability:
    """反向追溯：从 Palace 原文 → KG 事实"""
    
    def __init__(self, kg: KnowledgeGraph, palace: Palace):
        self.kg = kg
        self.palace = palace
    
    def trace_drawer_to_facts(self, drawer_id: str) -> list[dict]:
        """
        从 drawer 追溯到相关的事实
        
        场景：查看某条原始记录对应的结构化事实
        """
        # 1. 获取 drawer
        drawer = self.palace.get_drawer(drawer_id)
        if not drawer:
            return []
        
        # 2. 读取 source_closet
        source_closet = drawer.get("metadata", {}).get("source_closet")
        
        facts = []
        
        # 3. 如果有链接，获取链接的三元组
        if source_closet:
            triple = self.kg.get_triple(source_closet)
            if triple:
                facts.append(triple)
        
        # 4. 也可以通过实体查找相关事实
        # 从 drawer 内容中提取实体，查找相关事实
        entities = self._extract_entities(drawer["document"])
        for entity in entities:
            related = self.kg.get_entity_triples(subject=entity)
            facts.extend(related)
        
        # 去重
        seen = set()
        unique_facts = []
        for fact in facts:
            if fact["id"] not in seen:
                seen.add(fact["id"])
                unique_facts.append(fact)
        
        return unique_facts
    
    def _extract_entities(self, text: str) -> list[str]:
        """从文本中提取实体（简化版）"""
        # 实际应该用 NER，这里用简单实现
        # 假设实体是首字母大写的词
        import re
        return re.findall(r'\b[A-Z][a-z]+\b', text)
```

---

## 五、一致性验证实现

### 5.1 KG 事实一致性检查

```python
# palace/consistency.py

class ConsistencyChecker:
    """一致性检查"""
    
    def __init__(self, kg: KnowledgeGraph, palace: Palace):
        self.kg = kg
        self.palace = palace
    
    def check_entity_consistency(self, entity: str) -> dict:
        """
        检查实体的所有事实是否一致
        
        场景：
        - KG 说 "Maya works_on Orion"
        - KG 说 "Maya works_on Driftwood"
        - 两者是否矛盾？
        """
        # 1. 获取实体的所有三元组
        triples = self.kg.get_entity_triples(subject=entity)
        
        # 2. 按谓词分组
        by_predicate = {}
        for triple in triples:
            pred = triple["predicate"]
            if pred not in by_predicate:
                by_predicate[pred] = []
            by_predicate[pred].append(triple)
        
        # 3. 检查每个谓词下的事实
        consistency_report = []
        
        for predicate, facts in by_predicate.items():
            if len(facts) == 1:
                # 只有一个事实，无冲突
                continue
            
            # 检查时间上是否有重叠
            timeline_check = self._check_timeline(facts)
            
            consistency_report.append({
                "predicate": predicate,
                "facts": facts,
                "timeline_check": timeline_check,
            })
        
        return {
            "entity": entity,
            "total_triples": len(triples),
            "consistency_issues": consistency_report,
            "overall_consistent": all(
                r["timeline_check"]["consistent"] 
                for r in consistency_report
            ),
        }
    
    def _check_timeline(self, facts: list[dict]) -> dict:
        """
        检查事实的时间线是否一致
        
        逻辑：如果同一谓词下的多个事实时间不重叠，可能是冲突或更新
        """
        # 按时间排序
        sorted_facts = sorted(
            facts,
            key=lambda x: x.get("valid_from", "")
        )
        
        # 检查时间连续性
        issues = []
        for i in range(len(sorted_facts) - 1):
            current = sorted_facts[i]
            next_fact = sorted_facts[i + 1]
            
            current_end = current.get("valid_to")
            next_start = next_fact.get("valid_from")
            
            # 如果当前事实还未结束，下一个事实就开始，可能是冲突
            if not current_end and next_start:
                issues.append({
                    "type": "overlap",
                    "fact1": f"{current['subject']} {current['predicate']} {current['object']}",
                    "fact2": f"{next_fact['subject']} {next_fact['predicate']} {next_fact['object']}",
                })
        
        return {
            "sorted_facts": sorted_facts,
            "issues": issues,
            "consistent": len(issues) == 0,
        }
    
    def verify_against_original(self, triple_id: str) -> dict:
        """
        验证 KG 事实是否与原文一致
        
        场景：检查 triple 说的是否和 drawer 原文一致
        """
        triple = self.kg.get_triple(triple_id)
        if not triple:
            return {"error": "Triple not found"}
        
        # 追溯原文
        drawer_id = triple.get("source_closet")
        if not drawer_id:
            return {
                "verified": False,
                "reason": "No source_closet link",
                "triple": triple,
            }
        
        drawer = self.palace.get_drawer(drawer_id)
        if not drawer:
            return {
                "verified": False,
                "reason": "Drawer not found",
                "triple": triple,
            }
        
        # 检查原文是否包含三元组的关键信息
        original_text = drawer["document"].lower()
        subject = triple["subject"].lower()
        object = triple["object"].lower()
        predicate = triple["predicate"].lower()
        
        # 简单匹配检查
        checks = {
            "subject_mentioned": subject in original_text,
            "object_mentioned": object in original_text,
            "predicate_mentioned": predicate in original_text,
        }
        
        return {
            "verified": all(checks.values()),
            "checks": checks,
            "triple": triple,
            "original_text": drawer["document"],
        }
```

### 5.2 全文 vs 片段一致性

```python
class ContentConsistency:
    """内容一致性检查"""
    
    def __init__(self, palace: Palace, llm_client=None):
        self.palace = palace
        self.llm = llm_client
    
    def check_drawer_consistency(self, drawer_id: str) -> dict:
        """
        检查 drawer 内容的一致性
        
        场景：一个 drawer 可能包含多个事实，需要检查是否自相矛盾
        """
        drawer = self.palace.get_drawer(drawer_id)
        if not drawer:
            return {"error": "Drawer not found"}
        
        text = drawer["document"]
        
        # 如果有 LLM，可以做更深入的一致性检查
        if self.llm:
            return self._llm_check_consistency(text)
        
        # 简单检查：提取时间/数量等是否矛盾
        return self._simple_check_consistency(text)
    
    def _simple_check_consistency(self, text: str) -> dict:
        """简单一致性检查"""
        issues = []
        
        # 检查时间一致性
        dates = self._extract_dates(text)
        if len(dates) > 1:
            sorted_dates = sorted(dates)
            if sorted_dates[0] > sorted_dates[-1]:
                issues.append({
                    "type": "time_contradiction",
                    "message": "时间顺序矛盾"
                })
        
        # 检查数量一致性
        numbers = self._extract_numbers(text)
        for num in numbers:
            if num["text"].count(num["value"]) > 1:
                issues.append({
                    "type": "number_contradiction",
                    "message": f"数字 {num['value']} 出现多次"
                })
        
        return {
            "consistent": len(issues) == 0,
            "issues": issues,
        }
    
    def _llm_check_consistency(self, text: str) -> dict:
        """使用 LLM 做一致性检查"""
        prompt = f"""
        检查以下文本是否存在内部矛盾：

        文本：
        {text}

        输出格式（JSON）：
        {{
            "consistent": true/false,
            "issues": [
                {{"type": "类型", "description": "描述"}}
            ]
        }}
        """
        
        result = self.llm.generate_json(prompt)
        return result
```

---

## 六、矛盾检测实现

### 6.1 新旧知识矛盾检测

```python
# palace/contradiction.py

class ContradictionDetector:
    """矛盾检测"""
    
    def __init__(self, kg: KnowledgeGraph, palace: Palace):
        self.kg = kg
        self.palace = palace
    
    def detect(
        self,
        new_triple: dict,
    ) -> dict:
        """
        检测新事实是否与旧事实矛盾
        
        场景：
        - 旧记录：(team, decided, Clerk) @ 2026-01-17
        - 新知识：用户说"团队最终选择了 Auth0"
        """
        subject = new_triple.get("subject")
        predicate = new_triple.get("predicate")
        new_object = new_triple.get("object")
        
        # 1. 查找相同主语+谓语的历史记录
        existing = self.kg.get_entity_triples(
            subject=subject,
            predicate=predicate,
        )
        
        # 2. 检查是否有不同对象的事实
        contradictions = []
        for old_triple in existing:
            if old_triple["object"] != new_object:
                # 可能矛盾
                contradiction = self._analyze_contradiction(
                    old_triple, new_triple
                )
                if contradiction["is_contradiction"]:
                    contradictions.append(contradiction)
        
        if contradictions:
            return {
                "has_contradiction": True,
                "contradictions": contradictions,
                "new_fact": new_triple,
                "resolution_options": [
                    "保留旧事实，忽略新事实",
                    "invalidate 旧事实，接受新事实",
                    "保留两条，标记为冲突",
                    "让用户确认",
                ],
            }
        
        return {
            "has_contradiction": False,
            "new_fact": new_triple,
        }
    
    def _analyze_contradiction(
        self,
        old_triple: dict,
        new_triple: dict,
    ) -> dict:
        """
        分析新旧事实的矛盾类型
        """
        # 1. 获取两条记录的原文证据
        old_drawer_id = old_triple.get("source_closet")
        new_drawer_id = new_triple.get("source_closet")
        
        old_text = None
        new_text = None
        
        if old_drawer_id:
            old_drawer = self.palace.get_drawer(old_drawer_id)
            old_text = old_drawer["document"] if old_drawer else None
        
        if new_drawer_id:
            new_drawer = self.palace.get_drawer(new_drawer_id)
            new_text = new_drawer["document"] if new_drawer else None
        
        # 2. 分析矛盾类型
        contradiction_type = self._classify_contradiction(
            old_triple, new_triple
        )
        
        # 3. 计算严重程度
        severity = self._calculate_severity(
            old_triple, new_triple
        )
        
        return {
            "is_contradiction": True,
            "type": contradiction_type,
            "severity": severity,
            "old_fact": {
                "id": old_triple["id"],
                "triple": f"{old_triple['subject']} {old_triple['predicate']} {old_triple['object']}",
                "valid_from": old_triple.get("valid_from"),
                "evidence": old_text,
            },
            "new_fact": {
                "id": new_triple["id"],
                "triple": f"{new_triple['subject']} {new_triple['predicate']} {new_triple['object']}",
                "evidence": new_text,
            },
        }
    
    def _classify_contradiction(
        self,
        old_triple: dict,
        new_triple: dict,
    ) -> str:
        """
        分类矛盾类型
        """
        old_from = old_triple.get("valid_from")
        new_from = new_triple.get("valid_from")
        
        # 时间型矛盾：同一关系，不同对象
        # 例如：旧说用 Clerk，新说用 Auth0
        return "factual"  # 事实型矛盾
    
    def _calculate_severity(
        self,
        old_triple: dict,
        new_triple: dict,
    ) -> str:
        """
        计算矛盾严重程度
        """
        # 高：直接影响决策的事实
        # 中：次要属性
        # 低：可忽略的差异
        
        high_impact_predicates = [
            "decided", "approved", "rejected", 
            "responsible_for", "owns"
        ]
        
        if old_triple["predicate"] in high_impact_predicates:
            return "high"
        
        return "medium"
```

### 6.2 矛盾解决

```python
class ContradictionResolver:
    """矛盾解决"""
    
    def __init__(self, kg: KnowledgeGraph, palace: Palace):
        self.kg = kg
        self.palace = palace
    
    def resolve(
        self,
        contradiction: dict,
        resolution: str,
        **kwargs
    ):
        """
        解决矛盾
        
        Args:
            resolution: 
                - "accept_new": 接受新事实，invalidate 旧事实
                - "keep_old": 保留旧事实
                - "keep_both": 保留两条，标记冲突
                - "merge": 合并两条
        """
        old_triple = contradiction["old_fact"]
        new_triple = contradiction["new_fact"]
        
        if resolution == "accept_new":
            # Invalidate 旧事实
            self.kg.invalidate(
                triple_id=old_triple["id"],
                ended=new_triple.get("valid_from") or datetime.now().isoformat(),
            )
            
            return {
                "resolved": True,
                "action": "old_invalidated",
                "new_fact_kept": new_triple,
            }
        
        elif resolution == "keep_old":
            return {
                "resolved": True,
                "action": "old_kept",
                "new_fact_rejected": new_triple,
            }
        
        elif resolution == "keep_both":
            # 标记为冲突
            self._flag_as_conflict(old_triple["id"], new_triple["id"])
            
            return {
                "resolved": True,
                "action": "both_kept_flagged",
                "conflict_id": f"conflict_{old_triple['id']}_{new_triple['id']}",
            }
        
        elif resolution == "merge":
            # 合并两条事实
            merged = self._merge_triples(old_triple, new_triple, **kwargs)
            return {
                "resolved": True,
                "action": "merged",
                "merged_fact": merged,
            }
    
    def _flag_as_conflict(self, triple_id1: str, triple_id2: str):
        """标记为冲突"""
        cursor = self.kg.conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO conflicts
            (id, triple_id_1, triple_id_2, status, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, 
            f"conflict_{triple_id1}_{triple_id2}",
            triple_id1,
            triple_id2,
            "open",
            datetime.now().isoformat(),
        )
        self.kg.conn.commit()
```

---

## 七、实际使用示例

### 7.1 典型查询流程

```python
# 使用示例

async def query_demo():
    """典型查询示例"""
    
    kg = KnowledgeGraph("memory/knowledge_graph.sqlite3")
    palace = Palace("memory/palace/")
    traceability = Traceability(kg, palace)
    
    # ========== 查询1：追溯事实的来源 ==========
    print("=== 查询1：Maya 负责什么项目？ ===")
    
    # 从 KG 获取事实
    triples = kg.get_entity_triples(subject="Maya", predicate="works_on")
    
    for triple in triples:
        # 追溯原文
        trace = traceability.trace_triple_to_original(triple["id"])
        
        print(f"事实：{triple['subject']} {triple['predicate']} {triple['object']}")
        print(f"时间：{triple.get('valid_from')}")
        print(f"原文：{trace['original_text']}")
        print()
    
    # ========== 查询2：查看项目的时间线 ==========
    print("=== 查询2：Orion 项目的演进 ===")
    
    timeline = kg.timeline("Orion")
    
    for event in timeline:
        trace = traceability.trace_triple_to_original(event["id"])
        print(f"[{event.get('valid_from', 'Unknown')}]")
        print(f"  事实：{event['subject']} {event['predicate']} {event['object']}")
        print(f"  来源：{trace['original_text'][:100]}...")
        print()
    
    # ========== 查询3：一致性检查 ==========
    print("=== 查询3：Maya 的信息是否一致？ ===")
    
    consistency = ConsistencyChecker(kg, palace)
    report = consistency.check_entity_consistency("Maya")
    
    print(f"实体：{report['entity']}")
    print(f"总事实数：{report['total_triples']}")
    print(f"一致性：{'✅ 一致' if report['overall_consistent'] else '❌ 有冲突'}")
    
    if report['consistency_issues']:
        for issue in report['consistency_issues']:
            print(f"  问题：{issue['predicate']} - {issue['timeline_check']['issues']}")
```

### 7.2 矛盾检测示例

```python
# 矛盾检测示例

async def contradiction_demo():
    """矛盾检测示例"""
    
    kg = KnowledgeGraph("memory/knowledge_graph.sqlite3")
    palace = Palace("memory/palace/")
    detector = ContradictionDetector(kg, palace)
    resolver = ContradictionResolver(kg, palace)
    
    # 当前 KG 中的事实
    current_fact = {
        "subject": "team",
        "predicate": "decided",
        "object": "Clerk",
        "valid_from": "2026-01-17",
    }
    
    # 新输入：用户说团队选择了 Auth0
    user_input = "团队最终选择了 Auth0 而不是 Clerk"
    
    # 检测矛盾
    # （实际应该先提取三元组，这里简化）
    new_fact = {
        "subject": "team",
        "predicate": "decided",
        "object": "Auth0",
        "source_closet": None,  # 新事实还没有 drawer
    }
    
    result = detector.detect(new_fact)
    
    if result["has_contradiction"]:
        print("⚠️ 检测到矛盾！")
        for c in result["contradictions"]:
            print(f"类型：{c['type']}")
            print(f"严重程度：{c['severity']}")
            print(f"旧事实：{c['old_fact']['triple']}")
            print(f"旧证据：{c['old_fact']['evidence']}")
            print(f"新事实：{c['new_fact']['triple']}")
            print(f"新证据：{c['new_fact']['evidence']}")
            print()
            print("解决选项：")
            for i, option in enumerate(result["resolution_options"]):
                print(f"  {i+1}. {option}")
    else:
        print("✅ 未检测到矛盾")
```

---

## 八、总结

### 8.1 双向指针的价值

| 能力 | 说明 | 价值 |
|------|------|------|
| **来源追溯** | 从 KG → Palace 原文 | 验证事实可信度 |
| **一致性保证** | 两边数据始终对应 | 数据完整性 |
| **矛盾检测** | 新旧知识对比 | 知识质量 |
| **上下文完整** | 事实 + 原文 + 时间线 | 可解释性 |

### 8.2 实现要点

```
写入时：
1. 同时写入 Palace 和 KG
2. 建立双向指针
3. Palace.source_closet = KG.id
4. KG.source_closet = Palace.id

读取时：
1. KG 查询事实
2. 通过 source_closet 追溯原文
3. 提供完整上下文

一致性：
1. 定期检查 Palace 和 KG 的对应关系
2. 删除时同时清理两边
3. 更新时同步指针
```

### 8.3 与你的系统对比

| 维度 | MemPalace | 你的模糊记忆系统 |
|------|-----------|-----------------|
| **存储** | ChromaDB + SQLite | PostgreSQL + ChromaDB |
| **指针** | source_closet | 可用 metadata 实现 |
| **追溯** | ✅ 完整实现 | 需要实现 |
| **一致性** | ✅ 完整实现 | 需要实现 |
| **矛盾检测** | ✅ 完整实现 | 需要实现 |

### 8.4 对你的启发

```
在你的模糊记忆系统中实现双向指针：

1. 简化版实现：
   - 用 metadata 字段存储关联 ID
   - 类似 MemPalace 的 source_closet

2. 关键实现：
   - 写入时同步建立指针
   - 读取时支持追溯
   - 删除时同步清理

3. 可选增强：
   - 一致性检查（定期验证对应关系）
   - 矛盾检测（新知识 vs 旧知识）
```
