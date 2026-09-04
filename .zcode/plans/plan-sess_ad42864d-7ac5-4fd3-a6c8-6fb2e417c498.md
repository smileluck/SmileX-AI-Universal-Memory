# 主流编码工具植入(codex / cursor / zcode / trae / workbuddy)

## 背景

项目已有 `ToolAdapter` 注入框架(`src/smilex/cli.py`),`smilex-memory init <项目目录>` 可把 MCP 配置非破坏性写入目标工具的项目配置。目前仅 kimi/claude 两个适配器,且契约硬编码了「JSON + 顶层 `mcpServers` + 项目级路径」。本次为 5 个主流工具新增适配器,并将契约扩展为支持 **TOML 格式(codex)、嵌套键(zcode)、手动指引(workbuddy)、用户级全局 scope**。

设计决策(未获用户回复,按推荐执行):支持 `--scope user|project` 双 scope;本次只加点名的 5 个工具。

## 各工具配置事实(已调研)

| 工具 | 项目级路径 | 用户级路径 | 格式 | smilex 条目形状 |
|---|---|---|---|---|
| codex | `<repo>/.codex/config.toml` | `~/.codex/config.toml` | TOML `[mcp_servers.X]` | `url=...` 或 `command`+`args[]` |
| cursor | `<repo>/.cursor/mcp.json` | `~/.cursor/mcp.json` | JSON `mcpServers` | 默认(`{"url":...}`) |
| zcode | `<repo>/.zcode/config.json` | `~/.zcode/cli/config.json` | JSON 嵌套 `mcp.servers` | 显式 `"type":"http"/"stdio"` + 字段 |
| trae | `<repo>/.trae/mcp.json` | 探测 `~/Library/Application Support/{Trae CN,Trae}/User/mcp.json` | JSON `mcpServers` | 默认 |
| workbuddy | —(MCP 由客户端 UI 管理,无可安全写入文件) | — | manual | 打印连接器 JSON + UI 步骤 |

zcode 注意:配置 schema 严格,未知键会导致 server 被静默丢弃 → 条目只含 `type/command/args`(stdio)或 `type/url`(http)。claude 现有适配器顺手获得 user scope(`~/.claude.json` 顶层 `mcpServers`,合并逻辑同构)。kimi 保持仅项目级。

## 实现步骤

### 1. 扩展 ToolAdapter 契约 + 新适配器(`src/smilex/cli.py`)

- `ToolAdapter` 新增类属性:`servers_key: str | tuple[str, ...] = "mcpServers"`(支持嵌套定位 zcode 的 `("mcp", "servers")`)、`kind: str = "json"`(`"json" | "toml" | "manual"`)、`user_config_path() -> Path | None`(默认 None=不支持用户级)。
- `inject_tool_config(adapter, project_dir, *, url, stdio, scope="project")`:JSON 路径按 `servers_key` 逐级 `setdefault` 导航;scope=user 时改用 `user_config_path()`(kimi/不支持的工具报错提示)。备份/幂等/合并语义不变。
- 新增 `CursorAdapter`、`TraeAdapter`(纯 `config_rel_path` 子类);`TraeAdapter` 覆写 `user_config_path()` 探测 CN/国际两个目录,都不存在返回 None 并在 init 输出里降级为手动指引。
- `ZCodeAdapter`:`servers_key=("mcp","servers")`,覆写 `http_entry`→`{"type":"http","url":...}`、`stdio_entry`→`{"type":"stdio","command":...,"args":[...]}`,guide 默认 AGENTS.md。
- `CodexAdapter`:`kind="toml"`,项目/用户级都支持。
- `WorkBuddyAdapter`:`kind="manual"`,提供 `manual_instructions(url, stdio, db_path) -> str`(含连接器 `mcpServers` JSON 片段与「连接器 → MCP 服务管理」步骤);init 对 manual 适配器只打印不写文件。
- `stdio_entry` 基类改进:用 `shutil.which("smilex-memory")` 解析绝对路径(GUI 启动的工具没有 shell PATH),失败回退原字面量。

### 2. TOML 注入(codex 专用,零新依赖)

新函数 `_inject_toml_config(...)`,算法:
1. 文件不存在 → 新建仅含目标段的 TOML。
2. `tomllib`(3.11+ stdlib)解析现有文件取 `mcp_servers."smilex-memory"`,与目标条目相等 → 幂等跳过。
3. 不一致 → 备份 `.bak` 后做**文本级手术**:正则定位 `^\[mcp_servers\.smilex-memory\]` 段头,删除到下一个顶层 `[` 段或 EOF,再在文件尾追加新段;文件其余内容(含注释、model_provider 等)byte-for-byte 保留。
4. 自写 mini 序列化仅覆盖 `str / list[str]`(本条目只需要这些),TOML 字符串正确转义。
5. 测试用 `tomllib` 回读验证合法性与保留字段。

### 3. CLI 子命令变更(`_cmd_init` / `build_parser`)

- `init`:`project_dir` 改 `nargs="?"`(scope=user 时可省,project 时必填并校验);新增 `--scope {project,user}` 默认 project;`--tool` choices 由 ADAPTERS 自动扩展为 `kimi claude codex cursor zcode trae workbuddy all`。
- manual 适配器(workbuddy 及 trae 用户级降级)打印指引;末尾提示语按所选工具输出针对性说明(如 Codex 需重启会话、ZCode 需检查 server 是否加载成功)。

### 4. 测试(`tests/unit/test_init_inject.py` 扩展,+~150 行)

- cursor/trae 新建/合并/幂等(参数化复用现有断言模式)。
- zcode:嵌套 `mcp.servers` 写入、`type` 字段、幂等、原有 `mcpServers` 不被误建。
- codex TOML:新建;已有 `model_provider` 等其他配置完整保留;幂等;变更覆盖+备份;`tomllib` 回读验证。
- workbuddy:manual 输出含 `mcpServers` JSON 与操作步骤。
- user scope:monkeypatch HOME 到 tmp_path,验证写入 `~/.codex/config.toml`、`~/.cursor/mcp.json`、`~/.zcode/cli/config.json`、`~/.claude.json`(保留其他顶层键)。
- `shutil.which` 解析:monkeypatch 后验证绝对路径回填。
- 全量 `uv run pytest`(653+)与 `uv run ruff check` 通过。

### 5. 文档同步

- `README.md` §服务化使用:工具列表、`--tool` 取值、`--scope user` 示例。
- `docs/design/modules/13-server-layer.md` §3.4:命令行签名、适配器总表(路径/格式/scope)、「新增工具 = 子类 + 注册」说明保持。
- `CHANGELOG.md` Unreleased 新增条目。

## 明确不做

- 不动在途的面板改造(`server/api.py`、panel 前端,属另一计划)。
- 不实际写入本机真实的 `~/.codex`、`~/.zcode` 等配置(冒烟验证一律用假 HOME 的 tmp 目录);真实植入由用户自行执行。
- 不引入 TOML 写入第三方依赖;不支持 windsurf/vscode/cline(用户未点名,保留扩展点)。

## 验证

`uv run pytest tests/unit/test_init_inject.py -q` → `uv run ruff check src tests` → 全量 `uv run pytest -q`;手动冒烟:tmp 假 HOME 下 `init /tmp/demo --tool all`、`init --scope user --tool codex,cursor,zcode` 检查生成文件内容。