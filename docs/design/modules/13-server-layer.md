# 13. Server 层 — MCP 服务 / Web 面板 / 项目注入

> **版本**:v1.1
> **更新日期**:2026-09-04
> **代码位置**:`src/smilex/server/`、`src/smilex/cli/`、`scripts/register-service-*`
> **目的**:把嵌入式记忆库服务化,让任意支持 MCP 的 Agent 工具(Kimi Code / Claude Code / …)接入同一套长期记忆

---

## 1. 定位与依赖方向

```
Agent 工具(Kimi Code / Claude Code)
   │  MCP 协议(stdio 或 streamable-http)
   ▼
server 层(src/smilex/server/ + cli/)     ← 唯一新增层
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
  `embedder` / `reranker` / `fact_extractor` / `token_budget` / `enable_scheduler`
- `--config` 可指定任意路径,按扩展名识别格式:`.yaml`/`.yml` 走
  `yaml.safe_load`(PyYAML 已是核心依赖),其余走 `tomllib`;扁平键、字段名相同;
  全字段注释示例见仓库根 `config.example.yaml`(守卫测试防漂移)
- 优先级:CLI 参数 > 配置文件 > 默认值;`serve` 首次运行自动生成模板
  (显式 `--config` 指定不存在文件时直接报错,不静默落默认值)

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

### 3.4 `cli/` — smilex-memory 命令

```
smilex-memory serve [--config <路径>] [--db ...] [--host ...] [--port ...]
                    [--log-level debug|info|warning|error]     # 前台运行
smilex-memory start [--config ...] [--db ...] [--host ...] [--port ...]
                    [--log-file ...] [--log-level ...]          # 后台静默运行
smilex-memory stop  [--config ...] [--host ...] [--port ...] [--force]
smilex-memory status [--config ...] [--host ...] [--port ...]  # 0=运行中 1=未运行
smilex-memory mcp [--config <路径>] [--db ...]                  # stdio(项目级)
smilex-memory init [项目目录] [--config <路径>] [--tool <工具>...|all]
                   [--scope project|user] [--stdio] [--guide] [--scan] [--url ...]
smilex-memory doctor [--config <路径>]                          # 环境自检
```

`--config` 全子命令通用;`--host`/`--port` 覆盖配置文件(CLI > 文件 > 默认
`127.0.0.1:8765`)。

### 3.4.1 `server/daemon.py` — 后台控制(start / stop / status)

安装后 `smilex-memory start` 即后台静默运行,无需终端常驻:

- **start**:`Popen([sys.executable, "-m", "smilex.cli", "serve", ...])` 再执行
  (不依赖控制台脚本路径,`cli/__main__.py` 提供模块入口),POSIX
  `start_new_session=True` / Windows `DETACHED_PROCESS` 脱离终端进程组
- **pidfile / 日志按端口隔离**:`~/.smilex/serve-{port}.pid`、
  `~/.smilex/logs/serve-{port}.log`(`--log-file` 可覆盖),多实例互不干扰
- **就绪探测**:轮询 `GET /api/health`(单次 1s、总预算 30s,兼容
  sentence-transformers 慢加载);子进程早夭打印日志尾部 20 行并清 pidfile;
  超时但进程存活视为"仍在初始化",不终止
- **stop**:SIGTERM(uvicorn 自带信号处理 → lifespan 级联关停调度器与
  MemoryService)→ 10s 超时 SIGKILL 兜底,`--force` 跳过优雅退出
- **幂等**:重复 start 检测 pidfile 活进程直接返回;stale pidfile 自动清理;
  端口有服务但无 pidfile 提示"非 daemon 启动"不盲杀
- 全 stdlib 实现(subprocess/os/signal/urllib),零新增依赖

`init` 注入逻辑(`inject_tool_config`):

- **非破坏性合并**:保留目标项目已有其他 MCP server 配置
- **幂等**:同名牌同配置跳过;同名不同配置先备份 `.bak` 再覆盖
- **双 scope**:`--scope project` 写项目内配置;`--scope user` 写工具全局配置
  (仅 HTTP 模式,项目目录可省略)
- `--guide`:向 `AGENTS.md`(Claude 另有 `CLAUDE.md`)追加记忆使用约定,
  以 `<!-- smilex-memory-guide -->` 标记幂等
- 新增工具支持 = 写一个 `ToolAdapter` 子类并注册到 `ADAPTERS`
  (类属性:`servers_key` 定位 JSON server 表,支持嵌套元组;`kind` =
  `"json"`/`"toml"`/`"manual"`;`user_config_path()` 声明用户级配置)

适配器总表(注册名 → 配置位置与格式):

| 工具 | name | 项目级 | 用户级 | 格式 |
|------|------|--------|--------|------|
| Kimi Code | `kimi` | `.kimi-code/mcp.json` | — | JSON `mcpServers` |
| Claude Code | `claude` | `.mcp.json` | `~/.claude.json` | JSON `mcpServers` |
| Codex | `codex` | `.codex/config.toml` | `~/.codex/config.toml` | TOML `[mcp_servers.X]` |
| Cursor | `cursor` | `.cursor/mcp.json` | `~/.cursor/mcp.json` | JSON `mcpServers` |
| ZCode | `zcode` | `.zcode/config.json` | `~/.zcode/cli/config.json` | JSON 嵌套 `mcp.servers`(schema 严格,条目最小化) |
| Trae | `trae` | `.trae/mcp.json` | `Application Support/{Trae CN,Trae}/User/mcp.json`(探测存在者) | JSON `mcpServers` |
| WorkBuddy | `workbuddy` | — | — | manual(MCP 由客户端 UI 管理,打印连接器 JSON + 步骤) |

特殊实现说明:

- **Codex(TOML)**:`_inject_toml_config` 做**文本级手术**——`tomllib` 解析判断
  幂等/覆盖,正则删除目标段(到下一个顶层 `[` 段)后在文件尾追加新段,其余内容
  与注释 byte-for-byte 保留(不用 tomli-w 整体重写,避免破坏用户配置)
- **stdio 条目绝对路径**:`shutil.which("smilex-memory")` 解析回填
  (GUI 启动的工具没有 shell PATH),解析失败回退字面量

### 3.5 常驻注册脚本(`scripts/`)

| 平台 | 机制 | 脚本 |
|------|------|------|
| Windows | 计划任务(登录时启动,无窗口) | `register-service-windows.ps1 [-Unregister]` |
| Linux | `systemd --user` | `register-service-linux.sh [--uninstall]` |
| macOS | launchd LaunchAgent | `register-service-macos.sh [--uninstall]` |

均为"登录后自动跑 `smilex-memory serve`",服务本体不变;支持
`SMILEX_SERVE_ARGS="--config ... --port 9000"` 环境变量向 ExecStart 传参。

## 4. 典型接入流程(全局模式)

```bash
pip install 'smilex-ai-memory[server]'
smilex-memory start            # 后台静默运行(或 serve 前台调试 / 注册脚本常驻)
smilex-memory init D:\proj\foo --guide   # 一键注入 MCP 配置 + 使用约定
# 之后在 Kimi Code / Claude Code / Codex / Cursor / ZCode / Trae 中打开该项目
# 即可用 mcp__smilex-memory__* 工具(WorkBuddy 按打印的指引在 UI 中添加)
```

## 5. 设计取舍

- **单库 + scope 隔离**(而非每工具一库):跨项目召回与个人成长模块(§10)需要
  全局视图;scope 字段天然支持隔离
- **stateless HTTP**:重启服务不影响已配置的工具;记忆一致性由 SQLite 保证
- **面板不做写入**:写入语义(锁/冲突检测/晋升)只在 middleware 一处实现,
  面板加写等于维护第二份写路径
- **stdio 模式保留**:无网络/强隔离场景兜底,与全局模式代码完全复用
