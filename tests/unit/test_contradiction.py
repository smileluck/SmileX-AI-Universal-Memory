"""Unit tests for P1-b Layer 5 同步矛盾检测(L5.1)— ContradictionDetector.

验证:
- 四维规则: VALUE / NUMERIC / TEMPORAL / CAUSAL(同键因果 + 反向因果)
- check_new 写入时同步检测
- 退出标准(§14.4): 标注数据集(27 样本,13 正例 + 13 负例 + 1 复合)准确率 > 90%
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from smilex.memory.models import ConflictType, MemoryScope, Triple
from smilex.memory.quality import (
    ContradictionDetector,
    ContradictionKind,
)
from smilex.memory.storage.storage_engine import StorageEngine

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _d(y: int, m: int = 1) -> datetime:
    return datetime(y, m, 1, tzinfo=UTC)


def make_triple(
    subject: str,
    predicate: str,
    obj: str,
    *,
    rt: str = "semantic",
    by_id: bool = False,
    vf: datetime | None = None,
    vt: datetime | None = None,
) -> Triple:
    return Triple(
        triple_id=f"{subject}|{predicate}|{obj}|{vf}|{vt}",
        subject_id=subject,
        predicate=predicate,
        object_id=obj if by_id else None,
        object_value=None if by_id else obj,
        scope=MemoryScope.PROJECT,
        valid_from=vf or _BASE,
        valid_to=vt,
        relation_type=rt,
    )


@pytest.fixture
async def engine():
    eng = StorageEngine(":memory:", load_vec=False)
    await eng.initialize()
    yield eng
    await eng.close()


async def _write_all(engine, scope_id: str, triples: list[Triple]) -> None:
    for t in triples:
        await engine.write_triple(t, scope_id=scope_id)


# ---------- 标注数据集(正例 13 + 负例 13 + 复合 1) ----------
# 每条: (用例名, triple 列表, 期望矛盾维度集合)
# 注: project_state/task_status/config 覆写会被 §6.3 触发器自动 LWW(旧值置
# valid_to),scan 时值冲突正值用枚举谓词(semantic 边不经过触发器);状态覆写
# 的写入时检测见 test_check_new_detects_value_conflict

LABELED_SAMPLES: list[tuple[str, list[Triple], set[ContradictionKind]]] = [
    # ===== 正例: 应检出 =====
    ("value_enum_color", [
        make_triple("s1", "color", "red"),
        make_triple("s1", "color", "blue"),
    ], {ContradictionKind.VALUE}),
    ("value_enum_status", [
        make_triple("s1", "status", "active"),
        make_triple("s1", "status", "deprecated"),
    ], {ContradictionKind.VALUE}),
    ("value_enum_cn", [
        make_triple("s1", "状态", "进行中"),
        make_triple("s1", "状态", "已完成"),
    ], {ContradictionKind.VALUE}),
    ("value_enum_priority", [
        make_triple("s1", "priority", "high"),
        make_triple("s1", "priority", "low"),
    ], {ContradictionKind.VALUE}),
    ("value_enum_category", [
        make_triple("s1", "category", "frontend"),
        make_triple("s1", "category", "backend"),
    ], {ContradictionKind.VALUE}),
    ("numeric_team_size", [
        make_triple("s1", "team_size", "3 人"),
        make_triple("s1", "team_size", "5 人"),
    ], {ContradictionKind.NUMERIC}),
    ("numeric_latency", [
        make_triple("s1", "latency", "100ms"),
        make_triple("s1", "latency", "200ms"),
    ], {ContradictionKind.NUMERIC}),
    ("numeric_percent", [
        make_triple("s1", "coverage", "80%"),
        make_triple("s1", "coverage", "30%"),
    ], {ContradictionKind.NUMERIC}),
    ("temporal_open_overlap", [
        make_triple("s1", "role", "engineer", vf=_d(2020), vt=_d(2021, 6)),
        make_triple("s1", "role", "lead", vf=_d(2021)),
    ], {ContradictionKind.TEMPORAL}),
    ("temporal_point_open", [
        make_triple("s1", "version", "v1", vf=_d(2022), vt=_d(2023)),
        make_triple("s1", "version", "v2", vf=_d(2022, 6)),
    ], {ContradictionKind.TEMPORAL}),
    ("temporal_both_closed", [
        make_triple("s1", "owner", "alice", vf=_d(2020), vt=_d(2022)),
        make_triple("s1", "owner", "bob", vf=_d(2021), vt=_d(2023)),
    ], {ContradictionKind.TEMPORAL}),
    ("causal_same_key", [
        make_triple("entA", "causes", "entB", rt="causal", by_id=True),
        make_triple("entA", "causes", "entC", rt="causal", by_id=True),
    ], {ContradictionKind.CAUSAL}),
    ("causal_inverse", [
        make_triple("entA", "导致", "entB", rt="causal", by_id=True),
        make_triple("entB", "导致", "entA", rt="causal", by_id=True),
    ], {ContradictionKind.CAUSAL}),
    # ===== 负例: 不应检出 =====
    ("neg_same_fact", [
        make_triple("s1", "color", "red"),
        make_triple("s1", "color", "red"),
    ], set()),
    ("neg_state_overwrite_lww", [
        # §6.3 触发器 LWW: 旧值被置 valid_to,区间相邻不重叠 → 非矛盾
        make_triple("s1", "deploy_env", "dev", rt="project_state"),
        make_triple("s1", "deploy_env", "prod", rt="project_state"),
    ], set()),
    ("neg_numeric_within_tolerance", [
        make_triple("s1", "qps", "100"),
        make_triple("s1", "qps", "105"),
    ], set()),
    ("neg_numeric_equal_diff_text", [
        make_triple("s1", "team", "团队 5 人"),
        make_triple("s1", "team", "5 人团队"),
    ], set()),
    ("neg_multivalue_semantic", [
        make_triple("s1", "uses_tech", "Python"),
        make_triple("s1", "uses_tech", "Redis"),
    ], set()),
    ("neg_multivalue_spatial", [
        make_triple("s1", "located_in", "北京", rt="spatial"),
        make_triple("s1", "located_in", "上海", rt="spatial"),
    ], set()),
    ("neg_diff_predicate", [
        make_triple("s1", "color", "red"),
        make_triple("s1", "size", "red"),
    ], set()),
    ("neg_diff_subject", [
        make_triple("s1", "color", "red"),
        make_triple("s2", "color", "blue"),
    ], set()),
    ("neg_temporal_adjacent", [
        make_triple("s1", "owner", "alice", vf=_d(2020), vt=_d(2021)),
        make_triple("s1", "owner", "bob", vf=_d(2021), vt=_d(2022)),
    ], set()),
    ("neg_temporal_disjoint_open", [
        make_triple("s1", "owner", "alice", vf=_d(2020), vt=_d(2021)),
        make_triple("s1", "owner", "bob", vf=_d(2022)),
    ], set()),
    ("neg_temporal_same_value", [
        make_triple("s1", "owner", "alice", vf=_d(2020), vt=_d(2021)),
        make_triple("s1", "owner", "alice", vf=_d(2021), vt=_d(2022)),
    ], set()),
    ("neg_causal_same_value", [
        make_triple("entA", "causes", "entB", rt="causal", by_id=True),
        make_triple("entA", "causes", "entB", rt="causal", by_id=True),
    ], set()),
    ("neg_causal_diff_key", [
        make_triple("entA", "causes", "entB", rt="causal", by_id=True),
        make_triple("entC", "causes", "entD", rt="causal", by_id=True),
    ], set()),
    # ===== 复合: 一个 scope 内两类矛盾 =====
    ("compound_temporal_value", [
        make_triple("s1", "role", "engineer", vf=_d(2020), vt=_d(2021, 6)),
        make_triple("s1", "role", "lead", vf=_d(2021)),
        make_triple("s2", "color", "red"),
        make_triple("s2", "color", "blue"),
    ], {ContradictionKind.TEMPORAL, ContradictionKind.VALUE}),
]


async def test_scan_accuracy_above_90_percent(engine):
    """退出标准(§14.4): 标注数据集矛盾检测准确率 > 90%."""
    detector = ContradictionDetector(engine)
    failures: list[str] = []
    for i, (name, triples, expected) in enumerate(LABELED_SAMPLES):
        scope_id = f"acc{i:02d}"
        await _write_all(engine, scope_id, triples)
        report = await detector.scan(f"project:{scope_id}")
        detected = {c.kind for c in report.contradictions}
        if detected != expected:
            failures.append(f"{name}: 期望 {sorted(expected)},实得 {sorted(detected)}")
    accuracy = (len(LABELED_SAMPLES) - len(failures)) / len(LABELED_SAMPLES)
    assert accuracy > 0.9, f"准确率 {accuracy:.1%} 未达标;失败用例:\n" + "\n".join(failures)


async def test_scan_report_fields(engine):
    """报告字段: 扫描条数/维度统计/ConflictType 映射/耗时."""
    await _write_all(engine, "r1", [
        make_triple("s1", "color", "red"),
        make_triple("s1", "color", "blue"),
        make_triple("s2", "uses_tech", "Python"),
    ])
    detector = ContradictionDetector(engine)
    report = await detector.scan("project:r1")
    assert report.scanned_triples == 3
    assert report.count_by_kind() == {ContradictionKind.VALUE: 1}
    contradiction = report.contradictions[0]
    assert contradiction.conflict_type is ConflictType.WRITE_WRITE
    assert contradiction.predicate == "color"
    assert {contradiction.existing_object, contradiction.new_object} == {"red", "blue"}
    assert report.elapsed_ms >= 0


async def test_scan_causal_maps_to_causal_contradiction(engine):
    """因果矛盾复用 ConflictType.CAUSAL_CONTRADICTION."""
    await _write_all(engine, "r2", [
        make_triple("entA", "导致", "entB", rt="causal", by_id=True),
        make_triple("entB", "导致", "entA", rt="causal", by_id=True),
    ])
    detector = ContradictionDetector(engine)
    report = await detector.scan("project:r2")
    assert len(report.contradictions) == 1
    assert report.contradictions[0].conflict_type is ConflictType.CAUSAL_CONTRADICTION
    assert "反向因果" in report.contradictions[0].message


async def test_check_new_detects_value_conflict(engine):
    """写入时同步检测: 与现行单值状态冲突."""
    await _write_all(engine, "r3", [
        make_triple("s1", "deploy_env", "dev", rt="project_state"),
    ])
    detector = ContradictionDetector(engine)
    found = await detector.check_new(
        scope="project:r3",
        subject_id="s1",
        predicate="deploy_env",
        object_value="prod",
        relation_type="project_state",
    )
    assert [c.kind for c in found] == [ContradictionKind.VALUE]


async def test_check_new_ignores_multivalue(engine):
    """多值谓词(uses_tech)不同取值不算矛盾."""
    await _write_all(engine, "r4", [make_triple("s1", "uses_tech", "Python")])
    detector = ContradictionDetector(engine)
    found = await detector.check_new(
        scope="project:r4",
        subject_id="s1",
        predicate="uses_tech",
        object_value="Redis",
    )
    assert found == []


async def test_check_new_inverse_causal(engine):
    """写入时反向因果: 已有 (B 导致 A),新写 (A 导致 B) 检出矛盾."""
    await _write_all(engine, "r5", [
        make_triple("entB", "导致", "entA", rt="causal", by_id=True),
    ])
    detector = ContradictionDetector(engine)
    found = await detector.check_new(
        scope="project:r5",
        subject_id="entA",
        predicate="导致",
        object_id="entB",
        relation_type="causal",
    )
    assert [c.kind for c in found] == [ContradictionKind.CAUSAL]


async def test_numeric_tolerance_configurable(engine):
    """容差可调: 20% 容差下 100 vs 105 仍不算矛盾,100 vs 130 算."""
    await _write_all(engine, "r6", [
        make_triple("s1", "qps", "100"),
        make_triple("s1", "qps", "130"),
        make_triple("s2", "qps", "100"),
        make_triple("s2", "qps", "115"),
    ])
    detector = ContradictionDetector(engine, numeric_tolerance=0.2)
    report = await detector.scan("project:r6")
    # 130: 30/130 ≈ 23% > 20% → NUMERIC;115: 15/115 ≈ 13% ≤ 20% → 不报
    assert [c.subject_id for c in report.contradictions] == ["s1"]
