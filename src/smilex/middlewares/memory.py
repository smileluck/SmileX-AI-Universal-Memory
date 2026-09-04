"""MemoryMiddleware — Agent 访问记忆系统的唯一入口(主文档 §3.1).

组合 Layer 0-3,对外提供最小契约(M.2-M.5):
- initialize_project: 委托 ProjectBootstrap(§9.5 冷启动),计时组装响应
- clone_project: 委托 CrossProjectCloner(§9.5.1 机制 6 跨项目克隆,P2)
- write: 构建 FuzzyMemory → L0(PromotionManager.put,>800 tokens 自动晋升 L1),
  relations(TripleInput)解析/写入存储层,实体/晋升片段写入向量
- recall: 委托 ContextBuilder.build_context(多路召回 + Token 预算裁剪)
- close_session / restore_session: L0 快照持久化 + 会话清理/恢复

向量写入约定:
- 驻留 L0 的记忆不写向量 — recall 的 L0 通道全量覆盖,晋升时由
  PromotionManager 写入 fragment 向量(避免 vector_links 悬空/重复)
- relations 自动补建的实体写入名称向量(与 ProjectBootstrap 一致)

事务约定: 需要 conn 直写的部分(L0 晋升)由本类 commit/rollback;
StorageEngine 方法(write_entity/write_triple)各自管理事务.

使用方法:
    mw = MemoryMiddleware("~/.smilex/data.db")
    await mw.initialize()
    resp = await mw.initialize_project(ProjectInitRequest(name="demo"))
    wr = await mw.write(WriteRequest(scope=MemoryScope.PROJECT, content="..."),
                        session_id="sess_1")
    rr = await mw.recall(RecallRequest(query="..."), session_id="sess_1")
    await mw.close_session("sess_1")
    await mw.close()
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from ..memory.concurrency import ConcurrencyController
from ..memory.lifecycle.context_builder import ContextBuilder
from ..memory.lifecycle.embedder import Embedder, get_embedder
from ..memory.lifecycle.extractor import FactExtractor, PassThroughExtractor
from ..memory.lifecycle.l0_snapshot import L0SnapshotStore
from ..memory.lifecycle.l0_working_memory import L0WorkingMemory
from ..memory.lifecycle.promotion import PromotionManager
from ..memory.lifecycle.reranker import Reranker
from ..memory.lifecycle.token_counter import TokenCounter
from ..memory.models import (
    Entity,
    FuzzyMemory,
    LockType,
    MemoryLayer,
    MemoryScope,
    ScopeFilter,
    Triple,
)
from ..memory.storage.storage_engine import StorageEngine, parse_scope_path, scope_path
from ..memory.storage.vector_store import VectorStore
from ..utils.ids import generate_id
from ..utils.timeutil import now_utc, to_iso
from .dto import (
    ConflictInfo,
    MemoryRef,
    ProjectInitRequest,
    ProjectInitResponse,
    RecallRequest,
    RecallResponse,
    WriteRequest,
    WriteResponse,
    WriteStatus,
)

if TYPE_CHECKING:
    from ..memory.quality.archiver import ArchiveResult, RestoreResult, RetentionPolicy
    from ..memory.scheduler.bootstrap.bulk_importer import ImportResult, ImportSource
    from ..memory.scheduler.bootstrap.cross_project_cloner import (
        CloneFilter,
        CloneResult,
    )
    from ..memory.scheduler.bootstrap.project_bootstrap import ProjectBootstrap
    from ..memory.scheduler.bootstrap.seeds import ExtractionResult

# 注意: ProjectBootstrap / make_entity_id(scheduler.bootstrap)不能模块级导入 —
# bootstrap 反向依赖 middlewares.dto,模块级导入会形成循环依赖
# (middlewares.__init__ → .memory → bootstrap → middlewares.dto).
# 两者均在方法内延迟导入(initialize / _resolve_entity_ref).

# ContextSource.layer("L0"/"L1"/"L2") → MemoryLayer 枚举
_LAYER_MAP: dict[str, MemoryLayer] = {
    "L0": MemoryLayer.L0_WORKING,
    "L1": MemoryLayer.L1_SHORT,
    "L2": MemoryLayer.L2_LONG,
}

# MemoryRef.snippet 截断长度
_SNIPPET_MAX_CHARS = 120

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


class _RelationConflictError(Exception):
    """relation 写入被 Layer 4 冲突策略拒绝(write 内部捕获 → CONFLICT 响应)."""

    def __init__(self, info: ConflictInfo) -> None:
        self.info = info
        super().__init__(info.message or "relation 写入冲突")


class MemoryMiddleware:
    """Agent 访问记忆系统的唯一入口(§3.1).

    Args:
        db_path: SQLite 数据库路径(默认 ":memory:";engine 提供时忽略)
        engine: 可选,注入已建 StorageEngine(默认按 db_path 自建;
            自建实例由本类负责 close)
        embedder: 文本编码器(决策 D5),None 时 get_embedder() 默认
            HashEmbedder(仅确定性,无语义相似度保证)
        token_counter: token 计数器(None 时新建默认实例)
        l0: L0 工作记忆(None 时新建默认实例)
        concurrency: Layer 4 并发控制器(None 时新建默认实例);
            注入可自定义锁超时/冲突解决策略
        promotion_threshold: L0→L1 晋升阈值(默认 800 tokens;批量导入历史
            数据等场景可调低,如 0 = 全部直入 L1 + 向量)
        reranker: 检索精排器(None 时 NoopReranker 不干预排序;
            cross-encoder 见 smilex.memory.lifecycle.reranker)
        fact_extractor: 写入时事实抽取器(None 时 PassThroughExtractor,
            原 content 整块写入,行为与现状一致;LLM 抽取见
            smilex.memory.lifecycle.extractor)
        facts_bypass_l0: 抽取产出的事实是否跳过 L0 直送 L1 晋升
            (默认 True;短事实留在 L0 对 recall 不可见,仅 PassThrough 下无影响)
    """

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        *,
        engine: StorageEngine | None = None,
        embedder: Embedder | None = None,
        token_counter: TokenCounter | None = None,
        l0: L0WorkingMemory | None = None,
        concurrency: ConcurrencyController | None = None,
        promotion_threshold: int = 800,
        reranker: Reranker | None = None,
        fact_extractor: FactExtractor | None = None,
        facts_bypass_l0: bool = True,
    ) -> None:
        self._owns_engine = engine is None
        self._engine = engine or StorageEngine(db_path)
        self._concurrency = concurrency or ConcurrencyController()
        self._vector_store = VectorStore(embedder or get_embedder())
        self._l0 = l0 or L0WorkingMemory()
        self._counter = token_counter or TokenCounter()
        self._promotion = PromotionManager(
            self._l0,
            vector_store=self._vector_store,
            token_counter=self._counter,
            threshold=promotion_threshold,
        )
        self._context_builder = ContextBuilder(
            l0=self._l0,
            vector_store=self._vector_store,
            token_counter=self._counter,
            reranker=reranker,
        )
        self._extractor = fact_extractor or PassThroughExtractor()
        self._facts_bypass_l0 = facts_bypass_l0
        self._snapshots = L0SnapshotStore(self._engine)
        self._bootstrap: ProjectBootstrap | None = None
        # 最近一次 initialize_project 的项目 ID,write 的 scope_id 缺省值
        self._current_scope_id: str | None = None

    # ==================== 生命周期 ====================

    async def initialize(self) -> None:
        """初始化存储引擎与冷启动器(幂等: 已初始化则跳过)."""
        if not self._engine.is_initialized:
            await self._engine.initialize()
        from ..memory.scheduler.bootstrap.project_bootstrap import (
            ProjectBootstrap,  # 延迟导入: 避免与 middlewares.dto 循环依赖
        )

        self._bootstrap = ProjectBootstrap(
            self._engine, vector_store=self._vector_store
        )

    async def close(self) -> None:
        """关闭: 清空 L0 热缓存,关闭自建的存储引擎."""
        self._l0.clear()
        if self._owns_engine:
            await self._engine.close()

    async def __aenter__(self) -> MemoryMiddleware:
        await self.initialize()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def is_initialized(self) -> bool:
        return self._engine.is_initialized

    @property
    def engine(self) -> StorageEngine:
        """底层存储引擎(高级用法,如直接调用查询 API)."""
        return self._engine

    @property
    def l0(self) -> L0WorkingMemory:
        """L0 工作记忆(测试/诊断用)."""
        return self._l0

    @property
    def concurrency(self) -> ConcurrencyController:
        """Layer 4 并发控制器(测试/诊断/任务链接入用)."""
        return self._concurrency

    def _require_initialized(self) -> None:
        if not self._engine.is_initialized:
            raise RuntimeError("MemoryMiddleware 未初始化,请先调用 await initialize()")

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
        result = await CrossProjectCloner(self._engine).clone_to(
            source_scope, target_scope, clone_filter
        )
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

    # ==================== M.3 写入 ====================

    async def write(
        self,
        request: WriteRequest,
        *,
        session_id: str,
        scope_id: str | None = None,
        chain_id: str | None = None,
        detect_conflicts: bool = True,
    ) -> WriteResponse:
        """写入记忆事件(§3.2): FuzzyMemory → L0(超阈值自动晋升 L1)+ relations.

        Args:
            request: 写入请求(content/entities/relations/...)
            session_id: L0 工作记忆会话 ID
            scope_id: 项目/租户 ID;None 时用最近一次 initialize_project 的项目
            chain_id: 任务链标识(Layer 4 锁持有者/冲突归因;None 时用 session_id)
            detect_conflicts: False 跳过 Layer 4 冲突检测(测试/迁移用)

        Returns:
            WriteResponse: 正常为 SAVED;冲突且策略为拒绝/人工时返回 CONFLICT
            + ConflictInfo,L0 记忆不落库(同一 request 内冲突 relation 之前
            的 relations 已写入者保留,之后的不再写入)

        Raises:
            LockTimeoutError: 并发写入同一实体等待锁超时(§10 死锁预防兜底)

        并发控制(P1/Layer 4,§10): relations 涉及的实体按 ID 排序加
        EXCLUSIVE 锁(锁排序防死锁),序列化多任务链对同一实体的并发写;
        每条 triple 写前经 ConflictDetector 检测 WRITE_WRITE,默认策略
        AUTO_LAST(LWW)放行,拒绝/人工策略返回 CONFLICT。
        """
        self._require_initialized()
        scope_id = scope_id or self._current_scope_id
        scope_str = scope_path(request.scope, scope_id)  # 提前校验 PROJECT/TENANT 必须有 scope_id
        holder = chain_id or session_id

        # 0. Layer 4: 按 relations 涉及的实体加锁(锁排序防死锁)+ 冲突检测
        lock_resources: list[str] = []
        if detect_conflicts:
            lock_resources = sorted(
                {
                    f"entity:{scope_str}:{rel.subject_id or rel.subject_name}"
                    for rel in request.relations
                    if rel.subject_id or rel.subject_name
                }
            )
        handles = await self._concurrency.locks.acquire_many(
            lock_resources, LockType.EXCLUSIVE, holder=holder
        )
        # 1. relations: TripleInput 解析(名称 → 实体)+ 冲突检测 + 写入存储层(L2)
        try:
            triple_ids = await self._write_relations(
                request, scope_id=scope_id, holder=holder, detect=detect_conflicts
            )
        except _RelationConflictError as exc:
            return WriteResponse(
                memory_id="",
                status=WriteStatus.CONFLICT,
                conflict=exc.info,
            )
        finally:
            for handle in handles:
                await handle.release()

        # 2. 事实抽取 → 每条事实一个 FuzzyMemory → L0(超阈值自动晋升 L1 + 向量)
        #    PassThrough(默认)下 facts == [request.content],行为与历史一致
        try:
            facts = await self._extractor.extract(request.content)
        except Exception:
            facts = [request.content]
        if not facts:  # 防御: 抽取器返回空列表时退回原文
            facts = [request.content]
        bypass = self._facts_bypass_l0 and not isinstance(
            self._extractor, PassThroughExtractor
        )
        conn = self._engine.conn
        primary_id = ""
        promoted_any = False
        try:
            for fact in facts:
                memory = FuzzyMemory(
                    content=fact,
                    time_range=request.time_range,
                    location=request.location,
                    entities=list(request.entities),
                    relations=triple_ids if not primary_id else [],
                    scope=request.scope,
                    importance=request.importance,
                )
                if not primary_id:
                    primary_id = memory.id
                await self._promotion.put(
                    conn, session_id, memory, scope_id=scope_id,
                    auto_promote=not bypass,
                )
                if bypass:
                    # 抽取产出的事实(通常短于阈值)直送 L1,保证可检索
                    await self._promotion.promote(
                        conn, session_id, memory.id, scope_id=scope_id
                    )
                    promoted_any = True
                elif self._l0.get(session_id, memory.id) is None:
                    promoted_any = True
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

        layers = [MemoryLayer.L1_SHORT if promoted_any else MemoryLayer.L0_WORKING]
        if triple_ids:
            layers.append(MemoryLayer.L2_LONG)
        return WriteResponse(
            memory_id=primary_id or memory.id,
            status=WriteStatus.SAVED,
            layers_affected=layers,
        )

    async def _write_relations(
        self,
        request: WriteRequest,
        *,
        scope_id: str | None,
        holder: str,
        detect: bool,
    ) -> list[str]:
        """TripleInput 列表 → Triple 写入存储层,返回 triple.id 列表.

        detect 开启时每条 triple 写前经 Layer 4 检测 WRITE_WRITE(§10.3):
        自动解决(LWW)放行;拒绝/人工策略抛 _RelationConflictError 中止整笔写入。
        """
        scope_str = scope_path(request.scope, scope_id)
        # 批量解析名称引用的实体(H1): 一次 IN 查询代替逐条 SELECT
        name_map = await self._resolve_entity_names(request, scope_id=scope_id)
        triples: list[Triple] = []
        for rel in request.relations:
            subject_id = rel.subject_id or name_map[rel.subject_name or ""]
            if detect:
                guard = await self._concurrency.guard_triple_write(
                    self._engine.conn,
                    scope=scope_str,
                    subject_id=subject_id,
                    predicate=rel.predicate,
                    object_id=rel.object_id,
                    object_value=rel.object_value,
                    chain_id=holder,
                )
                if not ConcurrencyController.should_proceed(guard):
                    assert guard is not None
                    raise _RelationConflictError(
                        ConflictInfo(
                            conflict_type=guard.conflict.conflict_type,
                            conflicting_memory_id=guard.conflict.conflicting_memory_id,
                            message=guard.resolution.message,
                        )
                    )
            triples.append(
                Triple(
                    triple_id=(
                        f"{subject_id}|{rel.predicate}|"
                        f"{rel.object_id or rel.object_value}"
                    ),
                    subject_id=subject_id,
                    predicate=rel.predicate,
                    object_id=rel.object_id,
                    object_value=rel.object_value,
                    scope=request.scope,
                    certainty=rel.certainty,
                )
            )
        # 单事务批量写入(H1): N 条 triple 只刷一次 WAL
        await self._engine.write_triples(triples, scope_id=scope_id)
        return [t.id for t in triples]

    async def _resolve_entity_names(
        self,
        request: WriteRequest,
        *,
        scope_id: str | None,
    ) -> dict[str, str]:
        """批量解析 relations 中的 subject_name → entities.id(H1).

        一次 `name IN (...)` 查出已存在实体;缺失的名称走 _resolve_entity_ref
        的 get-or-create 路径补 concept 实体(同名去重,整笔请求只建一次)。
        """
        names = {
            rel.subject_name
            for rel in request.relations
            if not rel.subject_id and rel.subject_name
        }
        for rel in request.relations:
            if not rel.subject_id and not rel.subject_name:
                raise ValueError("TripleInput 需要 subject_id 或 subject_name 之一")
        if not names:
            return {}
        scope_str = scope_path(request.scope, scope_id)
        placeholders = ",".join("?" for _ in names)
        cursor = await self._engine.conn.execute(
            f"SELECT id, name FROM entities WHERE scope = ? AND name IN ({placeholders})",
            [scope_str, *names],
        )
        resolved = {str(row["name"]): str(row["id"]) for row in await cursor.fetchall()}
        for name in names - resolved.keys():
            resolved[name] = await self._resolve_entity_ref(
                request.scope, scope_id, entity_id=None, name=name
            )
        return resolved

    async def _resolve_entity_ref(
        self,
        scope: MemoryScope,
        scope_id: str | None,
        *,
        entity_id: str | None,
        name: str | None,
    ) -> str:
        """TripleInput 端点解析: id 直通;名称按 scope 查找,缺失自动补 concept 实体."""
        if entity_id:
            return entity_id
        if not name:
            raise ValueError("TripleInput 需要 subject_id 或 subject_name 之一")
        from ..memory.scheduler.bootstrap.seeds import (  # 延迟导入: 同上
            make_entity_id,
        )

        scope_str = scope_path(scope, scope_id)
        conn = self._engine.conn
        cursor = await conn.execute(
            "SELECT id FROM entities WHERE scope = ? AND name = ?",
            [scope_str, name],
        )
        row = await cursor.fetchone()
        if row is not None:
            return str(row["id"])
        entity = Entity(
            entity_id=make_entity_id("concept", name),
            entity_type="concept",
            name=name,
            scope=scope,
        )
        await self._engine.write_entity(entity, scope_id=scope_id)
        await self._vector_store.add_text(conn, entity.name, entity_id=entity.id)
        await conn.commit()
        return entity.id

    # ==================== M.4 检索 ====================

    async def recall(
        self,
        request: RecallRequest,
        *,
        session_id: str | None = None,
        include_archived: bool = False,
    ) -> RecallResponse:
        """检索记忆(§3.2): 多路召回(L0 全量 + L1 向量 KNN + L2 混合)+ 预算裁剪.

        Args:
            request: 检索请求(query/scope_filter/top_k/token_budget/...)
            session_id: L0 会话 ID(None 跳过 L0 通道)
            include_archived: True 时把归档数据(§11.6 冷热分层)并入结果 —
                走 Archiver.query_archived 非向量通道(scope + 关键词),
                以 ``[归档]`` 前缀追加到 context,MemoryRef 标记为 L2_LONG;
                默认 False 只查热数据(向后兼容)
        """
        self._require_initialized()
        scope_filter = self._effective_scope_filter(request.scope_filter)
        built = await self._context_builder.build_context(
            self._engine.conn,
            session_id=session_id,
            query_text=request.query,
            entity_id=request.entity_filter,
            time_range=_time_range_tuple(request),
            scope_filter=scope_filter,
            token_budget=request.token_budget,
            l1_top_k=request.top_k,
            l2_top_k=request.top_k,
        )
        sources = [
            MemoryRef(
                id=src.memory_id,
                layer=_LAYER_MAP[src.layer],
                score=min(max(src.score, 0.0), 1.0),
                snippet=src.content[:_SNIPPET_MAX_CHARS],
            )
            for src in built.sources
        ]
        context_text = built.text
        token_count = built.total_tokens
        if include_archived:
            archived_lines, archived_refs, archived_tokens = await self._recall_archived(
                request, scope_filter
            )
            if archived_lines:
                context_text = (
                    f"{context_text}\n{archived_lines}" if context_text else archived_lines
                )
                sources.extend(archived_refs)
                token_count += archived_tokens
        layers_used = list(dict.fromkeys(ref.layer for ref in sources))
        return RecallResponse(
            context=context_text,
            sources=sources,
            layers_used=layers_used,
            token_count=token_count,
        )

    async def _recall_archived(
        self, request: RecallRequest, scope_filter: ScopeFilter
    ) -> tuple[str, list[MemoryRef], int]:
        """归档召回(include_archived=True 的辅助): 按有效 scope 查归档表.

        Returns:
            (context 追加行, MemoryRef 列表, 追加 token 数)
        """
        from ..memory.quality.archiver import Archiver  # 延迟导入: 按需

        scopes = self._archived_query_scopes(scope_filter)
        archiver = Archiver(self._engine)
        rows: list[dict] = []
        for scope in scopes:
            rows.extend(
                await archiver.query_archived(
                    scope=scope, keyword=request.query, limit=request.top_k
                )
            )
        lines: list[str] = []
        refs: list[MemoryRef] = []
        tokens = 0
        for row in rows[: request.top_k]:
            if row["table"] == "fragments":
                content = str(row["content"])
            else:
                obj = row["object_id"] or row["object_value"] or "?"
                content = f"{row['subject_id']} {row['predicate']} {obj}"
            line = f"[归档] {content}"
            lines.append(line)
            tokens += self._counter.count(line)
            refs.append(
                MemoryRef(
                    id=str(row["id"]),
                    layer=MemoryLayer.L2_LONG,
                    score=0.0,
                    snippet=content[:_SNIPPET_MAX_CHARS],
                )
            )
        return "\n".join(lines), refs, tokens

    def _archived_query_scopes(self, scope_filter: ScopeFilter) -> list[str | None]:
        """把有效 ScopeFilter 折算为归档查询的 scope 列表(None = 不过滤)."""
        if scope_filter.include_all_projects:
            return [None]
        scopes: list[str | None] = []
        if scope_filter.include_global:
            scopes.append("global")
        if scope_filter.include_project:
            scopes.append(scope_path(MemoryScope.PROJECT, scope_filter.include_project))
        if scope_filter.include_tenant:
            scopes.append(scope_path(MemoryScope.TENANT, scope_filter.include_tenant))
        return scopes or [None]

    # ==================== M.6 历史归档(P2 收尾,§11.5/§11.6) ====================

    async def archive_expired(
        self,
        policy: RetentionPolicy | None = None,
        *,
        scope: str | None = None,
    ) -> ArchiveResult:
        """触发一次历史归档(薄封装 Archiver.archive_expired,幂等).

        Args:
            policy: 保留策略(None 用默认: 三元组 365 天 / 片段 180 天)
            scope: 只归档该 scope 全路径(None = 全部)
        """
        self._require_initialized()
        from ..memory.quality.archiver import Archiver  # 延迟导入: 按需

        return await Archiver(self._engine, policy=policy).archive_expired(scope=scope)

    async def restore_archived(
        self,
        *,
        table: str = "both",
        ids: list[str] | None = None,
        scope: str | None = None,
    ) -> RestoreResult:
        """归档数据回迁(显式 unarchive;回迁后普通 recall 即可查到).

        Args:
            table: "triples" / "fragments" / "both"
            ids: 只回迁指定 id(None = 不过滤)
            scope: 只回迁指定 scope 全路径(None = 不过滤)
        """
        self._require_initialized()
        from ..memory.quality.archiver import Archiver  # 延迟导入: 按需

        return await Archiver(self._engine).restore(table=table, ids=ids, scope=scope)

    def _effective_scope_filter(self, scope_filter: ScopeFilter) -> ScopeFilter:
        """scope_filter 未指定项目时,默认带上当前项目(§5.3 典型场景).

        存储层默认 ScopeFilter 只含 global;middleware 已 initialize_project
        时,未显式指定 include_project / include_all_projects 视为查当前项目.
        """
        if (
            scope_filter.include_project is None
            and not scope_filter.include_all_projects
            and self._current_scope_id is not None
        ):
            return replace(scope_filter, include_project=self._current_scope_id)
        return scope_filter

    # ==================== M.5 会话生命周期 ====================

    async def close_session(self, session_id: str, *, persist: bool = True) -> int:
        """关闭会话: L0 快照持久化(save_snapshot)+ 清理热缓存,返回快照条数.

        Args:
            session_id: 会话 ID
            persist: False 时只清理 L0,不写快照(不恢复)
        """
        self._require_initialized()
        memories = self._l0.export_session(session_id)
        if persist:
            await self._snapshots.save_snapshot(session_id, memories)
        self._l0.clear_session(session_id)
        return len(memories)

    async def restore_session(self, session_id: str) -> int:
        """从快照恢复会话到 L0(load_snapshot + import_session),返回恢复条数.

        快照不存在或已过期返回 0(快照保留,幂等可重复恢复).
        """
        self._require_initialized()
        memories = await self._snapshots.load_snapshot(session_id)
        if not memories:
            return 0
        return self._l0.import_session(session_id, memories)


def _time_range_tuple(request: RecallRequest) -> tuple[str, str] | None:
    """RecallRequest.time_range(TimeRange)→ build_context 的 (start, end) ISO 元组.

    relative 模式(如「昨天下午」)无法转换为区间,MVP 忽略(None).
    """
    tr = request.time_range
    if tr is None:
        return None
    if tr.exact is not None:
        iso = to_iso(tr.exact)
        return (iso, iso)
    if tr.approx_start is not None or tr.approx_end is not None:
        return (
            to_iso(tr.approx_start or now_utc()),
            to_iso(tr.approx_end or now_utc()),
        )
    return None


__all__ = ["MemoryMiddleware"]
