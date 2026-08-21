"""Onboarding wizard(规则版)— §9.5.1 初始化向导.

MVP 非交互: 上层(Middleware / CLI)收集结构化答案 dict,这里负责
问题清单定义与「答案 → 种子」的转换.交互式向导留到服务暴露层(ADR-013).

答案 dict 约定(键即 WizardQuestion.key):
    {
        "goal": "提供电商后台 API",            # str
        "tech_stack": ["Python", "FastAPI"],   # str 或 list[str]
        "constraints": "必须单机运行",           # str 或 list[str]
        "stage": "prototype",                  # str
    }
"""

from __future__ import annotations

from dataclasses import dataclass

from .seeds import (
    STATE_RELATION_TYPE,
    EntitySeed,
    ExtractionResult,
    TripleSeed,
)


@dataclass(frozen=True)
class WizardQuestion:
    """向导问题."""

    key: str  # 答案 dict 的键;对应谓词 project_{key}
    prompt: str
    required: bool = True


# 5 个核心问题(§9.5.2 步骤 1 + 04-layer3 §3.5)
CORE_QUESTIONS: tuple[WizardQuestion, ...] = (
    WizardQuestion("project_name", "项目名称是什么?"),
    WizardQuestion("goal", "项目的主要目标是什么?"),
    WizardQuestion("tech_stack", "核心技术栈有哪些?"),
    WizardQuestion("constraints", "有哪些关键约束?"),
    WizardQuestion("stage", "项目当前处于什么阶段?"),
)

# tech_stack 走实体 + uses_tech 三元组,不写状态谓词
_STATE_QUESTION_KEYS = ("goal", "constraints", "stage")


def tech_seeds(project_name: str, tech_stack: list[str]) -> ExtractionResult:
    """技术栈 → tech 实体 + uses_tech 三元组(供向导与 bootstrap 复用)."""
    result = ExtractionResult()
    for tech in tech_stack:
        name = str(tech).strip()
        if not name:
            continue
        result.entities.append(EntitySeed(name=name, entity_id=f"tech:{name.lower()}"))
        result.triples.append(
            TripleSeed(subject=project_name, predicate="uses_tech", object_name=name)
        )
    return result


def process_answers(project_name: str, answers: dict) -> ExtractionResult:
    """把结构化向导答案转换为种子.

    - goal / constraints / stage → project_state 三元组(触发 §6.3 当前状态汇总)
    - tech_stack → tech 实体 + uses_tech 三元组
    - 未知键与空值忽略
    """
    result = ExtractionResult()
    tech_value = answers.get("tech_stack")
    if tech_value:
        techs = [tech_value] if isinstance(tech_value, str) else list(tech_value)
        result.merge(tech_seeds(project_name, techs))

    for key in _STATE_QUESTION_KEYS:
        value = answers.get(key)
        if not value:
            continue
        values = [value] if isinstance(value, str) else list(value)
        for item in values:
            item = str(item).strip()
            if item:
                result.triples.append(
                    TripleSeed(
                        subject=project_name,
                        predicate=f"project_{key}",
                        object_value=item,
                        relation_type=STATE_RELATION_TYPE,
                    )
                )
    return result


__all__ = [
    "CORE_QUESTIONS",
    "WizardQuestion",
    "process_answers",
    "tech_seeds",
]
