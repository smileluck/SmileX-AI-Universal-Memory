"""Unit tests for P1-b 批量导入(L3.6)— BulkImporter.

验证:
- Markdown 目录/单文件导入端到端(规则提取种子 + 每文件 L1 时序记忆,幂等)
- 大文件分块提取
- 文本批次导入(规则提取 + 时序记忆,幂等)
- Git 历史导入(tmp_path 临时仓库;提交 → 时序记忆,作者/文件 → 实体/三元组)
- Git 断点续传(预置 checkpoint cursor,跳过已处理提交)
- git 不可用时抛 RuntimeError
- MemoryMiddleware.import_source 集成入口
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from smilex.memory.scheduler.bootstrap import (
    BulkImporter,
    ImportKind,
    ImportSource,
    ProjectBootstrap,
)
from smilex.memory.scheduler.bootstrap import bulk_importer as bulk_importer_mod
from smilex.memory.scheduler.checkpoint import Checkpoint, CheckpointStore
from smilex.memory.storage.storage_engine import StorageEngine
from smilex.middlewares.dto import ProjectInitRequest
from smilex.middlewares.memory import MemoryMiddleware

GIT_AVAILABLE = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not GIT_AVAILABLE, reason="git 不可用")


@pytest.fixture
async def engine():
    eng = StorageEngine(":memory:", load_vec=False)
    await eng.initialize()
    yield eng
    await eng.close()


@pytest.fixture
async def project(engine):
    """已冷启动的项目,返回 (bootstrap, scope)."""
    bootstrap = ProjectBootstrap(engine)
    ctx = await bootstrap.initialize(ProjectInitRequest(name="demo", tech_stack=["Python"]))
    return bootstrap, ctx.scope


async def _fragments(engine, scope):
    cursor = await engine.conn.execute(
        "SELECT fragment_id, content, time_start, entities FROM temporal_fragments "
        "WHERE scope = ?",
        [scope],
    )
    return [dict(r) for r in await cursor.fetchall()]


async def _entity_ids(engine, scope):
    cursor = await engine.conn.execute(
        "SELECT entity_id FROM entities WHERE scope = ?", [scope]
    )
    return {r["entity_id"] for r in await cursor.fetchall()}


# ---------- Markdown 导入 ----------


async def test_import_markdown_directory(engine, project, tmp_path):
    """目录批量导入: 两个 .md 文件 → 技术/标题实体 + 2 条时序记忆."""
    bootstrap, scope = project
    (tmp_path / "arch.md").write_text(
        "# 架构\n## 模块划分\nBuilt with FastAPI and Redis.\n", encoding="utf-8"
    )
    sub = tmp_path / "docs"
    sub.mkdir()
    (sub / "deploy.md").write_text("# 部署\nUses Docker and Kubernetes.\n", encoding="utf-8")

    importer = BulkImporter(bootstrap)
    result = await importer.import_source(
        scope, ImportSource(kind=ImportKind.MARKDOWN, subject="demo", path=str(tmp_path))
    )
    assert result.errors == []
    assert result.source_count == 2
    assert result.memory_count == 2
    entity_ids = await _entity_ids(engine, scope)
    assert {"tech:fastapi", "tech:redis", "tech:docker", "tech:kubernetes"} <= entity_ids
    assert {"concept:架构", "concept:部署"} <= entity_ids  # 中文标题概念实体

    fragments = await _fragments(engine, scope)
    assert {f["fragment_id"] for f in fragments} == {"md:arch.md", "md:docs/deploy.md"}

    # 幂等: 重复导入不增数据
    again = await importer.import_source(
        scope, ImportSource(kind=ImportKind.MARKDOWN, subject="demo", path=str(tmp_path))
    )
    assert again.memory_count == 0
    assert again.skipped_count == 2
    assert (again.entity_count, again.triple_count) == (
        result.entity_count,
        result.triple_count,
    )


async def test_import_markdown_single_file(engine, project, tmp_path):
    """单文件导入: fragment_id 用文件名."""
    bootstrap, scope = project
    md = tmp_path / "README.md"
    md.write_text("# demo\n## 设计\nSQLite storage.\n", encoding="utf-8")
    importer = BulkImporter(bootstrap)
    result = await importer.import_source(
        scope, ImportSource(kind=ImportKind.MARKDOWN, subject="demo", path=str(md))
    )
    assert result.memory_count == 1
    assert "tech:sqlite" in await _entity_ids(engine, scope)
    fragments = await _fragments(engine, scope)
    assert fragments[0]["fragment_id"] == "md:README.md"


async def test_import_markdown_missing_path(project, tmp_path):
    bootstrap, scope = project
    importer = BulkImporter(bootstrap)
    with pytest.raises(RuntimeError, match="不存在"):
        await importer.import_source(
            scope,
            ImportSource(
                kind=ImportKind.MARKDOWN, subject="demo", path=str(tmp_path / "nope")
            ),
        )


async def test_import_markdown_chunked(engine, project, tmp_path):
    """大文件按行分块: 全部标题均被提取(分块不丢内容)."""
    bootstrap, scope = project
    headings = [f"## Section {i}\nsome text line\n" for i in range(30)]
    md = tmp_path / "big.md"
    md.write_text("# big\n" + "".join(headings), encoding="utf-8")
    importer = BulkImporter(bootstrap, markdown_chunk_chars=120)
    result = await importer.import_source(
        scope, ImportSource(kind=ImportKind.MARKDOWN, subject="demo", path=str(md))
    )
    assert result.errors == []
    entity_ids = await _entity_ids(engine, scope)
    # readme_parser 上限 20 个标题实体;分块后仍能提取到前 20 个
    assert "concept:section-0" in entity_ids
    assert "concept:section-19" in entity_ids


async def test_import_markdown_excludes_vendored_dirs(engine, project, tmp_path):
    """目录扫描跳过依赖/构建/隐藏目录(node_modules/.venv/build/.github 等)."""
    bootstrap, scope = project
    (tmp_path / "README.md").write_text("# Demo\nUses FastAPI.\n", encoding="utf-8")
    for d in ("node_modules/pkg", ".venv/lib", "build/out", ".github/workflows"):
        vendored = tmp_path / d
        vendored.mkdir(parents=True)
        (vendored / "vendor.md").write_text("# vendored doc\n", encoding="utf-8")

    importer = BulkImporter(bootstrap)
    result = await importer.import_source(
        scope, ImportSource(kind=ImportKind.MARKDOWN, subject="demo", path=str(tmp_path))
    )
    assert result.errors == []
    assert result.source_count == 1
    fragments = await _fragments(engine, scope)
    assert {f["fragment_id"] for f in fragments} == {"md:README.md"}


async def test_import_markdown_max_files(engine, project, tmp_path):
    """文件数超过 max_files: 截断导入并在 errors 记提示."""
    bootstrap, scope = project
    for i in range(3):
        (tmp_path / f"f{i}.md").write_text(f"# F{i}\n", encoding="utf-8")
    importer = BulkImporter(bootstrap)
    result = await importer.import_source(
        scope,
        ImportSource(
            kind=ImportKind.MARKDOWN, subject="demo", path=str(tmp_path), max_files=2
        ),
    )
    assert result.memory_count == 2
    assert len(result.errors) == 1
    assert "max_files" in result.errors[0]


# ---------- 源码文件导入 ----------


async def test_import_code_python_ast(engine, project, tmp_path):
    """.py AST 提取: docstring/顶层定义/依赖 → L1 记忆 + 文件/类/依赖种子,幂等."""
    bootstrap, scope = project
    app = tmp_path / "app"
    app.mkdir()
    (app / "__init__.py").write_text("", encoding="utf-8")
    (app / "utils.py").write_text("def f():\n    pass\n", encoding="utf-8")
    (app / "helpers.py").write_text("def g():\n    pass\n", encoding="utf-8")
    (app / "core.py").write_text(
        '"""核心模块."""\n'
        "import os\n"
        "import sys\n"
        "import fastapi\n"
        "from app.utils import f\n"
        "from .helpers import g\n\n"
        "class Service:\n    pass\n\n\ndef run():\n    pass\n",
        encoding="utf-8",
    )

    importer = BulkImporter(bootstrap)
    result = await importer.import_source(
        scope, ImportSource(kind=ImportKind.CODE, subject="demo", path=str(tmp_path))
    )
    assert result.errors == []
    assert result.source_count == 4  # 含空 __init__.py
    assert result.memory_count == 4

    fragments = await _fragments(engine, scope)
    core = next(f for f in fragments if f["fragment_id"] == "code:app/core.py")
    assert "核心模块" in core["content"]
    assert "Service" in core["content"] and "run" in core["content"]
    # 标准库(os/sys)过滤;fastapi 外部依赖;app.utils / .helpers 解析为仓库内路径
    assert "fastapi" in core["content"]
    assert "app/utils.py" in core["content"] and "app/helpers.py" in core["content"]
    assert "os" not in core["content"].replace("fastapi", "")

    entity_ids = await _entity_ids(engine, scope)
    # 文件实体 entity_id 与 git 通道同键(跨通道去重)
    assert "file:app-core-py" in entity_ids
    assert "tech:fastapi" in entity_ids
    assert "class:app-core-py-service" in entity_ids

    # 幂等: 重复导入不增数据
    again = await importer.import_source(
        scope, ImportSource(kind=ImportKind.CODE, subject="demo", path=str(tmp_path))
    )
    assert again.memory_count == 0
    assert again.skipped_count == 4
    assert (again.entity_count, again.triple_count) == (
        result.entity_count,
        result.triple_count,
    )


async def test_import_code_dep_resolution_src_layout_and_sibling(
    engine, project, tmp_path
):
    """绝对 import 解析: src 布局与同目录平级模块归内部,不误报 tech 实体."""
    bootstrap, scope = project
    pkg = tmp_path / "src" / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "models.py").write_text("y = 2\n", encoding="utf-8")
    (pkg / "api.py").write_text(
        "import mypkg.models\n", encoding="utf-8"
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "common.py").write_text("x = 1\n", encoding="utf-8")
    (scripts / "run.py").write_text("import common\n", encoding="utf-8")

    importer = BulkImporter(bootstrap)
    result = await importer.import_source(
        scope, ImportSource(kind=ImportKind.CODE, subject="demo", path=str(tmp_path))
    )
    assert result.errors == []
    fragments = {
        f["fragment_id"]: f["content"] for f in await _fragments(engine, scope)
    }
    # src 布局: import mypkg.models 解析为 src/mypkg/models 路径而非外部库
    assert "src/mypkg/models.py" in fragments["code:src/mypkg/api.py"]
    # 平级脚本: import common 解析为同目录文件而非外部库
    assert "scripts/common.py" in fragments["code:scripts/run.py"]
    entity_ids = await _entity_ids(engine, scope)
    assert "tech:mypkg" not in entity_ids
    assert "tech:common" not in entity_ids


async def test_import_code_other_extensions_header_comment(engine, project, tmp_path):
    """非 .py 源码: 文件头注释兜底成片段;忽略目录与 max_files 同 markdown."""
    bootstrap, scope = project
    (tmp_path / "index.js").write_text(
        "// 页面入口\n// 处理路由与渲染\nimport x from 'y'\n", encoding="utf-8"
    )
    vendored = tmp_path / "node_modules" / "lib"
    vendored.mkdir(parents=True)
    (vendored / "dep.js").write_text("// ignored\n", encoding="utf-8")

    importer = BulkImporter(bootstrap)
    result = await importer.import_source(
        scope, ImportSource(kind=ImportKind.CODE, subject="demo", path=str(tmp_path))
    )
    assert result.errors == []
    assert result.source_count == 1
    fragments = await _fragments(engine, scope)
    js = next(f for f in fragments if f["fragment_id"] == "code:index.js")
    assert "页面入口" in js["content"] and "处理路由与渲染" in js["content"]


async def test_import_code_missing_path(project, tmp_path):
    bootstrap, scope = project
    importer = BulkImporter(bootstrap)
    with pytest.raises(RuntimeError, match="不存在"):
        await importer.import_source(
            scope,
            ImportSource(
                kind=ImportKind.CODE, subject="demo", path=str(tmp_path / "nope")
            ),
        )


# ---------- 文本批次导入 ----------


async def test_import_text_batch(engine, project):
    """文本批次: 规则提取技术实体 + 每条文本一条时序记忆,幂等."""
    bootstrap, scope = project
    importer = BulkImporter(bootstrap)
    source = ImportSource(
        kind=ImportKind.TEXT,
        subject="demo",
        texts=["我们用 FastAPI 和 Redis", "部署用 Docker"],
    )
    result = await importer.import_source(scope, source)
    assert result.memory_count == 2
    assert {"tech:fastapi", "tech:redis", "tech:docker"} <= await _entity_ids(
        engine, scope
    )

    again = await importer.import_source(scope, source)
    assert again.memory_count == 0
    assert again.skipped_count == 2
    assert again.entity_count == result.entity_count


# ---------- Git 导入 ----------


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path):
    """临时 git 仓库: 3 个提交(2 个文件,1 次修改)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev Chen")
    (repo / "a.py").write_text("print(1)\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init: add a.py")
    (repo / "b.py").write_text("print(2)\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat: add b.py")
    (repo / "a.py").write_text("print(3)\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fix: update a.py")
    return repo


@needs_git
async def test_import_git_history(engine, project, git_repo):
    """git 导入: 3 提交 → 3 条时序记忆 + 作者/文件实体 + 三元组."""
    bootstrap, scope = project
    importer = BulkImporter(bootstrap)
    result = await importer.import_source(
        scope, ImportSource(kind=ImportKind.GIT, subject="demo", path=str(git_repo))
    )
    assert result.errors == []
    assert result.source_count == 3
    assert result.memory_count == 3

    entity_ids = await _entity_ids(engine, scope)
    assert "person:dev-chen" in entity_ids  # 作者实体
    assert {"file:a-py", "file:b-py"} <= entity_ids  # 文件实体

    cursor = await engine.conn.execute(
        "SELECT predicate FROM triples WHERE scope = ?", [scope]
    )
    predicates = {r["predicate"] for r in await cursor.fetchall()}
    assert {"contributes_to", "contains_file"} <= predicates

    fragments = await _fragments(engine, scope)
    assert all(f["fragment_id"].startswith("git:repo:") for f in fragments)
    contents = "\n".join(f["content"] for f in fragments)
    assert "init: add a.py" in contents
    assert "fix: update a.py" in contents
    # 时态记忆锚定提交时间(time_start 非空且为 ISO)
    assert all(f["time_start"] for f in fragments)
    # 提交时序记忆关联了作者实体
    assert any(f["entities"] != "[]" for f in fragments)

    # 幂等: 重复导入不增数据
    again = await importer.import_source(
        scope, ImportSource(kind=ImportKind.GIT, subject="demo", path=str(git_repo))
    )
    assert again.memory_count == 0
    assert again.skipped_count == 3
    assert (again.entity_count, again.triple_count) == (
        result.entity_count,
        result.triple_count,
    )


@needs_git
async def test_import_git_max_commits(engine, project, git_repo):
    """max_commits 限制导入提交数(git log 新→旧,取最新 N 条)."""
    bootstrap, scope = project
    importer = BulkImporter(bootstrap)
    result = await importer.import_source(
        scope,
        ImportSource(
            kind=ImportKind.GIT, subject="demo", path=str(git_repo), max_commits=1
        ),
    )
    assert result.memory_count == 1
    fragments = await _fragments(engine, scope)
    assert "fix: update a.py" in fragments[0]["content"]


@needs_git
async def test_import_git_resume_from_checkpoint(engine, project, git_repo):
    """断点续传: 预置 cursor=最新提交,导入只处理其余 2 条."""
    bootstrap, scope = project
    latest_hash = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    store = CheckpointStore(engine)
    task_id = f"import:{scope}:git:repo"
    await store.save(Checkpoint(task_id=task_id, step=1, cursor=latest_hash))

    importer = BulkImporter(bootstrap)
    result = await importer.import_source(
        scope, ImportSource(kind=ImportKind.GIT, subject="demo", path=str(git_repo))
    )
    assert result.resumed is True
    assert result.memory_count == 2
    fragments = await _fragments(engine, scope)
    assert all("fix: update a.py" not in f["content"] for f in fragments)
    # 全部成功: 断点已清理
    assert await store.load_latest(task_id) is None


@needs_git
async def test_import_git_not_a_repo(engine, project, tmp_path):
    """非 git 目录: git log 失败抛 RuntimeError."""
    bootstrap, scope = project
    importer = BulkImporter(bootstrap)
    with pytest.raises(RuntimeError, match="git log 失败"):
        await importer.import_source(
            scope, ImportSource(kind=ImportKind.GIT, subject="demo", path=str(tmp_path))
        )


async def test_import_git_unavailable(engine, project, tmp_path, monkeypatch):
    """git 不可用(PATH 无 git): 抛 RuntimeError."""
    monkeypatch.setattr(bulk_importer_mod.shutil, "which", lambda _: None)
    bootstrap, scope = project
    importer = BulkImporter(bootstrap)
    with pytest.raises(RuntimeError, match="git 不可用"):
        await importer.import_source(
            scope, ImportSource(kind=ImportKind.GIT, subject="demo", path=str(tmp_path))
        )


# ---------- middleware 集成 ----------


async def test_middleware_import_source(tmp_path):
    """MemoryMiddleware.import_source: 缺省 scope = 当前项目."""
    (tmp_path / "note.md").write_text("# 笔记\nUses PostgreSQL.\n", encoding="utf-8")
    mw = MemoryMiddleware()
    await mw.initialize()
    try:
        resp = await mw.initialize_project(ProjectInitRequest(name="demo"))
        result = await mw.import_source(
            ImportSource(kind=ImportKind.MARKDOWN, subject="demo", path=str(tmp_path))
        )
        assert result.scope == resp.scope
        assert result.memory_count == 1
        cursor = await mw.engine.conn.execute(
            "SELECT entity_id FROM entities WHERE scope = ?", [resp.scope]
        )
        assert "tech:postgresql" in {r["entity_id"] for r in await cursor.fetchall()}
    finally:
        await mw.close()
