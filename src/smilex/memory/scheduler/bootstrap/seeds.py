"""冷启动种子数据结构 — EntitySeed / TripleSeed / ExtractionResult.

Per 04-layer3-scheduling.md §3.4: 冷启动各机制(向导/模板/README 解析/主动学习)
都产出统一的「种子」,由 ProjectBootstrap 统一去重、解析引用并批量写入.

注意: entities 表 CHECK 约束 entity_type ∈ {person, location, object, concept, event}
(001_entities.sql),因此项目/技术实体统一用 "concept";
归一化标识放在 entity_id(如 "project:shopapi" / "tech:python").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_SLUG_RE = re.compile(r"[^\w]+")  # \w 含 Unicode 字母(中文标题可生成 slug)

# 状态跟踪类谓词使用的 relation_type(D6: 触发 §6.3 project_current_state)
STATE_RELATION_TYPE = "project_state"


def normalize_name(name: str) -> str:
    """名称 → 归一化 slug(小写,非词字符折叠为 '-';保留中文等 Unicode 字母)."""
    return _SLUG_RE.sub("-", name.lower()).strip("-")


def make_entity_id(kind: str, name: str) -> str:
    """构造归一化 entity_id,如 make_entity_id("tech", "FastAPI") → "tech:fastapi"."""
    slug = normalize_name(name)
    if not slug:
        raise ValueError(f"无法从名称生成 entity_id: {name!r}")
    return f"{kind}:{slug}"


@dataclass
class EntitySeed:
    """待写入的实体种子(未持久化,无 ULID)."""

    name: str
    entity_type: str = "concept"
    entity_id: str = ""  # 空时按 make_entity_id(entity_type, name) 归一化

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("EntitySeed.name 不能为空")
        if not self.entity_id:
            self.entity_id = make_entity_id(self.entity_type, self.name)


@dataclass
class TripleSeed:
    """待写入的三元组种子.

    subject / object_name 以实体名引用 EntitySeed(由 ProjectBootstrap 解析为
    实体 ULID);object_name 与 object_value 至少一个非空(与 Layer 0 Triple 一致).
    """

    subject: str  # 实体名(必须在种子集中存在,缺失时自动补 concept 实体)
    predicate: str
    object_name: str | None = None  # 引用实体名
    object_value: str | None = None  # 字面值
    relation_type: str = "semantic"  # semantic / project_state / ...(见 002_triples.sql)

    def __post_init__(self) -> None:
        if not self.subject:
            raise ValueError("TripleSeed.subject 不能为空")
        if not self.predicate:
            raise ValueError("TripleSeed.predicate 不能为空")
        if not self.object_name and not self.object_value:
            raise ValueError("TripleSeed: object_name 或 object_value 至少一个非空")

    @property
    def dedupe_key(self) -> tuple[str, str, str | None, str | None]:
        return (self.subject, self.predicate, self.object_name, self.object_value)


@dataclass
class ExtractionResult:
    """一次提取(向导/README/模板/主动学习)产出的种子集合."""

    entities: list[EntitySeed] = field(default_factory=list)
    triples: list[TripleSeed] = field(default_factory=list)

    def merge(self, other: ExtractionResult) -> ExtractionResult:
        """合并另一份结果并按 entity_id / dedupe_key 去重(先出现者优先)."""
        seen_entities = {e.entity_id for e in self.entities}
        for entity in other.entities:
            if entity.entity_id not in seen_entities:
                seen_entities.add(entity.entity_id)
                self.entities.append(entity)
        seen_triples = {t.dedupe_key for t in self.triples}
        for triple in other.triples:
            if triple.dedupe_key not in seen_triples:
                seen_triples.add(triple.dedupe_key)
                self.triples.append(triple)
        return self


__all__ = [
    "STATE_RELATION_TYPE",
    "EntitySeed",
    "ExtractionResult",
    "TripleSeed",
    "make_entity_id",
    "normalize_name",
]
