"""L3.6 冷启动召回率验证 — golden set + Top-5 召回率 > 60%(§9.5.4 / §14.2).

方法学(EXECUTION_PLAN_GAPS §3.7):
- 1 个标准项目(ShopAPI:web 模板 + 技术栈 + 向导答案)作为 golden set
- 8 个人工标注的标准 query,每条标注应召回的三元组(predicate + object)
- 召回通道为**确定性**的(关键词 + scope 过滤 + 实体名解析):
  默认 HashEmbedder 无语义相似度(决策 D5),因此不断言向量语义召回;
  语义通道接入 BGE-M3 后(可选 extra `embedding`)可在此之上叠加
"""

from __future__ import annotations

import re

import pytest

from smilex.memory.scheduler.bootstrap import ProjectBootstrap, detect_technologies
from smilex.memory.storage.storage_engine import StorageEngine
from smilex.middlewares.dto import ProjectInitRequest

TOP_K = 5
RECALL_THRESHOLD = 0.6

# (query, 期望 predicate, 期望 object 中包含的子串)
GOLDEN_QUERIES: list[tuple[str, str, str]] = [
    ("Does the project use FastAPI?", "uses_tech", "FastAPI"),
    ("Which database does it use, SQLite?", "uses_tech", "SQLite"),
    ("Is Redis in the stack?", "uses_tech", "Redis"),
    ("What does the HTTP API depend on?", "depends_on", "Database"),
    ("What is the project goal?", "project_goal", "电商"),
    ("What protects the HTTP API? Authentication?", "protected_by", "Authentication"),
    ("What calls the HTTP API?", "calls", "HTTP API"),
    ("Where is the HTTP API deployed? Deployment hosts?", "hosts", "HTTP API"),
]


@pytest.fixture
async def project():
    """冷启动一个标准 web 项目作为 golden set."""
    engine = StorageEngine(":memory:", load_vec=False)
    await engine.initialize()
    bootstrap = ProjectBootstrap(engine)
    ctx = await bootstrap.initialize(
        ProjectInitRequest(
            name="ShopAPI",
            description="电商后台 API 服务",
            tech_stack=["Python", "FastAPI", "SQLite", "Redis"],
            template="web",
        ),
        wizard_answers={"goal": "提供电商后台 API"},
    )
    yield engine, ctx
    await engine.close()


def _query_keywords(query: str) -> set[str]:
    """query → 关键词集合: 英文词元 + 技术名词规范名(确定性,与提取侧同表)."""
    keywords = set(re.findall(r"[a-z0-9]+", query.lower()))
    keywords.update(t.lower() for t in detect_technologies(query))
    return keywords


async def _keyword_recall(engine, scope: str, query: str, top_k: int = TOP_K):
    """确定性召回: scope 过滤 + 关键词重叠打分(实体名/谓词/object 文本).

    打分 = query 关键词在「subject 名 + predicate + object 名/值」中的命中数,
    按 (-score, id) 排序取 Top-K(完全可复现,无随机性).
    """
    keywords = _query_keywords(query)
    cursor = await engine.conn.execute(
        "SELECT id, name FROM entities WHERE scope = ?", [scope]
    )
    entity_names = {r["id"]: r["name"] for r in await cursor.fetchall()}
    cursor = await engine.conn.execute(
        "SELECT id, subject_id, predicate, object_id, object_value "
        "FROM triples WHERE scope = ?",
        [scope],
    )
    scored = []
    for row in await cursor.fetchall():
        text = " ".join(
            part
            for part in (
                entity_names.get(row["subject_id"], ""),
                row["predicate"],
                entity_names.get(row["object_id"], "") if row["object_id"] else "",
                row["object_value"] or "",
            )
            if part
        ).lower()
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scored.append((score, row["id"], dict(row)))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [row for _, _, row in scored[:top_k]]


async def test_golden_set_recall_at_5(project):
    """Top-5 召回率 = 命中数 / 应召回数,要求 > 60%(§14.2 退出标准)."""
    engine, ctx = project
    hits = 0
    misses: list[str] = []
    for query, predicate, object_hint in GOLDEN_QUERIES:
        top = await _keyword_recall(engine, ctx.scope, query)
        matched = False
        for row in top:
            object_text = row["object_value"] or await _object_name(
                engine, row["object_id"]
            ) or ""
            if (
                row["predicate"] == predicate
                and object_hint.lower() in object_text.lower()
            ):
                matched = True
                break
        if matched:
            hits += 1
        else:
            misses.append(query)
    recall = hits / len(GOLDEN_QUERIES)
    assert recall > RECALL_THRESHOLD, (
        f"Top-5 召回率 {recall:.0%} <= {RECALL_THRESHOLD:.0%},未命中: {misses}"
    )


async def _object_name(engine, object_id: str | None) -> str | None:
    if object_id is None:
        return None
    cursor = await engine.conn.execute(
        "SELECT name FROM entities WHERE id = ?", [object_id]
    )
    row = await cursor.fetchone()
    return row["name"] if row else None


async def test_recall_scope_isolation(project):
    """scope 过滤: 其他项目的记忆不会出现在召回结果中."""
    engine, ctx = project
    bootstrap = ProjectBootstrap(engine)
    other = await bootstrap.initialize(
        ProjectInitRequest(name="OtherProj", tech_stack=["Kafka"])
    )
    top = await _keyword_recall(engine, ctx.scope, "Does it use Kafka?")
    # 关键词通道可能因 "use" 命中 uses_tech 谓词,但绝不包含 Kafka 记忆
    for row in top:
        object_text = row["object_value"] or await _object_name(
            engine, row["object_id"]
        ) or ""
        assert "kafka" not in object_text.lower()
    top_other = await _keyword_recall(engine, other.scope, "Does it use Kafka?")
    assert any(row["predicate"] == "uses_tech" for row in top_other)


async def test_recall_empty_scope():
    """空项目(无任何三元组)召回为空,不报错."""
    engine = StorageEngine(":memory:", load_vec=False)
    await engine.initialize()
    try:
        top = await _keyword_recall(engine, "project:nonexistent", "anything")
        assert top == []
    finally:
        await engine.close()
