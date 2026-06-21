# 项目记忆导出与共享 — 设计方案

> **配套文档**：[10-personal-growth.md](10-personal-growth.md)
> **目的**：把 SmileX 记忆"外化"到项目仓库，让**未安装 SmileX** 的开发者（或 Agent）也能继承经验
> **核心理念**：记忆不能只存在 SmileX 里，必须能沉淀为**项目原生的、版本化的、人机可读**的文件

---

## 0. 问题诊断：为什么需要导出？

### 0.1 典型场景

```
场景 1：项目交接
  开发者 A 用 SmileX 半年，积累了 1000+ 实体、200+ 决策
  → 转交给开发者 B（没装 SmileX）
  → B 如何快速理解项目历史与陷阱？

场景 2：开源项目
  项目对外开源，社区贡献者要快速上手
  → 把 SmileX 的"项目记忆"转为 README / ADR / 贡献指南
  → 任何人 clone 后即可访问

场景 3：多 Agent 协作
  开发者本地用 SmileX，但团队其他人用 Cursor / Claude Code
  → 导出为 .cursor/rules/、CLAUDE.md
  → 团队每个人用自己的 Agent 也能享受同样的项目知识

场景 4：备份与迁移
  项目知识是宝贵资产，不能只存在单一工具中
  → 导出为 .smilex/snapshot.json（完整快照）
  → 可迁移到其他记忆系统或恢复
```

### 0.2 设计原则

```
1. 标准优先：导出到约定路径（CLAUDE.md、.cursor/rules/），而非自定义
2. 隐私可控：分级导出（公开/内部/私有）
3. 单向为主：SmileX → 项目仓库，避免双向同步复杂度
4. Git 友好：导出内容适合 diff、合并、版本管理
5. 人机可读：Markdown 优先，JSON 作为机器消费兜底
```

---

## 1. 三种导出模式

### 1.1 模式对比

| 模式 | 适用场景 | 优点 | 缺点 |
|------|---------|------|------|
| **A. 静态文件导出** ★默认 | 项目交接、开源 | 人机可读、Git 友好 | 需手动触发更新 |
| **B. 完整快照** | 备份、迁移 | 数据无损 | 不可读、体积大 |
| **C. MCP Server** | 实时协作 | 实时同步 | 需 SmileX 运行 |

### 1.2 推荐组合

```
默认：A（静态文件）+ 可选 B（快照备份）+ 可选 C（MCP 实时）
```

---

## 2. 模式 A：静态文件导出（核心）

### 2.1 标准文件约定（多 Agent 兼容）

针对主流 AI Agent，导出到它们的**约定路径**：

```
project-root/
├── CLAUDE.md                          # Claude Code 上下文
├── AGENTS.md                          # 通用 Agent 上下文（新兴标准）
├── .cursor/
│   └── rules/
│       ├── general.mdc                # Cursor 规则
│       ├── tech-stack.mdc
│       └── conventions.mdc
├── .github/
│   └── copilot-instructions.md        # GitHub Copilot
├── .clinerules                        # Cline
├── .windsurfrules                     # Windsurf
├── CONVENTIONS.md                     # Aider
├── .continuerc.json                   # Continue
├── docs/
│   ├── decisions/                     # ADR（架构决策记录）
│   │   ├── 0001-use-sqlite.md
│   │   ├── 0002-cachebox-for-cache.md
│   │   └── 0003-leiden-for-community.md
│   ├── lessons-learned.md             # 经验教训
│   ├── tech-context.md                # 技术上下文
│   └── team-glossary.md               # 团队术语
└── .smilex/
    ├── snapshot.json                  # 完整快照（模式 B）
    ├── export-manifest.yaml           # 导出清单
    └── privacy.yaml                   # 隐私配置
```

### 2.2 各文件内容定义

#### 2.2.1 `CLAUDE.md` / `AGENTS.md`（核心）

```markdown
# Project: my-web-app

> 自动由 SmileX 生成，最后更新：2026-06-21
> 不要手动编辑此文件，修改请用 `smilex edit-context`

## 项目概述
这是一个基于 FastAPI + PostgreSQL 的电商后端服务...

## 技术栈
- **后端**: Python 3.13, FastAPI, SQLAlchemy 2.0
- **数据库**: PostgreSQL 16 + Redis 7
- **部署**: Docker + Kubernetes

## 关键架构决策（详见 docs/decisions/）
- [ADR-0001] 选择 PostgreSQL 而非 MySQL（JSON 操作更强）
- [ADR-0002] 使用 Redis 做缓存，未用 Memcached（数据结构更丰富）
- [ADR-0003] API 采用 CQRS 模式（读写分离）

## 代码规范
- 函数命名：snake_case
- 类型注解：强制
- 测试覆盖：> 80%
- 详见 `.cursor/rules/conventions.mdc`

## 避坑指南（详见 docs/lessons-learned.md）
- ⚠️ Redis 大 key 会导致阻塞，单 key 不超过 10KB
- ⚠️ SQLAlchemy 2.0 的 Session 是异步的，记得 await
- ⚠️ 部署时 K8s liveness probe 间隔不要 < 10s

## 团队术语（详见 docs/team-glossary.md）
- "DR" = Decision Record（决策记录）
- "Spike" = 探索性任务
- "SLO" = Service Level Objective
```

#### 2.2.2 `docs/decisions/0001-use-sqlite.md`（ADR 格式）

```markdown
# ADR-0001: 选择 SQLite 作为主存储

- **状态**: Accepted
- **日期**: 2026-06-15
- **决策者**: 开发团队

## 背景
项目初期需要选择主数据库。考虑过 PostgreSQL、MySQL、SQLite。

## 决策
选择 SQLite 3.40+。

## 理由
1. 个人电脑零安装
2. 单文件分发
3. 进程内访问延迟 < 1ms
4. 跨平台一致

## 后果
- ✅ 部署简单
- ⚠️ 单写者模型，高并发受限
- ⚠️ 不适合多用户 SaaS 场景

## 关联
- 替代方案: PostgreSQL（若需多用户可切回）
- 相关复盘: docs/lessons-learned.md#sqlite-write-lock-issue
```

#### 2.2.3 `docs/lessons-learned.md`（经验教训）

```markdown
# 经验教训（Lessons Learned）

> 自动由 SmileX 生成，按时间倒序

## 2026-06-20: Redis OOM 导致服务不可用 🔴

**场景**: 大促期间 Redis 内存满了，导致所有缓存请求超时
**根因**: 没做容量评估，峰值流量是预估的 5 倍
**教训**: 
1. 关键中间件必须做容量评估 + 压测
2. Redis 配置 maxmemory + 淘汰策略
3. 监控内存使用率（> 80% 告警）

**相关技能**: redis, capacity_planning
**适用场景**: 上线前检查、中间件部署

---

## 2026-06-15: SQLAlchemy 2.0 异步陷阱 🟡

**场景**: 升级到 SQLAlchemy 2.0 后，部分查询不返回结果
**根因**: Session 是异步的，但代码用了同步写法
**教训**: 
1. 升级主版本前必须读 Migration Guide
2. 异步代码必须用 `await session.execute(...)`

**相关技能**: sqlalchemy
```

#### 2.2.4 `.cursor/rules/tech-stack.mdc`（Cursor 规则）

```yaml
---
description: 项目技术栈规则
globs: "**/*.py"
alwaysApply: false
---

# 技术栈约束

## 后端
- Python >= 3.13
- FastAPI（不要用 Flask）
- SQLAlchemy 2.0（异步模式）

## 数据库
- 主库 SQLite（不要引入 MySQL）
- 缓存 cachebox（不要用 Redis）

## 禁止
- 不要引入新的 ORM
- 不要用 print() 调试，用 logging
- 不要在 main 分支直接提交

## 推荐
- 新功能先写测试
- 提交前跑 ruff + mypy
- 复杂逻辑写 docstring
```

### 2.3 导出契约配置（`.smilex/export.yaml`）

```yaml
# .smilex/export.yaml - 用户可自定义导出规则

version: 1
scope: project:my-web-app

# 隐私级别
privacy:
  level: internal              # public / internal / private
  redact_names: true           # 自动脱敏人名
  exclude_personal: true       # 排除个人成长数据

# 导出目标
targets:
  # AI Agent 配置文件
  - name: claude_context
    template: claude_md
    output: CLAUDE.md
    enabled: true
    
  - name: agents_md
    template: agents_md
    output: AGENTS.md
    enabled: true
    
  - name: cursor_rules
    template: cursor_mdc
    output: .cursor/rules/
    enabled: true
    split_by: topic            # 按 topic 分多个文件
    
  - name: copilot_instructions
    template: copilot_md
    output: .github/copilot-instructions.md
    enabled: false             # 默认关闭
    
  - name: aider_conventions
    template: conventions_md
    output: CONVENTIONS.md
    enabled: false
  
  # 文档类
  - name: adr_records
    template: adr
    output: docs/decisions/
    enabled: true
    filter:
      edge_kind: semantic
      predicate: decided
      min_confidence: 0.7
    
  - name: lessons_learned
    template: lessons_md
    output: docs/lessons-learned.md
    enabled: true
    filter:
      reflection_type: [failure, lesson]
      privacy: public
  
  - name: tech_context
    template: tech_context_md
    output: docs/tech-context.md
    enabled: true
  
  - name: glossary
    template: glossary_md
    output: docs/team-glossary.md
    enabled: true

# 完整快照（模式 B）
snapshot:
  enabled: true
  output: .smilex/snapshot.json
  include_vectors: false       # 向量不导出（体积大）
  
# 同步策略
sync:
  auto_on_commit: false        # 默认手动触发
  git_hook: pre-push           # 可选：pre-push 自动导出
  ignore_in_gitignore: false   # 导出文件是否加 .gitignore
```

---

## 3. 导出器实现

### 3.1 核心类设计

```python
# src/smilex/export/exporter.py
from pathlib import Path
from dataclasses import dataclass

class PrivacyLevel(StrEnum):
    PUBLIC = "public"          # 可公开（开源场景）
    INTERNAL = "internal"      # 团队内部
    PRIVATE = "private"        # 仅自己

@dataclass
class ExportConfig:
    scope: str
    privacy: PrivacyLevel
    targets: list[ExportTarget]
    snapshot: bool
    output_dir: Path

class Exporter:
    """项目记忆导出器"""
    
    def __init__(self, db, llm, templates: TemplateEngine):
        self.db = db
        self.llm = llm
        self.templates = templates
    
    async def export(
        self, config: ExportConfig
    ) -> ExportResult:
        """执行导出"""
        result = ExportResult()
        
        # 1. 加载项目记忆
        project_data = await self._load_project_data(config.scope, config.privacy)
        
        # 2. 按目标生成
        for target in config.targets:
            if not target.enabled:
                continue
            
            content = await self._render(target, project_data)
            output_path = config.output_dir / target.output
            
            await self._write(output_path, content)
            result.add(target.name, output_path)
        
        # 3. 完整快照（如果启用）
        if config.snapshot:
            snapshot = await self._generate_snapshot(config.scope)
            snapshot_path = config.output_dir / ".smilex/snapshot.json"
            await self._write(snapshot_path, snapshot)
            result.add("snapshot", snapshot_path)
        
        # 4. 生成 manifest（清单）
        manifest = self._generate_manifest(result)
        await self._write(
            config.output_dir / ".smilex/export-manifest.yaml",
            manifest,
        )
        
        return result
    
    async def _load_project_data(
        self, scope: str, privacy: PrivacyLevel
    ) -> ProjectData:
        """加载并过滤项目数据"""
        return ProjectData(
            overview=await self.db.get_project_overview(scope),
            tech_stack=await self.db.get_tech_stack(scope),
            decisions=await self._get_decisions(scope, privacy),
            lessons=await self._get_lessons(scope, privacy),
            glossary=await self.db.get_glossary(scope),
            conventions=await self.db.get_conventions(scope),
            entities=await self.db.get_entities(scope, limit=100),
        )
    
    async def _get_decisions(
        self, scope: str, privacy: PrivacyLevel
    ) -> list[Decision]:
        """获取决策记录，按隐私过滤"""
        decisions = await self.db.query_triples(
            scope=scope,
            predicate="decided",
            edge_kind=EdgeKind.SEMANTIC,
        )
        
        # 隐私过滤
        if privacy == PrivacyLevel.PUBLIC:
            decisions = [d for d in decisions if d.provenance == Provenance.EXTRACTED]
        
        return decisions
    
    async def _get_lessons(
        self, scope: str, privacy: PrivacyLevel
    ) -> list[Reflection]:
        """获取经验教训"""
        reflections = await self.db.query_reflections(
            trigger_project=scope,
            type=[ReflectionType.FAILURE, ReflectionType.LESSON],
        )
        
        # 公开导出时脱敏
        if privacy == PrivacyLevel.PUBLIC:
            reflections = [
                r for r in reflections
                if not self._contains_sensitive(r)
            ]
            for r in reflections:
                r = self._redact(r)
        
        return reflections
```

### 3.2 模板引擎

```python
# src/smilex/export/templates.py
from jinja2 import Environment, FileSystemLoader

class TemplateEngine:
    """导出模板引擎"""
    
    def __init__(self):
        self.env = Environment(
            loader=FileSystemLoader("src/smilex/export/templates/"),
            trim_blocks=True,
            lstrip_blocks=True,
        )
    
    async def render(
        self, template_name: str, data: ProjectData
    ) -> str:
        template = self.env.get_template(f"{template_name}.j2")
        return template.render(**asdict(data))
```

### 3.3 多 Agent 模板示例

```
src/smilex/export/templates/
├── claude_md.j2              # CLAUDE.md
├── agents_md.j2              # AGENTS.md
├── cursor_mdc.j2             # .cursor/rules/*.mdc
├── copilot_md.j2             # .github/copilot-instructions.md
├── conventions_md.j2         # CONVENTIONS.md (Aider)
├── adr.j2                    # ADR 单条记录
├── lessons_md.j2             # docs/lessons-learned.md
├── tech_context_md.j2        # docs/tech-context.md
├── glossary_md.j2            # docs/team-glossary.md
└── snapshot_json.j2          # .smilex/snapshot.json
```

**`claude_md.j2` 示例**：
```jinja2
# Project: {{ overview.name }}

> 自动由 SmileX 生成，最后更新：{{ generated_at }}
> 不要手动编辑此文件，修改请用 `smilex edit-context`

## 项目概述
{{ overview.description }}

## 技术栈
{% for tech in tech_stack %}
- **{{ tech.category }}**: {{ tech.items | join(", ") }}
{% endfor %}

## 关键架构决策（详见 docs/decisions/）
{% for d in decisions %}
- [ADR-{{ loop.index | zfill(4) }}] {{ d.title }}
{% endfor %}

## 代码规范
{% for c in conventions %}
- {{ c.rule }}
{% endfor %}

## 避坑指南（详见 docs/lessons-learned.md）
{% for l in lessons[:5] %}
- ⚠️ {{ l.key_lesson }}
{% endfor %}

## 团队术语（详见 docs/team-glossary.md）
{% for term in glossary %}
- "{{ term.acronym }}" = {{ term.full }}
{% endfor %}
```

---

## 4. 模式 B：完整快照

### 4.1 快照格式

```json
{
  "version": "1.0",
  "generated_at": "2026-06-21T15:30:00Z",
  "scope": "project:my-web-app",
  "smilex_version": "0.1.0",
  
  "metadata": {
    "entity_count": 1234,
    "triple_count": 5678,
    "reflection_count": 89,
    "exported_by": "smilex export --snapshot"
  },
  
  "entities": [
    {
      "id": "01HXY...",
      "name": "FastAPI",
      "normalized_name": "fastapi",
      "entity_type": "tech",
      "scope": "project:my-web-app",
      "embedding_included": false,
      "created_at": "2026-06-15T..."
    }
  ],
  
  "triples": [
    {
      "subject_id": "01HXY...",
      "predicate": "uses",
      "object_id": "01HYZ...",
      "edge_kind": "semantic",
      "edge_properties": {},
      "provenance": "extracted",
      "confidence": 0.95,
      "valid_from": "2026-06-15T..."
    }
  ],
  
  "reflections": [
    {
      "type": "failure",
      "key_lesson": "...",
      "what_was_expected": "...",
      "what_actually_happened": "...",
      "why_it_happened": "...",
      "what_to_do_differently": "...",
      "related_skills": ["redis", "capacity_planning"]
    }
  ],
  
  "schema_version": "1.0",
  "compatible_smilex_versions": ">=0.1.0"
}
```

### 4.2 快照用途

| 用途 | 说明 |
|------|------|
| **备份** | 定期备份到 Git，灾难恢复 |
| **迁移** | 转移到其他项目 / 其他用户 |
| **导入** | 新用户 `smilex import snapshot.json` 快速建立记忆 |
| **审计** | 历史时点完整状态 |

### 4.3 快照排除项

```yaml
snapshot_excludes:
  - vectors             # 体积大，可重建
  - l0_snapshot         # 临时工作记忆
  - checkpoints         # 任务执行状态
  - personal:goals      # 个人目标（隐私）
  - personal:reflection # 个人复盘（隐私，除非显式启用）
```

---

## 5. 模式 C：MCP Server（实时协作）

### 5.1 工作模式

```
开发者 A 本地运行 SmileX（含项目记忆）
   ↓ 启动 MCP Server（监听本地端口）
其他 Agent（Cursor / Claude Code）配置 MCP endpoint
   ↓ 通过 MCP 协议查询
获得实时记忆访问（无需导出）
```

### 5.2 MCP 工具定义

```python
# src/smilex/mcp/server.py
from mcp import Server, Tool

class SmileXMcpServer(Server):
    """SmileX MCP Server"""
    
    @tool
    async def recall(
        self, query: str, scope: str = None
    ) -> str:
        """从项目记忆检索"""
        return await self.middleware.recall(query, scope)
    
    @tool
    async def get_decisions(
        self, scope: str
    ) -> list[dict]:
        """获取项目决策列表"""
        return await self.db.get_decisions(scope)
    
    @tool
    async def get_lessons(
        self, scope: str, type: str = None
    ) -> list[dict]:
        """获取经验教训"""
        return await self.db.get_lessons(scope, type)
    
    @tool
    async def find_entity(
        self, name: str, scope: str = None
    ) -> dict:
        """查找实体"""
        return await self.db.find_entity(name, scope)
```

### 5.3 客户端配置

**Cursor 配置**（`.cursor/mcp.json`）：
```json
{
  "mcpServers": {
    "smilex": {
      "command": "smilex",
      "args": ["mcp", "serve", "--scope", "project:my-web-app"]
    }
  }
}
```

**Claude Code 配置**（`.mcp.json`）：
```json
{
  "mcpServers": {
    "smilex": {
      "command": "smilex mcp serve"
    }
  }
}
```

---

## 6. 隐私分级导出

### 6.1 三级隐私

| 级别 | 含义 | 适用场景 |
|------|------|---------|
| `PUBLIC` | 完全公开 | 开源项目导出到公开仓库 |
| `INTERNAL` | 团队内部 | 公司内部 GitLab |
| `PRIVATE` | 仅自己 | 个人备份 |

### 6.2 各级别过滤规则

```python
PRIVACY_FILTERS = {
    PrivacyLevel.PUBLIC: {
        # 仅导出 EXTRACTED provenance
        "triple_provenance": [Provenance.EXTRACTED],
        # 排除所有个人数据
        "scope_patterns": ["project:", "global"],
        # 复盘仅导出 lesson（不含具体失败细节）
        "reflection_types": [ReflectionType.LESSON, ReflectionType.PATTERN],
        # 自动脱敏
        "redact_pii": True,
        "redact_names": True,
    },
    PrivacyLevel.INTERNAL: {
        # 包含 EXTRACTED + INFERRED
        "triple_provenance": [Provenance.EXTRACTED, Provenance.INFERRED],
        # 包含个人复盘（与项目相关的）
        "scope_patterns": ["project:", "global", "personal:reflection"],
        "reflection_types": [
            ReflectionType.FAILURE, ReflectionType.SUCCESS,
            ReflectionType.LESSON, ReflectionType.PATTERN,
        ],
        "redact_pii": False,
        "redact_names": False,
    },
    PrivacyLevel.PRIVATE: {
        # 全部导出
        "triple_provenance": None,
        "scope_patterns": None,
        "reflection_types": None,
        "redact_pii": False,
        "redact_names": False,
    },
}
```

### 6.3 PII 自动脱敏

```python
class PiiRedactor:
    """个人身份信息脱敏"""
    
    PATTERNS = {
        # 人名（中文 + 英文）
        "person_name": re.compile(r"(?<![A-Za-z])([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)(?![A-Za-z])"),
        # 邮箱
        "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
        # 手机号
        "phone": re.compile(r"\b1[3-9]\d{9}\b"),
        # 身份证
        "id_card": re.compile(r"\b\d{17}[\dXx]\b"),
    }
    
    def redact(self, text: str) -> str:
        for pii_type, pattern in self.PATTERNS.items():
            text = pattern.sub(f"[{pii_type}]", text)
        return text
```

---

## 7. 同步策略

### 7.1 手动触发（默认）

```bash
# 基本导出
smilex export --scope project:my-web-app

# 指定隐私级别
smilex export --privacy public    # 开源场景
smilex export --privacy internal  # 默认

# 仅导出特定目标
smilex export --only claude_md,adr

# 完整快照
smilex export --snapshot --output .smilex/snapshot.json
```

### 7.2 Git Hook 自动触发

**`.git/hooks/pre-push`**：
```bash
#!/bin/sh
# Push 前自动导出（仅当 .smilex/export.yaml 配置了 auto_on_push）

if [ -f ".smilex/export.yaml" ]; then
    auto=$(yq '.sync.auto_on_push' .smilex/export.yaml)
    if [ "$auto" = "true" ]; then
        echo "🔄 SmileX 自动导出项目记忆..."
        smilex export --quiet
        git add CLAUDE.md AGENTS.md docs/ .cursor/rules/ .smilex/
        git commit -m "chore(smilex): auto-export project memory" --no-verify
    fi
fi
```

### 7.3 .gitignore 策略

```gitignore
# .gitignore（由 smilex init 自动生成）

# 默认追踪（团队共享）
!CLAUDE.md
!AGENTS.md
!.cursor/rules/
!docs/decisions/
!docs/lessons-learned.md
!docs/tech-context.md

# 默认忽略（个人数据）
.smilex/personal/
.smilex/cache/
.smilex/logs/

# 快照可选追踪（默认不追踪，体积大）
.smilex/snapshot.json
```

---

## 8. 导入（双向）

### 8.1 从快照导入

```bash
# 新开发者克隆项目后
smilex import .smilex/snapshot.json --scope project:my-web-app
```

```python
class Importer:
    async def import_snapshot(
        self, snapshot_path: Path, scope: str
    ) -> ImportResult:
        snapshot = json.loads(snapshot_path.read_text())
        
        # 版本兼容性检查
        if not self._is_compatible(snapshot["schema_version"]):
            raise IncompatibleSnapshotError()
        
        # 导入实体
        async with self.db.transaction():
            for entity in snapshot["entities"]:
                await self.db.upsert_entity(entity)
            
            # 导入三元组
            for triple in snapshot["triples"]:
                await self.db.upsert_triple(triple)
            
            # 导入复盘
            for reflection in snapshot["reflections"]:
                await self.db.upsert_reflection(reflection)
        
        return ImportResult(
            imported_entities=len(snapshot["entities"]),
            imported_triples=len(snapshot["triples"]),
        )
```

### 8.2 从 Markdown 导入（反向解析）

```bash
# 从已有 CLAUDE.md 反向提取知识到 SmileX
smilex import --from CLAUDE.md --mode extract
```

```python
class MarkdownImporter:
    async def import_from_markdown(
        self, md_path: Path, scope: str
    ) -> ImportResult:
        content = md_path.read_text()
        
        # 用 LLM 从 Markdown 提取实体与关系
        extraction = await self.llm.extract(
            content,
            instructions="从项目文档提取技术栈、决策、规范等结构化信息",
        )
        
        # 写入 SmileX
        async with self.db.transaction():
            for entity in extraction.entities:
                await self.db.upsert_entity(entity, scope)
            for triple in extraction.triples:
                await self.db.upsert_triple(triple, scope)
```

---

## 9. CLI 命令

```bash
# 初始化项目导出配置
smilex export init
  → 生成 .smilex/export.yaml 模板
  → 生成 .gitignore 条目

# 执行导出
smilex export [options]
  --scope <scope>              # 默认当前项目
  --privacy <level>            # public/internal/private
  --only <targets>             # 仅导出指定目标
  --output <dir>               # 输出目录（默认项目根）
  --snapshot                   # 额外生成完整快照
  --dry-run                    # 仅打印将要导出的内容
  --quiet                      # 静默模式

# 启动 MCP Server（模式 C）
smilex mcp serve
  --scope <scope>              # 暴露哪个项目
  --port <port>                # 默认 8765
  --transport stdio|http       # 传输方式

# 导入快照
smilex import <snapshot.json>
  --scope <scope>              # 导入到哪个 scope
  --merge                      # 合并模式（默认）
  --replace                    # 替换模式（危险）

# 查看导出状态
smilex export status
  → 显示最后导出时间、变更数量、需要更新的文件
```

---

## 10. 用户工作流示例

### 场景 1：开源项目分享

```bash
# 1. 配置导出（一次性）
smilex export init
# 编辑 .smilex/export.yaml，设 privacy: public

# 2. 导出
smilex export
# → 生成 CLAUDE.md、docs/decisions/、docs/lessons-learned.md 等

# 3. 提交 Git
git add CLAUDE.md docs/ .cursor/rules/
git commit -m "docs: export project knowledge for contributors"
git push

# 4. 社区贡献者 clone 后
# → 直接看到 CLAUDE.md，了解项目上下文
# → 用 Cursor/Claude Code 时自动读取规则
```

### 场景 2：项目交接

```bash
# 开发者 A
smilex export --privacy internal
git push  # 包含完整内部知识

# 开发者 B 接手
git clone <repo>
smilex import .smilex/snapshot.json  # 建立 SmileX 记忆
smilex mcp serve  # 启动 MCP 给自己的 Agent 用
```

### 场景 3：多 Agent 协作

```bash
# 团队成员各自用不同 Agent
# A 用 SmileX + Claude Code
# B 用 Cursor
# C 用 GitHub Copilot

# A 维护 SmileX 作为 source of truth
smilex export  # 生成所有 Agent 的配置文件

# B 的 Cursor 自动读取 .cursor/rules/
# C 的 Copilot 自动读取 .github/copilot-instructions.md
# 所有人享受同样的项目知识
```

### 场景 4：个人备份

```bash
# 定期备份到私有 Git 仓库
smilex export --privacy private --snapshot
git add .smilex/snapshot.json
git commit -m "backup: full memory snapshot"
git push private-backup
```

---

## 11. 实施路线图

### Phase 1：MVP（1 周）

| 任务 | 工作量 |
|------|--------|
| ExportConfig + .smilex/export.yaml 解析 | 1 天 |
| TemplateEngine + Jinja2 模板 | 1 天 |
| 5 个核心模板（claude_md、agents_md、adr、lessons、tech_context）| 2 天 |
| Exporter 主流程 | 1 天 |
| CLI `smilex export` | 0.5 天 |
| 单元测试 | 1 天 |

### Phase 2：增强（1.5 周）

| 任务 | 工作量 |
|------|--------|
| 完整快照（snapshot.json）| 1 天 |
| 隐私分级过滤 | 1 天 |
| PII 脱敏 | 1 天 |
| 从 Markdown 反向导入 | 2 天 |
| Git hook 集成 | 1 天 |
| 多 Agent 模板（cursor、copilot、aider）| 1 天 |

### Phase 3：MCP（按需，1 周）

| 任务 | 工作量 |
|------|--------|
| MCP Server 实现 | 2 天 |
| MCP 工具定义（recall、get_decisions 等）| 2 天 |
| 客户端配置文档 | 1 天 |
| 集成测试 | 2 天 |

---

## 12. 关键决策（ADR）

### ADR-019: 导出采用"标准文件约定"而非自定义路径

- **选择**：导出到 CLAUDE.md、.cursor/rules/ 等约定路径
- **理由**：兼容主流 Agent，零配置即可被消费
- **权衡**：文件路径固定，灵活性低
- **回退**：通过 export.yaml 可自定义

### ADR-020: 默认单向导出，不做双向同步

- **选择**：SmileX → 项目仓库（单向）
- **理由**：双向同步会有冲突复杂度；导出内容是"快照"，非"源"
- **权衡**：用户改了 CLAUDE.md 不会自动回写 SmileX
- **缓解**：提供 `smilex import --from CLAUDE.md` 反向导入

### ADR-021: 隐私分级而非单开关

- **选择**：public / internal / private 三级
- **理由**：开源、内部、个人三种场景差异大
- **权衡**：用户需理解三级的差异
- **回退**：默认 internal，覆盖大部分场景

### ADR-022: 完整快照排除向量

- **选择**：snapshot.json 不含 embedding
- **理由**：向量体积大（10K 实体 = 60MB），且可重建
- **权衡**：导入后需重新计算向量（耗时）
- **缓解**：导入时后台异步重建

---

## 13. 与现有系统的协同

### 13.1 与个人成长的协同

```
项目记忆（project:xxx）
   +
个人成长（personal:skills, personal:reflection）
   ↓ 导出时
按 privacy 决定是否包含个人数据：
  - public: 仅项目记忆 + 脱敏后的 lesson
  - internal: + 相关复盘（去除敏感细节）
  - private: + 全部个人成长
```

### 13.2 与社区检测的协同

```
导出 ADR 时：
  - 按社区组织（同一架构主题的决策放一起）
  - 社区摘要作为 ADR 的概述
  → docs/decisions/ 按 topic 分目录
```

### 13.3 与增量索引的协同

```
导出触发：
  - 文件 SHA256 变更 → 标记决策已更新
  - 增量导出（仅更新变化的文件）
  → smilex export --incremental
```

---

## 14. 数据量估算

### 单次导出大小

| 文件 | 大小 |
|------|------|
| CLAUDE.md / AGENTS.md | ~10 KB |
| docs/decisions/（20 条 ADR）| ~100 KB |
| docs/lessons-learned.md（50 条）| ~50 KB |
| docs/tech-context.md | ~5 KB |
| .cursor/rules/（5 个 mdc）| ~25 KB |
| **静态导出合计** | **~200 KB** |
| .smilex/snapshot.json（不含向量）| ~1-5 MB |

**Git 仓库影响**：静态导出几乎无感知（< 1 MB），快照可控。

---

## 15. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 导出文件与手写文档冲突 | 提供 `smilex edit-context` 命令统一编辑入口 |
| 敏感信息泄露 | 默认 internal 级别 + PII 脱敏 + 公开导出前 review |
| 导出过时 | Git hook 自动触发 + 状态检查命令 |
| 多 Agent 模板维护成本 | 模板引擎抽象，新增 Agent 仅加一个 .j2 文件 |
| 快照体积过大 | 排除向量 + gzip 压缩 + 可选 chunk |

---

## 16. 结论

### 16.1 核心价值

```
┌──────────────────────────────────────────────────────────────┐
│  没有导出能力                  │  有导出能力                    │
│  ─────────────────            │  ─────────────                │
│  知识锁在 SmileX 里            │  ✅ 沉淀为项目原生文件          │
│  其他人无法继承                 │  ✅ clone 即可继承             │
│  Agent 各搞各的                │  ✅ 团队 Agent 知识统一         │
│  无备份                        │  ✅ Git 版本化 + 快照备份       │
│  开源项目无文档                 │  ✅ 自动生成 ADR + 教训         │
└──────────────────────────────────────────────────────────────┘
```

### 16.2 设计哲学

```
记忆 = 动态资产（SmileX 内部）
   ↓ 导出
知识 = 静态资产（项目仓库内）
   ↓ 任何 Agent / 人 都可消费
```

**关键认知**：
- **记忆系统是"源"，导出文件是"产物"**
- 不要把项目知识锁在单一工具里
- 导出文件应该**人机可读、Git 友好、标准约定**
- 通过**多 Agent 约定路径**，让导出零配置被消费

### 16.3 一句话总结

> **SmileX 不只是"装得下"记忆，更要"拿得出"记忆。**
>
> 把知识外化到项目仓库，让任何 Agent、任何开发者、任何场景都能继承项目的智慧。
