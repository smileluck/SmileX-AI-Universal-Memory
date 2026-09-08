"""MemoryMiddleware 项目冷启动门面(M.2 系)— 冷启动 / 扫描导入 / 种子注入 / 克隆.

2026-09 从 memory.py 拆出: 方法体不变,经 _BootstrapFacadeMixin 组合回
MemoryMiddleware;共享状态(_engine/_bootstrap/_current_scope_id 与
_require_initialized)由宿主类提供.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..memory.contracts import ProjectInitRequest, ProjectInitResponse
from ..memory.models import MemoryScope
from ..memory.storage.storage_engine import parse_scope_path, scope_path
from ..utils.ids import generate_id

if TYPE_CHECKING:
    from ..memory.scheduler.bootstrap.bulk_importer import ImportResult, ImportSource
    from ..memory.scheduler.bootstrap.cross_project_cloner import (
        CloneFilter,
        CloneResult,
    )
    from ..memory.scheduler.bootstrap.seeds import ExtractionResult

# 项目根 README 文件名候选(大小写两档,取第一个命中)
_README_GLOBS = ("README*.md", "readme*.md")


def _find_readme(root: Path) -> str | None:
    """读项目根 README 内容;无匹配或读取失败返回 None(冷启动降级继续)."""
    for pattern in _README_GLOBS:
        matches = sorted(root.glob(pattern))
        if matches:
            try:
                return matches[0].read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return None
    return None


class _BootstrapFacadeMixin:
    """M.2 项目冷启动门面(组合进 MemoryMiddleware)."""

    # ==================== M.2 项目冷启动 ====================

    async def initialize_project(
        self,
        request: ProjectInitRequest,
        *,
        wizard_answers: dict | None = None,
    ) -> ProjectInitResponse:
        """新项目冷启动初始化(§9.5),返回 scope 全路径与阶段."""
        self._require_initialized()
        assert self._bootstrap is not None
        start = time.monotonic()
        ctx = await self._bootstrap.initialize(request, wizard_answers=wizard_answers)
        _, self._current_scope_id = parse_scope_path(ctx.scope)
        return ProjectInitResponse(
            scope=ctx.scope,
            entity_count=ctx.entity_count,
            triple_count=ctx.triple_count,
            stage=ctx.stage,
            elapsed_ms=int((time.monotonic() - start) * 1000),
        )

    # ==================== M.2a 冷启动 + 扫描导入(README/git/markdown) ====================

    async def bootstrap_project(
        self,
        name: str,
        *,
        project_path: str | Path | None = None,
        description: str = "",
        tech_stack: list[str] | None = None,
        readme_content: str | None = None,
        scan_git: bool = True,
        scan_markdown: bool = True,
        scan_code: bool = True,
        max_commits: int | None = None,
    ) -> dict:
        """项目初始化 = 冷启动 + 扫描生成初始记忆(MCP/CLI 共用编排).

        initialize_project 建立项目 scope(同名复用)后,对 project_path
        依次导入 git 历史 / markdown 文档 / 源码文件(README 缺省时自动从
        项目根读取);单源失败(如目录无 .git、git 不可用)记入 skipped 继续执行.

        Args:
            name: 项目名(scope 复用与种子挂载的主体)
            project_path: 项目根目录;None 时只做冷启动不扫描
            readme_content: README 文本;None 且有 project_path 时自动读取
            scan_git / scan_markdown / scan_code: 是否导入对应数据源
            max_commits: git 导入提交数上限(None = 全部)

        Returns:
            {"init": ProjectInitResponse, "imports": {kind: ImportResult},
             "skipped": [原因...], "project_path": str | None}
        """
        self._require_initialized()
        root = None
        if project_path is not None:
            root = Path(project_path).expanduser().resolve()
            if not root.is_dir():
                raise ValueError(f"project_path 不是有效目录: {root}")
        if readme_content is None and root is not None:
            readme_content = _find_readme(root)

        init = await self.initialize_project(
            ProjectInitRequest(
                name=name,
                description=description,
                tech_stack=list(tech_stack or []),
                readme_content=readme_content,
            )
        )

        from ..memory.scheduler.bootstrap.bulk_importer import (  # 延迟导入: 同 initialize
            ImportKind,
            ImportSource,
        )

        imports: dict[str, dict] = {}
        skipped: list[str] = []
        if root is None:
            if scan_git or scan_markdown or scan_code:
                skipped.append("扫描跳过: 未提供 project_path")
        else:
            sources: list[tuple[str, ImportSource]] = []
            if scan_git:
                if (root / ".git").exists():
                    sources.append((
                        "git",
                        ImportSource(
                            kind=ImportKind.GIT,
                            subject=name,
                            path=str(root),
                            max_commits=max_commits,
                        ),
                    ))
                else:
                    skipped.append("git: 目录无 .git")
            if scan_markdown:
                sources.append((
                    "markdown",
                    ImportSource(
                        kind=ImportKind.MARKDOWN, subject=name, path=str(root)
                    ),
                ))
            if scan_code:
                sources.append((
                    "code",
                    ImportSource(kind=ImportKind.CODE, subject=name, path=str(root)),
                ))
            for kind_label, source in sources:
                try:
                    imports[kind_label] = (
                        await self.import_source(source)
                    ).to_dict()
                except Exception as exc:  # 单源失败不中断其他源
                    skipped.append(f"{kind_label}: {exc}")

        return {
            "init": init.to_dict(),
            "imports": imports,
            "skipped": skipped,
            "project_path": str(root) if root is not None else None,
        }

    # ==================== M.2b 批量导入 / 种子注入(P1-b,§9.5.2 步骤 3/4) ====================

    async def import_source(
        self, source: ImportSource, *, scope: str | None = None
    ) -> ImportResult:
        """批量导入(Git/Markdown/文本批次)到指定 scope(§9.5.2 步骤 3).

        Args:
            source: 导入源(bootstrap.bulk_importer.ImportSource)
            scope: 目标 scope 全路径;None 时用最近一次 initialize_project 的项目

        Returns:
            ImportResult(实体/三元组总量 + 新写入时序记忆数 + 错误列表)
        """
        self._require_initialized()
        assert self._bootstrap is not None
        from ..memory.scheduler.bootstrap.bulk_importer import (  # 延迟导入: 同 initialize
            BulkImporter,
        )

        return await BulkImporter(self._bootstrap).import_source(
            self._resolve_scope(scope), source
        )

    async def inject_seeds(
        self, source: ExtractionResult | str | Path, *, scope: str | None = None
    ) -> tuple[int, int]:
        """种子注入(§9.5.2 步骤 4): 模板 key / YAML 种子包 / ExtractionResult.

        Returns:
            (实体总数, 三元组总数) — 该 scope 注入后当前总量
        """
        self._require_initialized()
        assert self._bootstrap is not None
        from ..memory.scheduler.bootstrap.seed_injector import (  # 延迟导入: 同上
            SeedInjector,
        )

        return await SeedInjector(self._bootstrap).inject(self._resolve_scope(scope), source)

    # ==================== M.2c 跨项目克隆(P2,§9.5.1 机制 6) ====================

    async def clone_project(
        self,
        source_scope: str,
        target_name: str | None = None,
        *,
        clone_filter: CloneFilter | None = None,
    ) -> CloneResult:
        """跨项目克隆(§9.5.1 机制 6): 把 source_scope 的记忆克隆到新项目 scope.

        Args:
            source_scope: 来源 scope 全路径(如 "project:proj_a")
            target_name: 新项目名(可选;提供时写入项目实体种子,
                与 initialize_project 一致)
            clone_filter: 克隆过滤(实体类型/层/时间;None = 全部现行记录)

        Returns:
            CloneResult(target_scope 为新 project scope 全路径 + 各项计数)
        """
        self._require_initialized()
        assert self._bootstrap is not None
        from ..memory.scheduler.bootstrap.cross_project_cloner import (  # 延迟导入: 同 initialize
            CrossProjectCloner,
        )

        target_scope = scope_path(MemoryScope.PROJECT, generate_id())
        # 注入 vector_store: 克隆的新实体/新片段即时重嵌向量,
        # 避免克隆出的 scope 只有 FTS 单通道(ADR-022 重建是一等公民)
        result = await CrossProjectCloner(
            self._engine, vector_store=self._vector_store
        ).clone_to(source_scope, target_scope, clone_filter)
        if target_name:
            from ..memory.scheduler.bootstrap.seeds import (  # 延迟导入: 同上
                EntitySeed,
                ExtractionResult,
                normalize_name,
            )

            await self._bootstrap.apply_seeds(
                target_scope,
                ExtractionResult(
                    entities=[
                        EntitySeed(
                            name=target_name,
                            entity_id=f"project:{normalize_name(target_name)}",
                        )
                    ]
                ),
            )
        return result

    def _resolve_scope(self, scope: str | None) -> str:
        """scope 缺省回退到当前项目;两者皆无时报错."""
        if scope is not None:
            return scope
        if self._current_scope_id is None:
            raise RuntimeError("未指定 scope 且尚未 initialize_project")
        return scope_path(MemoryScope.PROJECT, self._current_scope_id)
