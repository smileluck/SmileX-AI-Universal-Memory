"""主动学习 stub(无 LLM 版)— §9.5.1 / 04-layer3 §3.5 / GAPS §3.6.

LLM provider 未定 → MVP 用规则兜底:
- LLMProvider Protocol: 接口留好,P1 接 OpenAI 兼容 Function Calling(§2.4)
- RuleBasedExtractor: 关键词/正则提取(复用 TECH_KEYWORDS),零依赖默认实现
- ActiveLearner: 基于知识缺口的引导提问(已有谓词 → 跳过对应问题)
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .onboarding import WizardQuestion, tech_seeds
from .readme_parser import detect_technologies
from .seeds import STATE_RELATION_TYPE, EntitySeed, ExtractionResult, TripleSeed


@runtime_checkable
class LLMProvider(Protocol):
    """LLM 提取协议(P1 接入 OpenAI 兼容 API,MVP 用 RuleBasedExtractor)."""

    async def extract(self, text: str, *, context: str | None = None) -> ExtractionResult:
        """从文本提取实体与三元组种子."""
        ...


class RuleBasedExtractor:
    """规则兜底提取器: 技术名词关键词匹配 + 全文留作 object_value."""

    async def extract(self, text: str, *, context: str | None = None) -> ExtractionResult:
        result = ExtractionResult()
        if context:
            result.merge(tech_seeds(context, detect_technologies(text)))
        else:
            for tech in detect_technologies(text):
                result.entities.append(
                    EntitySeed(name=tech, entity_id=f"tech:{tech.lower()}")
                )
        return result


# 主动学习引导问题(04-layer3 §3.5 QUESTION_TEMPLATES);谓词为 project_{key}
LEARNING_QUESTIONS: tuple[WizardQuestion, ...] = (
    WizardQuestion("goal", "项目的主要目标是什么?"),
    WizardQuestion("tech", "用了哪些核心技术?"),
    WizardQuestion("team", "团队结构是怎样的?", required=False),
    WizardQuestion("timeline", "项目预期什么时候完成?", required=False),
    WizardQuestion("risks", "目前最大的风险是什么?", required=False),
)


class ActiveLearner:
    """主动学习: 基于已有知识的缺口选择下一个问题,并记录答案."""

    def __init__(self, provider: LLMProvider | None = None) -> None:
        self.provider: LLMProvider = provider or RuleBasedExtractor()

    def pending_questions(self, known_predicates: set[str]) -> list[WizardQuestion]:
        """返回尚未收集的问题(谓词 project_{key} 未出现即视为缺口)."""
        return [
            q for q in LEARNING_QUESTIONS if f"project_{q.key}" not in known_predicates
        ]

    def get_next_question(self, known_predicates: set[str]) -> WizardQuestion | None:
        """基于知识缺口选择下一个问题;全部已收集返回 None."""
        pending = self.pending_questions(known_predicates)
        return pending[0] if pending else None

    async def answer_to_seeds(
        self, project_name: str, question_key: str, answer: str
    ) -> ExtractionResult:
        """用户答案 → 种子: 状态三元组 + LLM/规则提取的技术实体."""
        result = ExtractionResult()
        result.triples.append(
            TripleSeed(
                subject=project_name,
                predicate=f"project_{question_key}",
                object_value=answer,
                relation_type=STATE_RELATION_TYPE,
            )
        )
        result.merge(await self.provider.extract(answer, context=project_name))
        return result


__all__ = [
    "LEARNING_QUESTIONS",
    "ActiveLearner",
    "LLMProvider",
    "RuleBasedExtractor",
]
