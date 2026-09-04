"""E2E tests for MemoryMiddleware(M.2-M.6).

覆盖场景:
- M.2 initialize_project: 冷启动响应(scope/计数/stage/elapsed_ms)
- M.3 write: 小记忆驻留 L0、relations 解析写入存储层、大 content(>800 tokens)自动晋升 L1
- M.4 recall: L0/L1 召回 + MemoryRef 组装 + token 预算
- M.5 close_session/restore_session: L0 快照持久化与恢复(含跨实例重启)
- 生命周期: 未初始化拒绝、PROJECT scope 缺 scope_id 报错

说明: 默认 HashEmbedder 只保证确定性(相同文本 → 相同向量),
所以 L1 召回验证用「query == 完整 content」的精确匹配(距离 0).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from smilex.memory.lifecycle.promotion import L0_PROMOTION_THRESHOLD
from smilex.memory.models import MemoryLayer, MemoryScope
from smilex.middlewares import (
    MemoryMiddleware,
    ProjectInitRequest,
    RecallRequest,
    TripleInput,
    WriteRequest,
    WriteStatus,
)

SESSION = "sess_1"

# 稳定超过 800 tokens 的大 content(tiktoken 与 chars/4 估算两种模式都成立)
BIG_CONTENT = "alpha beta gamma delta epsilon zeta eta theta " * 200


@pytest.fixture
async def mw(tmp_path):
    m = MemoryMiddleware(tmp_path / "e2e.db")
    await m.initialize()
    yield m
    await m.close()


async def _init_project(mw: MemoryMiddleware, name: str = "demo"):
    return await mw.initialize_project(
        ProjectInitRequest(name=name, tech_stack=["Python"], template="web")
    )


# ---------- 生命周期 ----------

async def test_write_requires_initialize(tmp_path):
    m = MemoryMiddleware(tmp_path / "x.db")
    with pytest.raises(RuntimeError, match="initialize"):
        await m.write(
            WriteRequest(scope=MemoryScope.GLOBAL, content="x"), session_id=SESSION
        )


async def test_write_project_scope_requires_scope_id(tmp_path):
    """PROJECT scope 既未显式给 scope_id、也未初始化过项目 → ValueError."""
    async with MemoryMiddleware(tmp_path / "x.db") as m:
        with pytest.raises(ValueError, match="scope_id"):
            await m.write(
                WriteRequest(scope=MemoryScope.PROJECT, content="x"),
                session_id=SESSION,
            )


# ---------- M.2 initialize_project ----------

async def test_initialize_project(mw):
    resp = await _init_project(mw)
    assert resp.scope.startswith("project:")
    assert resp.entity_count > 0
    assert resp.triple_count > 0
    assert resp.elapsed_ms >= 0
    assert resp.to_dict()["scope"] == resp.scope


# ---------- M.2a bootstrap_project(冷启动 + 扫描导入) ----------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")


def _make_project_dir(tmp_path, *, with_git: bool = False) -> Path:
    """临时项目: README + docs/*.md(+ 可选 1 个 git 提交)."""
    (tmp_path / "README.md").write_text(
        "# Demo 项目\n\nBuilt with FastAPI and SQLite.\n", encoding="utf-8"
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "arch.md").write_text("# 架构\nUses Redis.\n", encoding="utf-8")
    if with_git:
        env_git = ["git", "-C", str(tmp_path)]
        subprocess.run(
            [*env_git, "-c", "user.email=dev@example.com", "-c", "user.name=Dev",
             "init"],
            check=True, capture_output=True,
        )
        subprocess.run(
            [*env_git, "-c", "user.email=dev@example.com", "-c", "user.name=Dev",
             "commit", "--allow-empty", "-m", "init: bootstrap"],
            check=True, capture_output=True,
        )
    return tmp_path


async def _scope_entity_ids(mw: MemoryMiddleware, scope: str) -> set[str]:
    cursor = await mw.engine.conn.execute(
        "SELECT entity_id FROM entities WHERE scope = ?", [scope]
    )
    return {r["entity_id"] for r in await cursor.fetchall()}


async def test_bootstrap_project_scan_without_git(mw, tmp_path):
    """无 .git 项目: README 自动读取 + markdown 导入,git 源记入 skipped."""
    root = _make_project_dir(tmp_path)
    result = await mw.bootstrap_project("demo", project_path=root)

    assert result["project_path"] == str(root)
    assert result["init"]["scope"].startswith("project:")
    # README 种子(FastAPI)与 markdown 种子(Redis)都落库
    ids = await _scope_entity_ids(mw, result["init"]["scope"])
    assert {"tech:fastapi", "tech:redis"} <= ids
    # README.md + docs/arch.md 两条 L1 记忆
    assert result["imports"]["markdown"]["memory_count"] == 2
    assert "git" not in result["imports"]
    assert any("无 .git" in s for s in result["skipped"])


async def test_bootstrap_project_idempotent(mw, tmp_path):
    """重复初始化: scope 复用,记忆零新增,scope 总量稳定不翻倍."""
    root = _make_project_dir(tmp_path)
    r1 = await mw.bootstrap_project("demo", project_path=root)
    r2 = await mw.bootstrap_project("demo", project_path=root)

    assert r1["init"]["scope"] == r2["init"]["scope"]
    md1, md2 = r1["imports"]["markdown"], r2["imports"]["markdown"]
    assert md2["memory_count"] == 0
    assert md2["skipped_count"] == md1["memory_count"]
    # 第一次导入完成后的 scope 总量,第二次 init/markdown 均零新增
    assert r2["init"]["entity_count"] == md1["entity_count"]
    assert md2["entity_count"] == md1["entity_count"]
    assert md2["triple_count"] == md1["triple_count"]
    # 行级无重复: entity_id 去重后数量一致
    ids = await _scope_entity_ids(mw, r1["init"]["scope"])
    assert len(ids) == md1["entity_count"]


@needs_git
async def test_bootstrap_project_scan_git(mw, tmp_path):
    """git 仓库: 提交历史导入为时序记忆."""
    root = _make_project_dir(tmp_path, with_git=True)
    result = await mw.bootstrap_project("demo", project_path=root)
    git = result["imports"]["git"]
    assert git["memory_count"] >= 1
    assert git["source_count"] >= 1


async def test_bootstrap_project_rejects_invalid_path(mw, tmp_path):
    with pytest.raises(ValueError, match="project_path"):
        await mw.bootstrap_project("demo", project_path=tmp_path / "nope")


# ---------- M.3 write ----------

async def test_write_small_memory_stays_in_l0(mw):
    await _init_project(mw)
    resp = await mw.write(
        WriteRequest(scope=MemoryScope.PROJECT, content="用户偏好深色主题"),
        session_id=SESSION,
    )
    assert resp.status is WriteStatus.SAVED
    assert resp.layers_affected == [MemoryLayer.L0_WORKING]
    assert resp.memory_id
    assert mw.l0.get(SESSION, resp.memory_id) is not None


async def test_write_with_relations(mw):
    """TripleInput: subject_name 自动补建 concept 实体并复用,triple 落库(L2)."""
    init = await _init_project(mw)
    resp = await mw.write(
        WriteRequest(
            scope=MemoryScope.PROJECT,
            content="Alice 负责记忆模块",
            entities=["Alice"],
            relations=[
                TripleInput(
                    subject_name="Alice",
                    predicate="works_on",
                    object_value="memory module",
                )
            ],
        ),
        session_id=SESSION,
    )
    assert resp.layers_affected == [MemoryLayer.L0_WORKING, MemoryLayer.L2_LONG]
    memory = mw.l0.get(SESSION, resp.memory_id)
    assert memory is not None
    assert len(memory.relations) == 1
    triple = await mw.engine.get_triple(memory.relations[0])
    assert triple is not None
    assert triple.predicate == "works_on"
    assert triple.object_value == "memory module"

    # 同名 subject 复用实体,不重复创建
    cursor = await mw.engine.conn.execute(
        "SELECT COUNT(*) FROM entities WHERE scope = ? AND name = 'Alice'",
        [init.scope],
    )
    assert (await cursor.fetchone())[0] == 1
    await mw.write(
        WriteRequest(
            scope=MemoryScope.PROJECT,
            content="Alice 也负责检索",
            relations=[
                TripleInput(
                    subject_name="Alice", predicate="works_on", object_value="recall"
                )
            ],
        ),
        session_id=SESSION,
    )
    cursor = await mw.engine.conn.execute(
        "SELECT COUNT(*) FROM entities WHERE scope = ? AND name = 'Alice'",
        [init.scope],
    )
    assert (await cursor.fetchone())[0] == 1


async def test_write_large_content_auto_promotes(mw):
    """content > 800 tokens → 自动晋升 L1(temporal_fragments + 向量),L0 移除."""
    init = await _init_project(mw)
    assert mw._counter.count(BIG_CONTENT) > L0_PROMOTION_THRESHOLD
    resp = await mw.write(
        WriteRequest(scope=MemoryScope.PROJECT, content=BIG_CONTENT),
        session_id=SESSION,
    )
    assert resp.layers_affected == [MemoryLayer.L1_SHORT]
    assert mw.l0.get(SESSION, resp.memory_id) is None  # 已从 L0 移除
    cursor = await mw.engine.conn.execute(
        "SELECT layer, scope, content FROM temporal_fragments WHERE id = ?",
        [resp.memory_id],
    )
    row = await cursor.fetchone()
    assert row["layer"] == "L1"
    assert row["scope"] == init.scope
    assert row["content"] == BIG_CONTENT
    # 晋升写入向量(fragment_id 关联)
    cursor = await mw.engine.conn.execute(
        "SELECT COUNT(*) FROM vector_links WHERE fragment_id = ?",
        [resp.memory_id],
    )
    assert (await cursor.fetchone())[0] == 1


# ---------- M.4 recall ----------

async def test_recall_l0_roundtrip(mw):
    await _init_project(mw)
    wr = await mw.write(
        WriteRequest(scope=MemoryScope.PROJECT, content="构建系统用 uv 管理依赖"),
        session_id=SESSION,
    )
    rr = await mw.recall(RecallRequest(query="依赖管理"), session_id=SESSION)
    assert MemoryLayer.L0_WORKING in rr.layers_used
    assert wr.memory_id in {s.id for s in rr.sources}
    assert "构建系统用 uv 管理依赖" in rr.context
    assert rr.token_count > 0


async def test_recall_promoted_memory_via_l1(mw):
    """晋升后的记忆经 L1 向量 KNN 召回(HashEmbedder 精确文本距离 0)."""
    await _init_project(mw)
    wr = await mw.write(
        WriteRequest(scope=MemoryScope.PROJECT, content=BIG_CONTENT),
        session_id=SESSION,
    )
    rr = await mw.recall(RecallRequest(query=BIG_CONTENT), session_id=SESSION)
    assert MemoryLayer.L1_SHORT in rr.layers_used
    src = next(s for s in rr.sources if s.id == wr.memory_id)
    assert src.layer is MemoryLayer.L1_SHORT
    assert 0.0 <= src.score <= 1.0
    assert src.snippet is not None


async def test_recall_respects_token_budget(mw):
    await _init_project(mw)
    for i in range(5):
        await mw.write(
            WriteRequest(scope=MemoryScope.PROJECT, content=f"第 {i} 条记忆 " * 20),
            session_id=SESSION,
        )
    budget = 50
    rr = await mw.recall(
        RecallRequest(query="记忆", token_budget=budget), session_id=SESSION
    )
    assert rr.token_count <= int(budget * 0.7)


# ---------- M.5 close_session / restore_session ----------

async def test_close_and_restore_session(mw):
    await _init_project(mw)
    ids = []
    for content in ("决定: 用 SQLite 单机存储", "约定: 全路径 scope"):
        wr = await mw.write(
            WriteRequest(scope=MemoryScope.PROJECT, content=content),
            session_id=SESSION,
        )
        ids.append(wr.memory_id)

    saved = await mw.close_session(SESSION)
    assert saved == 2
    assert mw.l0.list(SESSION) == []
    rr = await mw.recall(RecallRequest(query="存储"), session_id=SESSION)
    assert not (set(ids) & {s.id for s in rr.sources})

    restored = await mw.restore_session(SESSION)
    assert restored == 2
    rr = await mw.recall(RecallRequest(query="存储"), session_id=SESSION)
    assert set(ids) <= {s.id for s in rr.sources}


async def test_close_session_without_persist(mw):
    await _init_project(mw)
    await mw.write(
        WriteRequest(scope=MemoryScope.PROJECT, content="临时记忆"),
        session_id=SESSION,
    )
    saved = await mw.close_session(SESSION, persist=False)
    assert saved == 1
    assert await mw.restore_session(SESSION) == 0


async def test_restore_session_after_restart(tmp_path):
    """跨实例: 关闭 middleware 后新实例从同一 db 恢复 L0 快照."""
    db = tmp_path / "restart.db"
    async with MemoryMiddleware(db) as m:
        wr = await m.write(
            WriteRequest(scope=MemoryScope.GLOBAL, content="重启后要恢复的记忆"),
            session_id=SESSION,
        )
        assert await m.close_session(SESSION) == 1

    async with MemoryMiddleware(db) as m2:
        assert m2.l0.list(SESSION) == []
        assert await m2.restore_session(SESSION) == 1
        memory = m2.l0.get(SESSION, wr.memory_id)
        assert memory is not None
        assert memory.content == "重启后要恢复的记忆"
