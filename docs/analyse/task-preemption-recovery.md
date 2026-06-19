# 任务抢占与中断恢复设计方案

> 设计日期：2026-05-26
> 版本：v1.0

---

## 1. 问题背景

在记忆系统中，任务执行经常面临以下场景：

```
场景1: 长任务执行中插入紧急任务
─────────────────────────────────
[模式发现] ──── 进度 60% ────→ [预计还需30分钟]
          ↓
    用户突然输入新信息
          ↓
[上下文整合] ──── 必须立即执行 ────→ (打断模式发现)

场景2: 任务链的部分执行
─────────────────────────────────
[任务A] → [任务B] → [任务C] → [任务D]
   ✓        ✓       ⏸️(暂停)     ⏳
                    ↓
              需要从任务C恢复

场景3: 资源争用
─────────────────────────────────
[任务A: GPU密集型] ──── 运行中 ────→
[任务B: 内存密集型] ──── 需等待 ────→

场景4: 系统级中断
─────────────────────────────────
[批量整合] ──── 1000条 ────→ [预计1小时]
          ↓
     系统低电量/内存告急
          ↓
   保存进度，暂停执行
```

---

## 2. 核心设计

### 2.1 任务抢占策略

```python
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime
from uuid import UUID, uuid4
import asyncio
import signal
from contextlib import contextmanager

class TaskStatus(Enum):
    """任务状态（扩展版）"""
    # 基本状态
    PENDING = "pending"           # 等待中
    QUEUED = "queued"             # 已入队
    RUNNING = "running"           # 执行中
    COMPLETED = "completed"       # 已完成
    FAILED = "failed"            # 失败
    CANCELLED = "cancelled"       # 取消
    
    # 抢占相关状态
    PAUSING = "pausing"           # 正在暂停
    PAUSED = "paused"            # 已暂停（可恢复）
    SUSPENDED = "suspended"       # 被抢占挂起
    INTERRUPTED = "interrupted"  # 被中断
    RESUMING = "resuming"         # 正在恢复
    
    # 特殊状态
    WAITING_RESOURCE = "waiting_resource"  # 等待资源
    PREEMPTED = "preempted"       # 被抢占

class PreemptionPolicy(Enum):
    """
    抢占策略
    
    定义高优先级任务如何打断低优先级任务
    """
    # 不允许抢占
    NONE = "none"
    
    # 立即抢占（丢弃低优先级）
    IMMEDIATE = "immediate"
    
    # 优雅抢占（等待检查点）
    GRACEFUL = "graceful"
    
    # 协作式抢占（低优先级任务主动让步）
    COOPERATIVE = "cooperative"
    
    # 优先级继承（低优先级继承高优先级任务的优先级）
    PRIORITY_INHERITANCE = "priority_inheritance"

@dataclass
class PreemptionConfig:
    """
    抢占配置
    
    控制任务被抢占时的行为
    """
    # 抢占策略
    policy: PreemptionPolicy = PreemptionPolicy.GRACEFUL
    
    # 抢占等待超时（秒）
    # 超过此时间，低优先级任务将被强制中断
    grace_timeout: float = 30.0
    
    # 是否保存进度
    save_checkpoint: bool = True
    
    # 抢占回调
    on_preempt: Optional[Callable[['Task', 'Task'], None]] = None
    
    # 恢复回调
    on_resume: Optional[Callable[['Task'], None]] = None
    
    # 允许抢占的优先级差
    # 例如: 2 表示高2级以上的任务可以抢占
    min_priority_diff: int = 2

@dataclass
class Checkpoint:
    """
    任务检查点
    
    保存任务执行状态，用于中断后恢复
    """
    checkpoint_id: UUID = field(default_factory=uuid4)
    task_id: UUID
    
    # 进度信息
    progress: float = 0.0          # 0.0 - 1.0
    step: int = 0                   # 当前步骤
    total_steps: int = 0            # 总步骤数
    
    # 状态快照
    state: Dict[str, Any] = field(default_factory=dict)
    intermediate_results: List[Any] = field(default_factory=list)
    
    # 游标信息（用于迭代任务）
    cursor: Any = None
    
    # 时间信息
    created_at: datetime = field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None
    
    # 元数据
    description: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def is_valid(self) -> bool:
        """检查点是否有效"""
        if self.expires_at and datetime.now() > self.expires_at:
            return False
        return True
    
    def save_state(self, state: Dict[str, Any], description: str = ""):
        """保存状态"""
        self.state.update(state)
        self.description = description or self.description
    
    def save_cursor(self, cursor: Any):
        """保存游标位置"""
        self.cursor = cursor
```

---

## 3. 任务可中断执行器

### 3.1 可中断任务基类

```python
class InterruptibleTask:
    """
    可中断任务基类
    
    支持:
    - 中断点保存
    - 优雅停止
    - 状态恢复
    """
    
    def __init__(self, task_id: UUID):
        self.task_id = task_id
        self._status = TaskStatus.PENDING
        self._checkpoint: Optional[Checkpoint] = None
        self._should_stop = False
        self._should_pause = False
        
        # 执行上下文
        self._context: Dict[str, Any] = {}
        
        # 子任务链
        self._subtask_ids: List[UUID] = []
        self._current_subtask: Optional[int] = None
    
    @property
    def status(self) -> TaskStatus:
        return self._status
    
    @property
    def can_be_preempted(self) -> bool:
        """是否可以被抢占"""
        return True
    
    @property
    def is_resumable(self) -> bool:
        """是否可恢复"""
        return self._checkpoint is not None and self._checkpoint.is_valid
    
    def request_stop(self):
        """请求停止"""
        self._should_stop = True
    
    def request_pause(self):
        """请求暂停（等待检查点）"""
        self._should_pause = True
    
    def check_interruption(self):
        """
        检查是否需要中断
        
        应该在循环中定期调用
        如果需要中断，将抛出 InterruptException
        """
        if self._should_stop:
            raise InterruptException(
                reason="stop_requested",
                checkpoint=self.create_checkpoint()
            )
        
        if self._should_pause:
            raise InterruptException(
                reason="pause_requested",
                checkpoint=self.create_checkpoint(),
                can_resume=True
            )
    
    def create_checkpoint(self, description: str = "") -> Checkpoint:
        """
        创建检查点
        
        子类应重写此方法以保存特定状态
        """
        checkpoint = Checkpoint(
            task_id=self.task_id,
            progress=self._calculate_progress(),
            step=self._current_subtask or 0,
            state=self._context.copy(),
            description=description
        )
        self._checkpoint = checkpoint
        return checkpoint
    
    def _calculate_progress(self) -> float:
        """计算进度"""
        if self._current_subtask is None:
            return 0.0
        
        if not self._subtask_ids:
            return 1.0
        
        return self._current_subtask / len(self._subtask_ids)
    
    @abstractmethod
    async def execute(self) -> Any:
        """执行任务（子类实现）"""
        pass
    
    @abstractmethod
    async def restore(self, checkpoint: Checkpoint) -> Any:
        """从检查点恢复执行（子类实现）"""
        pass


class InterruptException(Exception):
    """
    中断异常
    
    用于在检查点处优雅中断任务
    """
    def __init__(
        self,
        reason: str,
        checkpoint: Checkpoint = None,
        can_resume: bool = True
    ):
        self.reason = reason
        self.checkpoint = checkpoint
        self.can_resume = can_resume
        super().__init__(f"Task interrupted: {reason}")


class ResourceConflictException(Exception):
    """资源冲突异常"""
    pass
```

### 3.2 支持中断的批量任务

```python
class BatchInterruptibleTask(InterruptibleTask):
    """
    可中断的批量任务
    
    适用于需要处理大量数据的任务
    支持断点续传
    """
    
    def __init__(
        self,
        task_id: UUID,
        items: List[Any],
        batch_size: int = 100,
        processor: Callable[[Any], Any] = None
    ):
        super().__init__(task_id)
        self.items = items
        self.batch_size = batch_size
        self.processor = processor
        self._subtask_ids = [uuid4() for _ in items]  # 每个item一个子任务
        self._processed_count = 0
        self._results = []
    
    def create_checkpoint(self, description: str = "") -> Checkpoint:
        """创建检查点（保存当前进度）"""
        checkpoint = Checkpoint(
            task_id=self.task_id,
            progress=self._calculate_progress(),
            step=self._processed_count,
            total_steps=len(self.items),
            state={
                "processed_count": self._processed_count,
                "results_count": len(self._results)
            },
            cursor={
                "next_index": self._processed_count,
                "batch_offset": self._processed_count % self.batch_size
            },
            description=description or f"已处理 {self._processed_count}/{len(self.items)}"
        )
        self._checkpoint = checkpoint
        return checkpoint
    
    async def execute(self) -> List[Any]:
        """执行批量任务"""
        self._results = []
        self._processed_count = 0
        
        for i, item in enumerate(self.items):
            # 检查是否需要中断
            self.check_interruption()
            
            try:
                result = await self._process_item(item)
                self._results.append(result)
            except Exception as e:
                # 单个失败不影响整体
                self._results.append({"error": str(e), "item": item})
            
            self._processed_count = i + 1
            
            # 每批次保存检查点
            if (i + 1) % self.batch_size == 0:
                self.create_checkpoint(f"批次 {i // self.batch_size + 1} 完成")
        
        return self._results
    
    async def restore(self, checkpoint: Checkpoint) -> List[Any]:
        """从检查点恢复执行"""
        cursor = checkpoint.cursor
        next_index = cursor["next_index"]
        
        # 恢复已处理的结果
        self._processed_count = checkpoint.state["processed_count"]
        
        # 继续处理剩余项目
        for i in range(next_index, len(self.items)):
            self.check_interruption()
            
            try:
                result = await self._process_item(self.items[i])
                self._results.append(result)
            except Exception as e:
                self._results.append({"error": str(e), "item": self.items[i]})
            
            self._processed_count = i + 1
        
        return self._results
    
    async def _process_item(self, item: Any) -> Any:
        """处理单个项目（子类实现）"""
        if self.processor:
            return await self._call_processor(item)
        return item
    
    async def _call_processor(self, item: Any) -> Any:
        """调用处理器"""
        if asyncio.iscoroutinefunction(self.processor):
            return await self.processor(item)
        return self.processor(item)


class MultiStepInterruptibleTask(InterruptibleTask):
    """
    可中断的多步骤任务
    
    适用于有明确步骤的任务链
    """
    
    def __init__(self, task_id: UUID):
        super().__init__(task_id)
        self._steps: List['TaskStep'] = []
        self._current_step_index = 0
    
    def add_step(
        self,
        name: str,
        func: Callable,
        rollback: Callable = None,
        critical: bool = False
    ):
        """
        添加步骤
        
        Args:
            name: 步骤名称
            func: 步骤执行函数
            rollback: 回滚函数（可选）
            critical: 是否为关键步骤（不可中断）
        """
        step = TaskStep(
            name=name,
            func=func,
            rollback=rollback,
            critical=critical
        )
        self._steps.append(step)
        return step
    
    def create_checkpoint(self, description: str = "") -> Checkpoint:
        """创建检查点"""
        checkpoint = Checkpoint(
            task_id=self.task_id,
            progress=self._calculate_progress(),
            step=self._current_step_index,
            total_steps=len(self._steps),
            state={
                "step_index": self._current_step_index,
                "step_results": [s.result for s in self._steps[:self._current_step_index]]
            },
            cursor={"step_index": self._current_step_index},
            description=description or f"步骤 {self._current_step_index}/{len(self._steps)}: {self._current_step_name()}"
        )
        self._checkpoint = checkpoint
        return checkpoint
    
    async def execute(self) -> Any:
        """执行多步骤任务"""
        self._current_step_index = 0
        results = []
        
        while self._current_step_index < len(self._steps):
            step = self._steps[self._current_step_index]
            
            # 检查是否需要中断
            if not step.critical:
                self.check_interruption()
            
            # 执行步骤
            try:
                result = await step.execute()
                step.result = result
                step.status = TaskStatus.COMPLETED
                results.append(result)
            except Exception as e:
                step.error = e
                step.status = TaskStatus.FAILED
                
                # 回滚已完成的步骤
                await self._rollback(self._current_step_index)
                raise
            
            self._current_step_index += 1
            
            # 关键步骤后保存检查点
            if step.critical:
                self.create_checkpoint(f"关键步骤 {step.name} 完成")
        
        return results
    
    async def restore(self, checkpoint: Checkpoint) -> Any:
        """从检查点恢复执行"""
        cursor = checkpoint.cursor
        self._current_step_index = cursor["step_index"]
        
        # 恢复步骤结果
        step_results = checkpoint.state.get("step_results", [])
        for i, result in enumerate(step_results):
            if i < len(self._steps):
                self._steps[i].result = result
        
        # 继续执行剩余步骤
        while self._current_step_index < len(self._steps):
            step = self._steps[self._current_step_index]
            
            if not step.critical:
                self.check_interruption()
            
            try:
                result = await step.execute()
                step.result = result
                step.status = TaskStatus.COMPLETED
            except Exception as e:
                step.error = e
                step.status = TaskStatus.FAILED
                await self._rollback(self._current_step_index)
                raise
            
            self._current_step_index += 1
        
        return [s.result for s in self._steps if s.result]
    
    async def _rollback(self, up_to_step: int):
        """回滚到指定步骤"""
        for i in range(up_to_step - 1, -1, -1):
            step = self._steps[i]
            if step.rollback and step.status == TaskStatus.COMPLETED:
                try:
                    await step.rollback(step.result)
                except Exception as e:
                    # 回滚失败，记录但不中断
                    pass
    
    def _current_step_name(self) -> str:
        if self._current_step_index < len(self._steps):
            return self._steps[self._current_step_index].name
        return "完成"


@dataclass
class TaskStep:
    """任务步骤"""
    name: str
    func: Callable
    rollback: Optional[Callable] = None
    critical: bool = False  # 关键步骤，不可中断
    
    status: TaskStatus = TaskStatus.PENDING
    result: Any = None
    error: Optional[Exception] = None
    
    async def execute(self) -> Any:
        """执行步骤"""
        if asyncio.iscoroutinefunction(self.func):
            return await self.func()
        return self.func()
```

---

## 4. 抢占管理器

### 4.1 抢占决策逻辑

```python
class PreemptionManager:
    """
    抢占管理器
    
    负责:
    1. 评估是否需要抢占
    2. 选择被抢占的任务
    3. 执行抢占操作
    """
    
    def __init__(self, config: PreemptionConfig = None):
        self.config = config or PreemptionConfig()
        self._running_tasks: Dict[UUID, InterruptibleTask] = {}
        self._waiting_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._preemption_lock = asyncio.Lock()
    
    def register_task(self, task: InterruptibleTask):
        """注册正在运行的任务"""
        self._running_tasks[task.task_id] = task
    
    def unregister_task(self, task_id: UUID):
        """注销任务"""
        self._running_tasks.pop(task_id, None)
    
    async def request_preemption(
        self,
        incoming_task: 'Task',
        reason: str = ""
    ) -> Optional[List[UUID]]:
        """
        请求抢占
        
        Args:
            incoming_task: 即将执行的高优先级任务
            reason: 抢占原因
            
        Returns:
            被抢占的任务ID列表（如果成功）
        """
        async with self._preemption_lock:
            # 找出可以被抢占的低优先级任务
            preemptible = self._find_preemptible_tasks(incoming_task)
            
            if not preemptible:
                return None
            
            # 按优先级从低到高排序
            preemptible.sort(key=lambda t: t.priority.value)
            
            # 选择需要抢占的任务
            to_preempt = []
            remaining_capacity = self.config.min_priority_diff
            
            for task in preemptible:
                if task.can_be_preempted:
                    to_preempt.append(task)
                    remaining_capacity -= (incoming_task.priority.value - task.priority.value)
                    
                    if remaining_capacity <= 0:
                        break
            
            # 执行抢占
            for task in to_preempt:
                await self._preempt_task(task, incoming_task)
            
            return [t.task_id for t in to_preempt]
    
    def _find_preemptible_tasks(
        self,
        incoming_task: 'Task'
    ) -> List[InterruptibleTask]:
        """找出可以被抢占的任务"""
        preemptible = []
        
        for task_id, task in self._running_tasks.items():
            # 检查优先级差
            priority_diff = incoming_task.priority.value - task.priority.value
            
            if priority_diff >= self.config.min_priority_diff:
                preemptible.append(task)
        
        return preemptible
    
    async def _preempt_task(
        self,
        task: InterruptibleTask,
        incoming_task: 'Task'
    ):
        """执行单个任务抢占"""
        # 调用抢占前回调
        if self.config.on_preempt:
            self.config.on_preempt(task, incoming_task)
        
        # 根据策略执行抢占
        if self.config.policy == PreemptionPolicy.IMMEDIATE:
            task.request_stop()
            task._status = TaskStatus.PREEMPTED
        
        elif self.config.policy == PreemptionPolicy.GRACEFUL:
            task.request_pause()
            # 等待检查点或超时
            await self._wait_for_checkpoint(task)
        
        elif self.config.policy == PreemptionPolicy.COOPERATIVE:
            # 协作式：设置标志，让任务自己决定何时停止
            task.request_pause()
    
    async def _wait_for_checkpoint(self, task: InterruptibleTask):
        """等待任务创建检查点"""
        start_time = asyncio.get_event_loop().time()
        
        while True:
            # 检查是否已创建检查点
            if task.is_resumable:
                task._status = TaskStatus.SUSPENDED
                break
            
            # 检查是否超时
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > self.config.grace_timeout:
                # 强制中断
                task.request_stop()
                task._status = TaskStatus.INTERRUPTED
                break
            
            # 等待一小段时间后重试
            await asyncio.sleep(0.5)
```

### 4.2 带抢占的调度器

```python
class PreemptiveScheduler:
    """
    可抢占的任务调度器
    
    支持:
    1. 优先级队列
    2. 任务抢占
    3. 中断恢复
    """
    
    def __init__(
        self,
        memory_store: 'MemoryStore',
        max_concurrent: int = 4,
        preemption_config: PreemptionConfig = None
    ):
        self.memory_store = memory_store
        self.max_concurrent = max_concurrent
        
        # 组件
        self.executor = TaskExecutor(memory_store)
        self.preemption_manager = PreemptionManager(preemption_config)
        
        # 任务管理
        self._task_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._running_tasks: Dict[UUID, InterruptibleTask] = {}
        self._suspended_tasks: Dict[UUID, InterruptibleTask] = {}  # 被抢占挂起的任务
        self._task_metadata: Dict[UUID, 'Task'] = {}  # 任务元数据
        
        # 状态
        self._running = False
        self._scheduler_task: Optional[asyncio.Task] = None
    
    async def submit(
        self,
        task: 'Task',
        interruptible: bool = True
    ) -> UUID:
        """
        提交任务
        
        Args:
            task: 任务定义
            interruptible: 是否支持中断
            
        Returns:
            任务ID
        """
        # 检查是否可以立即执行
        if len(self._running_tasks) >= self.max_concurrent:
            # 尝试抢占
            preempted = await self.preemption_manager.request_preemption(task)
            
            if preempted is None:
                # 无法抢占，加入队列
                self._task_queue.put((task.priority, task))
                return task.task_id
        
        # 立即执行
        self._task_metadata[task.task_id] = task
        asyncio.create_task(self._execute_task(task, interruptible))
        
        return task.task_id
    
    async def submit_critical(
        self,
        task: 'Task',
        reason: str = ""
    ) -> UUID:
        """
        提交关键任务（可抢占其他任务）
        
        Args:
            task: 关键任务
            reason: 抢占原因
            
        Returns:
            被抢占的任务ID列表
        """
        preempted = await self.preemption_manager.request_preemption(
            task, reason
        )
        
        # 提交关键任务
        await self.submit(task, interruptible=False)
        
        return preempted or []
    
    async def _execute_task(
        self,
        task: 'Task',
        interruptible: bool
    ):
        """执行任务"""
        interruptible_task = None
        
        try:
            # 创建可执行的任务对象
            if interruptible:
                interruptible_task = self._create_interruptible_task(task)
                self._running_tasks[task.task_id] = interruptible_task
                self.preemption_manager.register_task(interruptible_task)
            else:
                self._running_tasks[task.task_id] = None
            
            # 执行
            if interruptible_task:
                result = await interruptible_task.execute()
            else:
                result = await self.executor.execute(task)
            
            # 清理
            self._running_tasks.pop(task.task_id, None)
            if interruptible_task:
                self.preemption_manager.unregister_task(task.task_id)
            
            return result
            
        except InterruptException as e:
            # 被中断，保存检查点
            if e.checkpoint:
                await self._save_checkpoint(task.task_id, e.checkpoint)
            
            self._running_tasks.pop(task.task_id, None)
            if interruptible_task:
                self.preemption_manager.unregister_task(task.task_id)
            
            if e.can_resume:
                # 移动到挂起队列
                self._suspended_tasks[task.task_id] = interruptible_task
        
        except Exception as e:
            logging.error(f"任务执行错误 {task.task_id}: {e}")
            self._running_tasks.pop(task.task_id, None)
    
    def _create_interruptible_task(self, task: 'Task') -> InterruptibleTask:
        """根据任务类型创建可中断版本"""
        if task.task_type == TaskType.PATTERN_DISCOVER:
            return PatternDiscoveryTask(task)
        elif task.task_type == TaskType.CONSOLIDATE:
            return ConsolidationTask(task)
        elif task.task_type == TaskType.EPISODE_SUMMARIZE:
            return SummarizationTask(task)
        else:
            return GenericInterruptibleTask(task)
    
    async def _save_checkpoint(self, task_id: UUID, checkpoint: Checkpoint):
        """保存检查点到存储"""
        await self.memory_store.save_checkpoint(task_id, checkpoint)
    
    async def resume_task(self, task_id: UUID) -> bool:
        """
        恢复被挂起的任务
        
        Args:
            task_id: 任务ID
            
        Returns:
            是否成功恢复
        """
        if task_id not in self._suspended_tasks:
            return False
        
        task = self._suspended_tasks.pop(task_id)
        
        # 重新执行
        asyncio.create_task(self._execute_task(
            self._task_metadata[task_id],
            interruptible=True
        ))
        
        return True
    
    async def cancel_task(self, task_id: UUID, force: bool = False) -> bool:
        """
        取消任务
        
        Args:
            task_id: 任务ID
            force: 是否强制取消
            
        Returns:
            是否成功取消
        """
        # 尝试从挂起队列取消
        if task_id in self._suspended_tasks:
            del self._suspended_tasks[task_id]
            return True
        
        # 尝试从运行任务取消
        if task_id in self._running_tasks:
            running = self._running_tasks[task_id]
            if running and hasattr(running, 'request_stop'):
                running.request_stop()
                return True
            elif force:
                # 强制取消
                del self._running_tasks[task_id]
                return True
        
        return False
    
    async def get_task_status(self, task_id: UUID) -> Optional[Dict]:
        """获取任务状态"""
        if task_id in self._running_tasks:
            task = self._running_tasks[task_id]
            if task and hasattr(task, '_status'):
                return {
                    "task_id": task_id,
                    "status": task._status.value,
                    "progress": task._calculate_progress() if hasattr(task, '_calculate_progress') else None,
                    "can_resume": task.is_resumable if hasattr(task, 'is_resumable') else False
                }
        
        if task_id in self._suspended_tasks:
            task = self._suspended_tasks[task_id]
            return {
                "task_id": task_id,
                "status": "suspended",
                "progress": task._calculate_progress() if hasattr(task, '_calculate_progress') else None,
                "can_resume": True
            }
        
        return None


# ==================== 特定任务类型实现 ====================

class PatternDiscoveryTask(InterruptibleTask):
    """
    模式发现任务（可中断）
    
    分阶段发现记忆中的模式
    """
    
    def __init__(self, task: 'Task'):
        super().__init__(task.task_id)
        self.task = task
        self.min_support = task.payload.get("min_support", 0.6)
        self.patterns = []
        
        # 多步骤执行
        self._steps = [
            "load_candidates",    # 加载候选记忆
            "frequency_analysis",  # 频率分析
            "pattern_matching",    # 模式匹配
            "validation",         # 验证
            "storage"             # 存储
        ]
        self._current_step = 0
    
    async def execute(self) -> Any:
        """执行模式发现"""
        self._results = []
        
        for step_name in self._steps:
            self.check_interruption()
            await self._execute_step(step_name)
            self.create_checkpoint(f"步骤 {step_name} 完成")
        
        return self.patterns
    
    async def restore(self, checkpoint: Checkpoint) -> Any:
        """从检查点恢复"""
        self._current_step = checkpoint.state["step_index"]
        
        # 跳过已完成步骤
        for i, step_name in enumerate(self._steps):
            if i < self._current_step:
                continue
            self.check_interruption()
            await self._execute_step(step_name)
        
        return self.patterns
    
    async def _execute_step(self, step_name: str):
        """执行单个步骤"""
        step_methods = {
            "load_candidates": self._load_candidates,
            "frequency_analysis": self._frequency_analysis,
            "pattern_matching": self._pattern_matching,
            "validation": self._validation,
            "storage": self._storage
        }
        
        method = step_methods.get(step_name)
        if method:
            await method()


class ConsolidationTask(BatchInterruptibleTask):
    """
    记忆整合任务（可中断）
    
    批量处理记忆整合，支持断点续传
    """
    
    def __init__(self, task: 'Task'):
        super().__init__(
            task_id=task.task_id,
            items=[],  # 将在执行时加载
            batch_size=50,
            processor=self._consolidate_item
        )
        self.task = task
        self.limit = task.payload.get("limit", 100)
    
    async def execute(self) -> Any:
        """执行整合"""
        # 加载待整合项目
        self.items = await self._load_candidates()
        
        return await super().execute()
    
    async def _load_candidates(self) -> List:
        """加载候选记忆"""
        return []
    
    async def _consolidate_item(self, item: Any) -> Any:
        """整合单个记忆"""
        await asyncio.sleep(0.01)  # 模拟处理
        return {"consolidated": True, "item": item}


class GenericInterruptibleTask(InterruptibleTask):
    """通用可中断任务"""
    
    def __init__(self, task: 'Task'):
        super().__init__(task.task_id)
        self.task = task
        self._progress = 0
    
    async def execute(self) -> Any:
        """执行任务"""
        # 模拟任务执行
        for i in range(10):
            self.check_interruption()
            await asyncio.sleep(0.1)
            self._progress = (i + 1) / 10
            self.create_checkpoint(f"进度 {self._progress:.0%}")
        
        return {"completed": True}
    
    async def restore(self, checkpoint: Checkpoint) -> Any:
        """从检查点恢复"""
        self._progress = checkpoint.progress
        # 继续执行剩余部分
        remaining = int((1 - self._progress) * 10)
        for i in range(remaining):
            self.check_interruption()
            await asyncio.sleep(0.1)
            self._progress = 1 - (remaining - i - 1) / 10
            self.create_checkpoint(f"进度 {self._progress:.0%}")
        
        return {"completed": True, "resumed": True}
```

---

## 5. 协作式中断机制

### 5.1 信号驱动的优雅中断

```python
import signal

class SignalHandler:
    """
    信号处理器
    
    支持 SIGINT, SIGTERM 等系统信号
    实现优雅停止
    """
    
    def __init__(self, scheduler: PreemptiveScheduler):
        self.scheduler = scheduler
        self._received_signal = None
        
        # 注册信号处理器
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
    
    def _handle_signal(self, signum, frame):
        """处理信号"""
        self._received_signal = signum
        
        if signum == signal.SIGINT:
            print("\n收到中断信号 (Ctrl+C)，正在优雅停止...")
        elif signum == signal.SIGTERM:
            print("\n收到终止信号，正在优雅停止...")
        
        # 请求所有任务停止
        for task_id, task in self.scheduler._running_tasks.items():
            if task and hasattr(task, 'request_stop'):
                task.request_stop()
    
    @property
    def received(self) -> bool:
        return self._received_signal is not None


@contextmanager
async def graceful_shutdown(scheduler: PreemptiveScheduler):
    """
    上下文管理器：优雅关闭
    
    用法:
        async with graceful_shutdown(scheduler):
            # 执行任务
    """
    handler = SignalHandler(scheduler)
    
    try:
        yield handler
    finally:
        # 等待所有任务完成或中断
        timeout = 30  # 最多等待 30 秒
        start = asyncio.get_event_loop().time()
        
        while scheduler._running_tasks and \
              asyncio.get_event_loop().time() - start < timeout:
            await asyncio.sleep(1)
        
        print("优雅关闭完成")
```

### 5.2 优先级继承实现

```python
class PriorityInheritanceScheduler(PreemptiveScheduler):
    """
    优先级继承调度器
    
    当高优先级任务等待低优先级任务持有的资源时，
    临时提升低优先级任务的优先级
    """
    
    async def request_preemption(
        self,
        incoming_task: 'Task',
        reason: str = ""
    ) -> Optional[List[UUID]]:
        """
        请求抢占（带优先级继承）
        """
        async with self._preemption_lock:
            # 找出持有资源的高优先级任务
            waiting_for = self._find_blocked_tasks(incoming_task)
            
            if waiting_for:
                # 提升持有者的优先级
                for blocked_task in waiting_for:
                    original_priority = blocked_task.priority
                    blocked_task.priority = incoming_task.priority
                    blocked_task._original_priority = original_priority
                    
                    logging.info(
                        f"优先级继承: {blocked_task.task_id} "
                        f"{original_priority} -> {incoming_task.priority}"
                    )
                
                return []
            
            # 正常抢占
            return await super().request_preemption(incoming_task, reason)
    
    def _find_blocked_tasks(self, waiting_task: 'Task') -> List:
        """找出被阻塞的高优先级任务"""
        # 简化实现
        return []
```

---

## 6. 使用示例

```python
async def example_preemptive_scheduler():
    """可抢占调度器使用示例"""
    
    # 创建调度器
    scheduler = PreemptiveScheduler(
        memory_store=MemoryStore(),
        max_concurrent=4,
        preemption_config=PreemptionConfig(
            policy=PreemptionPolicy.GRACEFUL,
            grace_timeout=30.0,
            min_priority_diff=2
        )
    )
    
    # 启动调度器
    await scheduler.start()
    
    # 提交低优先级长任务
    long_task = Task(
        task_type=TaskType.PATTERN_DISCOVER,
        name="模式发现（长任务）",
        priority=TaskPriority.LOW,
        payload={"min_support": 0.6}
    )
    await scheduler.submit(long_task, interruptible=True)
    
    # 模拟执行一段时间
    await asyncio.sleep(5)
    
    # 检查任务状态
    status = await scheduler.get_task_status(long_task.task_id)
    print(f"长任务状态: {status}")
    
    # 提交高优先级关键任务（会抢占低优先级任务）
    critical_task = Task(
        task_type=TaskType.CONTEXT_SWITCH,
        name="紧急上下文切换",
        priority=TaskPriority.CRITICAL,
        payload={"context": "new_user_input"}
    )
    preempted = await scheduler.submit_critical(
        critical_task,
        reason="用户新输入需要立即处理"
    )
    print(f"被抢占的任务: {preempted}")
    
    # 等待关键任务完成
    await asyncio.sleep(2)
    
    # 恢复被挂起的任务
    for task_id in preempted or []:
        await scheduler.resume_task(task_id)
    
    # 优雅关闭
    async with graceful_shutdown(scheduler):
        await asyncio.sleep(60)
    
    print("演示完成")


async def example_batch_with_checkpoints():
    """带检查点的批量任务示例"""
    
    scheduler = PreemptiveScheduler(memory_store=MemoryStore())
    
    # 创建可中断的批量任务
    items = list(range(1000))  # 1000 个项目
    batch_task = BatchInterruptibleTask(
        task_id=uuid4(),
        items=items,
        batch_size=100
    )
    
    async def processor(item: int) -> Dict:
        # 模拟处理
        await asyncio.sleep(0.01)
        return {"id": item, "processed": True}
    
    batch_task.processor = processor
    
    # 执行
    try:
        results = await batch_task.execute()
        print(f"处理完成: {len(results)} 项")
    except InterruptException as e:
        if e.checkpoint:
            print(f"被中断，保存检查点: {e.checkpoint.description}")
            print(f"进度: {e.checkpoint.progress:.1%}")
```

---

## 7. 设计总结

### 7.1 抢占策略对比

| 策略 | 行为 | 适用场景 | 风险 |
|------|------|---------|------|
| **IMMEDIATE** | 立即中断，丢弃进度 | 紧急任务，不在乎丢失 | 可能丢失大量进度 |
| **GRACEFUL** | 等待检查点，超时强制 | 大多数场景 | 等待时间不确定 |
| **COOPERATIVE** | 任务主动让步 | 长时间运行任务 | 任务必须支持 |
| **PRIORITY_INHERITANCE** | 提升低优先级任务 | 资源竞争场景 | 可能导致优先级反转 |

### 7.2 检查点策略

```
┌────────────────────────────────────────────────────────────────┐
│ 检查点保存时机                                                   │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│  1. 批量边界: 每 N 个项目处理后                                  │
│     checkpoint.save_cursor({"index": i})                       │
│                                                                │
│  2. 步骤完成: 每个大步骤完成后                                   │
│     checkpoint.save_state({"step": 2, "results": [...]})      │
│                                                                │
│  3. 时间间隔: 每 N 秒保存一次                                    │
│     if time.time() - last_checkpoint > interval:             │
│         checkpoint = create_checkpoint()                       │
│                                                                │
│  4. 资源使用: 内存/CPU低于阈值时                                 │
│     if memory_usage < 0.5:                                     │
│         checkpoint = create_checkpoint()                      │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

### 7.3 状态流转

```
                    ┌──────────────┐
                    │   PENDING    │
                    └──────┬───────┘
                           │ submit()
                           ▼
                    ┌──────────────┐
                    │    QUEUED     │◄──────────────┐
                    └──────┬───────┘               │
                           │ dispatch()           │
                           ▼                       │
                    ┌──────────────┐               │
              ┌─────│   RUNNING    │─────┐         │
              │     └──────────────┘     │         │
              │            │             │         │
              │   pause() │  complete() │ stop()  │
              │            │             │         │
              ▼            ▼             ▼         ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
        │  PAUSED  │ │COMPLETED │ │INTERRUPTED│ │  FAILED  │
        └────┬─────┘ └──────────┘ └─────┬─────┘ └──────────┘
             │                          │
             │ resume()                 │ has_checkpoint?
             ▼                          ▼
        ┌──────────┐              ┌──────────┐
        │ RESUMING │─────────────►│ SUSPENDED │
        └────┬─────┘              └─────┬─────┘
             │                         │
             │ resume()                 │ resume()
             ▼                         ▼
        ┌──────────┐              ┌──────────┐
        │ RUNNING  │              │ RUNNING  │
        └──────────┘              └──────────┘
```

这套设计的核心理念：**让任务"安全地被打断"，而不丢失已做的工作。**
