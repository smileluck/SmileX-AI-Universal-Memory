"""README 解析 + 规则版实体提取 — §9.5.2 步骤 1 / L3.5.

规则版(零 LLM 依赖,EXECUTION_PLAN_GAPS §3.6 兜底):
- Markdown 标题(#{1,3})→ concept 实体 + has_section 三元组
- 常见技术名词表(TECH_KEYWORDS)→ tech 实体 + uses_tech 三元组
- 链接 [text](url) → 记录到 ReadmeInfo(不生成实体,P1 批量导入再用)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .seeds import EntitySeed, ExtractionResult, TripleSeed

# 常见技术名词表: 规范名 → 别名(全部小写,词边界匹配)
TECH_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Python": ("python",),
    "FastAPI": ("fastapi",),
    "Django": ("django",),
    "Flask": ("flask",),
    "PyTorch": ("pytorch",),
    "TensorFlow": ("tensorflow",),
    "LangChain": ("langchain",),
    "SQLite": ("sqlite",),
    "PostgreSQL": ("postgresql", "postgres"),
    "MySQL": ("mysql",),
    "Redis": ("redis",),
    "Kafka": ("kafka",),
    "MongoDB": ("mongodb",),
    "JavaScript": ("javascript",),
    "TypeScript": ("typescript",),
    "React": ("react",),
    "Vue": ("vue", "vue.js"),
    "Node.js": ("node.js", "nodejs"),
    "Docker": ("docker",),
    "Kubernetes": ("kubernetes", "k8s"),
}

_HEADING_RE = re.compile(r"^#{1,3}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")

# 单个项目从 README 提取的标题实体上限(防止巨型 README 灌入过多概念)
_MAX_HEADING_ENTITIES = 20


@dataclass
class ReadmeInfo:
    """README 解析结果."""

    title: str | None = None  # 首个 H1
    headings: list[str] = field(default_factory=list)
    techs: list[str] = field(default_factory=list)  # 规范名,按出现顺序去重
    links: list[tuple[str, str]] = field(default_factory=list)  # (text, url)


def detect_technologies(text: str) -> list[str]:
    """从任意文本中识别常见技术名词,返回规范名列表(按出现顺序去重)."""
    lowered = text.lower()
    found: list[str] = []
    for canonical, aliases in TECH_KEYWORDS.items():
        for alias in aliases:
            if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", lowered):
                found.append(canonical)
                break
    return found


def parse_readme(content: str) -> ReadmeInfo:
    """解析 README markdown,返回标题/技术名词/链接."""
    headings = _HEADING_RE.findall(content)
    return ReadmeInfo(
        title=headings[0] if headings else None,
        headings=headings,
        techs=detect_technologies(content),
        links=_LINK_RE.findall(content),
    )


def extract_from_readme(project_name: str, content: str) -> ExtractionResult:
    """README → 种子: 技术名词 + 章节标题(概念实体)."""
    info = parse_readme(content)
    result = ExtractionResult()
    for tech in info.techs:
        result.entities.append(
            EntitySeed(name=tech, entity_id=f"tech:{tech.lower()}")
        )
        result.triples.append(
            TripleSeed(subject=project_name, predicate="uses_tech", object_name=tech)
        )
    for heading in info.headings[:_MAX_HEADING_ENTITIES]:
        result.entities.append(EntitySeed(name=heading, entity_type="concept"))
        result.triples.append(
            TripleSeed(
                subject=project_name, predicate="has_section", object_name=heading
            )
        )
    return result


__all__ = [
    "TECH_KEYWORDS",
    "ReadmeInfo",
    "detect_technologies",
    "extract_from_readme",
    "parse_readme",
]
