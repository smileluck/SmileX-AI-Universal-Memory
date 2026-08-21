"""项目冷启动六机制(§9.5).

MVP: 向导 + 模板 + 主动学习 + README 解析;P1: 批量导入 + 种子注入;P2: 跨项目克隆.
"""

from .active_learner import (
    LEARNING_QUESTIONS,
    ActiveLearner,
    LLMProvider,
    RuleBasedExtractor,
)
from .bulk_importer import BulkImporter, ImportKind, ImportResult, ImportSource
from .cross_project_cloner import CloneFilter, CloneResult, CrossProjectCloner
from .onboarding import CORE_QUESTIONS, WizardQuestion, process_answers
from .project_bootstrap import ProjectBootstrap
from .readme_parser import (
    TECH_KEYWORDS,
    ReadmeInfo,
    detect_technologies,
    extract_from_readme,
    parse_readme,
)
from .seed_injector import SeedInjector, inject_seeds, load_seed_pack
from .seeds import EntitySeed, ExtractionResult, TripleSeed
from .templates import ProjectTemplate, list_templates, load_template, match_template

__all__ = [
    "CORE_QUESTIONS",
    "LEARNING_QUESTIONS",
    "TECH_KEYWORDS",
    "ActiveLearner",
    "BulkImporter",
    "CloneFilter",
    "CloneResult",
    "CrossProjectCloner",
    "EntitySeed",
    "ExtractionResult",
    "ImportKind",
    "ImportResult",
    "ImportSource",
    "LLMProvider",
    "ProjectBootstrap",
    "ProjectTemplate",
    "ReadmeInfo",
    "RuleBasedExtractor",
    "SeedInjector",
    "TripleSeed",
    "WizardQuestion",
    "detect_technologies",
    "extract_from_readme",
    "inject_seeds",
    "list_templates",
    "load_seed_pack",
    "load_template",
    "match_template",
    "parse_readme",
    "process_answers",
]
