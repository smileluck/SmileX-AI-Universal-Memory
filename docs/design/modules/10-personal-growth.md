# 个人学习成长模块 — 设计扩展

> **配套文档**：[09-key-migration-designs.md](09-key-migration-designs.md)
> **目的**：在项目知识管理之上，增加**个人学习成长**维度
> **核心理念**：项目是短暂的，成长是持续的 — 记忆系统要能跨项目追踪个人能力演进

---

## 0. 问题诊断：为什么现有设计不够？

### 0.1 项目记忆 vs 个人成长的关键差异

| 维度 | 项目记忆 | 个人学习成长 |
|------|---------|------------|
| **时间性** | 项目周期（周-月）| 跨年甚至终身 |
| **scope** | `project:xxx`（隔离）| 跨项目（贯穿）|
| **数据结构** | 实体 + 三元组（静态）| **等级 + 轨迹**（时序演进）|
| **核心问题** | "这个项目用了什么？" | **"我这半年成长了什么？"** |
| **失败价值** | 项目失败的决策（项目内）| **失败 → 反思 → 经验**（跨项目）|
| **关联性** | 项目内实体关联 | **技能 ↔ 项目 ↔ 学习 ↔ 复盘** 多维关联 |

### 0.2 现有设计的 3 个缺失

1. **无"个人"scope 维度**：只有 project/tenant/global，个人成长无归属
2. **无技能等级模型**：实体是静态的，无法表达"从入门到精通"的演进
3. **无复盘机制**：失败/成功经验没有结构化沉淀

---

## 1. 核心需求拆解

### 1.1 五大核心场景

```
┌──────────────────────────────────────────────────────────────┐
│  场景 1: 学习记录                                            │
│  "我昨天读了《DDD》，学了领域事件的概念"                       │
│  → 记录学习事件 + 关联技能 + 评估掌握度                       │
│                                                              │
│  场景 2: 技能成长追踪                                        │
│  "我的 Python 从入门到精通花了多久？"                         │
│  → 时序追踪等级变化 + 识别突破点/平台期                       │
│                                                              │
│  场景 3: 经验复盘                                            │
│  "上次项目失败是因为没做容量评估，这次要避免"                  │
│  → 结构化复盘 + 跨项目召回相似场景                            │
│                                                              │
│  场景 4: 学习路径推荐                                        │
│  "我想学系统设计，应该先学什么？"                             │
│  → 基于已有技能推荐前置 + 识别盲区                            │
│                                                              │
│  场景 5: 跨项目经验迁移                                      │
│  "项目 A 的缓存设计经验能用到项目 B 吗？"                     │
│  → 技能 ↔ 项目关联 + 相似度匹配                              │
└──────────────────────────────────────────────────────────────┘
```

---

## 2. 数据模型扩展

### 2.1 Scope 模型扩展（无需改枚举）

**复用现有 scope 字符串字段**，新增 `personal:` 前缀：

```python
# scope 命名空间约定（字符串语义）
SCOPE_PATTERNS = {
    r"project:(?P<id>\S+)":          "项目专属记忆",
    r"personal:skills/(?P<skill>\S+)":   "技能成长记录",
    r"personal:learning/(?P<topic>\S+)": "学习笔记",
    r"personal:reflection/(?P<event>\S+)": "复盘反思",
    r"personal:goals/(?P<goal>\S+)":     "个人目标",
    r"tenant:(?P<id>\S+)":           "租户专属",
    r"global":                        "全局通用",
}

# 不需要改 MemoryScope 枚举，scope 字段本就是字符串
# 但加一个辅助枚举用于分类
class ScopeCategory(StrEnum):
    PROJECT = "project"
    PERSONAL = "personal"      # ★ 新增分类
    TENANT = "tenant"
    GLOBAL = "global"

def categorize_scope(scope: str) -> ScopeCategory:
    if scope.startswith("project:"):
        return ScopeCategory.PROJECT
    elif scope.startswith("personal:"):
        return ScopeCategory.PERSONAL
    elif scope.startswith("tenant:"):
        return ScopeCategory.TENANT
    elif scope == "global":
        return ScopeCategory.GLOBAL
```

### 2.2 新增核心实体：Skill（技能）

```python
# src/smilex/models/skill.py
from enum import StrEnum
from dataclasses import dataclass, field
from datetime import datetime

class SkillLevel(StrEnum):
    """技能等级（借鉴 Dreyfus 模型）"""
    NOVICE = "novice"              # 入门（需要详细指导）
    ADVANCED_BEGINNER = "advanced_beginner"  # 进阶新手（能处理常规任务）
    COMPETENT = "competent"        # 胜任（能独立解决复杂问题）
    PROFICIENT = "proficient"      # 熟练（能识别模式 + 直觉判断）
    EXPERT = "expert"              # 精通（能创新 + 教导他人）

class SkillCategory(StrEnum):
    """技能大类"""
    TECHNICAL = "technical"        # 技术（Python/系统设计/K8s）
    SOFT = "soft"                  # 软技能（沟通/领导力）
    DOMAIN = "domain"              # 领域（金融/医疗/电商）
    TOOL = "tool"                  # 工具（Figma/Excel/Vim）

LEVEL_NUMERIC = {
    SkillLevel.NOVICE: 1,
    SkillLevel.ADVANCED_BEGINNER: 2,
    SkillLevel.COMPETENT: 3,
    SkillLevel.PROFICIENT: 4,
    SkillLevel.EXPERT: 5,
}

@dataclass
class Skill:
    """技能实体"""
    id: str                                  # ULID
    name: str                                # "Python", "系统设计"
    normalized_name: str                     # "python", "system_design"
    category: SkillCategory
    scope: str = "personal:skills/python"    # personal:skills/{name}
    
    # 当前等级
    current_level: SkillLevel = SkillLevel.NOVICE
    level_updated_at: str = field(default_factory=now_iso)
    
    # 实践统计
    total_practice_hours: float = 0.0
    practice_count: int = 0
    last_practiced_at: Optional[str] = None
    
    # 关联
    related_projects: list = field(default_factory=list)  # [project_id, ...]
    prerequisite_skills: list = field(default_factory=list)
    derived_skills: list = field(default_factory=list)    # 学了这个后能学什么
    
    # 元数据
    confidence: float = 0.5                  # 自评置信度
    user_self_assessed: bool = False         # 是否用户自评
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
```

### 2.3 新增：LearningEvent（学习事件）

```python
# src/smilex/models/learning.py
class EventType(StrEnum):
    """学习事件类型"""
    STUDIED = "studied"              # 学习（读书/上课/看视频）
    PRACTICED = "practiced"          # 实践（在项目中用）
    TAUGHT = "taught"                # 教导（写博客/分享/带人）
    FAILED = "failed"                # 失败（尝试但出错）
    MASTERED = "mastered"            # 突破（顿悟时刻）
    REVIEWED = "reviewed"            # 复习

class LearningSource(StrEnum):
    """学习来源"""
    BOOK = "book"
    COURSE = "course"                # 在线课程
    DOCUMENTATION = "documentation"
    PROJECT = "project"              # 在项目中学
    FAILURE = "failure"              # 从失败中学
    MENTOR = "mentor"                # 导师指导
    PEER = "peer"                    # 同伴交流
    SELF_REFLECTION = "self_reflection"

@dataclass
class LearningEvent:
    """学习事件（时序记录）"""
    id: str                                  # ULID
    skill_id: str                            # 关联技能
    event_type: EventType
    source: LearningSource
    
    # 时间
    started_at: str                          # ISO 8601
    duration_minutes: int = 0
    
    # 内容
    title: str = ""                          # "读《DDD》第3章"
    description: str = ""
    source_ref: Optional[str] = None         # 书名/课程 URL/项目 ID
    
    # 等级变化（可选）
    level_before: Optional[SkillLevel] = None
    level_after: Optional[SkillLevel] = None
    
    # 关联
    related_project: Optional[str] = None    # 在哪个项目学的
    related_event_ids: list = field(default_factory=list)  # 关联其他事件
    
    # 评估
    confidence_before: float = 0.0           # 学习前自评
    confidence_after: float = 0.0            # 学习后自评
    difficulty_perceived: float = 0.5        # 主观难度 0-1
    
    # 元数据
    scope: str = "personal:learning"
    created_at: str = field(default_factory=now_iso)
```

### 2.4 新增：Reflection（复盘）

```python
# src/smilex/models/reflection.py
class ReflectionType(StrEnum):
    """复盘类型"""
    SUCCESS = "success"              # 成功经验
    FAILURE = "failure"              # 失败教训
    LESSON = "lesson"                # 普适教训
    PATTERN = "pattern"              # 识别的模式
    SURPRISE = "surprise"            # 意外发现

@dataclass
class Reflection:
    """结构化复盘（借鉴美军 AAR - After Action Review）"""
    id: str                                  # ULID
    type: ReflectionType
    
    # 关联事件（可选）
    trigger_event_id: Optional[str] = None   # 触发复盘的事件
    trigger_project: Optional[str] = None    # 触发复盘的项目
    
    # AAR 四问
    what_was_expected: str = ""              # 原本想达成什么
    what_actually_happened: str = ""         # 实际发生了什么
    why_it_happened: str = ""                # 为什么会这样
    what_to_do_differently: str = ""         # 下次怎么做
    
    # 提炼
    key_lesson: str = ""                     # 一句话总结
    applicable_scenarios: list = field(default_factory=list)  # 适用场景
    
    # 关联技能
    related_skills: list = field(default_factory=list)
    
    # 元数据
    scope: str = "personal:reflection"
    created_at: str = field(default_factory=now_iso)
    revisited_count: int = 0                 # 被回顾次数（强化记忆）
    last_revisited_at: Optional[str] = None
```

### 2.5 新增：PersonalGoal（个人目标）

```python
# src/smilex/models/goal.py
class GoalStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABANDONED = "abandoned"

@dataclass
class PersonalGoal:
    """个人目标（驱动学习路径）"""
    id: str
    title: str                                # "成为高级系统设计师"
    description: str = ""
    
    # 目标分解
    target_skills: list = field(default_factory=list)  # [(skill_id, target_level), ...]
    deadline: Optional[str] = None
    
    # 进度
    status: GoalStatus = GoalStatus.ACTIVE
    progress: float = 0.0                     # 0-1
    milestones: list = field(default_factory=list)
    
    # 关联
    related_projects: list = field(default_factory=list)
    blocking_goals: list = field(default_factory=list)
    
    scope: str = "personal:goals"
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
```

---

## 3. EdgeKind 扩展

```python
# 在现有 EdgeKind 基础上扩展
class EdgeKind(StrEnum):
    # ... 原有 5 种
    SEMANTIC = "semantic"
    CAUSAL = "causal"
    TEMPORAL = "temporal"
    REFERENCE = "reference"
    CONTAINS = "contains"
    
    # ★ 个人成长专属
    LEADS_TO = "leads_to"             # 学习路径：A 是 B 的前置
    PRACTICED_IN = "practiced_in"     # 技能在项目中实践
    DERIVED_FROM = "derived_from"     # 经验/教训来源于
    BLOCKS = "blocks"                 # A 阻碍 B（盲区）
    ENABLES = "enables"               # 掌握 A 后能做 B
    REINFORCES = "reinforces"         # A 强化 B（共现学习）


# 学习路径边的属性 schema
class LeadsToProps(TypedDict, total=False):
    """LEADS_TO 边属性"""
    necessity: str                # required/recommended/optional
    estimated_hours: int          # 预计学习时长
    difficulty_jump: int          # 难度跳跃 1-5

class PracticedInProps(TypedDict, total=False):
    """PRACTICED_IN 边属性"""
    duration_hours: float
    complexity: str               # simple/moderate/complex
    outcome: str                  # success/partial/failure
```

---

## 4. SQLite Schema 扩展

```sql
-- src/smilex/storage/schema/010_personal_growth.sql

-- ============================================
-- 表: skills（技能）
-- ============================================
CREATE TABLE skills (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    category        TEXT NOT NULL,
    
    current_level   TEXT NOT NULL DEFAULT 'novice',
    level_updated_at TEXT,
    
    total_practice_hours REAL DEFAULT 0.0,
    practice_count  INTEGER DEFAULT 0,
    last_practiced_at TEXT,
    
    related_projects TEXT DEFAULT '[]',       -- JSON 数组
    prerequisite_skills TEXT DEFAULT '[]',
    derived_skills  TEXT DEFAULT '[]',
    
    confidence      REAL DEFAULT 0.5,
    user_self_assessed INTEGER DEFAULT 0,
    
    scope           TEXT DEFAULT 'personal:skills',
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now')),
    
    UNIQUE(normalized_name, scope)
);

CREATE INDEX idx_skills_category ON skills(category);
CREATE INDEX idx_skills_level ON skills(current_level);
CREATE INDEX idx_skills_normalized ON skills(normalized_name);

-- ============================================
-- 表: learning_events（学习事件）
-- ============================================
CREATE TABLE learning_events (
    id              TEXT PRIMARY KEY,
    skill_id        TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    source          TEXT NOT NULL,
    
    started_at      TEXT NOT NULL,
    duration_minutes INTEGER DEFAULT 0,
    
    title           TEXT,
    description     TEXT,
    source_ref      TEXT,
    
    level_before    TEXT,
    level_after     TEXT,
    
    related_project TEXT,
    related_event_ids TEXT DEFAULT '[]',
    
    confidence_before REAL DEFAULT 0,
    confidence_after REAL DEFAULT 0,
    difficulty_perceived REAL DEFAULT 0.5,
    
    scope           TEXT DEFAULT 'personal:learning',
    created_at      TEXT DEFAULT (datetime('now')),
    
    FOREIGN KEY (skill_id) REFERENCES skills(id)
);

CREATE INDEX idx_events_skill ON learning_events(skill_id);
CREATE INDEX idx_events_time ON learning_events(started_at);
CREATE INDEX idx_events_type ON learning_events(event_type);
CREATE INDEX idx_events_project ON learning_events(related_project);

-- ============================================
-- 表: reflections（复盘）
-- ============================================
CREATE TABLE reflections (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    
    trigger_event_id TEXT,
    trigger_project TEXT,
    
    what_was_expected TEXT,
    what_actually_happened TEXT,
    why_it_happened TEXT,
    what_to_do_differently TEXT,
    
    key_lesson      TEXT,
    applicable_scenarios TEXT DEFAULT '[]',
    
    related_skills  TEXT DEFAULT '[]',
    
    scope           TEXT DEFAULT 'personal:reflection',
    created_at      TEXT DEFAULT (datetime('now')),
    revisited_count INTEGER DEFAULT 0,
    last_revisited_at TEXT
);

CREATE INDEX idx_reflections_type ON reflections(type);
CREATE INDEX idx_reflections_project ON reflections(trigger_project);
CREATE INDEX idx_reflections_created ON reflections(created_at);

-- ============================================
-- 表: personal_goals（个人目标）
-- ============================================
CREATE TABLE personal_goals (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT,
    
    target_skills   TEXT DEFAULT '[]',        -- JSON: [{skill_id, target_level}]
    deadline        TEXT,
    
    status          TEXT DEFAULT 'active',
    progress        REAL DEFAULT 0.0,
    milestones      TEXT DEFAULT '[]',
    
    related_projects TEXT DEFAULT '[]',
    blocking_goals  TEXT DEFAULT '[]',
    
    scope           TEXT DEFAULT 'personal:goals',
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX idx_goals_status ON personal_goals(status);
```

---

## 5. 关键算法

### 5.1 成长曲线计算

```python
# src/smilex/lifecycle/growth_tracker.py

class GrowthTracker:
    """技能成长曲线分析"""
    
    async def get_skill_progress(
        self, skill_id: str, time_range: tuple = None
    ) -> GrowthCurve:
        """获取技能成长曲线"""
        events = await self.db.get_learning_events(
            skill_id, time_range, order_by="started_at"
        )
        
        if not events:
            return GrowthCurve.empty()
        
        # 1. 按时间排序的事件点
        points = []
        for event in events:
            points.append(TimelinePoint(
                timestamp=event.started_at,
                level=LEVEL_NUMERIC.get(event.level_after, None),
                confidence=event.confidence_after,
                event_type=event.event_type,
                duration_hours=event.duration_minutes / 60,
            ))
        
        # 2. 识别突破点（level 跳跃 ≥ 1）
        breakthroughs = []
        for i in range(1, len(points)):
            if (points[i].level and points[i-1].level 
                and points[i].level > points[i-1].level):
                breakthroughs.append(Breakthrough(
                    timestamp=points[i].timestamp,
                    from_level=points[i-1].level,
                    to_level=points[i].level,
                    trigger_event=points[i].event_type,
                ))
        
        # 3. 识别平台期（连续 N 个事件等级未变）
        plateaus = self._detect_plateaus(points, min_events=5)
        
        # 4. 计算成长速度（等级/月）
        if len(points) >= 2:
            months = (points[-1].timestamp - points[0].timestamp).days / 30
            level_gain = (points[-1].level or 0) - (points[0].level or 0)
            growth_rate = level_gain / max(months, 0.1)
        else:
            growth_rate = 0
        
        return GrowthCurve(
            skill_id=skill_id,
            points=points,
            breakthroughs=breakthroughs,
            plateaus=plateaus,
            total_hours=sum(p.duration_hours for p in points),
            growth_rate_per_month=growth_rate,
        )
    
    def _detect_plateaus(
        self, points: list, min_events: int = 5
    ) -> list[Plateau]:
        """识别平台期"""
        plateaus = []
        i = 0
        while i < len(points):
            if points[i].level is None:
                i += 1
                continue
            
            # 找连续同等级的事件
            j = i
            while (j < len(points) 
                   and points[j].level == points[i].level):
                j += 1
            
            if j - i >= min_events:
                plateaus.append(Plateau(
                    start=points[i].timestamp,
                    end=points[j-1].timestamp,
                    level=points[i].level,
                    event_count=j - i,
                ))
            i = j
        return plateaus
```

### 5.2 学习路径推荐

```python
# src/smilex/lifecycle/learning_path_recommender.py

class LearningPathRecommender:
    """基于已有技能推荐学习路径"""
    
    async def recommend_path(
        self, target_skill: str, current_skills: list[str] = None
    ) -> LearningPath:
        """推荐学习路径"""
        current_skills = current_skills or await self._get_user_skills()
        
        # 1. 检查是否已掌握
        if target_skill in current_skills:
            return LearningPath.already_mastered(target_skill)
        
        # 2. 找前置依赖（递归）
        prerequisites = await self._find_prerequisites(
            target_skill, current_skills
        )
        
        # 3. 识别缺失的前置
        missing = [p for p in prerequisites if p not in current_skills]
        
        # 4. 拓扑排序（学习顺序）
        ordered = await self._topological_sort(missing)
        
        # 5. 估算总时长
        total_hours = sum(
            await self._estimate_hours(skill) for skill in ordered
        )
        
        return LearningPath(
            target=target_skill,
            prerequisites=ordered,
            missing=missing,
            estimated_hours=total_hours,
            recommendations=await self._find_resources(ordered),
        )
    
    async def _find_prerequisites(
        self, skill: str, have: list[str], visited: set = None
    ) -> list[str]:
        """递归找所有前置依赖"""
        visited = visited or set()
        if skill in visited:
            return []
        visited.add(skill)
        
        direct_prereqs = await self.db.get_prerequisites(skill)
        result = list(direct_prereqs)
        
        for p in direct_prereqs:
            if p not in have:
                result.extend(
                    await self._find_prerequisites(p, have, visited)
                )
        
        return list(set(result))
```

### 5.3 盲区识别

```python
class GapIdentifier:
    """识别知识盲区"""
    
    async def identify_gaps(
        self, goal: PersonalGoal
    ) -> list[KnowledgeGap]:
        """对照目标识别盲区"""
        gaps = []
        
        for target in goal.target_skills:
            skill_id, target_level = target["skill_id"], target["level"]
            current = await self.db.get_skill(skill_id)
            
            if not current:
                gaps.append(KnowledgeGap(
                    skill_id=skill_id,
                    current_level=None,
                    target_level=target_level,
                    gap_size=LEVEL_NUMERIC[target_level],
                    type="missing",  # 完全没学
                ))
            elif LEVEL_NUMERIC[current.current_level] < LEVEL_NUMERIC[target_level]:
                gaps.append(KnowledgeGap(
                    skill_id=skill_id,
                    current_level=current.current_level,
                    target_level=target_level,
                    gap_size=LEVEL_NUMERIC[target_level] - LEVEL_NUMERIC[current.current_level],
                    type="insufficient",  # 学过但不够
                ))
        
        return sorted(gaps, key=lambda g: -g.gap_size)
```

### 5.4 跨项目经验迁移

```python
class ExperienceTransfer:
    """跨项目经验迁移分析"""
    
    async def find_transferable(
        self, from_project: str, to_project: str
    ) -> list[TransferableSkill]:
        """找出可迁移的技能/经验"""
        # 1. 从 from_project 提取所有实践过的技能
        from_skills = await self.db.get_practiced_skills(from_project)
        
        # 2. 从 to_project 识别需求（基于项目模板 + 已有实体）
        to_requirements = await self._infer_requirements(to_project)
        
        # 3. 匹配
        transferable = []
        for skill in from_skills:
            for req in to_requirements:
                sim = await self._compute_similarity(skill, req)
                if sim > 0.7:
                    transferable.append(TransferableSkill(
                        skill=skill,
                        target_requirement=req,
                        similarity=sim,
                        transfer_difficulty=1 - sim,
                    ))
        
        return sorted(transferable, key=lambda t: -t.similarity)
    
    async def recall_similar_failures(
        self, current_situation: str, top_k: int = 3
    ) -> list[Reflection]:
        """召回相似场景的失败复盘"""
        # 用 embedding 检索相似复盘
        query_emb = await self.embedder.embed(current_situation)
        return await self.db.search_reflections(
            embedding=query_emb,
            type=ReflectionType.FAILURE,
            top_k=top_k,
        )
```

---

## 6. 与现有系统的融合

### 6.1 项目记忆 ↔ 个人成长的关联

```
┌─────────────────────────────────────────────────────────────┐
│  项目记忆（project:xxx）                                      │
│  · 实体：技术栈、决策、人物                                    │
│  · 三元组：uses、decided、causes                              │
│        │                                                     │
│        │ PRACTICED_IN 边                                      │
│        ▼                                                     │
│  个人技能（personal:skills/python）                           │
│  · 等级：competent                                            │
│  · 实践小时：120h                                             │
│  · 关联项目：[project_a, project_b]                           │
│        │                                                     │
│        │ LEADS_TO 边                                          │
│        ▼                                                     │
│  衍生技能（personal:skills/fastapi）                          │
│  · 等级：beginner                                             │
│  · 前置：python                                               │
│        │                                                     │
│        │ DERIVED_FROM 边                                      │
│        ▼                                                     │
│  学习事件（personal:learning）                                │
│  · "在 project_a 中首次用 FastAPI"                            │
│  · 时长：40h                                                  │
│  · 等级变化：None → advanced_beginner                         │
│        │                                                     │
│        │ trigger_event_id                                    │
│        ▼                                                     │
│  复盘（personal:reflection）                                  │
│  · 类型：failure                                              │
│  · 教训："FastAPI 依赖注入在测试时要 mock"                    │
│  · 适用场景：[测试, 依赖注入]                                  │
└─────────────────────────────────────────────────────────────┘
```

### 6.2 社区检测在个人成长的应用

- **聚类主题**：发现"我最近主要在学什么"（社区 = 学习方向）
- **Global Search**：回答"我这半年的成长主线是什么？"
- **平台期检测**：长期停留在一个社区，提示需要突破

### 6.3 Provenance 在个人成长的应用

| Provenance | 学习场景含义 |
|-----------|-----------|
| `EXTRACTED` | 用户明确自评的等级 |
| `OBSERVED` | 从项目实践自动推断（如"用了 100 次可推断 proficient"）|
| `INFERRED` | LLM 从对话推断的技能 |
| `AMBIGUOUS` | 自评与实践不符（待用户确认）|

---

## 7. 新增 API

```python
# MemoryMiddleware 扩展
class MemoryMiddleware:
    # ... 原有方法
    
    # ★ 个人成长专属
    async def record_learning(
        self, request: LearningRequest
    ) -> LearningResponse:
        """记录学习事件（自动更新技能等级 + 关联项目）"""
    
    async def reflect(
        self, request: ReflectionRequest
    ) -> ReflectionResponse:
        """记录复盘（支持从对话/事件触发）"""
    
    async def get_growth_curve(
        self, skill_id: str, time_range: tuple = None
    ) -> GrowthCurve:
        """获取技能成长曲线"""
    
    async def recommend_learning_path(
        self, target_skill: str
    ) -> LearningPath:
        """推荐学习路径"""
    
    async def identify_gaps(
        self, goal_id: str
    ) -> list[KnowledgeGap]:
        """识别目标 vs 现状的盲区"""
    
    async def recall_similar_experiences(
        self, situation: str, type: str = None
    ) -> list[Reflection]:
        """召回相似场景的过往经验"""
    
    async def transfer_skills(
        self, from_project: str, to_project: str
    ) -> list[TransferableSkill]:
        """分析跨项目可迁移技能"""
```

---

## 8. 用户场景示例

### 场景 1：学习记录

```
用户：我昨天读了《DDD》第 3 章，学了领域事件
Agent：→ record_learning(
    skill_id="domain_driven_design",
    event_type="studied",
    source="book",
    title="读《DDD》第3章",
    duration_minutes=90,
    level_before="novice",
    level_after="advanced_beginner",
)
→ 自动更新 skill 等级 + 记录事件
```

### 场景 2：项目失败 → 复盘 → 经验

```
用户：项目 A 上线后 Redis 崩了，是因为没做容量评估
Agent：→ reflect(
    type="failure",
    trigger_project="project_a",
    what_was_expected="Redis 能支撑 QPS",
    what_actually_happened="OOM 崩溃",
    why_it_happened="没做容量评估，峰值流量是预估的 5 倍",
    what_to_do_differently="关键中间件必须做容量评估 + 压测",
    key_lesson="容量评估是上线前必做项",
    related_skills=["redis", "capacity_planning"],
)
```

### 场景 3：跨项目经验复用

```
用户：项目 B 也要用 Redis，有什么经验能迁移？
Agent：→ recall_similar_experiences("Redis 部署")
→ 返回项目 A 的复盘：
  "上次 Redis 崩了，记得做容量评估 + 压测"
→ transfer_skills("project_a", "project_b")
→ 返回：[redis (sim=0.95), capacity_planning (sim=0.8)]
```

### 场景 4：成长回顾

```
用户：我最近半年主要在学什么？
Agent：→ Global Search on personal:learning
→ 返回社区摘要：
  "最近半年主要学习了分布式系统相关技能：
   - 深入学习了 Kafka（从 beginner 到 competent）
   - 在 project_a 中实践了事件驱动架构
   - 从一次消费者组 rebalance 故障中复盘了 Kafka 运维"
```

### 场景 5：学习路径推荐

```
用户：我想学系统设计，应该怎么开始？
Agent：→ recommend_learning_path("system_design")
→ 返回：
  你当前已有：[python (competent), basic_networking]
  缺失前置：[data_structures, algorithms, distributed_basics]
  推荐顺序：
    1. data_structures (40h, required)
    2. algorithms (60h, required)
    3. distributed_basics (30h, recommended)
    4. system_design (50h, target)
  总计：约 180 小时
```

---

## 9. 实施路线图

### Phase 1：MVP（1 周）

| 任务 | 工作量 |
|------|--------|
| Skill / LearningEvent / Reflection dataclass | 1 天 |
| SQLite schema（4 张表）| 0.5 天 |
| `record_learning` + `reflect` API | 2 天 |
| 基础查询（按时间/技能/类型）| 1 天 |
| LLM 提取技能（从对话）| 1 天 |
| `.smilex/personal/` 默认配置 | 0.5 天 |

### Phase 2：增强（2 周）

| 任务 | 工作量 |
|------|--------|
| GrowthTracker（曲线 + 突破/平台检测）| 2 天 |
| LearningPathRecommender | 2 天 |
| GapIdentifier | 1 天 |
| ExperienceTransfer（相似度匹配）| 2 天 |
| 社区检测应用到 personal scope | 1 天 |
| 与项目记忆的 PRACTICED_IN 关联 | 2 天 |
| 集成测试 | 2 天 |

### Phase 3：洞察（按需）

- 成长报告生成（周报/月报）
- 学习效率分析（投入产出比）
- 推荐系统（基于成长轨迹推荐下一步）

---

## 10. 数据量估算

### 单用户 1 年的典型数据量

| 数据类型 | 数量 | 存储 |
|---------|------|------|
| Skill（技能）| 50 个 | 50 KB |
| LearningEvent（学习事件）| 1000 个 | 500 KB |
| Reflection（复盘）| 100 个 | 100 KB |
| PersonalGoal（目标）| 10 个 | 10 KB |
| 关联三元组 | 5000 个 | 250 KB |
| **合计** | — | **~1 MB** |

**对比项目记忆**：单项目 1 年约 70 MB，个人成长仅占 1.4%。

---

## 11. 隐私与安全考量

### 11.1 个人数据敏感性

个人成长数据比项目记忆更敏感（涉及自评、失败、心理状态）：

| 数据类型 | 敏感度 | 保护措施 |
|---------|--------|---------|
| 技能等级 | 低 | 默认明文 |
| 学习事件 | 中 | 可选加密 |
| 复盘（含失败）| **高** | **SQLCipher 加密** |
| 个人目标 | 中 | 可选加密 |

### 11.2 数据所有权

- ✅ 所有个人数据存储在用户本地（`~/.smilex/personal/`）
- ✅ 不上传任何外部服务
- ✅ 用户可随时导出（JSON / Markdown）
- ✅ 用户可一键清空 personal scope 而不影响项目记忆

```python
# 隐私优先的配置
@dataclass
class PersonalPrivacyConfig:
    encrypt_reflections: bool = True     # 复盘默认加密
    encrypt_goals: bool = False
    auto_redact_pii: bool = True         # 自动脱敏（姓名/地址）
    retention_days: int = 3650           # 默认保留 10 年
```

---

## 12. 与项目记忆的协同

### 12.1 自动关联（无需用户手动）

```python
# 在项目对话中检测到技能实践
async def on_project_event(event: ProjectEvent):
    # 1. 提取涉及的技能
    skills = await llm.extract_skills(event.content)
    
    # 2. 自动创建 PRACTICED_IN 关联
    for skill in skills:
        await db.upsert_triple(Triple(
            subject_id=skill.id,
            predicate="practiced_in",
            object_id=event.project_id,
            edge_kind=EdgeKind.PRACTICED_IN,
            edge_properties={
                "duration_hours": event.duration_hours,
                "outcome": event.outcome,
                "complexity": event.complexity,
            },
            scope="personal:skills",
            provenance=Provenance.OBSERVED,  # 行为观察
        ))
        
        # 3. 更新技能的实践统计
        await db.increment_skill_practice(
            skill.id, event.duration_hours
        )
```

### 12.2 检索时的双视角

```python
async def recall(query: str, perspective: str = "project"):
    """支持双视角检索"""
    if perspective == "project":
        # 项目视角：默认召回项目记忆 + 通用知识
        scope_filter = ScopeFilter(
            include_project=current_project,
            include_global=True,
        )
    elif perspective == "personal":
        # 个人视角：默认召回个人成长 + 项目关联
        scope_filter = ScopeFilter(
            include_personal=True,  # ★ 新增
            include_project=current_project,
        )
    
    return await hybrid_search(query, scope_filter)
```

---

## 13. 结论

### 个人成长模块的价值

```
┌──────────────────────────────────────────────────────────────┐
│  没有个人成长模块          │  有个人成长模块                  │
│  ─────────────────────    │  ──────────────────              │
│  记系统记不住"人"          │  ✅ 跨项目追踪个人能力演进        │
│  项目结束经验就丢了        │  ✅ 失败 → 复盘 → 跨项目复用     │
│  不知道学了什么             │  ✅ 成长曲线 + 突破点可视化       │
│  学习路径靠拍脑袋          │  ✅ 基于已有技能的数据驱动推荐    │
│  相似场景重复踩坑          │  ✅ 召回过往相似失败经验          │
└──────────────────────────────────────────────────────────────┘
```

### 核心设计原则

1. **scope 字符串扩展**（不改枚举）— 复用现有 `scope` 字段，加 `personal:` 前缀
2. **数据模型独立**（4 张新表）— 不污染项目记忆
3. **关联通过 EdgeKind**（PRACTICED_IN 等）— 保持图谱统一
4. **隐私优先**（复盘加密 + 本地存储）— 个人数据最敏感
5. **渐进实施**（MVP 先做记录，P2 再做分析）— 避免过度设计

### 与三大思想的协同

| 思想 | 在个人成长的应用 |
|------|---------------|
| **属性多重图** | `PRACTICED_IN` 边带时长/复杂度/结果属性 |
| **社区检测** | 发现"主要学习方向"+ Global Search 成长主线 |
| **provenance** | 自评（EXTRACTED）vs 推断（INFERRED）vs 观察（OBSERVED）|

**一句话总结**：

> 项目是短暂的，成长是持续的。SmileX 加上个人成长模块后，**不只是"项目的记忆"，更是"人的记忆"**。
