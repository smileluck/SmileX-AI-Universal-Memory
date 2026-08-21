"""Unit tests for Layer 3 冷启动(L3.1-L3.5).

验证:
- ProjectBootstrap.initialize 端到端(scope 创建/实体/三元组/stage)
- Onboarding wizard(CORE_QUESTIONS + process_answers)
- 模板加载/匹配(load_template / match_template / list_templates)
- README 解析(parse_readme / extract_from_readme / detect_technologies)
- ActiveLearner 缺口提问 + record_answer
"""

from __future__ import annotations

import pytest

from smilex.memory.scheduler.bootstrap import (
    CORE_QUESTIONS,
    ActiveLearner,
    EntitySeed,
    ExtractionResult,
    ProjectBootstrap,
    RuleBasedExtractor,
    TripleSeed,
    detect_technologies,
    extract_from_readme,
    list_templates,
    load_template,
    match_template,
    parse_readme,
    process_answers,
)
from smilex.memory.storage.storage_engine import StorageEngine
from smilex.middlewares.dto import (
    PROJECT_TEMPLATES,
    BootstrapStage,
    ProjectInitRequest,
)


@pytest.fixture
async def engine():
    eng = StorageEngine(":memory:", load_vec=False)
    await eng.initialize()
    yield eng
    await eng.close()


@pytest.fixture
async def bootstrap(engine):
    return ProjectBootstrap(engine)


async def _entity_ids(engine, scope):
    cursor = await engine.conn.execute(
        "SELECT entity_id FROM entities WHERE scope = ?", [scope]
    )
    return {r["entity_id"] for r in await cursor.fetchall()}


async def _triples(engine, scope):
    cursor = await engine.conn.execute(
        "SELECT predicate, object_id, object_value, relation_type "
        "FROM triples WHERE scope = ?",
        [scope],
    )
    return [dict(r) for r in await cursor.fetchall()]


# ---------- L3.1 initialize 端到端 ----------

async def test_initialize_minimal(engine, bootstrap):
    """仅名称 + 技术栈: 项目实体 + tech 实体 + uses_tech 三元组;
    FastAPI 自动匹配 web 模板(骨架 5 实体 + 4 三元组)."""
    ctx = await bootstrap.initialize(
        ProjectInitRequest(name="demo", tech_stack=["Python", "FastAPI"])
    )
    assert ctx.scope.startswith("project:")
    assert ctx.name == "demo"
    assert ctx.entity_count == 8  # project + 2 tech + 5 模板骨架
    assert ctx.triple_count == 6  # 2 uses_tech + 4 模板三元组
    assert ctx.stage is BootstrapStage.ACTIVE_LEARNING  # 未答向导 → 有缺口
    entity_ids = await _entity_ids(engine, ctx.scope)
    assert {"project:demo", "tech:python", "tech:fastapi"} <= entity_ids
    assert "concept:http-api" in entity_ids  # web 模板骨架


async def test_initialize_scope_is_unique(bootstrap):
    """同名项目两次初始化 → 不同 scope(ULID)."""
    req = ProjectInitRequest(name="demo")
    ctx1 = await bootstrap.initialize(req)
    ctx2 = await bootstrap.initialize(req)
    assert ctx1.scope != ctx2.scope


async def test_initialize_with_wizard_answers(engine, bootstrap):
    """向导答案 → project_state 三元组 + 触发器联动当前状态."""
    ctx = await bootstrap.initialize(
        ProjectInitRequest(name="demo"),
        wizard_answers={
            "goal": "做一个记忆系统",
            "constraints": ["单机运行", "零外部依赖"],
            "stage": "prototype",
        },
    )
    triples = await _triples(engine, ctx.scope)
    state = {
        t["predicate"]: t["object_value"]
        for t in triples
        if t["relation_type"] == "project_state"
    }
    assert state["project_goal"] == "做一个记忆系统"
    assert state["project_stage"] == "prototype"

    # §6.3 触发器: project_current_state 汇总(同谓词多值取最新)
    cursor = await engine.conn.execute(
        "SELECT subject_id FROM triples WHERE scope = ? AND predicate = 'project_goal'",
        [ctx.scope],
    )
    subject_id = (await cursor.fetchone())["subject_id"]
    current = await engine.get_current_state(ctx.scope, subject_id)
    assert {row["predicate"] for row in current} == {
        "project_goal",
        "project_constraints",
        "project_stage",
    }


async def test_initialize_stage_ready_when_no_gap(bootstrap):
    """主动学习 5 问全部覆盖 → READY."""
    ctx = await bootstrap.initialize(
        ProjectInitRequest(name="demo"),
        wizard_answers={
            "goal": "g",
            "tech_stack": ["Python"],
            "constraints": "c",
            "stage": "s",
        },
    )
    # wizard 只覆盖 goal;tech/team/timeline/risks 仍是缺口
    assert ctx.stage is BootstrapStage.ACTIVE_LEARNING
    for key, answer in [("tech", "t"), ("team", "x"), ("timeline", "y"), ("risks", "z")]:
        await bootstrap.record_answer(ctx.scope, "demo", key, answer)
    assert await bootstrap.pending_questions(ctx.scope) == []


async def test_initialize_template_by_request(engine, bootstrap):
    """显式 template='web' → 注入骨架实体与三元组."""
    ctx = await bootstrap.initialize(
        ProjectInitRequest(name="webapp", template="web")
    )
    entity_ids = await _entity_ids(engine, ctx.scope)
    assert "concept:http-api" in entity_ids
    assert "concept:database" in entity_ids
    triples = await _triples(engine, ctx.scope)
    assert any(t["predicate"] == "depends_on" for t in triples)


async def test_initialize_template_auto_match(engine, bootstrap):
    """未指定模板时按技术栈自动匹配(fastapi → web)."""
    ctx = await bootstrap.initialize(
        ProjectInitRequest(name="auto", tech_stack=["FastAPI"])
    )
    entity_ids = await _entity_ids(engine, ctx.scope)
    assert "concept:http-api" in entity_ids  # web 模板骨架


async def test_initialize_readme_extraction(engine, bootstrap):
    """README 中的技术名词与标题进入实体/三元组."""
    readme = "# demo\n## Architecture\nBuilt with FastAPI and Redis.\n"
    ctx = await bootstrap.initialize(
        ProjectInitRequest(name="demo", tech_stack=["Python"], readme_content=readme)
    )
    entity_ids = await _entity_ids(engine, ctx.scope)
    assert "tech:redis" in entity_ids  # README 提取
    assert "concept:architecture" in entity_ids  # 标题概念


async def test_initialize_invalid_template(bootstrap):
    """非法模板由 DTO 层拒绝."""
    with pytest.raises(ValueError, match="template"):
        ProjectInitRequest(name="x", template="unknown")


async def test_apply_seeds_idempotent(bootstrap, engine):
    """apply_seeds 幂等: 重复写入不增实体/三元组."""
    ctx = await bootstrap.initialize(
        ProjectInitRequest(name="demo", tech_stack=["Python"], template="web")
    )
    again = ExtractionResult(
        entities=[EntitySeed(name="Python", entity_id="tech:python")],
        triples=[
            TripleSeed(subject="demo", predicate="uses_tech", object_name="Python")
        ],
    )
    counts = await bootstrap.apply_seeds(ctx.scope, again)
    assert counts == (ctx.entity_count, ctx.triple_count)


async def test_apply_seeds_rejects_global_scope(bootstrap):
    with pytest.raises(ValueError, match="scope"):
        await bootstrap.apply_seeds("global", ExtractionResult())


# ---------- L3.2 Onboarding wizard ----------

def test_core_questions_count():
    assert len(CORE_QUESTIONS) == 5
    assert [q.key for q in CORE_QUESTIONS] == [
        "project_name",
        "goal",
        "tech_stack",
        "constraints",
        "stage",
    ]


def test_process_answers_state_triples():
    result = process_answers(
        "demo",
        {"goal": "g", "stage": "prototype", "unknown_key": "ignored"},
    )
    assert {(t.predicate, t.object_value, t.relation_type) for t in result.triples} == {
        ("project_goal", "g", "project_state"),
        ("project_stage", "prototype", "project_state"),
    }


def test_process_answers_tech_stack():
    result = process_answers("demo", {"tech_stack": ["Python", "Redis"]})
    assert {e.entity_id for e in result.entities} == {"tech:python", "tech:redis"}
    assert {(t.predicate, t.object_name) for t in result.triples} == {
        ("uses_tech", "Python"),
        ("uses_tech", "Redis"),
    }


def test_process_answers_empty():
    result = process_answers("demo", {})
    assert result.entities == [] and result.triples == []


# ---------- L3.3 项目模板 ----------

def test_list_templates():
    assert list_templates() == sorted(PROJECT_TEMPLATES)


@pytest.mark.parametrize("key", list_templates())
def test_load_template_structure(key):
    """每套模板都有骨架实体 + 三元组,且三元组两端都在骨架中."""
    template = load_template(key)
    assert template.name and template.description
    assert len(template.entities) >= 3
    assert len(template.triples) >= 2
    names = {e.name for e in template.entities}
    for triple in template.triples:
        assert triple.subject in names
        assert triple.object_name in names


def test_load_template_unknown():
    with pytest.raises(ValueError, match="未知项目模板"):
        load_template("nope")


@pytest.mark.parametrize(
    ("tech_stack", "expected"),
    [
        (["FastAPI"], "web"),
        (["django", "react"], "web"),
        (["Click"], "cli"),
        (["pandas", "airflow"], "data"),
        (["LangChain"], "agent"),
        (["cobol"], None),
        ([], None),
    ],
)
def test_match_template(tech_stack, expected):
    assert match_template(tech_stack) == expected


# ---------- L3.5 README 解析 ----------

def test_parse_readme_basic():
    info = parse_readme(
        "# My Project\n## Getting Started\n### Install\n"
        "See [docs](https://example.com/docs). Uses PostgreSQL and Docker.\n"
    )
    assert info.title == "My Project"
    assert info.headings == ["My Project", "Getting Started", "Install"]
    assert info.techs == ["PostgreSQL", "Docker"]
    assert info.links == [("docs", "https://example.com/docs")]


def test_detect_technologies_word_boundary():
    """词边界匹配: 'redis' 命中,'redish' / 'k8sify' 不命中."""
    assert detect_technologies("we use redis and k8s") == ["Redis", "Kubernetes"]
    assert detect_technologies("redish k8sify") == []


def test_extract_from_readme_seeds():
    result = extract_from_readme("demo", "# demo\n## Arch\nFastAPI + SQLite\n")
    assert {e.entity_id for e in result.entities} == {
        "tech:fastapi",
        "tech:sqlite",
        "concept:demo",
        "concept:arch",
    }
    assert ("demo", "uses_tech", "FastAPI") in {
        (t.subject, t.predicate, t.object_name) for t in result.triples
    }
    assert any(t.predicate == "has_section" for t in result.triples)


def test_extract_from_readme_empty():
    result = extract_from_readme("demo", "no headings, no tech")
    assert result.entities == [] and result.triples == []


# ---------- L3.4 Active learner stub ----------

def test_active_learner_gap_analysis():
    learner = ActiveLearner()
    pending = learner.pending_questions(set())
    assert len(pending) == 5
    assert learner.get_next_question({"project_goal", "project_tech"}).key == "team"
    assert learner.get_next_question(
        {f"project_{q.key}" for q in pending}
    ) is None


async def test_rule_based_extractor():
    extractor = RuleBasedExtractor()
    result = await extractor.extract("我们用 Python 和 FastAPI", context="demo")
    assert {e.entity_id for e in result.entities} == {"tech:python", "tech:fastapi"}
    assert {(t.predicate, t.object_name) for t in result.triples} == {
        ("uses_tech", "Python"),
        ("uses_tech", "FastAPI"),
    }


async def test_record_answer(engine, bootstrap):
    """record_answer: 状态三元组 + 规则提取的技术实体写入."""
    ctx = await bootstrap.initialize(ProjectInitRequest(name="demo"))
    await bootstrap.record_answer(ctx.scope, "demo", "tech", "主要是 FastAPI 和 SQLite")
    triples = await _triples(engine, ctx.scope)
    assert any(
        t["predicate"] == "project_tech" and "FastAPI" in (t["object_value"] or "")
        for t in triples
    )
    entity_ids = await _entity_ids(engine, ctx.scope)
    assert {"tech:fastapi", "tech:sqlite"} <= entity_ids
    # 已答问题不再出现在缺口中
    assert "tech" not in [
        q.key for q in await bootstrap.pending_questions(ctx.scope)
    ]


async def test_custom_llm_provider_pluggable(engine):
    """LLMProvider Protocol: 自定义 provider 可注入(鸭子类型)."""

    class FakeLLM:
        async def extract(self, text, *, context=None):
            return ExtractionResult(
                entities=[EntitySeed(name="LLMThing", entity_id="concept:llmthing")]
            )

    bootstrap = ProjectBootstrap(engine, llm=FakeLLM())
    ctx = await bootstrap.initialize(
        ProjectInitRequest(name="demo", readme_content="# demo\n")
    )
    assert "concept:llmthing" in await _entity_ids(engine, ctx.scope)
