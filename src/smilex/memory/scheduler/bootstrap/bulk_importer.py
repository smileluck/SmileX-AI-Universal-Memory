"""批量导入(§9.5.1 机制 4 / §9.5.2 步骤 3 / 04-layer3 §2.5)— Git/Markdown/文本批次.

三类数据源统一为 ImportSource,产出 ImportResult:
- markdown: 文件/目录批量,复用 readme_parser 规则提取(大文件按行分块),
  每个文件额外写一条 L1 时序记忆(fragment_id="md:{相对路径}" 幂等);
  目录扫描跳过依赖/构建/隐藏目录(EXCLUDED_DIRS),受 max_files 截断
- git: `git log` CLI 子进程(不引 GitPython,EXECUTION_PLAN_GAPS 约束)解析提交历史,
  提交消息 → L1 时序记忆(fragment_id="git:{仓库}:{hash}" 幂等),
  作者 → person 实体 + contributes_to 三元组,改动文件 → object 实体 +
  contains_file 三元组;分批写入 + CheckpointStore 断点续传(cursor=最后处理的提交)
- text: 文本批次,RuleBasedExtractor 规则提取 + 每条文本一条 L1 时序记忆
  (fragment_id="text:{sha256[:16]}" 幂等)

幂等性: 实体/三元组走 ProjectBootstrap.apply_seeds(按 entity_id/全键去重),
时序记忆按 (scope, fragment_id) 去重;重复导入不增数据.

git 不可用(shutil.which("git") is None)/路径非法时抛 RuntimeError;
单文件/单批次级错误收集进 ImportResult.errors,不中断整体导入.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from ....utils.ids import generate_id
from ....utils.timeutil import now_utc, to_iso
from ..checkpoint import Checkpoint, CheckpointStore
from .active_learner import RuleBasedExtractor
from .project_bootstrap import ProjectBootstrap
from .readme_parser import extract_from_readme
from .seeds import EntitySeed, ExtractionResult, TripleSeed, normalize_name

# 提交消息写入时序记忆时附带的改动文件上限
_MAX_COMMIT_FILES_IN_CONTENT = 20
# Markdown 时序记忆内容截断长度
_MAX_FRAGMENT_CHARS = 4000

# markdown 目录扫描跳过的目录段(依赖/构建产物/工具缓存;隐藏目录一律跳过)
EXCLUDED_DIRS = frozenset({
    ".git", ".smilex", "node_modules", ".venv", "venv", ".build", "dist",
    "build", "site-packages", "__pycache__", ".idea", ".vscode",
})


def _in_excluded_dir(rel: Path) -> bool:
    """相对路径是否位于忽略目录内(任一目录段命中忽略集或以 . 开头)."""
    return any(p in EXCLUDED_DIRS or p.startswith(".") for p in rel.parts[:-1])


class ImportKind(StrEnum):
    """导入数据源类型(04-layer3 §2.5,MVP 实现前三种)."""

    GIT = "git"
    MARKDOWN = "markdown"
    TEXT = "text"


@dataclass
class ImportSource:
    """批量导入输入源(GAPS §3.4 补全).

    Attributes:
        kind: 数据源类型
        subject: 导入挂载的主体实体名(通常是项目名;三元组的 subject)
        path: git 仓库路径 / markdown 文件或目录路径(kind=text 时不用)
        texts: 文本批次(仅 kind=text)
        max_commits: git 导入的提交数上限(None = 全部)
        since: git log --since 过滤(如 "2026-01-01")
        resume: 存在同名断点时是否续传(仅 git 分批生效)
        max_files: markdown 目录扫描的文件数上限(超出截断并记入 errors)
    """

    kind: ImportKind
    subject: str
    path: str | None = None
    texts: list[str] = field(default_factory=list)
    max_commits: int | None = None
    since: str | None = None
    resume: bool = True
    max_files: int = 500

    def __post_init__(self) -> None:
        if not self.subject:
            raise ValueError("ImportSource.subject 不能为空")
        if self.kind in (ImportKind.GIT, ImportKind.MARKDOWN) and not self.path:
            raise ValueError(f"ImportSource(kind={self.kind}) 需要 path")
        if self.kind is ImportKind.TEXT and not self.texts:
            raise ValueError("ImportSource(kind=text) 需要 texts")


@dataclass
class ImportResult:
    """批量导入结果(GAPS §3.4 补全).

    entity_count/triple_count 为导入后该 scope 的总量(与 apply_seeds 一致);
    memory_count 为本次新写入的时序记忆数(已存在跳过计入 skipped_count).
    """

    kind: ImportKind
    scope: str
    entity_count: int = 0
    triple_count: int = 0
    memory_count: int = 0
    skipped_count: int = 0
    source_count: int = 0  # 处理的源条目数(文件/提交/文本)
    elapsed_ms: int = 0
    resumed: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "kind": str(self.kind),
            "scope": self.scope,
            "entity_count": self.entity_count,
            "triple_count": self.triple_count,
            "memory_count": self.memory_count,
            "skipped_count": self.skipped_count,
            "source_count": self.source_count,
            "elapsed_ms": self.elapsed_ms,
            "resumed": self.resumed,
            "errors": list(self.errors),
        }


@dataclass
class _GitCommit:
    """一条 git 提交记录(git log 解析结果)."""

    hash: str
    author_name: str
    author_email: str
    authored_at: str  # ISO 8601(%aI)
    message: str
    files: list[str] = field(default_factory=list)


class BulkImporter:
    """批量导入器(§9.5.2 步骤 3).

    Args:
        bootstrap: 项目冷启动器(复用其 storage/vector_store 与 apply_seeds
            幂等写入;同包内访问其私有成员,避免扩张调度层公开 API)
        batch_size: git 导入的分批大小(每批落一次断点)
        markdown_chunk_chars: markdown 大文件分块阈值(按行累积切分)
    """

    def __init__(
        self,
        bootstrap: ProjectBootstrap,
        *,
        batch_size: int = 100,
        markdown_chunk_chars: int = 20000,
    ) -> None:
        self._bootstrap = bootstrap
        self._batch_size = batch_size
        self._markdown_chunk_chars = markdown_chunk_chars

    async def import_source(self, scope: str, source: ImportSource) -> ImportResult:
        """导入指定数据源到 scope(幂等,可重复调用)."""
        start = time.monotonic()
        if source.kind is ImportKind.MARKDOWN:
            result = await self._import_markdown(scope, source)
        elif source.kind is ImportKind.TEXT:
            result = await self._import_text(scope, source)
        else:
            result = await self._import_git(scope, source)
        result.elapsed_ms = int((time.monotonic() - start) * 1000)
        return result

    # ==================== Markdown ====================

    async def _import_markdown(self, scope: str, source: ImportSource) -> ImportResult:
        """markdown 文件/目录批量导入: 规则提取种子 + 每文件一条 L1 时序记忆."""
        assert source.path is not None
        root = Path(source.path)
        if root.is_dir():
            files = sorted(
                f for f in root.rglob("*.md")
                if not _in_excluded_dir(f.relative_to(root))
            )
        elif root.is_file():
            files = [root]
        else:
            raise RuntimeError(f"markdown 路径不存在: {root}")
        base = root if root.is_dir() else root.parent

        result = ImportResult(kind=source.kind, scope=scope)
        if len(files) > source.max_files:
            result.errors.append(
                f"markdown 文件数 {len(files)} 超过 max_files={source.max_files},"
                f"仅导入前 {source.max_files} 个(可缩小 path 范围或调大 max_files)"
            )
            files = files[:source.max_files]
        seeds = ExtractionResult()
        for path in files:
            rel = path.relative_to(base).as_posix()
            try:
                content = path.read_text(encoding="utf-8")
                # 大文件按行分块,逐块规则提取后合并(merge 内去重)
                for chunk in _chunk_text(content, self._markdown_chunk_chars):
                    seeds.merge(extract_from_readme(source.subject, chunk))
                written = await self._write_fragment(
                    scope,
                    fragment_id=f"md:{rel}",
                    content=f"[{rel}]\n{content[:_MAX_FRAGMENT_CHARS]}",
                    time_start=datetime.fromtimestamp(path.stat().st_mtime, UTC),
                )
                if written:
                    result.memory_count += 1
                else:
                    result.skipped_count += 1
                result.source_count += 1
            except Exception as exc:  # 单文件失败不中断整体导入
                result.errors.append(f"{rel}: {exc}")
        # H6: 循环内不逐条 commit,此处统一提交
        await self._bootstrap._storage.conn.commit()
        result.entity_count, result.triple_count = await self._bootstrap.apply_seeds(
            scope, seeds
        )
        return result

    # ==================== 文本批次 ====================

    async def _import_text(self, scope: str, source: ImportSource) -> ImportResult:
        """文本批次导入: 规则提取(技术名词)+ 每条文本一条 L1 时序记忆."""
        result = ImportResult(kind=source.kind, scope=scope)
        extractor = RuleBasedExtractor()
        seeds = ExtractionResult()
        for text in source.texts:
            try:
                seeds.merge(await extractor.extract(text, context=source.subject))
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
                written = await self._write_fragment(
                    scope,
                    fragment_id=f"text:{digest}",
                    content=text[:_MAX_FRAGMENT_CHARS],
                    time_start=now_utc(),
                )
                if written:
                    result.memory_count += 1
                else:
                    result.skipped_count += 1
                result.source_count += 1
            except Exception as exc:
                result.errors.append(f"text[{result.source_count}]: {exc}")
        # H6: 循环内不逐条 commit,此处统一提交
        await self._bootstrap._storage.conn.commit()
        result.entity_count, result.triple_count = await self._bootstrap.apply_seeds(
            scope, seeds
        )
        return result

    # ==================== Git ====================

    async def _import_git(self, scope: str, source: ImportSource) -> ImportResult:
        """git 历史导入: 提交 → 时序记忆,作者/文件 → 实体 + 三元组.

        分批处理,每批结束经 CheckpointStore 落断点(cursor=批尾提交 hash);
        存在断点且 source.resume 时从断点之后继续(git log 新→旧,
        跳过断点游标及其之前的已处理提交).
        """
        assert source.path is not None
        if shutil.which("git") is None:
            raise RuntimeError("git 不可用(PATH 中未找到),无法导入 git 仓库")
        repo = Path(source.path)
        if not repo.is_dir():
            raise RuntimeError(f"git 仓库路径不存在: {repo}")
        commits = await self._git_log(repo, source)
        repo_name = repo.resolve().name

        result = ImportResult(kind=source.kind, scope=scope)
        store = CheckpointStore(self._bootstrap._storage)
        task_id = f"import:{scope}:git:{repo_name}"
        start_index = 0
        if source.resume:
            checkpoint = await store.load_latest(task_id)
            if checkpoint is not None and checkpoint.cursor:
                cursor_hash = str(checkpoint.cursor)
                for i, commit in enumerate(commits):
                    if commit.hash == cursor_hash:
                        start_index = i + 1
                        result.resumed = True
                        break

        pending = commits[start_index:]
        # 作者名 → 实体 id 缓存(H6): 同一作者在上万条提交中反复出现,
        # 缓存跨批持有,避免每提交一次 (scope, name) 查询
        author_cache: dict[str, list[str]] = {}
        # apply_seeds 已存在键缓存(H7): 跨批持有,避免每批全量重载 scope
        seeds_known: dict = {}
        try:
            for offset in range(0, len(pending), self._batch_size):
                batch = pending[offset : offset + self._batch_size]
                try:
                    await self._import_git_batch(
                        scope, source, repo_name, batch, result, author_cache,
                        seeds_known,
                    )
                except Exception as exc:  # 单批失败记录后继续
                    result.errors.append(f"batch@{offset}: {exc}")
                await store.save(
                    Checkpoint(
                        task_id=task_id,
                        progress=(offset + len(batch)) / max(len(pending), 1),
                        step=start_index + offset + len(batch),
                        cursor=batch[-1].hash,
                    )
                )
        finally:
            if not result.errors:
                await store.delete(task_id)  # 全部成功: 清理断点
        return result

    async def _import_git_batch(
        self,
        scope: str,
        source: ImportSource,
        repo_name: str,
        batch: list[_GitCommit],
        result: ImportResult,
        author_cache: dict[str, list[str]] | None = None,
        seeds_known: dict | None = None,
    ) -> None:
        """单批提交: 作者/文件种子幂等写入 + 每提交一条时序记忆."""
        seeds = ExtractionResult()
        for commit in batch:
            author = commit.author_name or commit.author_email
            seeds.entities.append(EntitySeed(name=author, entity_type="person"))
            seeds.triples.append(
                TripleSeed(
                    subject=author,
                    predicate="contributes_to",
                    object_name=source.subject,
                )
            )
            for file in commit.files:
                seeds.entities.append(
                    EntitySeed(
                        name=file,
                        entity_type="object",
                        entity_id=f"file:{normalize_name(file)}",
                    )
                )
                seeds.triples.append(
                    TripleSeed(
                        subject=source.subject,
                        predicate="contains_file",
                        object_name=file,
                    )
                )
        result.entity_count, result.triple_count = await self._bootstrap.apply_seeds(
            scope, seeds, known=seeds_known
        )

        conn = self._bootstrap._storage.conn
        for commit in batch:
            files_note = ", ".join(commit.files[:_MAX_COMMIT_FILES_IN_CONTENT])
            content = f"[{commit.hash[:8]}] {commit.message}"
            if files_note:
                content += f"\nfiles: {files_note}"
            author_name = commit.author_name
            if author_cache is not None and author_name in author_cache:
                author_ids = author_cache[author_name]
            else:
                author_ids = await self._entity_ids_by_name(scope, author_name)
                if author_cache is not None:
                    author_cache[author_name] = author_ids
            written = await self._write_fragment(
                scope,
                fragment_id=f"git:{repo_name}:{commit.hash}",
                content=content,
                time_start=datetime.fromisoformat(commit.authored_at),
                entities=author_ids,
            )
            if written:
                result.memory_count += 1
            else:
                result.skipped_count += 1
            result.source_count += 1
        await conn.commit()

    async def _entity_ids_by_name(self, scope: str, name: str) -> list[str]:
        """按实体名查 ULID(时序记忆 entities 字段用;查不到返回空)."""
        if not name:
            return []
        cursor = await self._bootstrap._storage.conn.execute(
            "SELECT id FROM entities WHERE scope = ? AND name = ?", [scope, name]
        )
        return [str(r["id"]) for r in await cursor.fetchall()]

    @staticmethod
    async def _git_log(repo: Path, source: ImportSource) -> list[_GitCommit]:
        """`git log` 子进程 → 提交列表(新→旧).

        记录分隔 \\x1e,字段分隔 \\x1f,--name-only 输出改动文件.
        """
        args = [
            "git",
            "-C",
            str(repo),
            "log",
            "--pretty=format:%x1e%H%x1f%an%x1f%ae%x1f%aI%x1f%s",
            "--name-only",
        ]
        if source.since:
            args.append(f"--since={source.since}")
        if source.max_commits is not None:
            args.append(f"--max-count={source.max_commits}")
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"git log 失败({repo}): {stderr.decode('utf-8', 'replace').strip()}"
            )
        return _parse_git_log(stdout.decode("utf-8", "replace"))

    # ==================== 时序记忆写入 ====================

    async def _write_fragment(
        self,
        scope: str,
        *,
        fragment_id: str,
        content: str,
        time_start: datetime,
        entities: list[str] | None = None,
    ) -> bool:
        """写一条 L1 时序记忆(按 (scope, fragment_id) 幂等),返回是否新写入.

        vector_store 可用时同步写内容向量(L1 KNN 召回通道)。
        事务约定(H6): 不在此处 commit —— 逐条 commit 会把每次导入放大成
        N 次 WAL 刷盘;由调用方在循环结束后统一 commit。
        """
        storage = self._bootstrap._storage
        conn = storage.conn
        cursor = await conn.execute(
            "SELECT 1 FROM temporal_fragments WHERE scope = ? AND fragment_id = ?",
            [scope, fragment_id],
        )
        if await cursor.fetchone() is not None:
            return False
        row_id = generate_id()
        now = to_iso(now_utc())
        await conn.execute(
            "INSERT INTO temporal_fragments(id, fragment_id, time_start, time_end, "
            "content, entities, relations, scope, layer, importance, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, '[]', ?, 'L1', 0.5, ?, ?)",
            [
                row_id,
                fragment_id,
                to_iso(time_start),
                to_iso(time_start),
                content,
                json.dumps(entities or [], ensure_ascii=False),
                scope,
                now,
                now,
            ],
        )
        if self._bootstrap._vector_store is not None:
            await self._bootstrap._vector_store.add_text(
                conn, content, fragment_id=row_id
            )
        return True


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


__all__ = [
    "BulkImporter",
    "ImportKind",
    "ImportResult",
    "ImportSource",
]
