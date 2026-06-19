# 多任务链并发控制设计方案

> 设计日期：2026-05-26
> 版本：v1.0

---

## 1. 问题场景分析

### 1.1 典型冲突场景

```
┌─────────────────────────────────────────────────────────────────────┐
│ 场景1: Lost Update（丢失更新）                                        │
├─────────────────────────────────────────────────────────────────────┤
│  任务链A: [读取记忆X(v1)] → [修改A: X=v2] → [写回]                   │
│                        ↓                                            │
│  任务链B:           [读取记忆X(v1)] → [修改B: X=v3] → [写回]       │
│                                                                     │
│  结果: X最终=v3，A的修改丢失                                         │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ 场景2: Dirty Read（脏读）                                           │
├─────────────────────────────────────────────────────────────────────┤
│  任务链A: [读取X] → [修改X(未提交)] → [读取X(应该读到什么?)]         │
│                        ↓                                            │
│  任务链B:           [读取X] → [读取X] → [提交]                      │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ 场景3: 因果顺序冲突                                                  │
├─────────────────────────────────────────────────────────────────────┤
│  任务链A: [记忆X: 事件A导致事件B]                                    │
│                        ↓                                            │
│  任务链B:           [记忆X: 事件B导致事件A]                          │
│                                                                     │
│  结果: 因果链自相矛盾                                                │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ 场景4: 记忆版本链断裂                                                │
├─────────────────────────────────────────────────────────────────────┤
│  任务链A: [读取X(v1)] → [修改A] → [写回v2]                          │
│                        ↓  [任务链C也在操作X]                         │
│  任务链B:           [读取X(v2)] → [修改B] → [写回v3]               │
│                                                                     │
│  如果A的任务被抢占，后续恢复时基于v1继续会导致v2丢失                  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. 并发控制整体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Memory Concurrency Controller                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐            │
│  │  Lock Manager │  │Conflict Detector│ │ Version Tracker│            │
│  │   锁管理器    │  │   冲突检测器   │  │   版本追踪器   │            │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘            │
│         │                 │                  │                      │
│         └─────────────────┼──────────────────┘                      │
│                           ▼                                          │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │                    Task Chain Coordinator                      │ │
│  │                      任务链协调器                               │ │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐         │ │
│  │  │ Chain A │ │ Chain B │ │ Chain C │ │ Chain D │         │ │
│  │  │  任务链A │ │  任务链B │ │  任务链C │ │  任务链D │         │ │
│  │  └─────────┘ └─────────┘ └─────────┘ └─────────┘         │ │
│  └───────────────────────────────────────────────────────────────┘ │
│                           │                                          │
│                           ▼                                          │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │                    Memory Layer (记忆层)                        │ │
│  │  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐              │ │
│  │  │  L0    │ │  L1    │ │  L2    │ │  L3    │              │ │
│  │  └────────┘ └────────┘ └────────┘ └────────┘              │ │
│  └───────────────────────────────────────────────────────────────┘ │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. 锁机制设计

### 3.1 锁类型定义

```python
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Set, Dict, Optional, List
from datetime import datetime
from uuid import UUID
import asyncio
from collections import defaultdict

class LockType(Enum):
    """
    锁类型
    
    - SHARED: 共享锁，多个任务可同时持有（用于读取）
    - EXCLUSIVE: 排他锁，只有一个任务可持有（用于写入）
    - UPDATE: 更新锁，用于防止死锁
    - INTENT_SHARED: 意向共享锁
    - INTENT_EXCLUSIVE: 意向排他锁
    """
    SHARED = auto()              # 读锁，可共享
    EXCLUSIVE = auto()           # 写锁，排他
    UPDATE = auto()              # 更新锁，可升级为EXCLUSIVE
    INTENT_SHARED = auto()       # 意向共享（用于锁层次）
    INTENT_EXCLUSIVE = auto()    # 意向排他（用于锁层次）

class LockGranularity(Enum):
    """锁粒度"""
    MEMORY_ITEM = auto()         # 单个记忆项
    MEMORY_CHUNK = auto()        # 记忆块（一组相关记忆）
    MEMORY_LAYER = auto()        # 记忆层
    ENTITY = auto()              # 实体
    RELATION = auto()            # 关系（三元组）
    CAUSAL_CHAIN = auto()        # 因果链

class LockDuration(Enum):
    """锁持续时间"""
    SHORT = 1.0        # 短：单个操作
    MEDIUM = 10.0      # 中：任务步骤
    LONG = 60.0        # 长：整个任务
    TRANSACTION = -1    # 事务级别

@dataclass
class Lock:
    """
    锁对象
    
    Attributes:
        lock_id: 锁ID
        lock_type: 锁类型
        granularity: 锁粒度
        resource_id: 资源ID
        holder_id: 持有者ID（任务链ID）
        holders: 所有持有者（用于共享锁）
        duration: 锁持续时间
        created_at: 创建时间
        expires_at: 过期时间
        mode: 锁模式（悲观/乐观）
    """
    lock_id: UUID
    lock_type: LockType
    granularity: LockGranularity
    resource_id: str
    
    # 持有者
    holder_id: UUID  # 主持有者
    holders: Set[UUID] = field(default_factory=set)  # 所有持有者
    
    # 时间
    duration: LockDuration = LockDuration.MEDIUM
    created_at: datetime = field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None
    
    # 模式
    mode: str = "pessimistic"  # pessimistic / optimistic
    
    # 版本信息（用于乐观锁）
    version: int = 1
    expected_version: Optional[int] = None
    
    # 等待队列
    waiters: List['LockRequest'] = field(default_factory=list)
    
    @property
    def is_expired(self) -> bool:
        """检查是否过期"""
        if self.expires_at is None:
            return False
        return datetime.now() > self.expires_at
    
    @property
    def can_share(self) -> bool:
        """是否可以共享"""
        return self.lock_type in (LockType.SHARED, LockType.INTENT_SHARED)
    
    def is_compatible(self, other: 'Lock') -> bool:
        """
        检查两个锁是否兼容
        
        兼容性规则:
        - SHARED + SHARED = 兼容（都可以读）
        - EXCLUSIVE + 任何 = 不兼容
        - UPDATE + SHARED = 兼容
        - UPDATE + UPDATE = 不兼容（防止死锁）
        """
        if self.resource_id != other.resource_id:
            return True
        
        # 排他锁不兼容任何锁
        if self.lock_type == LockType.EXCLUSIVE or \
           other.lock_type == LockType.EXCLUSIVE:
            return False
        
        # 更新锁只允许一个
        if self.lock_type == LockType.UPDATE and \
           other.lock_type == LockType.UPDATE:
            return False
        
        return True


@dataclass
class LockRequest:
    """
    锁请求
    
    请求获取某个资源的锁
    """
    request_id: UUID
    task_id: UUID  # 请求的任务链ID
    lock_type: LockType
    granularity: LockGranularity
    resource_id: str
    
    # 锁模式
    mode: str = "pessimistic"
    expected_version: Optional[int] = None
    
    # 超时和重试
    timeout: float = 30.0
    retry_interval: float = 0.5
    max_retries: int = 10
    
    # 优先级
    priority: int = 0  # 数值越大优先级越高
    created_at: datetime = field(default_factory=datetime.now)
    
    # 结果
    granted: bool = False
    lock: Optional[Lock] = None
```

### 3.2 锁管理器实现

```python
class LockManager:
    """
    锁管理器
    
    职责:
    1. 管理锁的获取和释放
    2. 处理锁冲突
    3. 防止死锁
    4. 锁超时处理
    """
    
    def __init__(self, config: 'LockConfig' = None):
        self.config = config or LockConfig()
        
        # 锁存储: resource_id -> Lock
        self._locks: Dict[str, Lock] = {}
        
        # 任务持有的锁: task_id -> Set[Lock]
        self._task_locks: Dict[UUID, Set[str]] = defaultdict(set)
        
        # 等待队列: resource_id -> List[LockRequest]
        self._wait_queues: Dict[str, List[LockRequest]] = defaultdict(list)
        
        # 死锁检测
        self._wait_graph: Dict[UUID, Set[str]] = defaultdict(set)
        
        # 锁计数器
        self._lock_count = 0
        
        # 异步锁
        self._lock = asyncio.Lock()
    
    async def acquire(
        self,
        request: LockRequest
    ) -> Optional[Lock]:
        """
        获取锁
        
        Args:
            request: 锁请求
            
        Returns:
            获取到的锁对象，失败返回None
        """
        async with self._lock:
            # 1. 检查是否已有锁
            existing_lock = self._locks.get(request.resource_id)
            
            if existing_lock and existing_lock.is_expired:
                # 清理过期锁
                await self._release_lock(existing_lock)
                existing_lock = None
            
            # 2. 检查是否可以立即获取
            if existing_lock is None:
                return await self._grant_lock(request)
            
            # 3. 检查兼容性
            if self._is_compatible(existing_lock, request):
                return await self._upgrade_or_share(existing_lock, request)
            
            # 4. 不能获取，加入等待队列
            return await self._wait_for_lock(request)
    
    async def release(
        self,
        task_id: UUID,
        resource_id: str
    ) -> bool:
        """
        释放锁
        
        Args:
            task_id: 任务ID
            resource_id: 资源ID
            
        Returns:
            是否成功释放
        """
        async with self._lock:
            lock = self._locks.get(resource_id)
            if not lock:
                return False
            
            # 检查是否持有者
            if task_id not in lock.holders:
                return False
            
            # 移除持有者
            lock.holders.discard(task_id)
            self._task_locks[task_id].discard(resource_id)
            
            # 如果没有持有者，释放锁
            if not lock.holders:
                await self._release_lock(lock)
                # 唤醒等待队列中的请求
                await self._notify_waiters(resource_id)
            
            return True
    
    async def release_all(self, task_id: UUID):
        """释放任务持有的所有锁"""
        async with self._lock:
            resource_ids = self._task_locks.get(task_id, set()).copy()
            for resource_id in resource_ids:
                await self._release_lock(self._locks.get(resource_id))
                await self._notify_waiters(resource_id)
    
    async def upgrade(
        self,
        task_id: UUID,
        resource_id: str,
        new_type: LockType
    ) -> Optional[Lock]:
        """
        升级锁（只适用于UPDATE锁升级为EXCLUSIVE）
        
        Args:
            task_id: 任务ID
            resource_id: 资源ID
            new_type: 新的锁类型
            
        Returns:
            升级后的锁对象
        """
        async with self._lock:
            lock = self._locks.get(resource_id)
            if not lock:
                return None
            
            # 检查是否是持有者
            if task_id not in lock.holders:
                return None
            
            # 只允许 UPDATE -> EXCLUSIVE 升级
            if lock.lock_type != LockType.UPDATE or \
               new_type != LockType.EXCLUSIVE:
                return None
            
            # 检查是否可以升级（没有其他共享锁）
            if len(lock.holders) > 1:
                return None
            
            # 升级
            lock.lock_type = LockType.EXCLUSIVE
            return lock
    
    def _is_compatible(self, lock: Lock, request: LockRequest) -> bool:
        """检查锁兼容性"""
        # 资源不同，直接兼容
        if lock.resource_id != request.resource_id:
            return True
        
        # 排他锁不兼容任何新请求
        if lock.lock_type == LockType.EXCLUSIVE:
            return False
        
        # 更新锁只允许一个持有者
        if lock.lock_type == LockType.UPDATE:
            if len(lock.holders) > 0:
                return False
        
        return True
    
    async def _grant_lock(self, request: LockRequest) -> Lock:
        """授予锁"""
        lock = Lock(
            lock_id=UUID(),
            lock_type=request.lock_type,
            granularity=request.granularity,
            resource_id=request.resource_id,
            holder_id=request.task_id,
            holders={request.task_id},
            mode=request.mode,
            version=1
        )
        
        self._locks[request.resource_id] = lock
        self._task_locks[request.task_id].add(request.resource_id)
        request.granted = True
        request.lock = lock
        
        self._lock_count += 1
        
        return lock
    
    async def _upgrade_or_share(
        self,
        lock: Lock,
        request: LockRequest
    ) -> Optional[Lock]:
        """升级或共享现有锁"""
        # 如果是共享锁类型，可以共享
        if lock.can_share and request.lock_type == LockType.SHARED:
            lock.holders.add(request.task_id)
            self._task_locks[request.task_id].add(request.resource_id)
            request.granted = True
            request.lock = lock
            return lock
        
        return None
    
    async def _wait_for_lock(self, request: LockRequest) -> Optional[Lock]:
        """
        等待锁（加入等待队列）
        
        实现了公平锁：按优先级和时间排序
        """
        # 计算过期时间
        expires_at = datetime.now().replace(
            microsecond=0
        ).timestamp() + request.timeout
        
        # 按优先级插入队列
        queue = self._wait_queues[request.resource_id]
        inserted = False
        for i, waiter in enumerate(queue):
            if waiter.priority < request.priority:
                queue.insert(i, request)
                inserted = True
                break
        if not inserted:
            queue.append(request)
        
        # 更新等待图
        self._wait_graph[request.task_id].add(request.resource_id)
        
        # 检查死锁
        if self._detect_deadlock(request.task_id):
            self._wait_graph[request.task_id].discard(request.resource_id)
            return None
        
        # 异步等待
        request.expires_at = datetime.fromtimestamp(expires_at)
        
        return None  # 返回None，由调用者处理等待
    
    async def _release_lock(self, lock: Lock):
        """释放锁"""
        if not lock:
            return
        
        # 移除所有持有者的引用
        for task_id in lock.holders:
            self._task_locks[task_id].discard(lock.resource_id)
        
        # 清理等待图
        for task_id in lock.holders:
            self._wait_graph[task_id].discard(lock.resource_id)
        
        # 删除锁
        self._locks.pop(lock.resource_id, None)
    
    async def _notify_waiters(self, resource_id: str):
        """通知等待队列中的请求"""
        queue = self._wait_queues[resource_id]
        
        # 移除已过期或被取消的请求
        now = datetime.now()
        queue[:] = [r for r in queue if r.expires_at and r.expires_at > now]
        
        # 如果有等待者，授予第一个
        if queue:
            request = queue.pop(0)
            await self._grant_lock(request)
    
    def _detect_deadlock(self, task_id: UUID, visited: Set[UUID] = None) -> bool:
        """
        检测死锁
        
        使用有向图检测循环等待
        """
        if visited is None:
            visited = set()
        
        if task_id in visited:
            return True
        
        visited.add(task_id)
        
        for resource_id in self._wait_graph.get(task_id, set()):
            lock = self._locks.get(resource_id)
            if lock:
                for holder_id in lock.holders:
                    if self._detect_deadlock(holder_id, visited.copy()):
                        return True
        
        return False
    
    async def check_lock(self, resource_id: str) -> Optional[Lock]:
        """检查资源的锁状态"""
        return self._locks.get(resource_id)
    
    def get_task_locks(self, task_id: UUID) -> Set[str]:
        """获取任务持有的所有锁"""
        return self._task_locks.get(task_id, set())


@dataclass
class LockConfig:
    """锁管理器配置"""
    # 超时设置
    default_timeout: float = 30.0
    
    # 重试设置
    enable_retry: bool = True
    retry_interval: float = 0.5
    max_retries: int = 60
    
    # 死锁检测
    enable_deadlock_detection: bool = True
    deadlock_check_interval: float = 5.0
    
    # 乐观锁
    enable_optimistic: bool = True
    max_version_conflicts: int = 3
```

---

## 4. 冲突检测与解决

### 4.1 冲突类型定义

```python
class ConflictType(Enum):
    """冲突类型"""
    # 读写冲突
    READ_WRITE = auto()       # 读写冲突
    WRITE_READ = auto()       # 写读冲突
    WRITE_WRITE = auto()      # 写写冲突
    
    # 因果冲突
    CAUSAL_CONTRADICTION = auto()   # 因果矛盾
    CAUSAL_CYCLE = auto()           # 因果循环
    CAUSAL_ORDER = auto()           # 因果顺序冲突
    
    # 版本冲突
    VERSION_MISMATCH = auto()       # 版本不匹配
    VERSION_STALE = auto()          # 版本过期
    
    # 语义冲突
    SEMANTIC_CONFLICT = auto()      # 语义冲突
    DUPLICATE_ENTITY = auto()        # 实体重复

@dataclass
class Conflict:
    """
    冲突对象
    
    表示两个任务链之间的冲突
    """
    conflict_id: UUID
    conflict_type: ConflictType
    
    # 涉及的实体
    memory_id: str
    task_chain_a: UUID
    task_chain_b: UUID
    
    # 冲突操作
    operation_a: 'MemoryOperation'
    operation_b: 'MemoryOperation'
    
    # 冲突数据
    value_a: Any
    value_b: Any
    
    # 冲突版本
    version_a: int
    version_b: int
    
    # 时间戳
    detected_at: datetime = field(default_factory=datetime.now)
    
    # 解决状态
    resolved: bool = False
    resolution: Optional[str] = None
    resolved_at: Optional[datetime] = None

@dataclass
class MemoryOperation:
    """
    记忆操作
    
    记录对记忆的操作
    """
    operation_id: UUID
    task_id: UUID
    
    # 操作类型
    operation_type: str  # READ, WRITE, DELETE, MERGE
    
    # 目标记忆
    memory_id: str
    layer: str  # L0, L1, L2, L3
    
    # 操作数据
    old_value: Any
    new_value: Any
    
    # 版本
    version_before: int
    version_after: int
    
    # 时间
    timestamp: datetime = field(default_factory=datetime.now)
    
    # 检查点（用于中断恢复）
    checkpoint_id: Optional[UUID] = None
    
    # 因果信息
    causal_dependencies: List[UUID] = field(default_factory=list)  # 依赖的操作ID
    
    def __hash__(self):
        return hash(self.operation_id)
```

### 4.2 冲突检测器

```python
class ConflictDetector:
    """
    冲突检测器
    
    检测任务链之间的操作冲突
    """
    
    def __init__(self, lock_manager: LockManager):
        self.lock_manager = lock_manager
        
        # 操作历史
        self._operation_log: Dict[str, List[MemoryOperation]] = defaultdict(list)
        
        # 任务依赖图: task_id -> Set[依赖的task_id]
        self._dependency_graph: Dict[UUID, Set[UUID]] = defaultdict(set)
        
        # 冲突回调
        self._conflict_handlers: Dict[ConflictType, Callable] = {}
    
    def register_handler(
        self,
        conflict_type: ConflictType,
        handler: Callable[['Conflict'], 'Resolution']
    ):
        """注册冲突处理器"""
        self._conflict_handlers[conflict_type] = handler
    
    def record_operation(self, operation: MemoryOperation):
        """记录操作"""
        self._operation_log[operation.memory_id].append(operation)
        
        # 更新依赖图
        for dep_id in operation.causal_dependencies:
            self._dependency_graph[operation.task_id].add(dep_id)
        
        # 限制历史长度
        if len(self._operation_log[operation.memory_id]) > 100:
            self._operation_log[operation.memory_id] = \
                self._operation_log[operation.memory_id][-100:]
    
    def detect_conflicts(
        self,
        new_operation: MemoryOperation,
        existing_operations: List[MemoryOperation] = None
    ) -> List[Conflict]:
        """
        检测冲突
        
        Args:
            new_operation: 新操作
            existing_operations: 现有操作（不指定则从历史获取）
            
        Returns:
            检测到的冲突列表
        """
        if existing_operations is None:
            existing_operations = self._operation_log.get(
                new_operation.memory_id, []
            )
        
        conflicts = []
        
        for existing in existing_operations:
            # 跳过同一任务链的操作
            if existing.task_id == new_operation.task_id:
                continue
            
            # 跳过已解决的操作
            if hasattr(existing, 'resolved') and existing.resolved:
                continue
            
            # 检测不同类型的冲突
            conflict = self._check_conflict_type(
                new_operation, existing
            )
            
            if conflict:
                conflicts.append(conflict)
        
        return conflicts
    
    def _check_conflict_type(
        self,
        op1: MemoryOperation,
        op2: MemoryOperation
    ) -> Optional[Conflict]:
        """检查冲突类型"""
        # 1. 版本冲突（乐观锁）
        if self._lock_manager.config.enable_optimistic:
            if op2.operation_type == 'WRITE' and \
               op1.version_before <= op2.version_after:
                return Conflict(
                    conflict_id=UUID(),
                    conflict_type=ConflictType.VERSION_STALE,
                    memory_id=op1.memory_id,
                    task_chain_a=op1.task_id,
                    task_chain_b=op2.task_id,
                    operation_a=op1,
                    operation_b=op2,
                    value_a=op1.new_value,
                    value_b=op2.new_value,
                    version_a=op1.version_before,
                    version_b=op2.version_after
                )
        
        # 2. 写-写冲突
        if op1.operation_type == 'WRITE' and op2.operation_type == 'WRITE':
            return Conflict(
                conflict_id=UUID(),
                conflict_type=ConflictType.WRITE_WRITE,
                memory_id=op1.memory_id,
                task_chain_a=op1.task_id,
                task_chain_b=op2.task_id,
                operation_a=op1,
                operation_b=op2,
                value_a=op1.new_value,
                value_b=op2.new_value,
                version_a=op1.version_after,
                version_b=op2.version_after
            )
        
        # 3. 读写冲突
        if op1.operation_type == 'WRITE' and op2.operation_type == 'READ':
            return Conflict(
                conflict_id=UUID(),
                conflict_type=ConflictType.READ_WRITE,
                memory_id=op1.memory_id,
                task_chain_a=op1.task_id,
                task_chain_b=op2.task_id,
                operation_a=op1,
                operation_b=op2,
                value_a=op1.new_value,
                value_b=op2.old_value,
                version_a=op1.version_after,
                version_b=op2.version_after
            )
        
        # 4. 因果冲突
        causal_conflict = self._check_causal_conflict(op1, op2)
        if causal_conflict:
            return causal_conflict
        
        return None
    
    def _check_causal_conflict(
        self,
        op1: MemoryOperation,
        op2: MemoryOperation
    ) -> Optional[Conflict]:
        """检查因果冲突"""
        # 检查是否存在因果循环
        if self._would_create_cycle(op1.task_id, op2.task_id):
            return Conflict(
                conflict_id=UUID(),
                conflict_type=ConflictType.CAUSAL_CYCLE,
                memory_id=op1.memory_id,
                task_chain_a=op1.task_id,
                task_chain_b=op2.task_id,
                operation_a=op1,
                operation_b=op2,
                value_a=op1.new_value,
                value_b=op2.new_value,
                version_a=op1.version_after,
                version_b=op2.version_after
            )
        
        return None
    
    def _would_create_cycle(self, task_a: UUID, task_b: UUID) -> bool:
        """检查是否会产生因果循环"""
        # 如果 task_a 依赖 task_b，且 task_b 依赖 task_a，则形成循环
        return (task_a in self._dependency_graph.get(task_b, set()) and
                task_b in self._dependency_graph.get(task_a, set()))
```

### 4.3 冲突解决策略

```python
class ConflictResolution(Enum):
    """冲突解决策略"""
    # 自动解决
    AUTO_FIRST = auto()        # 自动使用第一个
    AUTO_LAST = auto()         # 自动使用最后一个
    AUTO_MERGE = auto()        # 自动合并
    AUTO_VERSION = auto()      # 基于版本号
    
    # 手动解决
    MANUAL = auto()            # 人工介入
    CHAIN_A_PRIORITY = auto()  # 任务链A优先
    CHAIN_B_PRIORITY = auto()  # 任务链B优先
    
    # 回滚
    ROLLBACK_A = auto()        # 回滚任务链A
    ROLLBACK_B = auto()        # 回滚任务链B
    ROLLBACK_BOTH = auto()     # 回滚两者

@dataclass
class Resolution:
    """解决结果"""
    resolution: ConflictResolution
    winning_value: Any
    winning_operation: MemoryOperation
    losing_operation: MemoryOperation
    merged_value: Optional[Any] = None
    compensation_actions: List['CompensationAction'] = field(default_factory=list)
    requires_rewrite: bool = False

@dataclass
class CompensationAction:
    """补偿动作（用于回滚）"""
    action_type: str
    target_memory_id: str
    previous_value: Any
    previous_version: int

class ConflictResolver:
    """
    冲突解决器
    
    根据配置和策略解决冲突
    """
    
    def __init__(self, memory_store: 'MemoryStore'):
        self.memory_store = memory_store
        
        # 默认策略
        self._default_strategy: Dict[ConflictType, ConflictResolution] = {
            ConflictType.VERSION_STALE: ConflictResolution.AUTO_MERGE,
            ConflictType.WRITE_WRITE: ConflictResolution.AUTO_LAST,
            ConflictType.READ_WRITE: ConflictResolution.AUTO_LAST,
            ConflictType.CAUSAL_CYCLE: ConflictResolution.ROLLBACK_B,
            ConflictType.CAUSAL_CONTRADICTION: ConflictResolution.MANUAL,
        }
        
        # 任务优先级
        self._task_priorities: Dict[UUID, int] = {}
    
    def set_task_priority(self, task_id: UUID, priority: int):
        """设置任务优先级"""
        self._task_priorities[task_id] = priority
    
    def resolve(
        self,
        conflict: Conflict
    ) -> Resolution:
        """
        解决冲突
        
        Args:
            conflict: 冲突对象
            
        Returns:
            解决结果
        """
        strategy = self._default_strategy.get(
            conflict.conflict_type,
            ConflictResolution.AUTO_LAST
        )
        
        return self._apply_strategy(conflict, strategy)
    
    def resolve_with_strategy(
        self,
        conflict: Conflict,
        strategy: ConflictResolution
    ) -> Resolution:
        """使用指定策略解决冲突"""
        return self._apply_strategy(conflict, strategy)
    
    def _apply_strategy(
        self,
        conflict: Conflict,
        strategy: ConflictResolution
    ) -> Resolution:
        """应用解决策略"""
        handlers = {
            ConflictResolution.AUTO_FIRST: self._resolve_auto_first,
            ConflictResolution.AUTO_LAST: self._resolve_auto_last,
            ConflictResolution.AUTO_MERGE: self._resolve_auto_merge,
            ConflictResolution.AUTO_VERSION: self._resolve_auto_version,
            ConflictResolution.CHAIN_A_PRIORITY: self._resolve_chain_a,
            ConflictResolution.CHAIN_B_PRIORITY: self._resolve_chain_b,
            ConflictResolution.ROLLBACK_A: self._resolve_rollback_a,
            ConflictResolution.ROLLBACK_B: self._resolve_rollback_b,
        }
        
        handler = handlers.get(strategy)
        if handler:
            return handler(conflict)
        
        return self._resolve_auto_last(conflict)
    
    def _resolve_auto_first(self, conflict: Conflict) -> Resolution:
        """使用第一个操作的值"""
        return Resolution(
            resolution=ConflictResolution.AUTO_FIRST,
            winning_value=conflict.value_a,
            winning_operation=conflict.operation_a,
            losing_operation=conflict.operation_b
        )
    
    def _resolve_auto_last(self, conflict: Conflict) -> Resolution:
        """使用最后一个操作的值"""
        # 最后一个通常是时间戳较晚的
        if conflict.operation_a.timestamp > conflict.operation_b.timestamp:
            return Resolution(
                resolution=ConflictResolution.AUTO_LAST,
                winning_value=conflict.value_a,
                winning_operation=conflict.operation_a,
                losing_operation=conflict.operation_b
            )
        else:
            return Resolution(
                resolution=ConflictResolution.AUTO_LAST,
                winning_value=conflict.value_b,
                winning_operation=conflict.operation_b,
                losing_operation=conflict.operation_a
            )
    
    def _resolve_auto_merge(self, conflict: Conflict) -> Resolution:
        """
        自动合并
        
        对于记忆系统，尝试合并两个修改
        """
        # 尝试结构性合并
        merged = self._try_structural_merge(
            conflict.value_a,
            conflict.value_b
        )
        
        if merged:
            return Resolution(
                resolution=ConflictResolution.AUTO_MERGE,
                winning_value=merged,
                winning_operation=conflict.operation_a,
                losing_operation=conflict.operation_b,
                merged_value=merged,
                requires_rewrite=True
            )
        
        # 合并失败，使用时间戳
        return self._resolve_auto_last(conflict)
    
    def _try_structural_merge(self, value_a: Any, value_b: Any) -> Optional[Any]:
        """尝试结构性合并"""
        # 对于字典/对象，尝试合并字段
        if isinstance(value_a, dict) and isinstance(value_b, dict):
            merged = value_a.copy()
            for key, val_b in value_b.items():
                if key not in merged:
                    merged[key] = val_b
                elif merged[key] != val_b:
                    # 字段冲突，标记为需要人工处理
                    return None
            return merged
        
        # 对于列表，尝试合并元素
        if isinstance(value_a, list) and isinstance(value_b, list):
            merged = list(set(value_a + value_b))
            return merged
        
        # 无法合并
        return None
    
    def _resolve_auto_version(self, conflict: Conflict) -> Resolution:
        """基于版本号解决"""
        if conflict.version_a > conflict.version_b:
            return Resolution(
                resolution=ConflictResolution.AUTO_VERSION,
                winning_value=conflict.value_a,
                winning_operation=conflict.operation_a,
                losing_operation=conflict.operation_b
            )
        else:
            return Resolution(
                resolution=ConflictResolution.AUTO_VERSION,
                winning_value=conflict.value_b,
                winning_operation=conflict.operation_b,
                losing_operation=conflict.operation_a
            )
    
    def _resolve_chain_a(self, conflict: Conflict) -> Resolution:
        """任务链A优先"""
        return Resolution(
            resolution=ConflictResolution.CHAIN_A_PRIORITY,
            winning_value=conflict.value_a,
            winning_operation=conflict.operation_a,
            losing_operation=conflict.operation_b
        )
    
    def _resolve_chain_b(self, conflict: Conflict) -> Resolution:
        """任务链B优先"""
        return Resolution(
            resolution=ConflictResolution.CHAIN_B_PRIORITY,
            winning_value=conflict.value_b,
            winning_operation=conflict.operation_b,
            losing_operation=conflict.operation_a
        )
    
    def _resolve_rollback_a(self, conflict: Conflict) -> Resolution:
        """回滚任务链A"""
        return Resolution(
            resolution=ConflictResolution.ROLLBACK_A,
            winning_value=conflict.value_b,
            winning_operation=conflict.operation_b,
            losing_operation=conflict.operation_a,
            compensation_actions=[
                CompensationAction(
                    action_type="ROLLBACK",
                    target_memory_id=conflict.memory_id,
                    previous_value=conflict.operation_a.old_value,
                    previous_version=conflict.version_a
                )
            ],
            requires_rewrite=True
        )
    
    def _resolve_rollback_b(self, conflict: Conflict) -> Resolution:
        """回滚任务链B"""
        return Resolution(
            resolution=ConflictResolution.ROLLBACK_B,
            winning_value=conflict.value_a,
            winning_operation=conflict.operation_a,
            losing_operation=conflict.operation_b,
            compensation_actions=[
                CompensationAction(
                    action_type="ROLLBACK",
                    target_memory_id=conflict.memory_id,
                    previous_value=conflict.operation_b.old_value,
                    previous_version=conflict.version_b
                )
            ],
            requires_rewrite=True
        )
```

---

## 5. 任务链协调器

### 5.1 协调器核心

```python
class TaskChainCoordinator:
    """
    任务链协调器
    
    负责:
    1. 管理多个任务链的执行
    2. 处理任务链间的协调
    3. 保证因果一致性
    """
    
    def __init__(
        self,
        lock_manager: LockManager,
        conflict_detector: ConflictDetector,
        conflict_resolver: ConflictResolver
    ):
        self.lock_manager = lock_manager
        self.conflict_detector = conflict_detector
        self.conflict_resolver = conflict_resolver
        
        # 任务链管理
        self._task_chains: Dict[UUID, 'TaskChain'] = {}
        self._running_chains: Set[UUID] = set()
        self._blocked_chains: Dict[UUID, 'BlockReason'] = {}
        
        # 共享资源访问
        self._shared_access_log: Dict[str, List['AccessRecord']] = defaultdict(list)
        
        # 协调锁
        self._coordinator_lock = asyncio.Lock()
    
    async def register_chain(self, chain: 'TaskChain'):
        """注册任务链"""
        async with self._coordinator_lock:
            self._task_chains[chain.chain_id] = chain
            self._running_chains.add(chain.chain_id)
    
    async def unregister_chain(self, chain_id: UUID):
        """注销任务链"""
        async with self._coordinator_lock:
            self._task_chains.pop(chain_id, None)
            self._running_chains.discard(chain_id)
            self._blocked_chains.pop(chain_id, None)
    
    async def request_access(
        self,
        chain_id: UUID,
        resource_id: str,
        access_type: LockType,
        expected_version: int = None
    ) -> 'AccessResult':
        """
        请求访问资源
        
        Args:
            chain_id: 任务链ID
            resource_id: 资源ID
            access_type: 访问类型（锁类型）
            expected_version: 期望版本（用于乐观锁）
            
        Returns:
            访问结果
        """
        async with self._coordinator_lock:
            chain = self._task_chains.get(chain_id)
            if not chain:
                return AccessResult(success=False, reason="Chain not found")
            
            # 1. 检查是否有因果依赖阻塞
            if self._is_blocked_by_dependency(chain):
                return AccessResult(
                    success=False,
                    reason="Blocked by causal dependency",
                    blocked_by=self._get_blocking_chains(chain)
                )
            
            # 2. 检查是否会产生冲突
            pending_conflicts = self._check_pending_conflicts(
                chain_id, resource_id
            )
            
            if pending_conflicts:
                return AccessResult(
                    success=False,
                    reason="Pending conflicts",
                    conflicts=pending_conflicts
                )
            
            # 3. 尝试获取锁
            lock_request = LockRequest(
                request_id=UUID(),
                task_id=chain_id,
                lock_type=access_type,
                granularity=LockGranularity.MEMORY_ITEM,
                resource_id=resource_id,
                mode="pessimistic",
                expected_version=expected_version,
                priority=chain.priority
            )
            
            lock = await self.lock_manager.acquire(lock_request)
            
            if lock:
                # 记录访问
                self._record_access(chain_id, resource_id, access_type)
                return AccessResult(
                    success=True,
                    lock=lock
                )
            else:
                # 获取失败，被阻塞
                return AccessResult(
                    success=False,
                    reason="Lock acquisition failed",
                    waiting=True
                )
    
    async def release_access(
        self,
        chain_id: UUID,
        resource_id: str
    ) -> bool:
        """释放资源访问"""
        success = await self.lock_manager.release(chain_id, resource_id)
        
        if success:
            # 清理访问记录
            self._shared_access_log[resource_id] = [
                r for r in self._shared_access_log[resource_id]
                if r.chain_id != chain_id
            ]
            
            # 检查是否可以唤醒被阻塞的链
            await self._check_blocked_chains()
        
        return success
    
    def _is_blocked_by_dependency(self, chain: 'TaskChain') -> bool:
        """检查是否被因果依赖阻塞"""
        for dep_id in chain.causal_dependencies:
            if dep_id in self._running_chains:
                # 依赖的链还在运行
                dep_chain = self._task_chains.get(dep_id)
                if dep_chain and not dep_chain.is_completed:
                    return True
        return False
    
    def _check_pending_conflicts(
        self,
        chain_id: UUID,
        resource_id: str
    ) -> List[Conflict]:
        """检查待处理的冲突"""
        # 简化实现
        return []
    
    def _record_access(
        self,
        chain_id: UUID,
        resource_id: str,
        access_type: LockType
    ):
        """记录资源访问"""
        record = AccessRecord(
            chain_id=chain_id,
            resource_id=resource_id,
            access_type=access_type,
            timestamp=datetime.now()
        )
        self._shared_access_log[resource_id].append(record)
    
    async def _check_blocked_chains(self):
        """检查被阻塞的链是否可以继续"""
        for chain_id in list(self._blocked_chains.keys()):
            chain = self._task_chains.get(chain_id)
            if not chain:
                continue
            
            if not self._is_blocked_by_dependency(chain):
                # 解除阻塞
                reason = self._blocked_chains.pop(chain_id)
                # 通知链可以继续
                await chain.notify_unblocked(reason)


@dataclass
class AccessResult:
    """访问结果"""
    success: bool
    reason: Optional[str] = None
    lock: Optional[Lock] = None
    waiting: bool = False
    conflicts: List[Conflict] = field(default_factory=list)
    blocked_by: List[UUID] = field(default_factory=list)

@dataclass
class AccessRecord:
    """访问记录"""
    chain_id: UUID
    resource_id: str
    access_type: LockType
    timestamp: datetime

@dataclass
class BlockReason:
    """阻塞原因"""
    reason: str
    blocked_by_chain: UUID
    blocking_resource: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
```

### 5.2 任务链实现

```python
@dataclass
class TaskChain:
    """
    任务链
    
    代表一个有序的任务执行序列
    """
    chain_id: UUID
    name: str
    
    # 任务列表
    tasks: List['Task'] = field(default_factory=list)
    current_task_index: int = 0
    
    # 优先级
    priority: int = 0
    
    # 因果依赖
    causal_dependencies: Set[UUID] = field(default_factory=default_set)
    
    # 状态
    status: TaskStatus = TaskStatus.PENDING
    is_completed: bool = False
    
    # 共享资源
    accessed_resources: Set[str] = field(default_factory=set)
    
    # 版本跟踪
    resource_versions: Dict[str, int] = field(default_factory=dict)
    
    # 协调器引用
    coordinator: Optional[TaskChainCoordinator] = field(default=None, repr=False)
    
    # 回调
    _on_unblocked: asyncio.Event = field(default_factory=asyncio.Event)
    
    async def notify_unblocked(self, reason: 'BlockReason'):
        """通知链已解除阻塞"""
        self._on_unblocked.set()
    
    async def wait_until_unblocked(self, timeout: float = None) -> bool:
        """等待解除阻塞"""
        try:
            await asyncio.wait_for(
                self._on_unblocked.wait(),
                timeout=timeout
            )
            return True
        except asyncio.TimeoutError:
            return False
    
    async def execute_next(self) -> Optional[Any]:
        """执行下一个任务"""
        if self.current_task_index >= len(self.tasks):
            self.is_completed = True
            self.status = TaskStatus.COMPLETED
            return None
        
        task = self.tasks[self.current_task_index]
        
        # 获取任务需要的资源
        required_resources = task.required_resources
        
        # 请求资源访问
        for resource_id in required_resources:
            result = await self.coordinator.request_access(
                self.chain_id,
                resource_id,
                LockType.EXCLUSIVE
            )
            
            if not result.success:
                # 被阻塞
                if result.blocked_by:
                    self.status = TaskStatus.WAITING_DEPENDENCY
                else:
                    self.status = TaskStatus.WAITING_RESOURCE
                return None
        
        # 执行任务
        try:
            result = await task.execute()
            self.current_task_index += 1
            return result
        finally:
            # 释放资源
            for resource_id in required_resources:
                await self.coordinator.release_access(
                    self.chain_id,
                    resource_id
                )
    
    async def execute_all(self) -> List[Any]:
        """执行所有任务"""
        results = []
        while not self.is_completed:
            result = await self.execute_next()
            if result is not None:
                results.append(result)
        return results


def default_set():
    """默认集合工厂"""
    return set()
```

---

## 6. 完整使用示例

```python
async def example_multi_chain_coordination():
    """
    多任务链协调示例
    
    场景：
    - 任务链A: 整合用户的早餐记忆
    - 任务链B: 整合用户的午餐记忆
    - 两条链都操作"用户偏好"记忆
    """
    
    # 1. 初始化组件
    lock_config = LockConfig(
        default_timeout=30.0,
        enable_optimistic=True
    )
    lock_manager = LockManager(lock_config)
    conflict_detector = ConflictDetector(lock_manager)
    conflict_resolver = ConflictResolver(MemoryStore())
    
    coordinator = TaskChainCoordinator(
        lock_manager,
        conflict_detector,
        conflict_resolver
    )
    
    # 2. 注册冲突处理器
    def handle_write_write(conflict: Conflict) -> Resolution:
        """处理写-写冲突"""
        # 合并两个记忆片段
        merged = {
            **conflict.value_a,
            **conflict.value_b
        }
        return Resolution(
            resolution=ConflictResolution.AUTO_MERGE,
            winning_value=merged,
            winning_operation=conflict.operation_a,
            losing_operation=conflict.operation_b,
            merged_value=merged,
            requires_rewrite=True
        )
    
    conflict_resolver.register_handler(
        ConflictType.WRITE_WRITE,
        handle_write_write
    )
    
    # 3. 创建任务链A: 早餐整合
    chain_a = TaskChain(
        chain_id=UUID(),
        name="早餐整合链",
        priority=5,
        coordinator=coordinator
    )
    
    # 添加任务
    chain_a.tasks = [
        Task(
            task_id=UUID(),
            name="读取早餐记忆",
            required_resources={"memory_user_preferences", "memory_breakfast_history"}
        ),
        Task(
            task_id=UUID(),
            name="更新偏好",
            required_resources={"memory_user_preferences"}
        ),
        Task(
            task_id=UUID(),
            name="创建因果链接",
            required_resources={"memory_causal_links"}
        )
    ]
    
    # 4. 创建任务链B: 午餐整合
    chain_b = TaskChain(
        chain_id=UUID(),
        name="午餐整合链",
        priority=5,
        coordinator=coordinator
    )
    
    chain_b.tasks = [
        Task(
            task_id=UUID(),
            name="读取午餐记忆",
            required_resources={"memory_user_preferences", "memory_lunch_history"}
        ),
        Task(
            task_id=UUID(),
            name="更新偏好",
            required_resources={"memory_user_preferences"}  # 与链A冲突！
        )
    ]
    
    # 5. 注册并执行
    await coordinator.register_chain(chain_a)
    await coordinator.register_chain(chain_b)
    
    # 6. 并发执行（带冲突处理）
    results_a = await chain_a.execute_all()
    results_b = await chain_b.execute_all()
    
    print(f"链A结果: {results_a}")
    print(f"链B结果: {results_b}")
    
    # 7. 清理
    await coordinator.unregister_chain(chain_a.chain_id)
    await coordinator.unregister_chain(chain_b.chain_id)


async def example_conflict_resolution():
    """
    冲突解决示例
    """
    
    # 模拟冲突
    conflict = Conflict(
        conflict_id=UUID(),
        conflict_type=ConflictType.WRITE_WRITE,
        memory_id="memory_user_preferences",
        task_chain_a=UUID(),
        task_chain_b=UUID(),
        operation_a=MemoryOperation(
            operation_id=UUID(),
            task_id=UUID(),
            operation_type='WRITE',
            memory_id="memory_user_preferences",
            layer="L2",
            old_value={"breakfast": "粥"},
            new_value={"breakfast": "面包"},
            version_before=1,
            version_after=2
        ),
        operation_b=MemoryOperation(
            operation_id=UUID(),
            task_id=UUID(),
            operation_type='WRITE',
            memory_id="memory_user_preferences",
            layer="L2",
            old_value={"breakfast": "粥"},
            new_value={"breakfast": "面条"},
            version_before=1,
            version_after=2
        ),
        value_a={"breakfast": "面包"},
        value_b={"breakfast": "面条"},
        version_a=2,
        version_b=2
    )
    
    # 解决冲突
    resolver = ConflictResolver(MemoryStore())
    resolution = resolver.resolve(conflict)
    
    print(f"解决策略: {resolution.resolution}")
    print(f"获胜值: {resolution.winning_value}")
    print(f"需要重写: {resolution.requires_rewrite}")
```

---

## 7. 设计总结

### 7.1 冲突类型与解决策略

| 冲突类型 | 描述 | 默认策略 | 替代策略 |
|---------|------|---------|---------|
| **VERSION_STALE** | 版本过期，读取了旧数据 | AUTO_MERGE | ROLLBACK |
| **WRITE_WRITE** | 两个写操作冲突 | AUTO_LAST | CHAIN_A/B_PRIORITY |
| **READ_WRITE** | 读到未提交的写 | AUTO_LAST | ROLLBACK_READER |
| **CAUSAL_CYCLE** | 因果循环依赖 | ROLLBACK_B | MANUAL |
| **CAUSAL_CONTRADICTION** | 因果矛盾 | MANUAL | - |

### 7.2 锁机制对比

| 模式 | 优点 | 缺点 | 适用场景 |
|------|------|------|---------|
| **悲观锁** | 简单，避免冲突 | 并发度低，可能死锁 | 写多读少 |
| **乐观锁** | 高并发，冲突少时高效 | 冲突多时开销大 | 读多写少 |
| **混合锁** | 平衡两者 | 实现复杂 | 大多数场景 |

### 7.3 协调流程

```
任务链A ──→ 请求资源X ──→ 获得锁 ──→ 执行任务
                │                          │
                │                          ▼
                │                     操作记忆X
                │                          │
                ▼                          ▼
任务链B ──→ 请求资源X ──→ 等待锁 ◄── 释放锁
                                    │
                                    ▼
                               获得锁
                                    │
                                    ▼
                               执行任务（处理可能的冲突）
```

### 7.4 关键设计原则

1. **锁的粒度要合理**: 太粗影响并发，太细管理复杂
2. **超时机制必须**: 防止死锁导致的永久等待
3. **冲突检测要及时**: 尽早发现尽早处理
4. **补偿机制要完善**: 回滚时要能恢复到正确状态
5. **因果关系要清晰**: 确保记忆的逻辑一致性
