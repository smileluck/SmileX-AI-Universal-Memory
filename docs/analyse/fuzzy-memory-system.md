# 模糊记忆系统：支持不确定信息的记忆实现方案

> 创建日期：2026-05-25
> 目标：构建一个能优雅处理碎片化、不完整、不确定信息的记忆系统
> 适用场景：机器人记忆、AI Agent、用户交互日志等无法保证信息完整性的场景

---

## 一、核心理念

### 1.1 问题背景

传统记忆系统要求信息完整才能存储：

```
传统思维：
├─ "时间呢？" → "不确定"
├─ "地点呢？" → "大概在客厅"
└─ 结论：无法存储 ❌

模糊记忆思维：
├─ 时间："大概下午" → 存储为不确定时间范围
├─ 地点："大概在客厅" → 存储为模糊位置
├─ 内容："好像是关于..." → 存储为低置信度记忆
└─ 结论：完整保留 ✅
```

### 1.2 核心哲学

> **"不完美的记忆 > 没有记忆"**
>
> 人的记忆本身就是模糊的、不完整的。
> 机器人的记忆系统也应该接受这种不确定性，
> 而不是要求完美的输入。

---

## 二、数据模型

### 2.1 确定性级别

```python
class CertaintyLevel(Enum):
    """确定性级别"""
    EXACT = "exact"           # 精确知道
    HIGH = "high"             # 基本确定
    MEDIUM = "medium"        # 不太确定
    LOW = "low"               # 猜测
    UNKNOWN = "unknown"       # 完全未知
```

### 2.2 不确定值

```python
@dataclass
class UncertainValue:
    """
    不确定值：值 + 确定性
    
    例如：
    - UncertainValue(value="客厅", certainty=CertaintyLevel.HIGH)
    - UncertainValue(value="大概下午3点", certainty=CertaintyLevel.MEDIUM)
    """
    value: any                          # 值
    certainty: CertaintyLevel = CertaintyLevel.MEDIUM
    note: Optional[str] = None          # 备注，如"用户说大概是"
    
    def to_display(self) -> str:
        """转换为可读显示"""
        if self.certainty == CertaintyLevel.EXACT:
            return str(self.value)
        elif self.certainty == CertaintyLevel.UNKNOWN:
            return "[未知]"
        else:
            return f"{self.value}"
```

### 2.3 时间范围

```python
@dataclass
class TimeRange:
    """
    时间范围：不确定时间用范围表示
    
    例如：
    - TimeRange(exact=datetime(2026, 5, 25, 14, 30))  # 精确时间
    - TimeRange(approx_start=datetime(2026, 5, 25, 14, 0), 
                approx_end=datetime(2026, 5, 25, 16, 0))  # 大概下午
    - TimeRange(relative="刚才")  # 相对时间
    - TimeRange(relative="今天早上")  # 今天某个时段
    """
    exact: Optional[datetime] = None
    
    # 近似时间范围
    approx_start: Optional[datetime] = None
    approx_end: Optional[datetime] = None
    
    # 相对时间描述
    relative: Optional[str] = None  # "刚才"、"之前"、"今天早上"
    
    # 不确定性级别
    certainty: CertaintyLevel = CertaintyLevel.MEDIUM
    
    def overlaps(self, other: 'TimeRange') -> bool:
        """检查两个时间范围是否有重叠"""
        if self.exact and other.exact:
            return self.exact == other.exact
        
        if self.approx_start and other.approx_end:
            return self.approx_start <= other.approx_end
        if self.approx_end and other.approx_start:
            return self.approx_end >= other.approx_start
        
        # 相对时间可能重叠
        return True  # 保守估计
    
    def to_context_string(self) -> str:
        """转换为上下文字符串"""
        if self.exact:
            return self.exact.strftime("%Y年%m月%d日 %H:%M")
        elif self.relative:
            return self.relative
        elif self.approx_start and self.approx_end:
            return f"大概 {self.approx_start.strftime('%H:%M')} - {self.approx_end.strftime('%H:%M')}"
        else:
            return "[时间未知]"
```

### 2.4 模糊位置

```python
@dataclass
class FuzzyLocation:
    """
    模糊位置：支持从精确到完全未知的位置
    
    例如：
    - FuzzyLocation(exact="客厅")  # 确定在客厅
    - FuzzyLocation(hierarchy=["家", "客厅"])  # 在家的客厅
    - FuzzyLocation(region="客厅附近")  # 大概在客厅附近
    - FuzzyLocation(description="走进去的第一个房间")  # 描述性位置
    """
    exact: Optional[str] = None          # 精确位置
    hierarchy: Optional[List[str]] = None  # 层级路径，如 ["家", "客厅"]
    region: Optional[str] = None         # 模糊区域
    description: Optional[str] = None      # 描述性位置
    
    # 不确定性
    certainty: CertaintyLevel = CertaintyLevel.MEDIUM
    
    def matches(self, other: 'FuzzyLocation') -> float:
        """
        计算两个模糊位置的匹配度
        返回 0.0 - 1.0
        """
        # 精确匹配
        if self.exact and other.exact:
            return 1.0 if self.exact == other.exact else 0.0
        
        # 层级匹配
        if self.hierarchy and other.hierarchy:
            common = set(self.hierarchy) & set(other.hierarchy)
            return len(common) / max(len(self.hierarchy), len(other.hierarchy))
        
        # 描述性匹配
        if self.description and other.description:
            words1 = set(self.description.split())
            words2 = set(other.description.split())
            overlap = len(words1 & words2) / len(words1 | words2)
            return overlap
        
        return 0.5  # 默认中等匹配
    
    def to_context_string(self) -> str:
        """转换为上下文字符串"""
        if self.exact:
            return self.exact
        elif self.hierarchy:
            return " > ".join(self.hierarchy)
        elif self.region:
            return self.region
        elif self.description:
            return f"[{self.description}]"
        else:
            return "[位置未知]"
```

### 2.5 模糊记忆

```python
@dataclass
class FuzzyMemory:
    """
    模糊记忆：支持不完整、不确定信息的记忆单元
    
    特点：
    - 所有字段都是可选的
    - 每个字段都有确定性级别
    - 支持渐进式补充和更新
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    
    # 核心内容（唯一必需字段）
    content: str = ""
    content_certainty: CertaintyLevel = CertaintyLevel.MEDIUM
    
    # 时空上下文（可选）
    time: Optional[TimeRange] = None
    location: Optional[FuzzyLocation] = None
    
    # 关系（可选）
    related_to: Optional[List[str]] = None  # 相关记忆 ID
    preceded_by: Optional[str] = None        # 前一个记忆 ID
    followed_by: Optional[str] = None       # 后一个记忆 ID
    
    # 来源和上下文
    source: str = ""                        # "用户说"、"机器人感知"、"推理"
    source_confidence: CertaintyLevel = CertaintyLevel.MEDIUM
    
    # 元数据
    tags: List[str] = field(default_factory=list)
    importance: float = 0.5
    
    # 创建和更新
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    
    # 补充历史
    supplements: List[dict] = field(default_factory=list)  # 后续补充的信息
    
    def get_overall_certainty(self) -> CertaintyLevel:
        """计算整体确定性"""
        certainties = [self.content_certainty]
        
        if self.time:
            certainities.append(self.time.certainty)
        if self.location:
            certainities.append(self.location.certainty)
        
        # 多数投票
        exact_count = certainities.count(CertaintyLevel.EXACT)
        high_count = certainities.count(CertaintyLevel.HIGH)
        medium_count = certainities.count(CertaintyLevel.MEDIUM)
        low_count = certainities.count(CertaintyLevel.LOW)
        
        max_count = max(exact_count, high_count, medium_count, low_count)
        
        if max_count == exact_count:
            return CertaintyLevel.EXACT
        elif max_count == high_count:
            return CertaintyLevel.HIGH
        elif max_count == medium_count:
            return CertaintyLevel.MEDIUM
        else:
            return CertaintyLevel.LOW
    
    def to_context_string(self) -> str:
        """转换为上下文字符串"""
        parts = []
        
        # 时间
        if self.time:
            parts.append(self.time.to_context_string())
        
        # 位置
        if self.location:
            parts.append(self.location.to_context_string())
        
        # 内容
        parts.append(self.content)
        
        # 确定性提示
        certainty = self.get_overall_certainty()
        if certainty == CertaintyLevel.LOW:
            parts.append("(不太确定)")
        elif certainty == CertaintyLevel.MEDIUM:
            parts.append("(隐约记得)")
        
        return "，".join(parts)
```

---

## 三、渐进式存储

### 3.1 核心实现

```python
class GradualMemoryStorage:
    """
    渐进式记忆存储
    
    设计原则：
    1. 接受任何形式的信息，哪怕只有一个词
    2. 信息会自动随时间完善
    3. 不确定信息会被标记，但不会被丢弃
    """
    
    def __init__(self, db):
        self.db = db
    
    async def remember(
        self,
        content: str,
        time: str = None,
        location: str = None,
        importance: float = 0.5,
        **kwargs
    ) -> FuzzyMemory:
        """
        存储记忆（支持碎片化输入）
        
        示例调用：
        await remember("用户喜欢喝热水")
        await remember("好像是在客厅")
        await remember("今天早上", location="厨房")
        await remember("有个会议", time="大概下午3点")
        """
        memory = FuzzyMemory(
            content=content,
            time=self._parse_time(time),
            location=self._parse_location(location),
            importance=importance,
            **kwargs
        )
        
        await self._store(memory)
        return memory
    
    def _parse_time(self, time_str: str) -> Optional[TimeRange]:
        """
        解析时间字符串（支持模糊输入）
        
        支持的格式：
        - "2026-05-25" → 精确日期
        - "14:30" → 今天某个时间
        - "今天早上" → 今天早上时段（6:00-12:00）
        - "今天下午" → 今天下午时段（12:00-18:00）
        - "刚才" → 当前时间前几分钟
        - "之前" → 相对之前
        - "大概下午3点" → 模糊时间
        """
        if not time_str:
            return None
        
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # 精确时间格式
        if re.match(r"^\d{4}-\d{2}-\d{2}$", time_str):
            return TimeRange(exact=datetime.fromisoformat(time_str))
        
        if re.match(r"^\d{2}:\d{2}$", time_str):
            hour, minute = map(int, time_str.split(":"))
            return TimeRange(
                exact=today_start.replace(hour=hour, minute=minute)
            )
        
        # 相对时间关键词
        relative_keywords = {
            "刚才": {"relative": "刚才", "certainty": CertaintyLevel.HIGH},
            "之前": {"relative": "之前", "certainty": CertaintyLevel.MEDIUM},
            "之后": {"relative": "之后", "certainty": CertaintyLevel.MEDIUM},
            "今天早上": {
                "relative": "今天早上",
                "approx_start": today_start.replace(hour=6),
                "approx_end": today_start.replace(hour=12),
                "certainty": CertaintyLevel.MEDIUM,
            },
            "今天下午": {
                "relative": "今天下午",
                "approx_start": today_start.replace(hour=12),
                "approx_end": today_start.replace(hour=18),
                "certainty": CertaintyLevel.MEDIUM,
            },
            "今天晚上": {
                "relative": "今天晚上",
                "approx_start": today_start.replace(hour=18),
                "approx_end": today_start.replace(hour=24),
                "certainty": CertaintyLevel.MEDIUM,
            },
            "今天": {
                "relative": "今天",
                "approx_start": today_start,
                "approx_end": now,
                "certainty": CertaintyLevel.HIGH,
            },
            "昨天": {
                "relative": "昨天",
                "approx_start": today_start - timedelta(days=1),
                "approx_end": today_start,
                "certainty": CertaintyLevel.HIGH,
            },
            "前几天": {
                "relative": "前几天",
                "approx_start": today_start - timedelta(days=7),
                "approx_end": today_start,
                "certainty": CertaintyLevel.LOW,
            },
        }
        
        for keyword, config in relative_keywords.items():
            if keyword in time_str:
                return TimeRange(
                    relative=config["relative"],
                    approx_start=config.get("approx_start"),
                    approx_end=config.get("approx_end"),
                    certainty=config["certainty"]
                )
        
        # 模糊时间（如"大概下午3点"）
        if "大概" in time_str or "可能" in time_str or "好像" in time_str:
            time_part = time_str.replace("大概", "").replace("可能", "").replace("好像", "").strip()
            
            # 尝试解析时间部分
            time_match = re.search(r"(\d{1,2})[点:](\d{0,2})", time_part)
            if time_match:
                hour = int(time_match.group(1))
                minute = int(time_match.group(2)) if time_match.group(2) else 0
                return TimeRange(
                    approx_start=today_start.replace(hour=max(0, hour-1), minute=minute),
                    approx_end=today_start.replace(hour=min(23, hour+1), minute=minute),
                    relative=time_part,
                    certainty=CertaintyLevel.MEDIUM
                )
            
            return TimeRange(
                relative=time_part,
                certainty=CertaintyLevel.LOW
            )
        
        # 默认：低确定性
        return TimeRange(relative=time_str, certainty=CertaintyLevel.LOW)
    
    def _parse_location(self, location_str: str) -> Optional[FuzzyLocation]:
        """
        解析位置字符串（支持模糊输入）
        
        支持的格式：
        - "客厅" → 精确位置
        - "家的客厅" → 层级 ["家", "客厅"]
        - "客厅附近" → 模糊区域
        - "好像是客厅" → 中等确定性
        """
        if not location_str:
            return None
        
        # 检查确定性词
        certainty = CertaintyLevel.MEDIUM
        if "好像" in location_str or "大概" in location_str or "可能" in location_str:
            certainty = CertaintyLevel.MEDIUM
            location_str = location_str.replace("好像", "").replace("大概", "").replace("可能", "").strip()
        else:
            certainty = CertaintyLevel.HIGH
        
        # 检查层级标记
        if "的" in location_str:
            hierarchy = [p.strip() for p in location_str.split("的")]
            return FuzzyLocation(
                hierarchy=hierarchy,
                certainty=certainty
            )
        
        # 检查模糊区域
        if "附近" in location_str or "周围" in location_str:
            region = location_str.replace("附近", "").replace("周围", "").strip()
            return FuzzyLocation(
                region=region,
                certainty=CertaintyLevel.LOW
            )
        
        # 描述性位置
        if any(word in location_str for word in ["左边", "右边", "前面", "后面", "旁边"]):
            return FuzzyLocation(
                description=location_str,
                certainty=CertaintyLevel.LOW
            )
        
        # 默认：精确位置
        return FuzzyLocation(exact=location_str, certainty=certainty)
```

### 3.2 补充机制

```python
async def supplement(
    self,
    memory_id: str,
    new_content: str = None,
    new_time: str = None,
    new_location: str = None,
    **kwargs
):
    """
    补充记忆：后来想起更多信息时补充
    
    示例：
    await supplement(id, "是在客厅", location="客厅")
    await supplement(id, "大概是下午3点", time="大概下午3点")
    """
    memory = await self.get(memory_id)
    
    # 补充内容
    if new_content:
        if memory.content:
            memory.content = f"{memory.content}；{new_content}"
        else:
            memory.content = new_content
    
    # 补充时间（不覆盖已有精确时间）
    if new_time:
        new_time_parsed = self._parse_time(new_time)
        if memory.time and memory.time.certainty >= CertaintyLevel.HIGH:
            # 已有较精确时间，不覆盖
            pass
        else:
            memory.time = new_time_parsed
    
    # 补充位置（不覆盖已有精确位置）
    if new_location:
        new_loc_parsed = self._parse_location(new_location)
        if memory.location and memory.location.certainty >= CertaintyLevel.HIGH:
            # 已有较精确位置，不覆盖
            pass
        else:
            memory.location = new_loc_parsed
    
    # 记录补充历史
    memory.supplements.append({
        "at": datetime.now().isoformat(),
        "content": new_content,
        "time": new_time,
        "location": new_location,
    })
    
    memory.updated_at = datetime.now()
    await self._store(memory)
```

---

## 四、模糊检索

### 4.1 多维度相似度匹配

```python
class FuzzyRetrieval:
    """
    模糊检索：处理不完整、不确定的查询
    
    特点：
    - 查询也可以是不完整的
    - 使用多维度相似度匹配
    - 返回结果带匹配分数
    """
    
    def __init__(
        self,
        storage: GradualMemoryStorage,
        embedding_model,
    ):
        self.storage = storage
        self.embedding = embedding_model
    
    async def recall(
        self,
        query: str = None,
        time_hint: str = None,
        location_hint: str = None,
        top_k: int = 10,
    ) -> List[dict]:
        """
        模糊检索
        
        示例调用：
        recall()  # 检索所有
        recall("喝水")  # 和喝水相关的
        recall(location_hint="客厅")  # 在客厅发生的
        recall("开会", time_hint="今天下午")  # 今天下午开会的
        """
        all_memories = await self.storage.get_all()
        results = []
        
        # 解析查询条件
        query_embedding = self.embedding.encode(query) if query else None
        time_range = self.storage._parse_time(time_hint)
        location = self.storage._parse_location(location_hint)
        
        for memory in all_memories:
            score = self._calculate_match_score(
                memory=memory,
                query_embedding=query_embedding,
                query=query,
                time_range=time_range,
                location=location,
            )
            
            if score > 0.1:  # 阈值
                results.append({
                    "memory": memory,
                    "score": score,
                })
        
        # 按分数排序
        results.sort(key=lambda x: x["score"], reverse=True)
        
        return results[:top_k]
    
    def _calculate_match_score(
        self,
        memory: FuzzyMemory,
        query_embedding: list = None,
        query: str = None,
        time_range: TimeRange = None,
        location: FuzzyLocation = None,
    ) -> float:
        """
        计算多维度匹配分数
        """
        scores = []
        weights = []
        
        # 1. 内容匹配（向量相似度）
        if query_embedding and memory.content:
            memory_embedding = self.embedding.encode(memory.content)
            content_score = cosine_similarity(query_embedding, memory_embedding)
            scores.append(content_score)
            weights.append(0.4)  # 内容权重 40%
        
        # 2. 关键词匹配
        if query and memory.content:
            query_words = set(query.lower().split())
            memory_words = set(memory.content.lower().split())
            keyword_score = len(query_words & memory_words) / len(query_words)
            scores.append(keyword_score)
            weights.append(0.2)  # 关键词权重 20%
        
        # 3. 时间匹配
        if time_range and memory.time:
            if memory.time.exact and time_range.relative:
                time_score = 1.0 if time_range.relative in memory.content else 0.5
            elif memory.time.exact and time_range.exact:
                time_score = 1.0 if memory.time == time_range else 0.0
            else:
                time_score = 0.7  # 都是模糊的
            scores.append(time_score)
            weights.append(0.2)  # 时间权重 20%
        
        # 4. 位置匹配
        if location and memory.location:
            location_score = memory.location.matches(location)
            scores.append(location_score)
            weights.append(0.2)  # 位置权重 20%
        
        # 5. 确定性加成
        certainty_bonus = {
            CertaintyLevel.EXACT: 1.0,
            CertaintyLevel.HIGH: 1.0,
            CertaintyLevel.MEDIUM: 0.9,
            CertaintyLevel.LOW: 0.7,
            CertaintyLevel.UNKNOWN: 0.5,
        }
        scores.append(certainty_bonus[memory.get_overall_certainty()])
        weights.append(0.0)  # 不占权重，但影响最终分数
        
        # 加权平均
        if not weights:
            return 0.0
        
        total_weight = sum(w for w in weights if w > 0)
        if total_weight == 0:
            return 0.0
        
        weighted_sum = sum(s * w for s, w in zip(scores, weights) if w > 0)
        
        return weighted_sum / total_weight
```

---

## 五、主动推理

```python
class MemoryInference:
    """
    记忆推理：从已知信息推断未知信息
    
    能力：
    1. 时间推理：已知"今天早上" + "做了某事" → 推断大概几点
    2. 位置推理：已知位置序列 → 推断可能在哪里
    3. 内容推理：从片段推断完整事件
    """
    
    def __init__(self, storage: GradualMemoryStorage):
        self.storage = storage
    
    async def infer_time(self, memory_id: str) -> TimeRange:
        """
        推断时间
        
        示例：
        - 已知"刚才" → 推断为当前时间的前几分钟
        - 已知"之前" → 查找"刚才"的记忆作为参考
        """
        memory = await self.storage.get(memory_id)
        
        if memory.time and memory.time.exact:
            return memory.time  # 已有精确时间
        
        context = await self._get_temporal_context(memory_id)
        
        if memory.time and memory.time.relative:
            relative = memory.time.relative
            
            if relative == "刚才":
                return TimeRange(
                    approx_start=datetime.now() - timedelta(minutes=10),
                    approx_end=datetime.now(),
                    relative="刚才"
                )
            
            elif relative in ["之前", "早些时候"]:
                recent = await self._get_most_recent()
                if recent and recent.time:
                    return TimeRange(
                        approx_end=recent.time.exact or datetime.now(),
                        relative="之前"
                    )
        
        return memory.time or TimeRange(certainty=CertaintyLevel.UNKNOWN)
    
    async def infer_location(self, memory_id: str) -> FuzzyLocation:
        """
        推断位置
        
        示例：
        - 已知进入厨房 → 推断在厨房
        - 已知路径序列 → 推断当前位置
        """
        memory = await self.storage.get(memory_id)
        
        if memory.location and memory.location.exact:
            return memory.location  # 已有精确位置
        
        # 从序列中推断
        if memory.preceded_by:
            prev_memory = await self.storage.get(memory.preceded_by)
            if prev_memory and prev_memory.location:
                return prev_memory.location
        
        return FuzzyLocation(certainty=CertaintyLevel.UNKNOWN)
    
    async def fill_gaps(self, memory_id: str) -> FuzzyMemory:
        """
        填补记忆空白
        
        1. 时间未填 → 从上下文推断
        2. 位置未填 → 从序列推断
        3. 内容片段 → 尝试补全
        """
        memory = await self.storage.get(memory_id)
        
        # 填补时间
        if not memory.time or memory.time.certainty == CertaintyLevel.UNKNOWN:
            memory.time = await self.infer_time(memory_id)
        
        # 填补位置
        if not memory.location or memory.location.certainty == CertaintyLevel.UNKNOWN:
            memory.location = await self.infer_location(memory_id)
        
        # 标记已推理
        memory.metadata = memory.metadata or {}
        memory.metadata["inferred"] = True
        memory.metadata["inferred_at"] = datetime.now().isoformat()
        
        await self.storage._store(memory)
        return memory
```

---

## 六、输入输出示例

### 6.1 存储示例

| 原始输入 | 存储的内容 | 存储的时间 | 存储的位置 |
|---------|-----------|-----------|-----------|
| `"今天早上在客厅"` | "在客厅" | 今天 6:00-12:00（中等确定） | 客厅（高度确定） |
| `"好像在客厅"` | "在客厅" | 无（未知） | 客厅（中等确定） |
| `"刚才去厨房了"` | "去厨房了" | 当前时间前5分钟 | 厨房（高度确定） |
| `"之前的事"` | "之前的事" | 今天之前（低确定） | 无（未知） |
| `"周末去过一次商场"` | "去过商场" | 最近一个周末（低确定） | 商场（中等确定） |

### 6.2 具体存储结构

#### 输入："今天早上在客厅"

```python
# 最终存储的 FuzzyMemory 对象

FuzzyMemory(
    id="mem_a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    
    # 内容
    content="在客厅",
    content_certainty=<CertaintyLevel.MEDIUM>,
    
    # 时间 - 自动推断
    time=TimeRange(
        exact=None,
        approx_start=datetime(2026, 5, 25, 6, 0),   # 今天早上 6:00
        approx_end=datetime(2026, 5, 25, 12, 0),    # 今天中午 12:00
        relative="今天早上",
        certainty=<CertaintyLevel.MEDIUM>,
    ),
    
    # 位置 - 解析结果
    location=FuzzyLocation(
        exact="客厅",
        hierarchy=["客厅"],
        region=None,
        description=None,
        certainty=<CertaintyLevel.HIGH>,  # 地点比较确定
    ),
    
    # 关系
    related_to=[],
    preceded_by=None,
    followed_by=None,
    
    # 来源
    source="user_input",
    source_confidence=<CertaintyLevel.MEDIUM>,
    
    # 元数据
    tags=["早晨活动", "位置记录"],
    importance=0.5,
    
    created_at=datetime(2026, 5, 25, 15, 30, 45),
    updated_at=datetime(2026, 5, 25, 15, 30, 45),
    supplements=[],
)

# 显示给用户时的格式：
"今天早上 06:00-12:00，在客厅"
```

### 6.3 检索示例

```python
# 用户问："今天早上你在哪？"

# 检索相关记忆
results = await recall("今天早上")

# 匹配到的记忆
memory = results[0]

# 输出格式
print(memory.to_context_string())
# → "今天早上 06:00-12:00，在客厅"

# 更友好的显示
print(f"我记得 {memory.time.to_context_string()} 在 {memory.location.to_context_string()}")
# → "我记得今天早上 06:00-12:00 在客厅"

# 如果时间不太确定
if memory.time.certainty == CertaintyLevel.MEDIUM:
    print(f"好像 {memory.location.to_context_string()}")
# → "好像在客厅"
```

### 6.4 补充示例

```python
# 用户说："是早上8点左右"
await supplement(memory_id, new_time="早上8点")

# 记忆更新为
time=TimeRange(
    exact=datetime(2026, 5, 25, 8, 0),  # ← 精确了！
    certainty=<HIGH>
)

# 显示变为
# → "今天早上 08:00，在客厅"
```

---

## 七、与机器人场景的结合

### 7.1 机器人感知的模糊处理

```python
class RobotFuzzyMemory:
    """
    机器人模糊记忆：处理机器人感知的不确定性
    """
    
    def __init__(self, storage: GradualMemoryStorage):
        self.storage = storage
    
    async def remember_from_sensor(
        self,
        sensor_type: str,  # "location", "object", "state"
        raw_value: str,
        confidence: float,  # 0.0 - 1.0
    ):
        """
        从传感器数据创建记忆
        
        示例：
        await remember_from_sensor("location", "room_kitchen", 0.7)
        await remember_from_sensor("object", "blue_cup", 0.9)
        """
        # 将置信度转换为确定性级别
        certainty = self._confidence_to_certainty(confidence)
        
        # 根据传感器类型解析
        if sensor_type == "location":
            location = FuzzyLocation(
                exact=raw_value,
                certainty=certainty
            )
            await self.storage.remember(
                content=f"感知到位置 {raw_value}",
                location=raw_value,
            )
        
        elif sensor_type == "object":
            await self.storage.remember(
                content=f"看到 {raw_value}",
            )
    
    def _confidence_to_certainty(self, confidence: float) -> CertaintyLevel:
        if confidence >= 0.9:
            return CertaintyLevel.EXACT
        elif confidence >= 0.7:
            return CertaintyLevel.HIGH
        elif confidence >= 0.5:
            return CertaintyLevel.MEDIUM
        elif confidence >= 0.3:
            return CertaintyLevel.LOW
        else:
            return CertaintyLevel.UNKNOWN
```

---

## 八、总结

### 8.1 核心特性

| 传统方案 | 模糊记忆方案 |
|---------|------------|
| 信息必须完整 | **任何碎片都可存储** |
| 精确时间戳 | **相对时间 + 时间范围** |
| GPS 精确位置 | **模糊位置 + 层级路径** |
| 精确匹配检索 | **多维度相似度匹配** |
| 丢弃不完整信息 | **保留 + 标记不确定性** |
| 被动存储 | **主动推理填补空白** |

### 8.2 设计原则

1. **接受不确定性**：任何碎片化的信息都可以存储
2. **渐进式补充**：信息会随时间完善
3. **明确标记**：不确定的信息会被明确标记
4. **智能检索**：支持模糊查询和多维度匹配
5. **主动推理**：自动填补合理的信息空白

### 8.3 适用场景

| 场景 | 示例 |
|------|------|
| **机器人记忆** | SLAM 定位不确定、传感器数据有噪声 |
| **用户交互日志** | 用户描述模糊、时间地点不明确 |
| **AI Agent 记忆** | 上下文信息不完整、记忆碎片化 |
| **生活记录** | 日记碎片化、记忆不完整 |

---

## 九、扩展方向

### 9.1 时间推理增强

```python
# 未来可以加入：
# - 基于用户习惯的时间推断
# - 基于位置序列的时间推断
# - 基于事件的因果推断
```

### 9.2 位置推理增强

```python
# 未来可以加入：
# - 基于 SLAM 地图的模糊位置解析
# - 基于移动轨迹的位置推断
# - 基于物体位置的位置推断
```

### 9.3 群体记忆

```python
# 未来可以加入：
# - 多机器人共享记忆
# - 记忆融合与冲突解决
# - 群体活动模式学习
```
