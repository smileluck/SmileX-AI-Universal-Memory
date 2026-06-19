# 记忆系统任务调度设计方案

> 设计日期：2026-05-26
> 版本：v1.0

---

## 1. 调度架构全景

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Memory Task Scheduler                         │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐              │
│  │  时间触发器   │    │  事件触发器   │    │  自适应触发器  │              │
│  │ Time Trigger│    │Event Trigger│    │Adaptive Trig│              │
│  └──────┬──────┘    └──────┬──────┘    └──────┬──────┘              │
│         │                   │                   │                     │
│         └───────────────────┼───────────────────┘                     │
│                             ▼                                         │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │                     Task Queue (优先级队列)                    │     │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐           │     │
│  │  │ CRITICAL│ │  HIGH   │ │  MEDIUM │ │   LOW   │           │     │
│  │  │   立即   │ │  高优   │ │  普通   │ │  批量   │           │     │
│  │  │ 执行    │ │  队列   │ │  队列   │ │  队列   │           │     │
│  │  └─────────┘ └─────────┘ └─────────┘ └─────────┘           │     │
│  └─────────────────────────────────────────────────────────────┘     │
│                             │                                         │
│                             ▼                                         │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │                   Task Executor Engine                      │     │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐       │     │
│  │  │ Consolid │ │  Forget  │ │Reinforce │ │  Extract │       │     │
│  │  │  整合任务  │ │  遗忘任务  │ │  强化任务  │ │  提取任务  │       │     │
│  │  └──────────┘ └──────────┘ └──────────┘ └──────────┘       │     │
│  └─────────────────────────────────────────────────────────────┘     │
│                             │                                         │
│                             ▼                                         │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │                   Memory Layers (4层记忆)                     │     │
│  │  ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐                          │     │
│  │  │ L0  │ │ L1  │ │ L2  │ │ L3  │                          │     │
│  │  │工作区│ │短时  │ │长时  │ │语义  │                          │     │
│  │  └─────┘ └─────┘ └─────┘ └─────┘                          │     │
│  └─────────────────────────────────────────────────────────────┘     │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. 任务类型定义

### 2.1 任务分类体系

```python
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime
from uuid import UUID, uuid4
import asyncio

class TaskType(Enum):
    """记忆系统任务类型"""
    
    # ========== L0 工作记忆任务 ==========
    CONTEXT_SWITCH = auto()      # 上下文切换
    WORKING_UPDATE = auto()      # 工作记忆更新
    ATTENTION_TRACK = auto()     # 注意力追踪
    
    # ========== L1 短时记忆任务 ==========
    SHORT_TERM_ENCODE = auto()   # 短时编码
    SHORT_TERM_CONSOLIDATE = auto()  # 短时整合
    SHORT_TERM_FORGET = auto()   # 短时遗忘
    
    # ========== L2 长时记忆任务 ==========
    LONG_TERM_STORE = auto()     # 长时存储
    LONG_TERM_RETRIEVE = auto()  # 长时检索
    LONG_TERM_PRUNE = auto()     # 长时修剪
    EPISODE_SUMMARIZE = auto()   # 情节摘要
    CAUSAL_LINK = auto()         # 因果关联
    SPATIO_TEMPORAL_INDEX = auto()  # 时空索引
    
    # ========== L3 语义记忆任务 ==========
    SEMANTIC_EXTRACT = auto()    # 语义提取
    CONCEPT_LINK = auto()        # 概念链接
    KNOWLEDGE_REFRESH = auto()   # 知识刷新
    PATTERN_DISCOVER = auto()    # 模式发现
    
    # ========== 系统任务 ==========
    GARBAGE_COLLECT = auto()     # 垃圾回收
    INDEX_REBUILD = auto()       # 索引重建
    STATISTICS_UPDATE = auto()   # 统计更新
    BACKUP = auto()              # 备份


class TaskPriority(Enum):
    """任务优先级"""
    CRITICAL = 0      # 立即执行，不可打断
    HIGH = 1         # 高优先级
    MEDIUM = 2       # 普通优先级
    LOW = 3          # 低优先级，可批量
    IDLE = 4         # 空闲时执行


class TaskStatus(Enum):
    """任务状态"""
    PENDING = "pending"           # 等待中
    QUEUED = "queued"             # 已入队
    RUNNING = "running"          # 执行中
    COMPLETED = "completed"      # 已完成
    FAILED = "failed"            # 失败
    CANCELLED = "cancelled"      # 取消
    RETRYING = "retrying"        # 重试中


class TriggerType(Enum):
    """触发类型"""
    TIME_BASED = "time"          # 定时触发
    EVENT_BASED = "event"        # 事件触发
    CONDITION_BASED = "condition"  # 条件触发
    ADAPTIVE = "adaptive"        # 自适应触发
    MANUAL = "manual"            # 手动触发
```

### 2.2 任务定义

```python
@dataclass
class Task:
    """
    记忆系统任务定义
    
    Attributes:
        task_id: 任务唯一标识
        task_type: 任务类型
        priority: 任务优先级
        trigger_type: 触发类型
        status: 任务状态
        
        # 执行参数
        payload: 任务参数数据
        target_layers: 目标记忆层
        
        # 时间控制
        scheduled_at: 计划执行时间
        started_at: 实际开始时间
        completed_at: 完成时间
        timeout: 超时时间（秒）
        
        # 重试控制
        max_retries: 最大重试次数
        retry_count: 当前重试次数
        retry_delay: 重试延迟（秒）
        
        # 依赖关系
        depends_on: 依赖的任务ID列表
        triggered_by: 触发该任务的事件
        
        # 结果
        result: 执行结果
        error: 错误信息
    """
    task_id: UUID = field(default_factory=uuid4)
    task_type: TaskType = None
    priority: TaskPriority = TaskPriority.MEDIUM
    
    # 标签和描述
    name: str = ""
    description: str = ""
    
    # 触发
    trigger_type: TriggerType = TriggerType.MANUAL
    
    # 状态
    status: TaskStatus = TaskStatus.PENDING
    
    # 数据
    payload: Dict[str, Any] = field(default_factory=dict)
    target_layers: List[str] = field(default_factory=list)
    
    # 时间
    scheduled_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    timeout: float = 300.0  # 默认 5 分钟超时
    
    # 重试
    max_retries: int = 3
    retry_count: int = 0
    retry_delay: float = 5.0
    
    # 依赖
    depends_on: List[UUID] = field(default_factory=list)
    triggered_by: Optional[str] = None
    
    # 结果
    result: Any = None
    error: Optional[str] = None
    
    # 元数据
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __lt__(self, other):
        """优先级比较：数值越小优先级越高"""
        return self.priority.value < other.priority.value
    
    def can_retry(self) -> bool:
        """是否可以重试"""
        return self.retry_count < self.max_retries
    
    def should_start(self, current_time: datetime) -> bool:
        """是否可以开始执行"""
        if self.status != TaskStatus.QUEUED:
            return False
        if self.scheduled_at and current_time < self.scheduled_at:
            return False
        return True
```

---

## 3. 调度策略实现

### 3.1 三种触发机制

```python
class TriggerStrategy(ABC):
    """触发策略基类"""
    
    @abstractmethod
    def should_trigger(self, context: 'SchedulerContext') -> bool:
        """判断是否应该触发"""
        pass
    
    @abstractmethod
    def create_tasks(self, context: 'SchedulerContext') -> List[Task]:
        """创建任务列表"""
        pass


class TimeBasedTrigger(TriggerStrategy):
    """
    时间触发器
    
    基于固定时间间隔或 cron 表达式触发任务
    """
    
    def __init__(
        self,
        interval_seconds: float = None,
        cron_expr: str = None,
        at_times: List[datetime] = None
    ):
        self.interval_seconds = interval_seconds
        self.cron_expr = cron_expr
        self.at_times = at_times or []
        self.last_trigger: Optional[datetime] = None
    
    def should_trigger(self, context: 'SchedulerContext') -> bool:
        now = context.current_time
        
        # 间隔触发
        if self.interval_seconds:
            if self.last_trigger is None:
                return True
            elapsed = (now - self.last_trigger).total_seconds()
            return elapsed >= self.interval_seconds
        
        # 指定时间触发
        for at_time in self.at_times:
            if abs((now - at_time).total_seconds()) < 60:  # 1 分钟容差
                return True
        
        return False
    
    def create_tasks(self, context: 'SchedulerContext') -> List[Task]:
        self.last_trigger = context.current_time
        return context.get_tasks_for_trigger(self)


class EventBasedTrigger(TriggerStrategy):
    """
    事件触发器
    
    基于特定事件触发任务
    """
    
    def __init__(self, event_types: List[str]):
        self.event_types = set(event_types)
        self.pending_events: List['MemoryEvent'] = []
    
    def should_trigger(self, context: 'SchedulerContext') -> bool:
        # 检查是否有匹配的事件
        for event in context.recent_events:
            if event.event_type in self.event_types:
                return True
        return False
    
    def create_tasks(self, context: 'SchedulerContext') -> List[Task]:
        tasks = []
        for event in context.recent_events:
            if event.event_type in self.event_types:
                task = Task(
                    task_type=self._get_task_type(event.event_type),
                    name=f"处理事件: {event.event_type}",
                    payload={"event": event.to_dict()},
                    triggered_by=event.event_type,
                    priority=self._get_priority(event.event_type)
                )
                tasks.append(task)
        return tasks
    
    def _get_task_type(self, event_type: str) -> TaskType:
        mapping = {
            "memory_full": TaskType.SHORT_TERM_CONSOLIDATE,
            "new_episode": TaskType.EPISODE_SUMMARIZE,
            "access_pattern": TaskType.KNOWLEDGE_REFRESH,
            "entity_link": TaskType.CAUSAL_LINK,
        }
        return mapping.get(event_type, TaskType.CONTEXT_SWITCH)
    
    def _get_priority(self, event_type: str) -> TaskPriority:
        mapping = {
            "memory_full": TaskPriority.HIGH,
            "new_episode": TaskPriority.MEDIUM,
            "access_pattern": TaskPriority.LOW,
            "entity_link": TaskPriority.MEDIUM,
        }
        return mapping.get(event_type, TaskPriority.MEDIUM)


class AdaptiveTrigger(TriggerStrategy):
    """
    自适应触发器
    
    根据系统状态和记忆使用情况动态决定触发
    """
    
    def __init__(self, rules: List['AdaptiveRule']):
        self.rules = rules
    
    def should_trigger(self, context: 'SchedulerContext') -> bool:
        # 评估所有规则
        for rule in self.rules:
            if rule.evaluate(context):
                return True
        return False
    
    def create_tasks(self, context: 'SchedulerContext') -> List[Task]:
        tasks = []
        for rule in self.rules:
            if rule.evaluate(context):
                tasks.extend(rule.create_tasks(context))
        return tasks


@dataclass
class AdaptiveRule:
    """自适应规则"""
    name: str
    description: str
    
    # 触发条件
    condition: Callable[['SchedulerContext'], bool]
    
    # 创建的任务
    task_factory: Callable[['SchedulerContext'], List[Task]]
    
    # 冷却时间（秒）
    cooldown_seconds: float = 60.0
    
    # 上次触发时间
    _last_triggered: Optional[datetime] = field(default=None, init=False)
    
    def evaluate(self, context: 'SchedulerContext') -> bool:
        # 冷却检查
        if self._last_triggered:
            elapsed = (context.current_time - self._last_triggered).total_seconds()
            if elapsed < self.cooldown_seconds:
                return False
        
        return self.condition(context)
    
    def create_tasks(self, context: 'SchedulerContext') -> List[Task]:
        self._last_triggered = context.current_time
        return self.task_factory(context)
```

### 3.2 自适应规则示例

```python
class AdaptiveRules:
    """预定义的自适应规则"""
    
    @staticmethod
    def memory_pressure_rule() -> AdaptiveRule:
        """
        记忆压力规则
        
        当短时记忆使用超过 80% 时，触发整合任务
        """
        return AdaptiveRule(
            name="memory_pressure",
            description="短时记忆压力过高时触发整合",
            condition=lambda ctx: (
                ctx.memory_usage.get("L1", {}).get("usage_percent", 0) > 0.8
            ),
            task_factory=lambda ctx: [
                Task(
                    task_type=TaskType.SHORT_TERM_CONSOLIDATE,
                    name="记忆压力整合",
                    priority=TaskPriority.HIGH,
                    payload={"reason": "memory_pressure"}
                )
            ],
            cooldown_seconds=300  # 5 分钟冷却
        )
    
    @staticmethod
    def idle_consolidation_rule() -> AdaptiveRule:
        """
        空闲整合规则
        
        当系统空闲且有未整合的短时记忆时触发
        """
        return AdaptiveRule(
            name="idle_consolidation",
            description="空闲时整合短时记忆",
            condition=lambda ctx: (
                ctx.system_idle and 
                ctx.pending_consolidations > 0 and
                ctx.cpu_usage < 0.3
            ),
            task_factory=lambda ctx: [
                Task(
                    task_type=TaskType.LONG_TERM_STORE,
                    name="空闲整合",
                    priority=TaskPriority.LOW,
                    payload={"count": ctx.pending_consolidations}
                )
            ],
            cooldown_seconds=60
        )
    
    @staticmethod
    def hot_memory_refresh_rule() -> AdaptiveRule:
        """
        热记忆刷新规则
        
        当高频访问的记忆接近遗忘阈值时强化
        """
        return AdaptiveRule(
            name="hot_memory_refresh",
            description="刷新高频访问的记忆",
            condition=lambda ctx: (
                len(ctx.hot_memories) > 0 and
                any(m.usage_count > 10 for m in ctx.hot_memories)
            ),
            task_factory=lambda ctx: [
                Task(
                    task_type=TaskType.REINFORCE,
                    name="热记忆强化",
                    priority=TaskPriority.MEDIUM,
                    payload={"memories": [m.id for m in ctx.hot_memories]}
                )
            ],
            cooldown_seconds=1800  # 30 分钟冷却
        )
    
    @staticmethod
    def pattern_discovery_rule() -> AdaptiveRule:
        """
        模式发现规则
        
        当积累足够多的新记忆时触发模式发现
        """
        return AdaptiveRule(
            name="pattern_discovery",
            description="发现记忆模式",
            condition=lambda ctx: (
                ctx.new_episodes_count >= 10 and
                ctx.last_pattern_discovery is None or
                (ctx.current_time - ctx.last_pattern_discovery).total_seconds() > 3600
            ),
            task_factory=lambda ctx: [
                Task(
                    task_type=TaskType.PATTERN_DISCOVER,
                    name="模式发现",
                    priority=TaskPriority.LOW,
                    payload={"min_support": 0.6}
                )
            ],
            cooldown_seconds=3600
        )
    
    @staticmethod
    def causal_inference_rule() -> AdaptiveRule:
        """
        因果推理规则
        
        当新记忆与已有记忆存在潜在关联时触发
        """
        return AdaptiveRule(
            name="causal_inference",
            description="推理因果关系",
            condition=lambda ctx: (
                ctx.potential_causal_links > 0 and
                ctx.system_load < 0.5
            ),
            task_factory=lambda ctx: [
                Task(
                    task_type=TaskType.CAUSAL_LINK,
                    name="因果关联",
                    priority=TaskPriority.MEDIUM,
                    payload={"links": ctx.potential_causal_links}
                )
            ],
            cooldown_seconds=300
        )
```

---

## 4. 任务执行引擎

### 4.1 调度器核心

```python
@dataclass
class SchedulerContext:
    """调度器上下文"""
    current_time: datetime
    memory_usage: Dict[str, Dict]  # 各层记忆使用情况
    system_idle: bool
    cpu_usage: float
    recent_events: List['MemoryEvent']
    hot_memories: List['MemoryItem']
    pending_consolidations: int
    new_episodes_count: int
    last_pattern_discovery: Optional[datetime]
    potential_causal_links: int


class MemoryTaskScheduler:
    """
    记忆系统任务调度器
    
    核心职责：
    1. 管理任务队列（优先级队列）
    2. 触发任务创建（时间/事件/自适应）
    3. 执行任务调度
    4. 监控任务状态
    """
    
    def __init__(
        self,
        memory_store: 'MemoryStore',
        executor: 'TaskExecutor',
        config: 'SchedulerConfig' = None
    ):
        self.memory_store = memory_store
        self.executor = executor
        self.config = config or SchedulerConfig()
        
        # 任务队列（优先级队列）
        self.task_queue: PriorityQueue[Task] = PriorityQueue()
        
        # 正在执行的任务
        self.running_tasks: Dict[UUID, Task] = {}
        
        # 触发器
        self.triggers: List[TriggerStrategy] = []
        
        # 调度器
        self._scheduler_task: Optional[asyncio.Task] = None
        self._running = False
        
        # 统计
        self.stats = SchedulerStats()
    
    def add_trigger(self, trigger: TriggerStrategy):
        """添加触发器"""
        self.triggers.append(trigger)
    
    def submit_task(self, task: Task):
        """
        提交任务
        
        Args:
            task: 任务对象
        """
        # 检查依赖
        if task.depends_on:
            for dep_id in task.depends_on:
                if not self._is_task_completed(dep_id):
                    # 依赖未完成，加入等待队列
                    task.status = TaskStatus.PENDING
                    self._wait_for_dependency(task, dep_id)
                    return
        
        task.status = TaskStatus.QUEUED
        self.task_queue.put(task)
        self.stats.tasks_submitted += 1
    
    def _is_task_completed(self, task_id: UUID) -> bool:
        """检查任务是否已完成"""
        # 检查已完成的任务历史
        # 简化实现
        return task_id not in self.running_tasks
    
    def _wait_for_dependency(self, task: Task, dep_id: UUID):
        """等待依赖任务完成"""
        # 可以在此处实现更复杂的依赖等待逻辑
        pass
    
    async def start(self):
        """启动调度器"""
        self._running = True
        self._scheduler_task = asyncio.create_task(self._run_scheduler())
    
    async def stop(self):
        """停止调度器"""
        self._running = False
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
    
    async def _run_scheduler(self):
        """
        主调度循环
        
        1. 检查触发器，创建新任务
        2. 检查任务队列，执行任务
        3. 清理完成的任务
        """
        while self._running:
            try:
                # 1. 构建上下文
                context = await self._build_context()
                
                # 2. 检查触发器
                for trigger in self.triggers:
                    if trigger.should_trigger(context):
                        new_tasks = trigger.create_tasks(context)
                        for task in new_tasks:
                            self.submit_task(task)
                
                # 3. 调度任务执行
                await self._dispatch_tasks()
                
                # 4. 清理和统计
                self._cleanup_completed()
                self._update_stats()
                
                # 5. 等待下一个周期
                await asyncio.sleep(self.config.tick_interval)
                
            except Exception as e:
                logging.error(f"调度循环错误: {e}")
                await asyncio.sleep(5)
    
    async def _build_context(self) -> SchedulerContext:
        """构建调度上下文"""
        # 获取记忆使用情况
        memory_usage = await self.memory_store.get_usage_stats()
        
        # 获取系统状态
        system_idle = self._check_system_idle()
        cpu_usage = psutil.cpu_percent() / 100.0
        
        # 获取最近事件
        recent_events = self.memory_store.get_recent_events(limit=100)
        
        # 获取热记忆
        hot_memories = await self.memory_store.get_hot_memories(limit=50)
        
        # 获取待整合数量
        pending = await self.memory_store.get_pending_consolidations()
        
        # 获取新情节数量
        new_episodes = await self.memory_store.get_new_episode_count()
        
        return SchedulerContext(
            current_time=datetime.now(),
            memory_usage=memory_usage,
            system_idle=system_idle,
            cpu_usage=cpu_usage,
            recent_events=recent_events,
            hot_memories=hot_memories,
            pending_consolidations=pending,
            new_episodes_count=new_episodes,
            last_pattern_discovery=self.stats.last_pattern_discovery,
            potential_causal_links=await self.memory_store.get_potential_links()
        )
    
    def _check_system_idle(self) -> bool:
        """检查系统是否空闲"""
        # 简单实现：检查是否有高优先级任务在队列中
        if not self.task_queue.empty():
            next_task = self.task_queue.queue[0]
            return next_task.priority >= TaskPriority.LOW
        return True
    
    async def _dispatch_tasks(self):
        """分发任务执行"""
        max_concurrent = self.config.max_concurrent_tasks
        
        while (
            len(self.running_tasks) < max_concurrent and 
            not self.task_queue.empty()
        ):
            task = self.task_queue.get()
            
            # 检查是否可以开始
            if not task.should_start(datetime.now()):
                self.task_queue.put(task)
                break
            
            # 执行任务
            asyncio.create_task(self._execute_task(task))
    
    async def _execute_task(self, task: Task):
        """执行单个任务"""
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now()
        self.running_tasks[task.task_id] = task
        
        try:
            # 设置超时
            result = await asyncio.wait_for(
                self.executor.execute(task),
                timeout=task.timeout
            )
            
            task.status = TaskStatus.COMPLETED
            task.result = result
            task.completed_at = datetime.now()
            self.stats.tasks_completed += 1
            
        except asyncio.TimeoutError:
            task.status = TaskStatus.FAILED
            task.error = f"任务超时 ({task.timeout}s)"
            self.stats.tasks_timeout += 1
            
        except Exception as e:
            task.error = str(e)
            
            if task.can_retry():
                task.status = TaskStatus.RETRYING
                task.retry_count += 1
                # 延迟重试
                await asyncio.sleep(task.retry_delay * task.retry_count)
                self.task_queue.put(task)
            else:
                task.status = TaskStatus.FAILED
                self.stats.tasks_failed += 1
        
        finally:
            # 从运行任务中移除
            self.running_tasks.pop(task.task_id, None)
    
    def _cleanup_completed(self):
        """清理已完成的任务"""
        # 可以在这里实现更复杂的清理逻辑
        # 比如保存任务历史等
        pass
    
    def _update_stats(self):
        """更新统计信息"""
        self.stats.queue_size = self.task_queue.qsize()
        self.stats.running_count = len(self.running_tasks)


@dataclass
class SchedulerConfig:
    """调度器配置"""
    tick_interval: float = 1.0        # 调度周期（秒）
    max_concurrent_tasks: int = 4     # 最大并发任务数
    default_timeout: float = 300.0    # 默认超时（秒）
    enable_adaptive: bool = True      # 启用自适应调度
    enable_time_trigger: bool = True  # 启用时间触发
    enable_event_trigger: bool = True  # 启用事件触发


@dataclass
class SchedulerStats:
    """调度器统计"""
    tasks_submitted: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    tasks_timeout: int = 0
    queue_size: int = 0
    running_count: int = 0
    last_pattern_discovery: Optional[datetime] = None
```

### 4.2 任务执行器

```python
class TaskExecutor:
    """
    任务执行器
    
    根据任务类型执行相应的记忆操作
    """
    
    def __init__(self, memory_store: 'MemoryStore'):
        self.memory_store = memory_store
        self.executors: Dict[TaskType, Callable] = {
            TaskType.CONTEXT_SWITCH: self._exec_context_switch,
            TaskType.SHORT_TERM_CONSOLIDATE: self._exec_consolidate,
            TaskType.LONG_TERM_STORE: self._exec_long_term_store,
            TaskType.EPISODE_SUMMARIZE: self._exec_summarize,
            TaskType.CAUSAL_LINK: self._exec_causal_link,
            TaskType.FORGET: self._exec_forget,
            TaskType.REINFORCE: self._exec_reinforce,
            TaskType.SPATIO_TEMPORAL_INDEX: self._exec_spatial_index,
            TaskType.SEMANTIC_EXTRACT: self._exec_semantic_extract,
            TaskType.PATTERN_DISCOVER: self._exec_pattern_discover,
        }
    
    async def execute(self, task: Task) -> Any:
        """执行任务"""
        executor = self.executors.get(task.task_type)
        if not executor:
            raise ValueError(f"未知任务类型: {task.task_type}")
        
        return await executor(task)
    
    # ========== 执行器实现 ==========
    
    async def _exec_context_switch(self, task: Task) -> Dict:
        """执行上下文切换"""
        old_context = task.payload.get("old_context")
        new_context = task.payload.get("new_context")
        
        # 保存旧上下文到短时记忆
        if old_context:
            await self.memory_store.save_short_term(old_context)
        
        # 加载新上下文
        if new_context:
            loaded = await self.memory_store.load_context(new_context)
            return {"loaded": loaded}
        
        return {"success": True}
    
    async def _exec_consolidate(self, task: Task) -> Dict:
        """
        执行记忆整合
        
        将短时记忆整合到长时记忆
        """
        limit = task.payload.get("limit", 10)
        reason = task.payload.get("reason", "scheduled")
        
        # 获取待整合的短时记忆
        candidates = await self.memory_store.get_consolidation_candidates(limit)
        
        consolidated = []
        for item in candidates:
            # 评估是否值得整合
            importance = self._calculate_importance(item)
            
            if importance > 0.5:
                # 整合到长时记忆
                await self.memory_store.consolidate_to_long_term(item)
                consolidated.append({
                    "id": item.id,
                    "importance": importance
                })
            else:
                # 标记为遗忘
                await self.memory_store.mark_forgotten(item.id)
        
        return {
            "consolidated_count": len(consolidated),
            "items": consolidated,
            "reason": reason
        }
    
    async def _exec_long_term_store(self, task: Task) -> Dict:
        """执行长时存储"""
        items = task.payload.get("items", [])
        
        stored = []
        for item_data in items:
            item = MemoryItem.from_dict(item_data)
            await self.memory_store.store_long_term(item)
            stored.append(item.id)
        
        return {"stored_count": len(stored), "ids": stored}
    
    async def _exec_summarize(self, task: Task) -> Dict:
        """
        执行情节摘要
        
        将连续的记忆片段压缩为摘要
        """
        episode_id = task.payload.get("episode_id")
        
        # 获取情节内容
        episode = await self.memory_store.get_episode(episode_id)
        
        # 生成摘要
        summary = await self._generate_summary(episode)
        
        # 保存摘要
        await self.memory_store.save_episode_summary(episode_id, summary)
        
        return {
            "episode_id": episode_id,
            "summary": summary,
            "compression_ratio": len(summary) / len(episode.content) if episode.content else 0
        }
    
    async def _exec_causal_link(self, task: Task) -> Dict:
        """
        执行因果关联
        
        发现并创建记忆间的因果关系
        """
        links_created = 0
        patterns_found = []
        
        # 获取潜在因果链接
        candidates = await self.memory_store.get_potential_causal_links()
        
        for link in candidates:
            # 验证因果关系
            if await self._verify_causal_link(link):
                # 创建因果链接
                await self.memory_store.create_causal_link(link)
                links_created += 1
                patterns_found.append({
                    "cause": link.cause_id,
                    "effect": link.effect_id,
                    "confidence": link.confidence
                })
        
        return {
            "links_created": links_created,
            "patterns": patterns_found
        }
    
    async def _exec_forget(self, task: Task) -> Dict:
        """
        执行遗忘任务
        
        清理低重要性的记忆
        """
        threshold = task.payload.get("threshold", 0.2)
        layer = task.payload.get("layer", "L1")
        
        # 获取需要遗忘的记忆
        to_forget = await self.memory_store.get_forgetting_candidates(
            layer=layer,
            threshold=threshold
        )
        
        forgotten = []
        for item in to_forget:
            await self.memory_store.delete(item.id)
            forgotten.append(item.id)
        
        return {
            "forgotten_count": len(forgotten),
            "layer": layer,
            "threshold": threshold
        }
    
    async def _exec_reinforce(self, task: Task) -> Dict:
        """
        执行记忆强化
        
        增加高频访问记忆的权重
        """
        memory_ids = task.payload.get("memories", [])
        
        reinforced = []
        for mem_id in memory_ids:
            await self.memory_store.reinforce(mem_id)
            reinforced.append(mem_id)
        
        return {"reinforced_count": len(reinforced)}
    
    async def _exec_spatial_index(self, task: Task) -> Dict:
        """执行时空索引"""
        # 重建时空索引
        await self.memory_store.rebuild_spatial_index()
        return {"success": True}
    
    async def _exec_semantic_extract(self, task: Task) -> Dict:
        """执行语义提取"""
        memory_ids = task.payload.get("memories", [])
        
        concepts = []
        for mem_id in memory_ids:
            concept = await self.memory_store.extract_concept(mem_id)
            if concept:
                concepts.append(concept)
        
        return {"extracted_count": len(concepts), "concepts": concepts}
    
    async def _exec_pattern_discover(self, task: Task) -> Dict:
        """
        执行模式发现
        
        发现记忆中的重复模式
        """
        min_support = task.payload.get("min_support", 0.6)
        
        # 发现频繁模式
        patterns = await self.memory_store.discover_patterns(min_support)
        
        # 存储发现的模式
        for pattern in patterns:
            await self.memory_store.save_pattern(pattern)
        
        return {
            "patterns_found": len(patterns),
            "patterns": patterns
        }
    
    # ========== 辅助方法 ==========
    
    def _calculate_importance(self, item: 'MemoryItem') -> float:
        """计算记忆重要性"""
        # 多因素评分
        access_score = min(item.access_count / 10, 1.0) * 0.3
        recency_score = self._recency_score(item.last_accessed) * 0.2
        emotion_score = item.emotion_weight * 0.3 if hasattr(item, 'emotion_weight') else 0.2
        novelty_score = item.novelty * 0.2
        
        return access_score + recency_score + emotion_score + novelty_score
    
    def _recency_score(self, last_accessed: datetime) -> float:
        """新鲜度评分"""
        if not last_accessed:
            return 0.0
        
        hours_ago = (datetime.now() - last_accessed).total_seconds() / 3600
        
        if hours_ago < 1:
            return 1.0
        elif hours_ago < 24:
            return 0.8
        elif hours_ago < 72:
            return 0.5
        else:
            return 0.2
    
    async def _verify_causal_link(self, link: 'CausalLink') -> bool:
        """验证因果关系"""
        # 简化实现
        return link.confidence > 0.7
    
    async def _generate_summary(self, episode: 'Episode') -> str:
        """生成摘要"""
        # 这里可以调用 LLM 生成摘要
        # 简化实现
        return f"情节摘要: {episode.content[:100]}..."
```

---

## 5. 调度策略配置示例

```python
async def create_default_scheduler(memory_store: MemoryStore) -> MemoryTaskScheduler:
    """创建默认配置的调度器"""
    
    # 创建执行器
    executor = TaskExecutor(memory_store)
    
    # 创建调度器
    scheduler = MemoryTaskScheduler(
        memory_store=memory_store,
        executor=executor,
        config=SchedulerConfig(
            tick_interval=1.0,
            max_concurrent_tasks=4,
            default_timeout=300.0
        )
    )
    
    # 添加时间触发器
    scheduler.add_trigger(TimeBasedTrigger(
        interval_seconds=60,  # 每分钟检查一次
        at_times=[
            # 每天凌晨 3 点执行垃圾回收
            datetime.now().replace(hour=3, minute=0, second=0),
        ]
    ))
    
    # 添加事件触发器
    scheduler.add_trigger(EventBasedTrigger([
        "memory_full",
        "new_episode",
        "access_pattern"
    ]))
    
    # 添加自适应触发器
    if scheduler.config.enable_adaptive:
        scheduler.add_trigger(AdaptiveTrigger([
            AdaptiveRules.memory_pressure_rule(),
            AdaptiveRules.idle_consolidation_rule(),
            AdaptiveRules.hot_memory_refresh_rule(),
            AdaptiveRules.pattern_discovery_rule(),
            AdaptiveRules.causal_inference_rule(),
        ]))
    
    return scheduler


# ========== 使用示例 ==========

async def main():
    # 初始化
    memory_store = MemoryStore()
    scheduler = await create_default_scheduler(memory_store)
    
    # 启动调度器
    await scheduler.start()
    
    # 提交手动任务
    scheduler.submit_task(Task(
        task_type=TaskType.EPISODE_SUMMARIZE,
        name="手动摘要任务",
        priority=TaskPriority.HIGH,
        payload={"episode_id": "ep_123"}
    ))
    
    # 等待一段时间
    await asyncio.sleep(3600)
    
    # 停止调度器
    await scheduler.stop()
    
    # 打印统计
    print(f"任务统计: {scheduler.stats}")
```

---

## 6. 优先级调度策略详解

### 6.1 优先级队列实现

```python
import heapq
from typing import Generic, TypeVar, List, Optional
from dataclasses import dataclass

T = TypeVar('T')

class PriorityQueue(Generic[T]):
    """
    基于堆的优先级队列
    
    支持:
    - 按优先级排序
    - FIFO 打破平局
    - 优先级相同时按入队顺序
    """
    
    def __init__(self):
        self._heap: List[tuple] = []
        self._counter = 0  # 用于 FIFO 打破平局
    
    def put(self, item: T):
        """
        入队
        
        Args:
            item: 可比较对象（实现 __lt__）
        """
        # (priority, counter, item)
        heapq.heappush(self._heap, (item.priority.value, self._counter, item))
        self._counter += 1
    
    def get(self) -> T:
        """出队"""
        if self.empty():
            raise IndexError("队列为空")
        _, _, item = heapq.heappop(self._heap)
        return item
    
    def peek(self) -> Optional[T]:
        """查看队首元素（不出队）"""
        if self.empty():
            return None
        _, _, item = self._heap[0]
        return item
    
    def empty(self) -> bool:
        """队列是否为空"""
        return len(self._heap) == 0
    
    def qsize(self) -> int:
        """队列大小"""
        return len(self._heap)
    
    def __len__(self) -> int:
        return len(self._heap)


class PriorityScheduler:
    """
    优先级调度器
    
    根据任务优先级分配执行资源
    """
    
    # 优先级到资源配置映射
    PRIORITY_RESOURCES = {
        TaskPriority.CRITICAL: {
            "concurrency": "unlimited",  # 不限制并发
            "timeout": 60,               # 60 秒超时
            "preempt": True              # 可抢占低优先级
        },
        TaskPriority.HIGH: {
            "concurrency": 2,
            "timeout": 120,
            "preempt": True
        },
        TaskPriority.MEDIUM: {
            "concurrency": 2,
            "timeout": 300,
            "preempt": False
        },
        TaskPriority.LOW: {
            "concurrency": 1,
            "timeout": 600,
            "preempt": False
        },
        TaskPriority.IDLE: {
            "concurrency": 1,
            "timeout": 3600,
            "preempt": False
        }
    }
    
    def __init__(self, max_total_concurrent: int = 4):
        self.max_concurrent = max_total_concurrent
        self.priority_queues: Dict[TaskPriority, PriorityQueue] = {
            p: PriorityQueue() for p in TaskPriority
        }
        self.running_by_priority: Dict[TaskPriority, int] = {
            p: 0 for p in TaskPriority
        }
    
    def submit(self, task: Task):
        """提交任务到对应优先级队列"""
        self.priority_queues[task.priority].put(task)
    
    def get_next_task(self) -> Optional[Task]:
        """
        获取下一个可执行任务
        
        按优先级从高到低检查各队列
        """
        for priority in TaskPriority:
            queue = self.priority_queues[priority]
            resources = self.PRIORITY_RESOURCES[priority]
            
            # 检查资源限制
            if self._can_execute(priority, resources):
                if not queue.empty():
                    return queue.get()
        
        return None
    
    def _can_execute(self, priority: TaskPriority, resources: Dict) -> bool:
        """检查是否可以执行该优先级任务"""
        current_running = sum(self.running_by_priority.values())
        
        if current_running >= self.max_concurrent:
            # 已达最大并发，检查是否可以抢占
            if resources["preempt"]:
                # 可以抢占，返回 True 让调用者处理
                return True
            return False
        
        # 检查该优先级的并发限制
        if self.running_by_priority[priority] >= resources["concurrency"]:
            return False
        
        return True
    
    def mark_started(self, task: Task):
        """标记任务开始执行"""
        self.running_by_priority[task.priority] += 1
    
    def mark_completed(self, task: Task):
        """标记任务完成"""
        self.running_by_priority[task.priority] -= 1
```

---

## 7. 调度监控与指标

```python
@dataclass
class SchedulerMetrics:
    """调度器指标"""
    
    # 吞吐量
    tasks_per_minute: float = 0.0
    tasks_per_hour: float = 0.0
    
    # 延迟
    avg_queue_time: float = 0.0    # 平均排队时间
    avg_execution_time: float = 0.0  # 平均执行时间
    p95_latency: float = 0.0        # P95 延迟
    
    # 成功率
    success_rate: float = 0.0
    failure_rate: float = 0.0
    timeout_rate: float = 0.0
    
    # 资源使用
    avg_queue_size: float = 0.0
    peak_queue_size: int = 0
    avg_concurrent_tasks: float = 0.0
    
    # 按类型统计
    tasks_by_type: Dict[TaskType, int] = field(default_factory=dict)
    
    def calculate_rates(self, stats: SchedulerStats):
        """计算比率"""
        total = stats.tasks_completed + stats.tasks_failed + stats.tasks_timeout
        if total > 0:
            self.success_rate = stats.tasks_completed / total
            self.failure_rate = stats.tasks_failed / total
            self.timeout_rate = stats.tasks_timeout / total


class SchedulerMonitor:
    """调度器监控"""
    
    def __init__(self, scheduler: MemoryTaskScheduler):
        self.scheduler = scheduler
        self.metrics_history: List[SchedulerMetrics] = []
        self.alert_rules: List['AlertRule'] = []
    
    def start_monitoring(self, interval_seconds: float = 60):
        """启动监控"""
        self._monitoring_task = asyncio.create_task(
            self._monitor_loop(interval_seconds)
        )
    
    async def stop_monitoring(self):
        """停止监控"""
        if hasattr(self, '_monitoring_task'):
            self._monitoring_task.cancel()
    
    async def _monitor_loop(self, interval: float):
        """监控循环"""
        while True:
            try:
                metrics = self._collect_metrics()
                self.metrics_history.append(metrics)
                
                # 检查告警规则
                for rule in self.alert_rules:
                    if rule.should_alert(metrics):
                        await self._send_alert(rule, metrics)
                
                # 保持最近 1 小时的指标
                if len(self.metrics_history) > 60:
                    self.metrics_history.pop(0)
                
                await asyncio.sleep(interval)
                
            except Exception as e:
                logging.error(f"监控错误: {e}")
                await asyncio.sleep(interval)
    
    def _collect_metrics(self) -> SchedulerMetrics:
        """收集指标"""
        stats = self.scheduler.stats
        
        metrics = SchedulerMetrics()
        metrics.avg_queue_size = stats.queue_size
        metrics.peak_queue_size = max(metrics.peak_queue_size, stats.queue_size)
        metrics.avg_concurrent_tasks = stats.running_count
        
        metrics.calculate_rates(stats)
        
        return metrics


@dataclass
class AlertRule:
    """告警规则"""
    name: str
    condition: Callable[[SchedulerMetrics], bool]
    severity: str  # "info", "warning", "critical"
    message_template: str


class DefaultAlertRules:
    """默认告警规则"""
    
    @staticmethod
    def high_failure_rate() -> AlertRule:
        return AlertRule(
            name="high_failure_rate",
            condition=lambda m: m.failure_rate > 0.1,  # 失败率 > 10%
            severity="warning",
            message_template="任务失败率过高: {failure_rate:.1%}"
        )
    
    @staticmethod
    def queue_overflow() -> AlertRule:
        return AlertRule(
            name="queue_overflow",
            condition=lambda m: m.avg_queue_size > 100,  # 队列 > 100
            severity="warning",
            message_template="任务队列积压: {avg_queue_size} 个任务"
        )
    
    @staticmethod
    def scheduler_stuck() -> AlertRule:
        return AlertRule(
            name="scheduler_stuck",
            condition=lambda m: m.tasks_per_minute == 0 and m.avg_queue_size > 0,
            severity="critical",
            message_template="调度器疑似停滞，队列积压 {avg_queue_size} 个任务"
        )
```

---

## 8. 总结

### 调度策略对比

| 触发方式 | 适用场景 | 优点 | 缺点 |
|---------|---------|------|------|
| **时间触发** | 定期任务 | 可预测、稳定 | 可能浪费资源 |
| **事件触发** | 响应式任务 | 及时、精准 | 可能频繁触发 |
| **自适应触发** | 智能调度 | 资源利用率高 | 实现复杂 |
| **手动提交** | 紧急任务 | 灵活控制 | 需要人工介入 |

### 任务优先级设计

```
┌─────────────────────────────────────────────────────┐
│                    优先级梯队                        │
├─────────────────────────────────────────────────────┤
│ CRITICAL │ 上下文切换、内存告急   │ 立即执行，不排队  │
│ HIGH     │ 记忆整合、因果推理     │ 高优先级队列     │
│ MEDIUM   │ 情节摘要、语义提取     │ 普通队列         │
│ LOW      │ 模式发现、垃圾回收     │ 批量执行         │
│ IDLE     │ 索引重建、备份        │ 空闲时执行       │
└─────────────────────────────────────────────────────┘
```

### 核心设计原则

1. **分层触发**：时间/事件/自适应三重触发机制
2. **优先级调度**：确保关键任务优先执行
3. **资源限制**：防止资源耗尽
4. **自适应**：根据系统状态动态调整
5. **可观测性**：完善的监控和告警
