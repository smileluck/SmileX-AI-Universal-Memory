"""种子注入(§9.5.1 机制 5 / §9.5.2 步骤 4)— 外部种子写入指定 scope 的薄封装.

不重复造轮子: 持久化统一走 ProjectBootstrap.apply_seeds(幂等),
本模块只负责把三类来源归一为 ExtractionResult:
- ExtractionResult: 直接注入
- 模板 key(str,如 "web"): 复用 templates.load_template
- YAML 种子包路径(str/Path): 与模板同构的外部 YAML(领域知识包)

YAML 种子包结构(与 templates/*.yaml 一致):
    name: "Payment Domain"
    description: "..."
    skeleton_entities:
      - {type: concept, name: "Payment Gateway"}
    skeleton_triples:
      - {subject: "Payment Gateway", predicate: "depends_on", object: "Database"}

注: §9.5.2 步骤 4 的「从 global 按技术栈召回经验」依赖语义检索,
HashEmbedder 无语义相似度,留待语义 Embedder 接入后实现(D5);
本模块覆盖「预定义种子集注入」路径.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .project_bootstrap import ProjectBootstrap
from .seeds import EntitySeed, ExtractionResult, TripleSeed
from .templates import load_template

_YAML_SUFFIXES = {".yaml", ".yml"}


def load_seed_pack(path: str | Path) -> ExtractionResult:
    """加载外部 YAML 种子包(与项目模板同构)→ ExtractionResult."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"种子包不存在: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return ExtractionResult(
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


class SeedInjector:
    """种子注入器 — 把外部种子(模板/YAML 知识包/手工种子集)注入指定 scope.

    Args:
        bootstrap: 项目冷启动器(复用 apply_seeds 幂等写入)
    """

    def __init__(self, bootstrap: ProjectBootstrap) -> None:
        self._bootstrap = bootstrap

    async def inject(
        self, scope: str, source: ExtractionResult | str | Path
    ) -> tuple[int, int]:
        """注入种子到 scope,返回该 scope 当前 (实体总数, 三元组总数).

        source 分派:
        - ExtractionResult → 直接注入
        - str: 已注册模板 key(load_template);或以 .yaml/.yml 结尾/存在的路径
          → load_seed_pack
        - Path → load_seed_pack

        幂等: 重复注入同一份种子不增实体/三元组(apply_seeds 去重).
        """
        if isinstance(source, ExtractionResult):
            seeds = source
        elif isinstance(source, Path):
            seeds = load_seed_pack(source)
        elif isinstance(source, str):
            path = Path(source)
            if path.suffix.lower() in _YAML_SUFFIXES or path.is_file():
                seeds = load_seed_pack(path)
            else:
                template = load_template(source)
                seeds = ExtractionResult(
                    entities=list(template.entities), triples=list(template.triples)
                )
        else:
            raise TypeError(f"不支持的种子来源类型: {type(source)!r}")
        return await self._bootstrap.apply_seeds(scope, seeds)


async def inject_seeds(
    bootstrap: ProjectBootstrap, scope: str, source: ExtractionResult | str | Path
) -> tuple[int, int]:
    """便捷函数: 一次性注入种子(等价于 SeedInjector(bootstrap).inject)."""
    return await SeedInjector(bootstrap).inject(scope, source)


__all__ = [
    "SeedInjector",
    "inject_seeds",
    "load_seed_pack",
]
