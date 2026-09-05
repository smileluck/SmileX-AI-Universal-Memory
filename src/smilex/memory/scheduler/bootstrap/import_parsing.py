"""批量导入的纯解析函数(目录过滤 / 源码 AST 提取 / git log 解析 / 文本分块).

2026-09 从 bulk_importer.py 拆出: 这里只放无 I/O、无状态、可独立单测的解析逻辑,
BulkImporter 编排(IO、种子写入、断点续传)留在原模块。
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .seeds import EntitySeed, ExtractionResult, TripleSeed, normalize_name

# markdown/code 目录扫描跳过的目录段(依赖/构建产物/工具缓存;隐藏目录一律跳过)
EXCLUDED_DIRS = frozenset({
    ".git", ".smilex", "node_modules", ".venv", "venv", ".build", "dist",
    "build", "site-packages", "__pycache__", ".idea", ".vscode",
})

# code 导入扫描的源码扩展名(.py 走 AST 全量提取,其余走文件头注释兜底)
CODE_EXTENSIONS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java",
    ".kt", ".swift", ".c", ".h", ".cpp", ".hpp", ".cc", ".rb", ".php", ".sh",
})
# 单文件顶层定义列出的上限(超出折叠为 "…等 N 个")
_MAX_DEFS_LISTED = 30
# 模块 docstring / 文件头注释截断长度
_MAX_HEADER_CHARS = 800


def _in_excluded_dir(rel: Path) -> bool:
    """相对路径是否位于忽略目录内(任一目录段命中忽略集或以 . 开头)."""
    return any(p in EXCLUDED_DIRS or p.startswith(".") for p in rel.parts[:-1])


# ==================== 源码文件提取(纯函数,便于单测) ====================


@dataclass
class _PySummary:
    """单个 .py 的 AST 提取结果."""

    docstring: str = ""
    classes: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    internal_deps: list[str] = field(default_factory=list)  # 仓库内相对路径
    external_deps: list[str] = field(default_factory=list)  # 外部库首段名


def _header_comment(text: str) -> str:
    """非 .py 源码的文件头注释提取(//、#、块注释三种风格,取首个连续块)."""
    lines = text.splitlines()
    start = 1 if lines and lines[0].startswith("#!") else 0
    block: list[str] = []
    in_block_comment = False
    for line in lines[start:]:
        stripped = line.strip()
        if not block and not in_block_comment:
            if not stripped:
                continue  # 跳过文件头空行
            if stripped.startswith(("//", "#")):
                block.append(stripped.lstrip("/# ").rstrip())
            elif stripped.startswith("/*"):
                body = stripped[2:].split("*/", 1)[0].strip()
                block.append(body)
                in_block_comment = "*/" not in stripped
            else:
                break  # 首个非注释行 → 结束
        elif in_block_comment:
            block.append(stripped.split("*/", 1)[0].strip())
            if "*/" in line:
                break
        elif stripped.startswith(("//", "#")):
            block.append(stripped.lstrip("/# ").rstrip())
        else:
            break
    return "\n".join(b for b in block if b)


def _module_to_rel(base: Path, module: str) -> str | None:
    """点分模块名 → base 下的相对路径(模块文件或包 __init__);未命中返回 None."""
    if not module:
        return None
    rel = Path(*module.split("."))
    for candidate in (rel.with_suffix(".py"), rel / "__init__.py"):
        if (base / candidate).is_file():
            return candidate.as_posix()
    return None


def _resolve_internal(
    root: Path, pkg_parts: tuple[str, ...], module: str
) -> str | None:
    """绝对 import → 仓库内相对路径;依次尝试本目录(平级脚本)/src 布局/仓库根."""
    for base, prefix in (
        (root.joinpath(*pkg_parts), pkg_parts),
        (root / "src", ("src",)),
        (root, ()),
    ):
        rel = _module_to_rel(base, module)
        if rel is not None:
            return "/".join((*prefix, rel))
    return None


def _add_dep(
    summary: _PySummary, root: Path, pkg_parts: tuple[str, ...],
    level: int, module: str,
) -> None:
    """一条 import → 内部路径或外部库首段(标准库过滤)."""
    if level == 0:
        internal = _resolve_internal(root, pkg_parts, module)
        if internal is not None:
            summary.internal_deps.append(internal)
        elif module.split(".")[0] not in sys.stdlib_module_names:
            summary.external_deps.append(module.split(".")[0])
        return
    # 相对导入: level=1 → 当前包,level=2 → 上一级 …(越界/空 module 放弃解析)
    if not module or level - 1 > len(pkg_parts):
        return
    base_parts = pkg_parts[: len(pkg_parts) - (level - 1)]
    internal = _module_to_rel(root.joinpath(*base_parts), module)
    if internal is not None:
        summary.internal_deps.append("/".join([*base_parts, internal]))


def _summarize_py(rel: str, text: str, root: Path) -> _PySummary:
    """AST 提取单个 .py: docstring / 顶层类与函数 / 依赖;语法错误降级文件头注释."""
    summary = _PySummary()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        summary.docstring = _header_comment(text)
        return summary
    summary.docstring = (ast.get_docstring(tree) or "").strip()
    pkg_parts = Path(rel).parts[:-1]
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            summary.classes.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            summary.functions.append(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                _add_dep(summary, root, pkg_parts, 0, alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or (node.names[0].name if node.names else "")
            _add_dep(summary, root, pkg_parts, node.level, module)
    return summary


def _merge_py_seeds(
    seeds: ExtractionResult, subject: str, rel: str, summary: _PySummary
) -> None:
    """单个 .py 的结构 → 种子(文件实体与 git 通道同键,跨通道去重)."""
    stem = Path(rel).stem
    # 文件实体 + contains_file(entity_id 约定与 git 导入一致)
    seeds.entities.append(
        EntitySeed(name=rel, entity_type="object", entity_id=f"file:{normalize_name(rel)}")
    )
    seeds.triples.append(
        TripleSeed(subject=subject, predicate="contains_file", object_name=rel)
    )
    for cls in summary.classes:
        qualified = f"{stem}.{cls}"  # 带模块前缀,避免跨文件同名类解析歧义
        seeds.entities.append(
            EntitySeed(
                name=qualified,
                entity_type="concept",
                entity_id=f"class:{normalize_name(rel)}-{normalize_name(cls)}",
            )
        )
        seeds.triples.append(
            TripleSeed(subject=rel, predicate="defines_class", object_name=qualified)
        )
    for dep in summary.internal_deps:
        seeds.entities.append(
            EntitySeed(
                name=dep, entity_type="object", entity_id=f"file:{normalize_name(dep)}"
            )
        )
        seeds.triples.append(
            TripleSeed(subject=rel, predicate="depends_on", object_name=dep)
        )
    for lib in sorted(set(summary.external_deps)):
        seeds.entities.append(
            EntitySeed(
                name=lib, entity_type="concept", entity_id=f"tech:{normalize_name(lib)}"
            )
        )
        seeds.triples.append(
            TripleSeed(subject=rel, predicate="depends_on", object_name=lib)
        )


@dataclass
class _GitCommit:
    """一条 git 提交记录(git log 解析结果)."""

    hash: str
    author_name: str
    author_email: str
    authored_at: str  # ISO 8601(%aI)
    message: str
    files: list[str] = field(default_factory=list)


def _chunk_text(content: str, max_chars: int) -> list[str]:
    """按行累积切分大文本(单块 ≤ max_chars;空输入返回空列表)."""
    if not content:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in content.splitlines(keepends=True):
        if current and current_len + len(line) > max_chars:
            chunks.append("".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


def _parse_git_log(output: str) -> list[_GitCommit]:
    """解析 `git log --pretty=format:%x1e... --name-only` 输出."""
    commits: list[_GitCommit] = []
    for chunk in output.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        lines = chunk.splitlines()
        fields = lines[0].split("\x1f")
        if len(fields) < 5:
            continue
        commits.append(
            _GitCommit(
                hash=fields[0],
                author_name=fields[1],
                author_email=fields[2],
                authored_at=fields[3],
                message=fields[4],
                files=[line.strip() for line in lines[1:] if line.strip()],
            )
        )
    return commits
