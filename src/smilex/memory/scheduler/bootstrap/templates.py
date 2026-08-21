"""项目模板 — web/cli/data/agent 预置骨架(§9.5.1 / 04-layer3 §2.3).

模板为包内 YAML(src/smilex/memory/scheduler/bootstrap/templates/*.yaml),
随 wheel 打包(hatchling packages=["src/smilex"] 自动包含包内数据文件).
加载用 PyYAML(已在主依赖).

YAML 结构(与 04-layer3 §2.3 一致):
    name: "Web Application"
    description: "..."
    match:
      tech_stack: ["fastapi", ...]   # 命中任一即匹配
    skeleton_entities:
      - {type: concept, name: "HTTP API"}
    skeleton_triples:
      - {subject: "HTTP API", predicate: "depends_on", object: "Database"}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import files

import yaml

from .seeds import EntitySeed, TripleSeed

_TEMPLATE_PACKAGE = "smilex.memory.scheduler.bootstrap"

_cache: dict[str, ProjectTemplate] = {}


@dataclass
class ProjectTemplate:
    """项目模板: 预置骨架实体 + 三元组."""

    key: str  # web/cli/data/agent
    name: str = ""
    description: str = ""
    match_tech: list[str] = field(default_factory=list)  # 小写
    entities: list[EntitySeed] = field(default_factory=list)
    triples: list[TripleSeed] = field(default_factory=list)


def list_templates() -> list[str]:
    """列出包内可用模板 key."""
    templates_dir = files(_TEMPLATE_PACKAGE).joinpath("templates")
    return sorted(
        resource.name.removesuffix(".yaml")
        for resource in templates_dir.iterdir()
        if resource.name.endswith(".yaml")
    )


def load_template(key: str) -> ProjectTemplate:
    """加载模板(带缓存);未知 key 抛 ValueError."""
    if key in _cache:
        return _cache[key]
    resource = files(_TEMPLATE_PACKAGE).joinpath(f"templates/{key}.yaml")
    if not resource.is_file():
        raise ValueError(f"未知项目模板: {key!r}(可用: {list_templates()})")
    raw = yaml.safe_load(resource.read_text(encoding="utf-8"))
    template = ProjectTemplate(
        key=key,
        name=raw.get("name", key),
        description=raw.get("description", ""),
        match_tech=[t.lower() for t in (raw.get("match") or {}).get("tech_stack", [])],
        entities=[
            EntitySeed(name=e["name"], entity_type=e.get("type", "concept"))
            for e in raw.get("skeleton_entities", [])
        ],
        triples=[
            TripleSeed(
                subject=t["subject"],
                predicate=t["predicate"],
                object_name=t.get("object"),
                object_value=t.get("object_value"),
                relation_type=t.get("relation_type", "semantic"),
            )
            for t in raw.get("skeleton_triples", [])
        ],
    )
    _cache[key] = template
    return template


def match_template(tech_stack: list[str]) -> str | None:
    """按技术栈匹配模板: 命中数最多者胜;零命中返回 None."""
    lowered = {t.lower() for t in tech_stack}
    best_key: str | None = None
    best_hits = 0
    for key in list_templates():
        hits = len(lowered & set(load_template(key).match_tech))
        if hits > best_hits:
            best_key, best_hits = key, hits
    return best_key


__all__ = [
    "ProjectTemplate",
    "list_templates",
    "load_template",
    "match_template",
]
