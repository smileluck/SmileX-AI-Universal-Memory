"""结构守卫 — 依赖分层规则与版本一致性(防架构腐化回归).

- memory/** 不得 import smilex.middlewares: contracts 下沉后循环依赖已断,
  新增反向边会让 dto 类问题复发(middlewares 是 memory 的消费者,不是依赖)
- memory/storage/** 不得 import memory.lifecycle: 存储层是最底层,
  embedder 等基础设施客户端已上移到 memory/,存储层只应依赖更底层
- pyproject version 与 smilex.__version__ 必须一致(发布流水线按
  pyproject 字面 grep 校验 tag,不能改用 dynamic version,只能人工对齐)
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import smilex

SRC = Path(__file__).parents[2] / "src" / "smilex"


def _imported_modules(path: Path) -> set[str]:
    """收集一个 .py 文件 import 的全部绝对模块名(相对导入按文件位置解析)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = path.relative_to(SRC.parent).with_suffix("")
    parts = list(package.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    pkg = ".".join(parts[:-1])
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                out.add(node.module or "")
            else:
                base = pkg.split(".")[: len(pkg.split(".")) - (node.level - 1)]
                out.add(".".join([*base, node.module]) if node.module else ".".join(base))
    return out


def _pyfiles(subdir: str) -> list[Path]:
    return sorted((SRC / subdir).rglob("*.py"))


def test_memory_never_imports_middlewares():
    """memory/** 反向依赖 middlewares = 循环依赖复发,禁止."""
    offenders: list[str] = []
    for path in _pyfiles("memory"):
        for mod in _imported_modules(path):
            if mod == "smilex.middlewares" or mod.startswith("smilex.middlewares."):
                offenders.append(f"{path.relative_to(SRC)}: {mod}")
    assert not offenders, "memory/** 禁止 import smilex.middlewares:\n" + "\n".join(offenders)


def test_storage_never_imports_lifecycle():
    """storage 是最底层,不得依赖生命周期层(lifecycle)."""
    offenders: list[str] = []
    for path in _pyfiles("memory/storage"):
        for mod in _imported_modules(path):
            if mod == "smilex.memory.lifecycle" or mod.startswith(
                "smilex.memory.lifecycle."
            ):
                offenders.append(f"{path.relative_to(SRC)}: {mod}")
    assert not offenders, "storage/** 禁止 import memory.lifecycle:\n" + "\n".join(offenders)


def test_version_matches_pyproject():
    """pyproject 字面 version 与 __version__ 一致(publish.yml 按字面 grep 校验)."""
    pyproject = Path(__file__).parents[2] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert data["project"]["version"] == smilex.__version__, (
        f"版本漂移: pyproject={data['project']['version']} "
        f"smilex.__version__={smilex.__version__}"
    )
