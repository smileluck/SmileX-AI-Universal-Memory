# 13. Server 层 — MCP 服务 / Web 面板 / 项目注入

> **版本**:v1.0
> **更新日期**:2026-08-22
> **代码位置**:`src/smilex/server/`、`src/smilex/cli.py`、`scripts/register-service-*`
> **目的**:把嵌入式记忆库服务化,让任意支持 MCP 的 Agent 工具(Kimi Code / Claude Code / …)接入同一套长期记忆

---

## 1. 定位与依赖方向

```
Agent 工具(Kimi Code / Claude Code)
   │  MCP 协议(stdio 或 streamable-http)
   ▼
server 层(src/smilex/server/ + cli.py)   ← 唯一新增层
   │  进程内直接调用
   ▼
middlewares → memory(Layer 0-5,不感知 server 层存在)
```

- **单向依赖**:server → middlewares;核心库**零新依赖**,`mcp`/`fastapi`/`uvicorn` 全部
  延迟导入,未安装时给 `pip install 'smilex-ai-memory[server]'` 提示
- **面板只读**:写入统一走 MCP 工具(`memory_write`),面板只做监控与召回调试,
  避免第二条写路径破坏单写者假设

## 2. 两种安装方式

| 方式 | 命令 | 数据库 | 适用 |
|------|------|--------|------|
| 全局常驻 | `smilex-memory serve` | `~/.smilex/memory.db`(单库,scope 隔离) | 多项目共享、跨项目复盘 |
| 项目级 | `smilex-memory mcp`(stdio) | `<项目>/.smilex/memory.db`(独立库) | 单项目、离线、随项目打包 |

全局模式各工具配置同一个 HTTP 地址(`http://127.0.0.1:8765/mcp`);
项目级模式各工具配置 stdio 命令,每个工具进程独立起 MCP server,共用项目内 db 文件
(SQLite 单写者由 middleware 层锁保证,进程交替使用而非并发写)。

## 3. 模块组成

### 3.1 `server/config.py` — ServerConfig

- 配置文件 `~/.smilex/config.toml`(TOML),字段:`db_path` / `host` / `port` /
  `embedder` / `token_budget` / `enable_scheduler`
- 优先级:CLI 参数 > 配置文件 > 默认值;`serve` 首次运行自动生成模板

### 3.2 `server/mcp_server.py` — MCP 工具集

`create_mcp_server(service)` 注册 4 个工具,**stdio 与 HTTP 模式复用同一工具集**:

| 工具 | 用途 |
|------|------|
| `memory_recall` | 多路召回(L0 + 向量 KNN + 时序/图谱/因果),按 token 预算裁剪 |
| `memory_write` | 沉淀事实/决策;`scope=project` 需 `scope_id` 或先 init_project |
| `memory_init_project` | 项目冷启动(README/技术栈种子),建立 project scope |
| `memory_stats` | 各层计数(调试/面板) |

- `MemoryService`:MemoryMiddleware 懒初始化持有器,单进程单实例 = 单写者
- 所有工具返回 **JSON 文本**(MCP 文本内容块),调用方自行 `json.loads`
- HTTP 模式用 `streamable_http_app(stateless_http=True)`:状态在 SQLite,
  不依赖 MCP 会话

### 3.3 `server/app.py` + `server/api.py` — FastAPI 应用

路由顺序即优先级:

1. `/api/*` — 只读 JSON API(health / stats / memories / memory/{id} / recall-test / tasks)
   - `GET /api/health`:started_at / uptime_s / 配置摘要(embedder、调度器开关等)
   - `GET /api/stats`:各层计数 + l0_snapshots / causal_chains / db_size_bytes
   - `GET /api/memories`:关键词 `q`(≥3 字符走 FTS5 trigram BM25 排序,短词/异常回退
     LIKE)、`kind`(fragment/entity/triple)、`layer`、`scope`、`limit` 过滤
   - `GET /api/memory/{id}`:单条全字段详情(BLOB 值替换为 `<binary N bytes>`)
   - `POST /api/recall-test`:query / top_k / session_id / token_budget
2. `/` + `/static/*` — Web 面板(`server/panel/`,vanilla JS 三标签:概览/浏览/召回测试;
   「记忆地层学」暗色视觉,L0-L3 层色贯穿徽标与分层叠条,tab 状态与自动刷新开关存
   localStorage,15s 自动刷新且页面隐藏时暂停)
3. `Mount("/")` — MCP 子应用(内部路径 `/mcp`),兜底挂载

lifespan:启动时初始化 MemoryService(早暴露 db 错误)→ 可选调度器
(`register_core_tasks` 5 类核心任务)→ 级联 MCP 子应用 lifespan(session manager)。

**踩坑记录**:`from __future__ import annotations` 下,FastAPI 无法解析闭包内定义的
请求体模型(会退化成 query 参数,POST 返回 422)。请求体模型必须定义在模块级
(`RecallTestBody`)。

### 3.4 `cli.py` — smilex-memory 命令

```
smilex-memory serve [--db ...] [--host ...] [--port ...]   # 全局常驻
smilex-memory mcp [--db ...]                               # stdio(项目级)
smilex-memory init <项目目录> [--tool kimi|claude|all] [--stdio] [--guide] [--url ...]
smilex-memory doctor                                       # 环境自检
```

`init` 注入逻辑(`inject_tool_config`):

- **非破坏性合并**:保留目标项目已有其他 MCP server 配置
- **幂等**:同名牌同配置跳过;同名不同配置先备份 `.bak` 再覆盖
- `--guide`:向 `AGENTS.md`(Claude 另有 `CLAUDE.md`)追加记忆使用约定,
  以 `<!-- smilex-memory-guide -->` 标记幂等
- 新增工具支持 = 写一个 `ToolAdapter` 子类并注册到 `ADAPTERS`

### 3.5 常驻注册脚本(`scripts/`)

| 平台 | 机制 | 脚本 |
|------|------|------|
| Windows | 计划任务(登录时启动,无窗口) | `register-service-windows.ps1 [-Unregister]` |
| Linux | `systemd --user` | `register-service-linux.sh [--uninstall]` |
| macOS | launchd LaunchAgent | `register-service-macos.sh [--uninstall]` |

均为"登录后自动跑 `smilex-memory serve`",服务本体不变。

## 4. 典型接入流程(全局模式)

```bash
pip install 'smilex-ai-memory[server]'
smilex-memory serve &          # 或先用注册脚本设为常驻
smilex-memory init D:\proj\foo --guide   # 一键注入 MCP 配置 + 使用约定
# 之后在 Kimi Code / Claude Code 中打开该项目即可用 mcp__smilex-memory__* 工具
```

## 5. 设计取舍

- **单库 + scope 隔离**(而非每工具一库):跨项目召回与个人成长模块(§10)需要
  全局视图;scope 字段天然支持隔离
- **stateless HTTP**:重启服务不影响已配置的工具;记忆一致性由 SQLite 保证
- **面板不做写入**:写入语义(锁/冲突检测/晋升)只在 middleware 一处实现,
  面板加写等于维护第二份写路径
- **stdio 模式保留**:无网络/强隔离场景兜底,与全局模式代码完全复用
