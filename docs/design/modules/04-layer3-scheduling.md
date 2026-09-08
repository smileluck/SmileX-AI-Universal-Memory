# Layer 3 任务调度 + 抢占恢复 + 项目冷启动 — 选型与算法优化

> **配套章节**：[agent-memory-design.md §8/§9/§9.5](../agent-memory-design.md)
> **阶段**：[MVP→P2] 冷启动 MVP，调度+抢占 P1，跨项目克隆 P2
> **依赖**：Layer 0/1/2

---

## 1. 模块概览

### 1.1 职责（三大子模块）

| 子模块 | 职责 | 阶段 |
|--------|------|------|
| **任务调度** | 三重触发 + 优先级队列 + 任务执行器 | P1 |
| **抢占恢复** | GRACEFUL 抢占 + Checkpoint + 断点续传 | P1 |
| **项目冷启动** | 六机制组合（向导/模板/批量导入/种子/主动学习/克隆）| MVP→P2 |

### 1.2 核心组件清单

```
src/memory/scheduler/
├── triggers/
│   ├── time_trigger.py        # cron 表达式
│   ├── event_trigger.py       # 事件订阅
│   └── adaptive_trigger.py    # 自适应规则（带冷却）
├── queue.py                   # 优先级队列（5 级）
├── executor/
│   ├── task_executor.py       # 20+ 任务实现
│   ├── interruptible.py       # 可中断基类
│   └── batch_task.py          # 批量任务
├── preemption/
│   ├── preemption_manager.py  # 抢占管理
│   ├── checkpoint.py          # 检查点管理
│   └── signal_handler.py      # 信号处理
└── bootstrap/
    ├── onboarding.py          # 初始化向导
    ├── templates.py           # 项目模板
    ├── seed_injector.py       # 种子注入
    ├── bulk_importer.py       # 批量导入
    ├── active_learner.py      # 主动学习
    └── cross_project_cloner.py # 跨项目克隆
```

---

## 2. 技术选型分析

### 2.1 调度框架：APScheduler vs Celery vs RQ vs 自研

| 方案 | 部署 | 功能 | 学习成本 | 异步原生 |
|------|------|------|---------|---------|
| **APScheduler** | 嵌入式 | cron + 间隔 + 事件 | 中 | ⚠️ |
| **Celery** | 独立 broker（Redis/RabbitMQ）| 强大（分布式）| 高 | ❌ |
| **RQ** | 独立 Redis | 简单 | 低 | ❌ |
| **自研 asyncio 调度** ★推荐 | 嵌入式 | 完全可控 | 中 | ✅ |

**推荐方案**：**自研轻量 asyncio 调度器**

**选型理由**：
- **零外部依赖**：不引入 Redis/RabbitMQ
- **asyncio 原生**：与 FastAPI / aiosqlite 一致
- **完全可控**：可定制抢占、优先级、冷却逻辑
- **个人场景足够**：单机不需要分布式

```python
class MemoryTaskScheduler:
    def __init__(self):
        self.queues = {p: asyncio.PriorityQueue() for p in TaskPriority}
        self.triggers = [TimeTrigger(), EventTrigger(), AdaptiveTrigger()]
        self.executor = TaskExecutor()
        self._running = False
    
    async def start(self):
        self._running = True
        await asyncio.gather(
            self._run_triggers(),
            self._run_executors(),
        )
```

**被否决方案**：
- **Celery**：依赖 Redis/RabbitMQ，违反嵌入式原则
- **APScheduler**：非 asyncio 原生，需线程池适配

---

### 2.2 优先级队列：asyncio.PriorityQueue vs heapdict vs 自研

| 方案 | 性能 | 公平性 | 特性 |
|------|------|--------|------|
| **asyncio.PriorityQueue** ★推荐 | 中 | 是 | 标准库 |
| **heapdict** | 高 | 是 | 第三方 |
| **list + sort** | 低（O(N log N)）| 是 | 简单 |
| **分级 list** ★推荐 | 高 | 是（按级别）| 易实现 |

**推荐方案**：**分级 list（5 个独立队列）**

```python
class PriorityScheduler:
    def __init__(self):
        # 5 个独立 deque，按优先级消费
        self.queues = {
            TaskPriority.CRITICAL: deque(),  # 立即执行
            TaskPriority.HIGH: deque(),
            TaskPriority.MEDIUM: deque(),
            TaskPriority.LOW: deque(),
            TaskPriority.IDLE: deque(),
        }
    
    async def next_task(self) -> Task:
        """按优先级消费，高级别优先"""
        for priority in TaskPriority:
            if self.queues[priority]:
                return self.queues[priority].popleft()
        await asyncio.sleep(0.1)  # 空队列等待
```

---

### 2.3 项目模板：硬编码 vs YAML 配置 vs JSON Schema

| 方案 | 灵活性 | 可读性 | 用户自定义 |
|------|--------|--------|-----------|
| **硬编码 Python** | 低 | ❌ | ❌ |
| **YAML 配置** ★推荐 | 高 | ✅ | ✅ |
| **JSON Schema** | 中 | 中 | ✅ |
| **数据库存储** | 高 | ❌ | ✅ |

**推荐方案**：**YAML 配置 + 内置默认模板**

**选型理由**：
- **用户可自定义**：编辑 YAML 即可扩展模板
- **可读性高**：YAML 支持注释、多行字符串
- **内置默认**：常见模板（web/cli/data/agent）随包分发

```yaml
# templates/web.yaml
name: "Web Application"
description: "Web 应用项目模板"
match:
  tech_stack: ["fastapi", "django", "flask"]
skeleton_entities:
  - type: concept
    name: "HTTP API"
    properties:
      methods: ["GET", "POST", "PUT", "DELETE"]
  - type: concept
    name: "Database"
    properties:
      types: ["SQLite", "PostgreSQL", "MySQL"]
  - type: concept
    name: "Authentication"
skeleton_triples:
  - subject: "HTTP API"
    predicate: "depends_on"
    object: "Database"
```

---

### 2.4 LLM 提取：Function Calling vs JSON Mode vs Prompt Engineering

| 方案 | 结构化 | 准确率 | 模型支持 |
|------|--------|--------|---------|
| **Function Calling** ★推荐 | ✅ 严格 | ⭐⭐⭐⭐⭐ | 主流模型 |
| **JSON Mode** | ✅ | ⭐⭐⭐⭐ | 部分 |
| **Prompt + 解析** | ⚠️ 易错 | ⭐⭐⭐ | 全部 |

**推荐方案**：**Function Calling（OpenAI 兼容）**

**选型理由**：
- **结构化输出**：JSON Schema 约束，低幻觉
- **主流模型支持**：OpenAI / Claude / DeepSeek / GLM 等
- **错误处理简单**：参数校验失败直接重试

```python
EXTRACT_SCHEMA = {
    "name": "extract_entities",
    "parameters": {
        "type": "object",
        "properties": {
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string", "enum": ["person", "tech", "concept"]},
                    },
                    "required": ["name", "type"],
                },
            },
            "triples": {"type": "array", ...},
        },
        "required": ["entities"],
    },
}

async def extract_from_readme(content: str) -> list[Entity]:
    result = await llm.chat(
        messages=[{"role": "user", "content": f"提取实体:\n{content}"}],
        tools=[EXTRACT_SCHEMA],
    )
    return [Entity(**e) for e in result.entities]
```

---

### 2.5 批量导入数据源：Git vs Markdown vs Jira vs OpenAPI

| 数据源 | 解析方式 | 实现难度 | 价值 |
|--------|---------|---------|------|
| **Git 历史** ★P1 | `git log --format=json` | 中 | 高（事件流）|
| **Markdown 文档** ★MVP | 正则 + LLM 提取 | 低 | 高（README/Wiki）|
| **OpenAPI** ★P1 | YAML 解析 | 中 | 高（API 实体）|
| **Jira/Linear** | REST API | 中 | 中（需凭证）|
| **Slack/Teams** | Export JSON | 中 | 中（对话流）|

**推荐路径**：MVP 先做 Markdown（最简单），P1 加 Git + OpenAPI。

---

## 3. 核心算法与优化

### 3.1 三重触发调度算法

```python
class AdaptiveTrigger:
    """自适应触发器（带冷却）"""
    
    COOLDOWNS = {
        "memory_pressure": 300,    # 5 分钟
        "idle_consolidate": 60,    # 1 分钟
        "hot_refresh": 1800,       # 30 分钟
        "pattern_discovery": 3600, # 1 小时
        "causal_inference": 300,   # 5 分钟
    }
    
    def __init__(self):
        self.last_fired = defaultdict(float)
    
    async def check_and_fire(self):
        for rule, cooldown in self.COOLDOWNS.items():
            if self._should_fire(rule, cooldown):
                await self._fire(rule)
                self.last_fired[rule] = time.time()
    
    def _should_fire(self, rule: str, cooldown: int) -> bool:
        if time.time() - self.last_fired[rule] < cooldown:
            return False  # 冷却中
        return await self._check_condition(rule)
```

---

### 3.2 优先级队列调度算法

```python
class PriorityExecutor:
    """按优先级消费，CRITICAL 立即执行"""
    
    CONCURRENCY = {
        TaskPriority.CRITICAL: float('inf'),  # 立即
        TaskPriority.HIGH: 3,
        TaskPriority.MEDIUM: 2,
        TaskPriority.LOW: 1,
        TaskPriority.IDLE: 1,
    }
    
    async def run(self):
        running = defaultdict(int)
        while True:
            task = await self._next_runnable(running)
            if task is None:
                await asyncio.sleep(0.1)
                continue
            
            asyncio.create_task(self._execute(task, running))
    
    async def _next_runnable(self, running):
        for priority in TaskPriority:
            limit = self.CONCURRENCY[priority]
            if running[priority] >= limit:
                continue
            if self.queues[priority]:
                return self.queues[priority].popleft()
        return None
```

---

### 3.3 抢占与检查点协议

```python
class InterruptibleTask:
    """可中断任务基类"""
    
    CHECKPOINT_INTERVAL = 100  # 每 100 步检查一次
    
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.step = 0
        self.cursor = None
        self.state = {}
        self._should_pause = False
    
    async def run(self):
        try:
            for item in self.cursor.iter():
                self.check_interruption()  # 抛 InterruptException
                await self.process(item)
                self.step += 1
                if self.step % self.CHECKPOINT_INTERVAL == 0:
                    await self.save_checkpoint()
        except InterruptException:
            await self.save_checkpoint(can_resume=True)
            raise
    
    def check_interruption(self):
        if self._should_pause:
            raise InterruptException(
                checkpoint=self.create_checkpoint(),
                can_resume=True,
            )
    
    async def save_checkpoint(self, can_resume=False):
        cp = Checkpoint(
            task_id=self.task_id,
            progress=self.step / self.total_steps,
            step=self.step,
            state=self.state,
            cursor=self.cursor.serialize(),
        )
        await db.save_checkpoint(cp)
    
    async def resume(self, checkpoint: Checkpoint):
        self.cursor = self.cursor.deserialize(checkpoint.cursor)
        self.state = checkpoint.state
        self.step = checkpoint.step
        await self.run()
```

**抢占决策算法**：

```python
async def preempt(task_id: str, policy: PreemptionPolicy):
    task = running_tasks[task_id]
    
    if policy == PreemptionPolicy.IMMEDIATE:
        task.cancel()  # 立即取消
    elif policy == PreemptionPolicy.GRACEFUL:
        task._should_pause = True  # 通知任务
        await asyncio.wait_for(task.done_event.wait(), timeout=30)
        if not task.done_event.is_set():
            task.cancel()  # 超时强制
    elif policy == PreemptionPolicy.COOPERATIVE:
        task._should_pause = True  # 等任务主动让步
```

---

### 3.4 项目冷启动 Pipeline 优化

**问题**：六机制组合，如何并行执行加速？

**算法**：DAG 调度

```python
async def bootstrap_pipeline(meta: ProjectMeta) -> ProjectContext:
    """冷启动 Pipeline"""
    # 创建 scope
    scope = await create_scope(meta)
    
    # 并行执行独立步骤
    parallel_tasks = [
        extract_from_readme(meta.readme),    # 步骤 1
        apply_template(meta.template),        # 步骤 2
    ]
    readme_entities, template_entities = await asyncio.gather(*parallel_tasks)
    
    # 串行依赖步骤
    await db.batch_insert(readme_entities + template_entities, scope)
    
    # 种子注入依赖前面的实体（用于相关性检索）
    seeds = await inject_seeds(meta.description, scope)
    
    # 异步触发主动学习（不阻塞）
    asyncio.create_task(trigger_active_learning(scope))
    
    return ProjectContext(scope=scope, stage="ready")
```

**性能优化**：
- **并行无依赖步骤**：README 提取 + 模板应用 并行
- **异步非关键步骤**：主动学习后台触发
- **批量写入**：所有实体一次事务提交

---

### 3.5 主动学习算法

**问题**：新项目首轮对话，Agent 该问什么？

**算法**：基于缺口的引导提问

```python
class ActiveLearner:
    """主动学习：基于已有知识的缺口提问"""
    
    QUESTION_TEMPLATES = {
        "goal": "项目的主要目标是什么？",
        "tech": "用了哪些核心技术？",
        "team": "团队结构是怎样的？",
        "timeline": "项目预期什么时候完成？",
        "risks": "目前最大的风险是什么？",
    }
    
    async def get_next_question(self, scope: str) -> Optional[str]:
        """基于知识缺口选择问题"""
        existing_predicates = await db.get_predicates(scope)
        
        for key, template in self.QUESTION_TEMPLATES.items():
            if key not in existing_predicates:
                return template
        return None  # 所有关键信息已收集
    
    async def record_answer(self, scope: str, question_key: str, answer: str):
        """记录用户回答，提取实体写入"""
        entities = await llm.extract(answer)
        async with db.transaction():
            await db.upsert_triple(
                subject=scope,
                predicate=f"project_{question_key}",
                object_value=answer,
                scope=scope,
            )
            for entity in entities:
                await db.upsert_entity(entity, scope)
```

---

### 3.6 跨项目克隆算法

**问题**：如何从相似项目克隆通用记忆而不复制具体内容？

**算法**：基于类型的过滤 + 通用性评分

```python
async def clone_from(src_scope: str, dst_scope: str, filter: CloneFilter):
    """跨项目克隆"""
    # 1. 获取源项目的所有实体
    src_entities = await db.get_entities(src_scope)
    
    # 2. 过滤通用实体（非项目专属）
    general_entities = [
        e for e in src_entities
        if e.entity_type in {"tech", "concept", "pattern"}
        and e.name not in filter.exclude_names
    ]
    
    # 3. 复制到目标 scope（重设 ID，保留 source_scope）
    for entity in general_entities:
        new_entity = entity.copy(
            id=str(ULID()),
            scope=dst_scope,
            source_scope=src_scope,
        )
        await db.insert_entity(new_entity)
    
    # 4. 复制相关三元组
    entity_map = {old_id: new_id for ...}
    src_triples = await db.get_triples_by_entities(src_scope, general_entities)
    for triple in src_triples:
        await db.insert_triple(triple.copy(
            id=str(ULID()),
            scope=dst_scope,
            subject_id=entity_map[triple.subject_id],
            ...
        ))
```

**通用性评分**：
- `tech`（技术）：克隆 ✓
- `concept`（概念）：克隆 ✓
- `pattern`（模式）：克隆 ✓
- `person`（人物）：默认不克隆（项目专属）
- `decision`（决策）：默认不克隆

---

## 4. 实施建议

### 4.1 MVP 范围

```
src/memory/scheduler/
├── bootstrap/
│   ├── onboarding.py        # ✅ 初始化向导
│   ├── templates.py         # ✅ 项目模板（web/cli/data/agent）
│   └── active_learner.py    # ✅ 主动学习
```

调度器与抢占 P1 实现。

### 4.2 关键风险

| 风险 | 缓解 |
|------|------|
| LLM 提取失败 | 重试 + 默认值兜底 |
| 模板不匹配 | 用户可手动选择 |
| 主动学习烦人 | 提供"跳过向导"选项 |
| 调度器死锁 | 任务超时 + 健康检查 |

### 4.3 验证方式

```python
async def test_bootstrap_completes_under_5min():
    """冷启动 < 5 分钟"""
    start = time.time()
    ctx = await bootstrap.initialize(name="test", description="...", tech_stack=["python"])
    assert time.time() - start < 300  # 5 分钟

async def test_bootstrap_exit_criteria():
    """退出标准达成"""
    ctx = await bootstrap.initialize(...)
    entities = await db.count_entities(ctx.scope)
    triples = await db.count_triples(ctx.scope)
    assert entities >= 50
    assert triples >= 200

async def test_preemption_graceful():
    """GRACEFUL 抢占可续传"""
    task = LongTask()
    asyncio.create_task(task.run())
    await asyncio.sleep(0.5)  # 让任务跑一会
    await scheduler.preempt(task.task_id, PreemptionPolicy.GRACEFUL)
    # 验证检查点已保存
    cp = await db.get_latest_checkpoint(task.task_id)
    assert cp is not None
    # 续传
    await scheduler.resume(task.task_id)
    assert task.completed
```

### 4.4 性能基线

| 操作 | 目标延迟 |
|------|---------|
| 项目初始化（含 LLM 提取）| < 30s |
| 模板应用 | < 1s |
| 种子注入（Top-50）| < 5s |
| 主动学习单轮提问 | < 100ms |
| 任务调度延迟（CRITICAL）| < 100ms |
| 任务调度延迟（IDLE）| < 60s |
| 检查点保存 | < 50ms |
| 任务续传恢复 | < 100ms |

---

## 5. 后续演进路径

| 阶段 | 增强点 |
|------|--------|
| MVP | 冷启动最小集（向导 + 模板 + 主动学习）|
| P1 | 调度器 + 抢占 + 批量导入（Git/Markdown）|
| P2 | 跨项目克隆 + 种子注入 |
| P3 | 统计驱动调度**[2026-09 降级: 原"学习型调度/ML 预测"否决]**
  — ML 预测破坏 checkpoint/抢占的确定性测试体系;改为队列深度/
  空闲检测/近期写入率的统计规则(现有自适应规则 + 冷却框架的延伸) |
