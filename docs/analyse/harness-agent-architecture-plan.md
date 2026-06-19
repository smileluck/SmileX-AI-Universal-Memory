# 通用 Harness Agent 工程架构计划

> 创建日期：2026-05-23
> 目标：构建一个通用 Harness Agent 工程，支持多模型、多场景，以 Web API 形式运行，兼容 Open WebUI / Lobe Chat 等前端

---

## 一、整体架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                        前端层（可选）                             │
│  Open WebUI │ Lobe Chat │ 自定义 Web UI │ CLI 客户端              │
└──────────────────────────┬──────────────────────────────────────┘
                           │ OpenAI Compatible API
                           │ (HTTP / SSE Stream)
┌──────────────────────────▼──────────────────────────────────────┐
│                     API Gateway 层                               │
│              FastAPI + OpenAI 兼容接口                            │
│    /v1/chat/completions │ /v1/models │ /v1/tools                │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                    Harness Engine 核心层                          │
│  ┌────────────┐ ┌────────────┐ ┌───────────┐ ┌──────────────┐ │
│  │ 任务规划器  │ │ 记忆管理器  │ │ 工具注册表 │ │ 安全沙箱     │ │
│  │ Planner    │ │ Memory     │ │ ToolReg   │ │ Sandbox      │ │
│  └────────────┘ └────────────┘ └───────────┘ └──────────────┘ │
│  ┌────────────┐ ┌────────────┐ ┌───────────┐ ┌──────────────┐ │
│  │ 状态持久化  │ │ 上下文管理  │ │ 钩子系统   │ │ 可观测性     │ │
│  │ State      │ │ Context    │ │ Hooks     │ │ Observe      │ │
│  └────────────┘ └────────────┘ └───────────┘ └──────────────┘ │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                    模型适配层（多模型支持）                        │
│  ┌────────┐ ┌─────────┐ ┌────────┐ ┌────────┐ ┌────────────┐ │
│  │ OpenAI │ │Anthropic│ │ Ollama │ │ DeepSeek│ │  自定义     │ │
│  └────────┘ └─────────┘ └────────┘ └────────┘ └────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

**核心思路**：Harness 后端对外暴露 **OpenAI 兼容 API**，这样 Open WebUI、Lobe Chat 以及任何支持 OpenAI API 的客户端都能直接对接。

---

## 二、技术选型

| 层次 | 技术选择 | 理由 |
|------|----------|------|
| **Web 框架** | FastAPI | 异步高性能，自动生成 API 文档，已有 FastAPI + SQLAlchemy 经验 |
| **模型调用** | LiteLLM | 统一接口调用 100+ 模型（OpenAI/Claude/Ollama/DeepSeek 等），一个接口切模型 |
| **向量存储** | ChromaDB | 轻量级，嵌入式，无需额外部署，适合入门；后续可切 Qdrant |
| **关系存储** | PostgreSQL + SQLAlchemy | 已有 SQLAlchemy 基础，存储会话/任务/日志 |
| **缓存** | Redis | 工作记忆（热数据）、会话状态、速率限制 |
| **任务队列** | Celery + Redis | 异步执行长任务、Agent 循环 |
| **沙箱** | Docker SDK for Python | 隔离代码执行环境 |
| **协议** | MCP（Model Context Protocol） | 工具调用的统一协议，2026 年事实标准 |
| **前端** | Open WebUI（推荐） | 开源、活跃、支持工具调用和 Agent 功能，Docker 一键部署 |

---

## 三、模块详细设计

### 模块 1：API Gateway（接口层）

**职责**：对外暴露 OpenAI 兼容接口，接收请求并路由到 Harness Engine

```
API 端点设计：

POST /v1/chat/completions      ← 核心入口（聊天 + 工具调用）
POST /v1/chat/completions      ← 支持 SSE 流式输出
GET  /v1/models                ← 列出可用模型
POST /v1/agent/run             ← Harness 专用：启动 Agent 任务
GET  /v1/agent/status/{id}     ← 查询 Agent 任务状态
POST /v1/agent/stop/{id}       ← 停止 Agent 任务
GET  /v1/memory/{session_id}   ← 查询会话记忆
GET  /v1/tools                 ← 列出可用工具
GET  /v1/observe/logs          ← 可观测性日志
```

**关键设计**：
- `/v1/chat/completions` 完全兼容 OpenAI 格式，Open WebUI / Lobe Chat 可直连
- `/v1/agent/*` 是 Harness 增强接口，前端自定义 UI 使用
- 所有接口支持 SSE（Server-Sent Events）流式响应

---

### 模块 2：Harness Engine（核心引擎）

**职责**：Agent 的"操作系统"，协调所有子系统

```
核心执行循环：

用户请求 → API Gateway
         → Harness Engine
            │
            ├── 1. 加载会话状态（State Manager）
            ├── 2. 加载记忆（Memory Manager）
            ├── 3. 构建上下文（Context Manager）
            ├── 4. 触发 Pre-Hooks（Hooks System）
            ├── 5. 调用 LLM（Model Adapter）
            ├── 6. 解析响应，判断是否需要工具调用
            │      ├── 需要工具 → 安全检查 → 沙箱执行 → 结果注入 → 回到步骤 3
            │      └── 不需要 → 触发 Post-Hooks → 返回结果
            ├── 7. 更新记忆（Memory Manager）
            ├── 8. 持久化状态（State Manager）
            └── 9. 记录日志（Observability）
```

---

### 模块 3：Memory Manager（记忆管理器）

**职责**：多层记忆的存储、检索、演化、遗忘

```
记忆架构：

┌─────────────────────────────────────────┐
│          Memory Manager                  │
│                                         │
│  ┌─────────────┐  ┌──────────────────┐  │
│  │ Working Mem  │  │ Short-term Mem   │  │
│  │ (Redis)      │  │ (滑动窗口)       │  │
│  │ 当前会话状态  │  │ 最近 N 轮对话    │  │
│  └─────────────┘  └──────────────────┘  │
│                                         │
│  ┌─────────────┐  ┌──────────────────┐  │
│  │ Long-term   │  │ Semantic Mem     │  │
│  │ (PostgreSQL) │  │ (ChromaDB)       │  │
│  │ 结构化事实    │  │ 语义向量检索     │  │
│  └─────────────┘  └──────────────────┘  │
│                                         │
│  记忆操作：                              │
│  · store(category, key, value)          │
│  · retrieve(query, top_k)               │
│  · update(key, value)                   │
│  · forget(key) / decay(threshold)       │
│  · build_context(session_id) → str      │
└─────────────────────────────────────────┘
```

**记忆生命周期**：

| 阶段 | 操作 | 存储位置 |
|------|------|----------|
| **写入** | 从对话中提取关键事实 | PostgreSQL + ChromaDB |
| **检索** | 语义搜索 + 关键词过滤 + 图推理 | 混合检索 |
| **演化** | 合并重复、更新过时、纠错 | PostgreSQL |
| **遗忘** | 时间衰减 + 低频清理 | 自动执行 |

---

### 模块 4：Tool Registry（工具注册表）

**职责**：统一管理 Agent 可调用的工具，支持 MCP 协议

```
工具架构：

┌─────────────────────────────────────────┐
│          Tool Registry                   │
│                                         │
│  内置工具：                              │
│  ├── bash_execute    → 执行命令          │
│  ├── file_read       → 读取文件          │
│  ├── file_write      → 写入文件          │
│  ├── http_request    → HTTP 请求         │
│  ├── code_execute    → 代码执行          │
│  └── web_search      → 网页搜索          │
│                                         │
│  MCP 工具（外部集成）：                   │
│  ├── mcp://filesystem  → 文件系统        │
│  ├── mcp://database    → 数据库          │
│  ├── mcp://git         → Git 操作        │
│  ├── mcp://browser     → 浏览器自动化     │
│  └── mcp://custom      → 自定义工具      │
│                                         │
│  工具操作：                              │
│  · register(tool_def)                   │
│  · execute(tool_name, params) → result  │
│  · list_tools() → [ToolDef]             │
│  · validate(tool_name, params) → bool   │
└─────────────────────────────────────────┘
```

**工具定义格式**（兼容 OpenAI Function Calling）：

```json
{
  "type": "function",
  "function": {
    "name": "bash_execute",
    "description": "在沙箱中执行 Bash 命令",
    "parameters": {
      "type": "object",
      "properties": {
        "command": {"type": "string", "description": "要执行的命令"},
        "timeout": {"type": "integer", "description": "超时秒数", "default": 30}
      },
      "required": ["command"]
    }
  }
}
```

---

### 模块 5：Sandbox（安全沙箱）

**职责**：隔离执行环境，防止 Agent 行为失控

```
沙箱设计：

┌─────────────────────────────────────────┐
│          Sandbox Manager                 │
│                                         │
│  执行策略：                              │
│  ├── Docker 容器隔离（推荐）             │
│  ├── 子进程 + 资源限制（轻量）           │
│  └── 远程执行（生产环境）                │
│                                         │
│  安全限制：                              │
│  ├── CPU / 内存 / 磁盘 配额             │
│  ├── 网络白名单                          │
│  ├── 文件系统只读挂载（按需读写）        │
│  ├── 执行超时自动终止                    │
│  └── 危险命令黑名单拦截                  │
│                                         │
│  沙箱操作：                              │
│  · create(config) → sandbox_id          │
│  · execute(sandbox_id, cmd) → result    │
│  · destroy(sandbox_id)                  │
│  · snapshot(sandbox_id) → checkpoint    │
│  · restore(checkpoint) → sandbox_id     │
└─────────────────────────────────────────┘
```

---

### 模块 6：Context Manager（上下文管理器）

**职责**：管理上下文窗口，防止"上下文腐化"

```
上下文管理策略：

┌─────────────────────────────────────────┐
│        Context Manager                   │
│                                         │
│  上下文组成（按优先级）：                 │
│  ┌─────────────────────────────────┐    │
│  │ 1. System Prompt（不可压缩）     │    │
│  │ 2. Harness 规则（不可压缩）      │    │
│  │ 3. 记忆注入（可压缩）            │    │
│  │ 4. 工具执行结果（可压缩）        │    │
│  │ 5. 对话历史（可压缩/淘汰）       │    │
│  └─────────────────────────────────┘    │
│                                         │
│  压缩策略：                              │
│  · 重要性评分 → 低分先淘汰               │
│  · 摘要压缩 → LLM 生成摘要替代原文       │
│  · 滑动窗口 → 保留最近 N 轮              │
│  · 递进式压缩 → 越早越简                 │
│                                         │
│  操作：                                  │
│  · build(session_id) → messages          │
│  · compact(session_id) → compressed      │
│  · estimate_tokens(messages) → int       │
└─────────────────────────────────────────┘
```

---

### 模块 7：Hooks System（钩子系统）

**职责**：在 Agent 执行的关键节点插入自定义逻辑

```
钩子触发点：

请求进入 → [UserPromptSubmit Hook]
           ↓
LLM 调用前 → [PreLLMCall Hook] → 可修改 prompt / 注入上下文
           ↓
工具调用前 → [PreToolUse Hook] → 可拦截危险操作
           ↓
工具执行 → 沙箱内执行
           ↓
工具调用后 → [PostToolUse Hook] → 可记录/校验结果
           ↓
LLM 响应后 → [PostLLMCall Hook] → 可校验/修正输出
           ↓
会话结束 → [SessionEnd Hook] → 可触发记忆持久化、日志记录

每个 Hook 可以：
· allow（放行）
· block（拦截，返回错误信息）
· modify（修改参数后放行）
```

---

### 模块 8：State Manager（状态持久化）

**职责**：跨会话的状态保存和恢复

```
状态存储设计（PostgreSQL）：

sessions 表：
├── session_id, user_id, title, status
├── created_at, updated_at
└── metadata (JSON)

tasks 表：
├── task_id, session_id, parent_task_id
├── description, status, priority
├── result, error_message
└── created_at, completed_at

messages 表：
├── message_id, session_id, role
├── content, tool_calls, tool_results
├── token_count, model_name
└── created_at

checkpoints 表：
├── checkpoint_id, session_id
├── state_snapshot (JSON)
├── git_commit_hash
└── created_at
```

---

### 模块 9：Planner（任务规划器）

**职责**：将复杂任务拆解为子任务，调度执行

```
规划策略（可选，按场景选择）：

1. ReAct 循环（默认）
   思考 → 行动 → 观察 → 思考 → ...

2. Plan-and-Execute
   规划所有步骤 → 逐步执行 → 遇错重新规划

3. Hierarchical（复杂场景）
   总规划器 → 子任务 → 子 Agent 执行 → 汇总

配置方式：
harness_config.yaml:
  planner:
    strategy: react          # react | plan_execute | hierarchical
    max_steps: 50
    max_retries: 3
    timeout_seconds: 300
```

---

### 模块 10：Observability（可观测性）

**职责**：全链路追踪、成本统计、行为审计

```
可观测性三支柱：

1. Logging（日志）
   ├── 每次工具调用记录
   ├── 每次模型调用记录
   ├── Token 消耗统计
   └── 异常/错误记录

2. Tracing（追踪）
   ├── 请求级 Trace ID
   ├── 每步 Span（开始时间/结束时间/状态）
   └── 调用链路可视化

3. Metrics（指标）
   ├── 请求量 / 成功率 / 延迟
   ├── Token 消耗 / 成本
   ├── 工具调用频次
   └── 错误率 / 重试率

存储：
├── 结构化日志 → PostgreSQL（已有 sys_operation_log 模式）
├── 追踪数据 → OpenTelemetry + Jaeger（可选）
└── 指标 → Prometheus（可选）
```

---

## 四、项目目录结构

```
harness-agent/
├── pyproject.toml                  # 项目配置（Poetry/PDM）
├── README.md
├── .env.example                    # 环境变量模板
├── docker-compose.yml              # 一键启动（PG + Redis + Chroma）
│
├── config/
│   ├── harness_config.yaml         # Harness 主配置
│   ├── models.yaml                 # 模型配置
│   ├── tools.yaml                  # 工具配置
│   └── safety.yaml                 # 安全策略配置
│
├── src/
│   ├── __init__.py
│   │
│   ├── api/                        # API Gateway 层
│   │   ├── __init__.py
│   │   ├── app.py                  # FastAPI 应用入口
│   │   ├── routes/
│   │   │   ├── chat.py             # /v1/chat/completions
│   │   │   ├── models.py           # /v1/models
│   │   │   ├── agent.py            # /v1/agent/*
│   │   │   ├── memory.py           # /v1/memory/*
│   │   │   ├── tools.py            # /v1/tools
│   │   │   └── observe.py          # /v1/observe/*
│   │   ├── middleware/
│   │   │   ├── auth.py             # 认证中间件
│   │   │   ├── rate_limit.py       # 限流
│   │   │   └── cors.py             # CORS
│   │   └── schemas/
│   │       ├── chat.py             # OpenAI 兼容的请求/响应模型
│   │       ├── agent.py
│   │       └── memory.py
│   │
│   ├── engine/                     # Harness Engine 核心层
│   │   ├── __init__.py
│   │   ├── harness.py              # Harness 主引擎（协调器）
│   │   ├── planner.py              # 任务规划器
│   │   ├── executor.py             # 执行循环
│   │   └── session.py              # 会话管理
│   │
│   ├── memory/                     # 记忆管理
│   │   ├── __init__.py
│   │   ├── manager.py              # 记忆管理器
│   │   ├── working.py              # 工作记忆（Redis）
│   │   ├── shortterm.py            # 短期记忆（滑动窗口）
│   │   ├── longterm.py             # 长期记忆（PostgreSQL）
│   │   ├── semantic.py             # 语义记忆（ChromaDB）
│   │   └── context.py              # 上下文构建与压缩
│   │
│   ├── tools/                      # 工具系统
│   │   ├── __init__.py
│   │   ├── registry.py             # 工具注册表
│   │   ├── base.py                 # 工具基类
│   │   ├── builtin/                # 内置工具
│   │   │   ├── bash.py
│   │   │   ├── file_ops.py
│   │   │   ├── http.py
│   │   │   ├── code.py
│   │   │   └── search.py
│   │   └── mcp/                    # MCP 协议集成
│   │       ├── client.py
│   │       └── adapter.py
│   │
│   ├── sandbox/                    # 安全沙箱
│   │   ├── __init__.py
│   │   ├── base.py                 # 沙箱基类
│   │   ├── docker_sandbox.py       # Docker 沙箱
│   │   ├── process_sandbox.py      # 子进程沙箱（轻量）
│   │   └── policy.py               # 安全策略引擎
│   │
│   ├── hooks/                      # 钩子系统
│   │   ├── __init__.py
│   │   ├── manager.py              # 钩子管理器
│   │   ├── base.py                 # 钩子基类
│   │   └── builtin/                # 内置钩子
│   │       ├── safety_check.py
│   │       ├── logging.py
│   │       └── rate_limit.py
│   │
│   ├── models/                     # 模型适配层
│   │   ├── __init__.py
│   │   ├── adapter.py              # 统一适配器（基于 LiteLLM）
│   │   ├── config.py               # 模型配置管理
│   │   └── router.py               # 模型路由（按任务选模型）
│   │
│   ├── state/                      # 状态持久化
│   │   ├── __init__.py
│   │   ├── manager.py              # 状态管理器
│   │   ├── database.py             # 数据库操作
│   │   └── checkpoint.py           # 检查点机制
│   │
│   └── observe/                    # 可观测性
│       ├── __init__.py
│       ├── logger.py               # 结构化日志
│       ├── tracer.py               # 链路追踪
│       └── metrics.py              # 指标收集
│
├── db/                             # 数据库
│   ├── migrations/                 # Alembic 迁移
│   └── seed/                       # 初始数据
│
├── tests/                          # 测试
│   ├── unit/
│   ├── integration/
│   └── e2e/
│
└── scripts/                        # 脚本
    ├── init_db.py                  # 初始化数据库
    ├── run_dev.py                  # 开发启动
    └── run_worker.py               # Celery Worker
```

---

## 五、核心配置文件设计

```yaml
# config/harness_config.yaml — Harness 主配置

# 服务配置
server:
  host: "0.0.0.0"
  port: 8000
  workers: 4
  cors_origins: ["http://localhost:3000", "http://localhost:8080"]

# 模型配置
models:
  default: "gpt-4o"
  # 按任务类型路由模型
  routing:
    coding: "claude-sonnet-4-20250514"
    analysis: "gpt-4o"
    simple: "deepseek-chat"
    local: "ollama://llama3"
  # 模型参数
  params:
    temperature: 0.7
    max_tokens: 4096
    top_p: 1.0

# 记忆配置
memory:
  working:
    backend: "redis"
    ttl: 3600                    # 1 小时过期
  short_term:
    max_rounds: 20               # 保留最近 20 轮
  long_term:
    backend: "postgresql"
    decay_days: 30               # 30 天后衰减
  semantic:
    backend: "chromadb"
    embedding_model: "text-embedding-3-small"
    top_k: 5

# 工具配置
tools:
  builtin:
    - name: "bash_execute"
      sandbox: true
      timeout: 30
    - name: "file_read"
      sandbox: false
    - name: "http_request"
      allowed_domains: ["*"]
      timeout: 15
  mcp:
    servers:
      - name: "filesystem"
        command: "mcp-server-filesystem"
        args: ["--root", "/workspace"]

# 安全配置
safety:
  sandbox:
    type: "docker"               # docker | process | none
    image: "harness-sandbox:latest"
    cpu_limit: "1.0"
    memory_limit: "512m"
    network: "none"              # 默认禁止网络
  hooks:
    pre_tool_use:
      - type: "command_blacklist"
        blocked: ["rm -rf /", "format C:", "DROP TABLE", "> /dev/sda"]
      - type: "resource_check"
        max_file_size: "10MB"
    post_tool_use:
      - type: "result_logging"
    session_end:
      - type: "memory_consolidate"

# 规划器配置
planner:
  strategy: "react"              # react | plan_execute | hierarchical
  max_steps: 50
  max_retries: 3
  retry_delay: 2
  timeout_seconds: 300

# 上下文配置
context:
  max_tokens: 128000
  compression_threshold: 0.8     # 使用量超 80% 触发压缩
  compression_strategy: "summary" # summary | sliding_window | hybrid

# 可观测性
observe:
  log_level: "INFO"
  trace_enabled: true
  metrics_enabled: true
  cost_tracking: true
```

---

## 六、与前端集成方案

### 方案 A：Open WebUI（推荐 ⭐）

```
集成方式：OpenAI Compatible API

Open WebUI  ←→  Harness Agent API (localhost:8000)
                  ├── 设置 → 外部连接 → OpenAI API
                  ├── API Base URL: http://localhost:8000/v1
                  ├── API Key: your-key
                  └── 自动发现所有模型

优势：
✅ 零改动集成，Docker 一键部署
✅ 支持工具调用展示
✅ 支持多用户、多会话
✅ 内置文件上传、代码高亮
✅ 社区活跃，插件丰富
```

### 方案 B：Lobe Chat

```
集成方式：OpenAI Compatible API + 自定义插件

Lobe Chat  ←→  Harness Agent API (localhost:8000)
                  ├── 设置 → 语言模型 → OpenAI
                  ├── API Proxy: http://localhost:8000/v1
                  └── 自定义工具插件 → 调用 Harness 工具 API

优势：
✅ UI 精美，插件生态好
✅ 支持 TTS、图片生成等多模态
⚠️ 工具调用需要自定义插件适配
```

### 方案 C：自定义前端（后期考虑）

```
集成方式：直接调用 Harness API

自定义 UI  ←→  Harness Agent API
                  ├── /v1/chat/completions  → 聊天
                  ├── /v1/agent/*           → Agent 控制
                  ├── /v1/memory/*          → 记忆管理
                  └── /v1/observe/*         → 监控面板
```

---

## 七、开发路线图

### Phase 1：最小可用（2-3 周）

```
目标：跑通 LLM 调用 + OpenAI 兼容 API

· FastAPI 项目骨架 + OpenAI 兼容接口
· LiteLLM 集成（多模型支持）
· 简单对话（无记忆、无工具）
· SSE 流式输出
· Open WebUI 能连上并正常对话

交付物：能通过 Open WebUI 和多模型对话的 API

验证：能用 Open WebUI 通过你的 API 和不同模型对话吗？流式输出正常吗？
```

### Phase 2：工具系统（2-3 周）

```
目标：Agent 能调用工具执行任务

· Tool Registry（工具注册表）
· 内置工具：bash、file、http
· OpenAI Function Calling 格式兼容
· 基础安全检查（命令黑名单）
· ReAct 执行循环

交付物：Agent 能执行"帮我查看当前目录"等指令

验证：Agent 能执行"列出当前目录文件"并返回结果吗？能拦截 rm -rf 吗？
```

### Phase 3：记忆系统（3-4 周）

```
目标：Agent 能跨会话记忆

· Working Memory（Redis）
· Short-term Memory（滑动窗口）
· Long-term Memory（PostgreSQL）
· Semantic Memory（ChromaDB）
· 记忆生命周期（写入/检索/衰减/遗忘）
· 上下文压缩

交付物：Agent 记住用户偏好，跨会话保持上下文

验证：关掉浏览器重新打开，Agent 还记得你之前说过"我喜欢 Python"吗？
```

### Phase 4：安全沙箱（2-3 周）

```
目标：Agent 安全执行代码

· Docker 沙箱实现
· 资源限制（CPU/内存/磁盘/网络）
· Hooks 系统（PreToolUse / PostToolUse）
· 安全策略引擎
· Checkpoint 机制

交付物：Agent 在隔离环境中安全执行代码

验证：Agent 执行恶意代码时，宿主机是否不受影响？
```

### Phase 5：高级特性（4-6 周）

```
目标：生产级能力

· 多 Agent 协作（Hierarchical Planner）
· 任务持久化与断点恢复
· MCP 协议支持
· 可观测性（日志/追踪/指标）
· 异步任务队列（Celery）
· 成本统计与限流

交付物：可用于生产环境的 Harness Agent 平台

验证：Agent 能否在长任务中断后从断点恢复？能否看到完整的执行追踪？
```

---

## 八、关键技术决策说明

| 决策点 | 选择 | 理由 |
|--------|------|------|
| **为什么选 LiteLLM 不自己封装？** | LiteLLM 已统一 100+ 模型的接口，自己封装维护成本高，且 LiteLLM 支持流式、工具调用、重试等 |
| **为什么选 OpenAI 兼容 API？** | 这是事实标准，Open WebUI / Lobe Chat / 任何 AI 客户端都支持，零适配成本 |
| **为什么 ChromaDB 不选 Qdrant？** | ChromaDB 嵌入式无需部署，适合入门学习；接口兼容，后续可无缝切换 Qdrant |
| **为什么 FastAPI 不选 Flask？** | 原生异步、自动 API 文档、类型校验，且已有 FastAPI + SQLAlchemy 基础 |
| **为什么 Docker 沙箱不选 gVisor？** | Docker SDK for Python 更易上手，gVisor 适合生产环境但 Windows 支持有限 |
| **为什么推荐 Open WebUI？** | 最活跃的开源 AI 前端，Docker 一键部署，原生支持 OpenAI API + 工具调用 |

---

## 九、核心原则

> 💡 **每个 Phase 都是独立可用的**——Phase 1 完成后你就有了一个能用的多模型聊天 API，Phase 2 完成后你就有了一个能执行任务的 Agent。**不要试图一步到位，增量构建。**

1. **增量构建**：每个阶段独立可用，逐步叠加能力
2. **接口优先**：OpenAI 兼容 API 确保生态兼容
3. **插件化**：工具、记忆、沙箱都是可插拔的
4. **安全默认**：沙箱隔离、钩子拦截、策略引擎
5. **可观测**：全链路追踪，每次 Agent 行为可审计

---
---

# ═══════════════════════════════════════════════════════════════
# Architecture 2.0 — 基于 Deep Agents 设计思路的升级版
# ═══════════════════════════════════════════════════════════════

> 创建日期：2026-05-23
> 基于：1.0 架构 + LangChain Deep Agents 设计理念
> 核心变化：从"模块堆叠"升级为"中间件驱动的 Harness OS"

---

## 一、2.0 架构升级概览

### 1.0 → 2.0 核心变化

```
1.0 架构（模块堆叠式）：
  Harness Engine → 调用各个模块（Planner, Memory, Tools, Sandbox...）
  问题：模块之间耦合较重，扩展需要修改核心引擎

2.0 架构（中间件驱动式）：
  Agent Core（纯执行循环）
    ├── Middleware 1: TodoListMiddleware     ← 注入规划能力
    ├── Middleware 2: FilesystemMiddleware   ← 注入文件系统
    ├── Middleware 3: SubAgentMiddleware     ← 注入子代理
    ├── Middleware 4: SummarizationMiddleware ← 注入上下文压缩
    ├── Middleware 5: MemoryMiddleware       ← 注入记忆系统
    ├── Middleware 6: SandboxMiddleware      ← 注入安全沙箱
    ├── Middleware 7: ObservabilityMiddleware ← 注入可观测性
    └── [自定义 Middleware]                  ← 你的业务逻辑
  优势：核心极简，所有能力通过中间件注入，热插拔
```

### 2.0 整体架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                          前端层（可选）                               │
│  Open WebUI │ Lobe Chat │ 自定义 Web UI │ CLI 客户端                  │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ OpenAI Compatible API (HTTP / SSE Stream)
┌──────────────────────────▼──────────────────────────────────────────┐
│                       API Gateway 层                                 │
│              FastAPI + OpenAI 兼容接口                                │
│    /v1/chat/completions │ /v1/models │ /v1/agent │ /v1/tools        │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────────┐
│                   Harness Engine 2.0 核心层                          │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │                    Agent Core（纯执行循环）                     │  │
│  │    LLM Call → Parse Response → Route (Tool/Finish/Error)      │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                              │                                      │
│              ┌───────────────┼───────────────┐                      │
│              ▼               ▼               ▼                      │
│  ┌─────────────────┐ ┌──────────────┐ ┌──────────────┐             │
│  │ Pre-Middlewares  │ │  Tool Router │ │Post-Middlewares│             │
│  │ (请求预处理)     │ │  (工具路由)   │ │ (响应后处理)   │             │
│  │                 │ │              │ │              │               │
│  │ · TodoList      │ │ · Builtin    │ │ · Summarize  │               │
│  │ · Memory Load   │ │ · MCP        │ │ · Memory Save│               │
│  │ · Context Build │ │ · SubAgent   │ │ · Observe    │               │
│  │ · Safety Check  │ │ · Sandbox    │ │ · Hooks      │               │
│  │ · Custom...     │ │ · Custom...  │ │ · Custom...  │               │
│  └─────────────────┘ └──────────────┘ └──────────────┘             │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │                  Backend Protocol（可插拔后端）                  │  │
│  │                                                               │  │
│  │  StateBackend          MemoryBackend         SandboxBackend   │  │
│  │  ├── PostgreSQL        ├── Redis (Working)   ├── Docker       │  │
│  │  ├── SQLite            ├── PG (Long-term)    ├── Process      │  │
│  │  ├── Redis             ├── ChromaDB (Semantic)│  └── Remote    │  │
│  │  └── File-based        └── File-based         │                │  │
│  │                                               │                │  │
│  │  CompositeBackend: 多后端组合（如 PG + S3 混合）                │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │                   Model Adapter（模型适配层）                    │  │
│  │  LiteLLM → OpenAI / Claude / Ollama / DeepSeek / 自定义         │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 二、2.0 新增核心概念

### 概念 1：Middleware Pipeline（中间件管道）

**借鉴自 Deep Agents 的核心设计模式**，将所有能力模块化为中间件：

```python
class Middleware(ABC):
    """中间件基类，所有能力模块的统一接口"""

    @abstractmethod
    async def on_request(self, context: AgentContext) -> AgentContext:
        """请求预处理：在 LLM 调用前执行"""
        ...

    @abstractmethod
    async def on_response(self, context: AgentContext) -> AgentContext:
        """响应后处理：在 LLM 响应后执行"""
        ...

    @abstractmethod
    async def on_tool_call(self, tool_call: ToolCall, context: AgentContext) -> ToolResult:
        """工具调用拦截：可修改/拦截工具调用"""
        ...
```

**中间件执行流程**：

```
用户请求
  │
  ▼
[Pre-Middleware Pipeline]（顺序执行）
  ├── TodoListMiddleware.on_request()      → 注入任务列表到上下文
  ├── MemoryMiddleware.on_request()        → 加载相关记忆
  ├── ContextMiddleware.on_request()       → 构建上下文窗口
  ├── SafetyMiddleware.on_request()        → 安全预检
  └── [Custom].on_request()
  │
  ▼
LLM 调用（Model Adapter）
  │
  ▼
解析响应 → 是否调用工具？
  │
  ├── YES → [Tool Middleware Pipeline]
  │         ├── SafetyMiddleware.on_tool_call()  → 危险操作拦截
  │         ├── SandboxMiddleware.on_tool_call() → 沙箱执行
  │         ├── FilesystemMiddleware.on_tool_call() → 文件操作
  │         ├── SubAgentMiddleware.on_tool_call() → 子代理委托
  │         └── [Custom].on_tool_call()
  │         │
  │         └── 结果注入 → 回到 LLM 调用
  │
  └── NO → [Post-Middleware Pipeline]（逆序执行）
            ├── ObservabilityMiddleware.on_response() → 记录日志/追踪
            ├── MemoryMiddleware.on_response()         → 保存新记忆
            ├── SummarizationMiddleware.on_response()  → 压缩上下文
            ├── TodoListMiddleware.on_response()        → 更新任务状态
            └── [Custom].on_response()
            │
            ▼
          返回结果给用户
```

---

### 概念 2：Backend Protocol（可插拔后端协议）

**借鉴自 Deep Agents 0.2 的 Pluggable Backend 机制**：

```python
class StateBackend(Protocol):
    """状态后端协议 - 可插拔的状态存储"""

    async def save_state(self, session_id: str, state: dict) -> None: ...
    async def load_state(self, session_id: str) -> dict: ...
    async def delete_state(self, session_id: str) -> None: ...
    async def list_sessions(self, user_id: str) -> list[str]: ...

class MemoryBackend(Protocol):
    """记忆后端协议 - 可插拔的记忆存储"""

    async def store(self, category: str, key: str, value: Any) -> None: ...
    async def retrieve(self, query: str, top_k: int = 5) -> list[MemoryItem]: ...
    async def forget(self, key: str) -> None: ...
    async def decay(self, threshold: float = 0.3) -> int: ...

class SandboxBackend(Protocol):
    """沙箱后端协议 - 可插拔的执行环境"""

    async def create(self, config: SandboxConfig) -> str: ...
    async def execute(self, sandbox_id: str, command: str) -> ExecutionResult: ...
    async def destroy(self, sandbox_id: str) -> None: ...
    async def snapshot(self, sandbox_id: str) -> str: ...

class CompositeBackend:
    """复合后端 - 多后端组合（借鉴 Deep Agents 0.2）"""

    def __init__(self, base: StateBackend, mounts: dict[str, StateBackend]):
        """
        示例：
          base = PostgreSQLBackend()
          mounts = {
              "/memories/": S3Backend(bucket="agent-memories"),
              "/cache/":    RedisBackend(),
          }
        """
        ...
```

---

### 概念 3：Skills（能力包）

**借鉴自 Deep Agents 的 Skills 机制**，将领域知识封装为可复用的能力包：

```python
class Skill(ABC):
    """能力包基类"""

    name: str                    # 技能名称
    description: str             # 技能描述
    tools: list[Tool]            # 该技能提供的工具
    system_prompt: str           # 该技能的 System Prompt 片段
    middlewares: list[Middleware] # 该技能需要的中间件

    def activate(self, harness: "HarnessEngine"):
        """激活技能：注册工具、中间件、提示词"""
        ...

    def deactivate(self, harness: "HarnessEngine"):
        """停用技能：清理注册"""
        ...
```

**内置 Skills 示例**：

```
skills/
├── coding/                  # 编程技能包
│   ├── __init__.py
│   ├── skill.py             # 定义工具 + Prompt
│   └── prompts/             # 代码生成相关的提示词模板
│
├── research/                # 研究调研技能包
│   ├── __init__.py
│   ├── skill.py             # 搜索 + 总结 + 引用工具
│   └── prompts/
│
├── data_analysis/           # 数据分析技能包
│   ├── __init__.py
│   ├── skill.py             # Python 执行 + 可视化 + 统计
│   └── prompts/
│
└── project_management/      # 项目管理技能包
    ├── __init__.py
    ├── skill.py             # 任务拆解 + 进度跟踪 + 报告
    └── prompts/
```

---

### 概念 4：Hanging Tool Call Recovery（悬挂调用恢复）

**借鉴自 Deep Agents 0.2 的新特性**：

```python
class HangingCallRecovery:
    """
    当工具调用被中断（超时/崩溃/用户取消）时，
    自动修复消息历史，保持执行流一致性。
    """

    async def check_and_repair(self, messages: list[Message]) -> list[Message]:
        """
        检测并修复悬挂的工具调用：
        1. 扫描消息列表中未配对的 tool_call
        2. 为悬挂的调用生成 "被中断" 的工具结果
        3. 注入修复后的消息历史
        """
        ...
```

---

### 概念 5：Large Result Offloading（大型结果转储）

**借鉴自 Deep Agents 0.2 的结果回收机制**：

```python
class ResultOffloading:
    """
    当工具调用结果超过指定 Token 限制时，
    自动转储到文件系统（Backend），防止上下文溢出。
    """

    def __init__(self, max_tokens: int = 4000, backend: FilesystemBackend = None):
        self.max_tokens = max_tokens
        self.backend = backend

    async def maybe_offload(self, result: ToolResult) -> ToolResult:
        """
        如果结果超限：
        1. 将完整结果写入文件系统
        2. 返回摘要 + 文件路径替代原始结果
        """
        ...
```

---

## 三、2.0 项目目录结构（更新）

```
harness-agent/
├── pyproject.toml
├── README.md
├── .env.example
├── docker-compose.yml
│
├── config/
│   ├── harness_config.yaml          # 主配置（支持 2.0 中间件配置）
│   ├── models.yaml
│   ├── tools.yaml
│   ├── safety.yaml
│   └── skills.yaml                  # 🆕 技能包配置
│
├── src/
│   ├── __init__.py
│   │
│   ├── api/                          # API Gateway 层（不变）
│   │   ├── __init__.py
│   │   ├── app.py
│   │   ├── routes/
│   │   │   ├── chat.py
│   │   │   ├── models.py
│   │   │   ├── agent.py
│   │   │   ├── memory.py
│   │   │   ├── tools.py
│   │   │   ├── skills.py             # 🆕 技能管理 API
│   │   │   └── observe.py
│   │   ├── middleware/
│   │   │   ├── auth.py
│   │   │   ├── rate_limit.py
│   │   │   └── cors.py
│   │   └── schemas/
│   │       ├── chat.py
│   │       ├── agent.py
│   │       └── memory.py
│   │
│   ├── core/                         # 🔄 重构：Agent Core 核心
│   │   ├── __init__.py
│   │   ├── agent.py                  # Agent 执行循环（极简核心）
│   │   ├── context.py                # AgentContext 定义
│   │   ├── pipeline.py               # 中间件管道执行器
│   │   └── session.py                # 会话管理
│   │
│   ├── middlewares/                  # 🆕 中间件层（替代原 engine/ 模块）
│   │   ├── __init__.py
│   │   ├── base.py                   # Middleware 基类
│   │   ├── registry.py               # 中间件注册表
│   │   │
│   │   ├── planning/                 # 规划中间件
│   │   │   ├── __init__.py
│   │   │   ├── todo_list.py          # TodoListMiddleware
│   │   │   ├── plan_execute.py       # PlanAndExecuteMiddleware
│   │   │   └── hierarchical.py       # HierarchicalMiddleware
│   │   │
│   │   ├── memory/                   # 记忆中间件
│   │   │   ├── __init__.py
│   │   │   ├── middleware.py          # MemoryMiddleware
│   │   │   └── context_builder.py     # 上下文构建
│   │   │
│   │   ├── filesystem/              # 文件系统中间件
│   │   │   ├── __init__.py
│   │   │   ├── middleware.py          # FilesystemMiddleware
│   │   │   └── result_offload.py     # 🆕 大型结果转储
│   │   │
│   │   ├── subagent/                # 子代理中间件
│   │   │   ├── __init__.py
│   │   │   ├── middleware.py          # SubAgentMiddleware
│   │   │   └── pool.py               # 子代理池管理
│   │   │
│   │   ├── summarization/           # 压缩中间件
│   │   │   ├── __init__.py
│   │   │   ├── middleware.py          # SummarizationMiddleware
│   │   │   └── strategies.py         # 压缩策略（summary/window/hybrid）
│   │   │
│   │   ├── safety/                  # 安全中间件
│   │   │   ├── __init__.py
│   │   │   ├── middleware.py          # SafetyMiddleware
│   │   │   ├── sandbox.py            # SandboxMiddleware
│   │   │   └── hanging_recovery.py   # 🆕 悬挂调用恢复
│   │   │
│   │   └── observe/                 # 可观测性中间件
│   │       ├── __init__.py
│   │       ├── middleware.py          # ObservabilityMiddleware
│   │       ├── tracer.py
│   │       └── metrics.py
│   │
│   ├── backends/                    # 🆕 可插拔后端层
│   │   ├── __init__.py
│   │   ├── protocols.py             # Backend Protocol 定义
│   │   ├── composite.py             # 复合后端
│   │   │
│   │   ├── state/                   # 状态后端实现
│   │   │   ├── __init__.py
│   │   │   ├── postgresql.py
│   │   │   ├── sqlite.py
│   │   │   ├── redis.py
│   │   │   └── file.py
│   │   │
│   │   ├── memory/                  # 记忆后端实现
│   │   │   ├── __init__.py
│   │   │   ├── redis_working.py
│   │   │   ├── postgres_longterm.py
│   │   │   ├── chroma_semantic.py
│   │   │   └── file.py
│   │   │
│   │   └── sandbox/                 # 沙箱后端实现
│   │       ├── __init__.py
│   │       ├── docker.py
│   │       ├── process.py
│   │       └── remote.py
│   │
│   ├── tools/                       # 工具系统（不变）
│   │   ├── __init__.py
│   │   ├── registry.py
│   │   ├── base.py
│   │   ├── builtin/
│   │   │   ├── bash.py
│   │   │   ├── file_ops.py
│   │   │   ├── http.py
│   │   │   ├── code.py
│   │   │   └── search.py
│   │   └── mcp/
│   │       ├── client.py
│   │       └── adapter.py
│   │
│   ├── skills/                      # 🆕 技能包层
│   │   ├── __init__.py
│   │   ├── base.py                  # Skill 基类
│   │   ├── registry.py              # 技能注册表
│   │   ├── loader.py                # 技能加载器（动态加载）
│   │   │
│   │   ├── builtin/                 # 内置技能包
│   │   │   ├── coding/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── skill.py
│   │   │   │   └── prompts/
│   │   │   ├── research/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── skill.py
│   │   │   │   └── prompts/
│   │   │   ├── data_analysis/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── skill.py
│   │   │   │   └── prompts/
│   │   │   └── project_management/
│   │   │       ├── __init__.py
│   │   │       ├── skill.py
│   │   │       └── prompts/
│   │   │
│   │   └── community/               # 🆕 社区技能包（用户贡献）
│   │       └── .gitkeep
│   │
│   ├── models/                      # 模型适配层（不变）
│   │   ├── __init__.py
│   │   ├── adapter.py
│   │   ├── config.py
│   │   └── router.py
│   │
│   └── utils/                       # 🆕 工具集
│       ├── __init__.py
│       ├── token_counter.py         # Token 计数
│       ├── retry.py                 # 重试策略
│       └── serialization.py         # 序列化工具
│
├── db/
│   ├── migrations/
│   └── seed/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
│
└── scripts/
    ├── init_db.py
    ├── run_dev.py
    └── run_worker.py
```

---

## 四、2.0 核心配置文件（更新）

```yaml
# config/harness_config.yaml — Harness 2.0 主配置

# 服务配置（不变）
server:
  host: "0.0.0.0"
  port: 8000
  workers: 4
  cors_origins: ["http://localhost:3000", "http://localhost:8080"]

# 模型配置（不变）
models:
  default: "gpt-4o"
  routing:
    coding: "claude-sonnet-4-20250514"
    analysis: "gpt-4o"
    simple: "deepseek-chat"
    local: "ollama://llama3"
  params:
    temperature: 0.7
    max_tokens: 4096
    top_p: 1.0

# 🆕 中间件配置（2.0 核心）
middlewares:
  # 中间件管道顺序（按执行顺序排列）
  pipeline:
    pre:
      - todo_list          # 任务规划
      - memory_load        # 记忆加载
      - context_build      # 上下文构建
      - safety_check       # 安全检查
    tool:
      - safety_guard       # 工具安全守卫
      - sandbox_execute    # 沙箱执行
      - filesystem         # 文件系统操作
      - subagent_delegate  # 子代理委托
    post:
      - observe            # 可观测性记录
      - memory_save        # 记忆保存
      - summarize          # 上下文压缩
      - todo_update        # 任务状态更新

  # 各中间件的详细配置
  todo_list:
    enabled: true
    max_tasks: 30
    auto_plan: true                # 自动拆解复杂任务
    allow_replan: true             # 允许重新规划

  memory:
    enabled: true
    auto_save: true                # 自动提取并保存记忆
    importance_threshold: 0.5      # 记忆重要性阈值

  context:
    enabled: true
    max_tokens: 128000
    compression_threshold: 0.8
    strategy: "hybrid"            # summary | sliding_window | hybrid

  summarization:
    enabled: true
    trigger_on_tokens: 100000      # Token 超过此值触发压缩
    keep_recent_rounds: 5          # 始终保留最近 5 轮

  filesystem:
    enabled: true
    max_file_size: "10MB"
    result_offload_tokens: 4000    # 🆕 工具结果超过此 Token 数时转储

  subagent:
    enabled: true
    max_concurrent: 3              # 最大并发子代理数
    timeout_seconds: 120           # 子代理超时

  safety:
    enabled: true
    command_blacklist: ["rm -rf /", "format C:", "DROP TABLE", "> /dev/sda"]
    require_confirmation: ["bash_execute", "file_write"]  # 需要用户确认的操作

  sandbox:
    enabled: true
    backend: "docker"              # docker | process | none
    image: "harness-sandbox:latest"
    cpu_limit: "1.0"
    memory_limit: "512m"
    network: "none"

  observe:
    enabled: true
    log_level: "INFO"
    trace_enabled: true
    cost_tracking: true

  hanging_recovery:
    enabled: true                  # 🆕 悬挂调用自动恢复

# 🆕 可插拔后端配置
backends:
  state:
    type: "postgresql"             # postgresql | sqlite | redis | file
    config:
      url: "postgresql://..."
      pool_size: 10

  memory:
    working:
      type: "redis"
      config:
        url: "redis://localhost:6379/0"
        ttl: 3600
    long_term:
      type: "postgresql"
      config:
        url: "postgresql://..."
        decay_days: 30
    semantic:
      type: "chromadb"
      config:
        embedding_model: "text-embedding-3-small"
        top_k: 5

  sandbox:
    type: "docker"
    config:
      image: "harness-sandbox:latest"

# 🆕 技能包配置
skills:
  auto_discover: true              # 自动发现 skills/ 目录下的技能包
  enabled:
    - coding
    - research
    - data_analysis
    - project_management
  custom_paths: []                 # 自定义技能包路径

# MCP 配置（不变）
mcp:
  servers:
    - name: "filesystem"
      command: "mcp-server-filesystem"
      args: ["--root", "/workspace"]
```

---

## 五、2.0 开发路线图（更新）

### Phase 1：极简核心 + API 兼容（2-3 周）

```
目标：跑通 Agent Core + OpenAI 兼容 API

· Agent Core 极简执行循环
· LiteLLM 集成（多模型支持）
· SSE 流式输出
· OpenAI Compatible API
· 基础中间件管道框架（空管道）
· Open WebUI 连接验证

交付物：能通过 Open WebUI 和多模型对话的 API

与 1.0 差异：核心循环更精简，预留中间件挂载点
```

### Phase 2：中间件框架 + 规划能力（2-3 周）

```
目标：建立中间件管道 + Agent 能规划任务

· Middleware Pipeline 执行器
· Middleware 基类 + 注册表
· TodoListMiddleware（任务拆解 + 进度跟踪）
· ContextMiddleware（上下文构建 + Token 计数）
· SafetyMiddleware（命令黑名单 + 预检）

交付物：Agent 能自动拆解复杂任务并跟踪进度

与 1.0 差异：规划能力通过中间件注入，而非独立 Planner 模块
```

### Phase 3：记忆 + 文件系统中间件（3-4 周）

```
目标：Agent 具备记忆和文件操作能力

· Backend Protocol 定义 + 多后端实现
· MemoryMiddleware（工作/短期/长期/语义记忆）
· FilesystemMiddleware（文件读写编辑）
· SummarizationMiddleware（上下文压缩）
· ResultOffloading（大型结果自动转储）
· CompositeBackend（多后端组合）

交付物：Agent 能跨会话记忆，能管理文件，能自动压缩上下文

与 1.0 差异：所有存储都是可插拔后端，记忆通过中间件注入
```

### Phase 4：安全沙箱 + 子代理（2-3 周）

```
目标：Agent 安全执行 + 多代理协作

· SandboxMiddleware（Docker/Process/Remote 后端）
· SubAgentMiddleware（子代理创建 + 并行执行）
· HangingCallRecovery（悬挂调用恢复）
· 安全策略引擎
· Checkpoint 机制

交付物：Agent 在隔离环境安全执行，复杂任务可委托子代理

与 1.0 差异：沙箱是后端协议，子代理是中间件能力
```

### Phase 5：技能包 + 可观测性（3-4 周）

```
目标：可复用技能包 + 生产级监控

· Skill 基类 + 注册表 + 动态加载器
· 内置技能包（coding/research/data_analysis/project_management）
· ObservabilityMiddleware（日志/追踪/指标/成本）
· MCP 协议集成
· 异步任务队列（Celery）
· 社区技能包支持

交付物：完整的生产级 Harness Agent 平台

与 1.0 差异：能力封装为可复用技能包，支持社区扩展
```

---

## 六、与 Deep Agents 的对比和相关性分析

### 6.1 架构定位对比

```
┌───────────────────────────────────────────────────────────────┐
│                  LangChain 生态 vs 独立生态                     │
│                                                               │
│  LangChain 生态：                你的 Harness 2.0：             │
│  ┌─────────────────────┐       ┌─────────────────────┐        │
│  │ DeepAgents (Harness) │  ←→   │ Harness Engine 2.0  │        │
│  ├─────────────────────┤       │ (独立 Harness)       │        │
│  │ LangGraph (Runtime)  │  ←→   │ Agent Core + Pipeline│        │
│  ├─────────────────────┤       │ (自研 Runtime)       │        │
│  │ LangChain (DSL)      │  ←→   │ LiteLLM + FastAPI    │        │
│  └─────────────────────┘       │ (开放生态替代)        │        │
│                                └─────────────────────┘        │
│                                                               │
│  本质关系：两者在同一抽象层（Harness 层），                     │
│  但技术栈和设计哲学不同                                        │
└───────────────────────────────────────────────────────────────┘
```

### 6.2 模块级映射关系

| 你的 Harness 2.0 模块 | Deep Agents 对应模块 | 相关度 | 说明 |
|---|---|---|---|
| **Agent Core** | LangGraph StateGraph（编译后） | 🔵 高 | 都是 Agent 执行循环的核心，但你的更轻量 |
| **Middleware Pipeline** | Deep Agents Middleware 机制 | 🟢 完全对应 | 直接借鉴的设计模式 |
| **TodoListMiddleware** | TodoListMiddleware | 🟢 完全对应 | 功能一致，实现独立 |
| **FilesystemMiddleware** | FilesystemMiddleware | 🟢 完全对应 | 功能一致，你的后端更灵活 |
| **SubAgentMiddleware** | SubAgentMiddleware | 🟢 完全对应 | 功能一致 |
| **SummarizationMiddleware** | SummarizationMiddleware | 🟢 完全对应 | 功能一致 |
| **ResultOffloading** | 大型工具结果回收 | 🟢 完全对应 | 直接借鉴 |
| **HangingCallRecovery** | 悬挂工具调用修复 | 🟢 完全对应 | 直接借鉴 |
| **Backend Protocol** | Pluggable Backend (0.2) | 🟢 完全对应 | 直接借鉴 |
| **CompositeBackend** | 复合后端 (0.2) | 🟢 完全对应 | 直接借鉴 |
| **MemoryMiddleware** | SummarizationMiddleware（部分） | 🟡 部分 | 你的记忆系统更完善（四层架构） |
| **SandboxMiddleware** | SandboxBackendProtocol | 🟡 部分 | Deep Agents 沙箱较简单 |
| **SafetyMiddleware** | Guardrails（安全护栏） | 🟡 部分 | 你的安全策略更全面 |
| **Skill 系统** | Skills 机制 | 🟢 完全对应 | 直接借鉴 |
| **API Gateway** | LangGraph Platform / LangServe | 🔵 替代 | 你用 FastAPI 替代 LangServe |
| **Model Adapter** | LangChain ChatModel | 🔵 替代 | 你用 LiteLLM 替代 LangChain 抽象 |
| **OpenAI Compatible API** | 无直接对应 | 🔴 独有 | Deep Agents 依赖 LangGraph 协议 |
| **Hooks System** | 无直接对应 | 🔴 独有 | Deep Agents 用 Middleware 覆盖 |
| **ObservabilityMiddleware** | LangSmith | 🔵 替代 | 你用自建可观测性替代 LangSmith |

### 6.3 关键差异深度分析

#### 差异 1：技术栈独立性

```
Deep Agents 的依赖链：
  deepagents → langgraph → langchain → Python
  你必须使用 LangChain 的抽象（ChatModel, Tool, State）
  切换成本极高

你的 Harness 2.0：
  harness-agent → litellm + fastapi + 自研 core
  模型层用 LiteLLM（100+ 模型，一个接口）
  Web 层用 FastAPI（成熟、高性能）
  无框架绑定，每个组件可独立替换
```

**影响**：你的项目更适合作为**独立产品**分发，不绑定任何 AI 框架。

#### 差异 2：协议开放性

```
Deep Agents：
  对外接口 → LangGraph Platform API（私有协议）
  前端集成 → 需要使用 LangGraph SDK 或 LangServe
  兼容性 → 仅 LangChain 生态内

你的 Harness 2.0：
  对外接口 → OpenAI Compatible API（事实标准）
  前端集成 → Open WebUI / Lobe Chat / 任何 OpenAI 兼容客户端
  兼容性 → 整个 AI 工具生态
```

**影响**：你的项目**即插即用**，任何支持 OpenAI API 的工具都能直接对接。

#### 差异 3：记忆系统深度

```
Deep Agents：
  · SummarizationMiddleware（对话压缩）
  · LangGraph Store（跨线程持久化）
  · 文件系统作为上下文载体
  → 两层记忆：压缩 + 持久化

你的 Harness 2.0：
  · Working Memory（Redis，实时热数据）
  · Short-term Memory（滑动窗口，最近 N 轮）
  · Long-term Memory（PostgreSQL，结构化事实）
  · Semantic Memory（ChromaDB，语义向量检索）
  · 记忆生命周期：写入 → 检索 → 演化 → 遗忘
  → 四层记忆 + 完整生命周期
```

**影响**：你的记忆系统**设计更完善**，更适合需要长期个性化记忆的场景。

#### 差异 4：安全体系

```
Deep Agents：
  · Guardrails（安全护栏，通过后端继承实现）
  · 沙箱隔离（SandboxBackendProtocol）
  → 安全作为后端的可选属性

你的 Harness 2.0：
  · SafetyMiddleware（独立安全中间件）
  · SandboxMiddleware（Docker/Process/Remote 多后端）
  · Hooks System（细粒度拦截点：PreToolUse/PostToolUse）
  · 命令黑名单 + 资源限制 + 网络隔离
  · 用户确认机制（危险操作需确认）
  → 安全作为一等公民
```

**影响**：你的安全体系**更系统化**，更适合生产环境。

#### 差异 5：扩展模式

```
Deep Agents 扩展：
  · 编写自定义 Middleware
  · 编写自定义 Skill
  · 继承 Backend 添加 Guardrails
  → 扩展点明确，但需理解 LangChain 体系

你的 Harness 2.0 扩展：
  · 编写自定义 Middleware（同样简洁）
  · 编写自定义 Skill（同样简洁）
  · 实现 Backend Protocol（更灵活的协议式扩展）
  · 注册 Hooks（额外的细粒度扩展点）
  · 自定义工具（Builtin / MCP 双协议）
  → 更多扩展点，但学习成本稍高
```

### 6.4 场景选型指南

```
你应该选择 Deep Agents 当：
  ✅ 已经在使用 LangChain + LangGraph 体系
  ✅ 团队熟悉 LangChain 抽象
  ✅ 需要 LangSmith 监控
  ✅ 快速验证 POC，不想自建基础设施

你应该选择自建 Harness 2.0 当：
  ✅ 需要模型无关（不被任何 AI 厂商绑定）
  ✅ 需要前端兼容（Open WebUI / Lobe Chat 即插即用）
  ✅ 需要更精细的安全控制
  ✅ 需要更完善的记忆系统
  ✅ 想要深入理解 Agent 内部机制（学习目的）
  ✅ 需要作为独立产品分发
  ✅ 团队不使用 LangChain 体系
```

### 6.5 综合评分对比

| 维度 | Deep Agents | 你的 Harness 2.0 | 优势方 |
|------|-------------|-------------------|--------|
| **开发速度** | ⭐⭐⭐⭐⭐（开箱即用） | ⭐⭐⭐（需要自建） | Deep Agents |
| **模型无关性** | ⭐⭐（LangChain 绑定） | ⭐⭐⭐⭐⭐（LiteLLM 100+） | Harness 2.0 |
| **前端兼容性** | ⭐⭐（LangGraph 协议） | ⭐⭐⭐⭐⭐（OpenAI 标准） | Harness 2.0 |
| **记忆系统** | ⭐⭐⭐（两层） | ⭐⭐⭐⭐⭐（四层 + 生命周期） | Harness 2.0 |
| **安全体系** | ⭐⭐⭐（护栏模式） | ⭐⭐⭐⭐⭐（中间件 + Hooks） | Harness 2.0 |
| **可扩展性** | ⭐⭐⭐⭐（Middleware） | ⭐⭐⭐⭐⭐（Middleware + Backend + Hook + Skill） | Harness 2.0 |
| **工程成熟度** | ⭐⭐⭐⭐⭐（LangChain 团队） | ⭐⭐（个人项目） | Deep Agents |
| **社区生态** | ⭐⭐⭐⭐⭐（LangChain 生态） | ⭐（起步阶段） | Deep Agents |
| **学习价值** | ⭐⭐⭐（用框架） | ⭐⭐⭐⭐⭐（造框架） | Harness 2.0 |
| **独立性** | ⭐⭐（深度绑定） | ⭐⭐⭐⭐⭐（完全独立） | Harness 2.0 |

### 6.6 关键结论

> **你的 Harness 2.0 和 Deep Agents 是同一抽象层（Harness 层）的两个独立实现。**
>
> - **设计理念**：高度一致——都采用中间件驱动、可插拔后端、技能包扩展
> - **技术路线**：完全不同——你选择独立技术栈，Deep Agents 选择 LangChain 生态
> - **能力覆盖**：你的设计**更全面**（四层记忆、更完善的安全、OpenAI 兼容协议）
> - **工程成熟度**：Deep Agents **遥遥领先**（LangChain 团队、生产验证、社区生态）
> - **定位**：Deep Agents 是**产品级框架**，你的项目是**学习级平台 + 潜在独立产品**

---

## 七、2.0 技术决策说明（更新）

| 决策点 | 1.0 选择 | 2.0 选择 | 变化原因 |
|--------|----------|----------|----------|
| **能力注入方式** | 独立模块直接调用 | 中间件管道注入 | 借鉴 Deep Agents，解耦更好 |
| **存储抽象** | 模块内部各自管理 | Backend Protocol 统一协议 | 借鉴 Deep Agents 0.2，可插拔 |
| **领域知识封装** | 无 | Skills 技能包机制 | 借鉴 Deep Agents，可复用 |
| **结果溢出处理** | 无 | ResultOffloading 自动转储 | 借鉴 Deep Agents 0.2 |
| **中断恢复** | 无 | HangingCallRecovery | 借鉴 Deep Agents 0.2 |
| **Hooks System** | 核心模块 | 保留，与 Middleware 互补 | Hooks 更细粒度，Middleware 更结构化 |
| **模型适配** | LiteLLM | LiteLLM（不变） | 已是最优选择 |
| **API 协议** | OpenAI Compatible | OpenAI Compatible（不变） | 已是最优选择 |
