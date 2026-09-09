"""MemoryMiddleware — Agent 访问记忆系统的唯一入口(主文档 §3.1).

组合 Layer 0-3,对外提供最小契约(M.2-M.5):
- initialize_project: 委托 ProjectBootstrap(§9.5 冷启动),计时组装响应
- clone_project: 委托 CrossProjectCloner(§9.5.1 机制 6 跨项目克隆,P2)
- write: 构建 FuzzyMemory → L0(PromotionManager.put,>800 tokens 自动晋升 L1),
  relations(TripleInput)解析/写入存储层,实体/晋升片段写入向量
- recall: 委托 ContextBuilder.build_context(多路召回 + Token 预算裁剪)
- close_session / restore_session: L0 快照持久化 + 会话清理/恢复

2026-09 拆分: M.2/M.3/M.4 三条路径分别在 _bootstrap_facade.py /
_write_path.py / _recall_path.py(方法体不变,经 mixin 组合回本类);
本模块保留构造/生命周期/M.5 会话/M.6 归档,模块路径不变.

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

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..memory.concurrency import ConcurrencyController
from ..memory.embedder import Embedder, get_embedder
from ..memory.extractor import FactExtractor, PassThroughExtractor
from ..memory.lifecycle.context_builder import ContextBuilder
from ..memory.lifecycle.l0_snapshot import L0SnapshotStore
from ..memory.lifecycle.l0_working_memory import L0WorkingMemory
from ..memory.lifecycle.promotion import PromotionManager
from ..memory.lifecycle.token_counter import TokenCounter
from ..memory.observability import get_logger
from ..memory.pii import NoopPIIMasker, PIIMasker
from ..memory.reranker import Reranker
from ..memory.storage.storage_engine import StorageEngine
from ..memory.storage.vector_store import VectorStore
from ._bootstrap_facade import _BootstrapFacadeMixin
from ._recall_path import _RecallPathMixin
from ._write_path import _WritePathMixin

if TYPE_CHECKING:
    from ..memory.quality.archiver import ArchiveResult, RestoreResult, RetentionPolicy
    from ..memory.scheduler.bootstrap.project_bootstrap import ProjectBootstrap

# 历史说明: ProjectBootstrap / make_entity_id(scheduler.bootstrap)此前必须延迟导入
# — bootstrap 反向依赖 middlewares.dto 形成循环;dto 已下沉为 memory.contracts
# (2026-09),循环不复存在,方法内延迟导入仅为避免启动期重依赖,可按需收紧.


class MemoryMiddleware(_WritePathMixin, _RecallPathMixin, _BootstrapFacadeMixin):
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
            cross-encoder 见 smilex.memory.reranker)
        fact_extractor: 写入时事实抽取器(None 时 PassThroughExtractor,
            原 content 整块写入,行为与现状一致;LLM 抽取见
            smilex.memory.extractor)
        facts_bypass_l0: 抽取产出的事实是否跳过 L0 直送 L1 晋升
            (默认 True;短事实留在 L0 对 recall 不可见,仅 PassThrough 下无影响)
        pii_masker: 写入前 PII 脱敏器(None 时 NoopPIIMasker 直通 —
            记忆系统常需记住用户联系方式,默认不脱敏;正则后端见
            smilex.memory.pii)
        track_access: 检索反馈闭环(默认 True)— recall 命中的 fragment
            累加 access_count/last_accessed_at,forget 任务据此续命/升值
            (§ 主动优化;关闭则 recall 保持纯只读)
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
        pii_masker: PIIMasker | None = None,
        track_access: bool = True,
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
        self._pii = pii_masker or NoopPIIMasker()
        self._track_access = track_access
        self._snapshots = L0SnapshotStore(self._engine)
        self._bootstrap: ProjectBootstrap | None = None
        # 最近一次 initialize_project 的项目 ID,write 的 scope_id 缺省值
        self._current_scope_id: str | None = None
        # 事件出口(server 层注入 scheduler.emit;None = 无调度器,空操作).
        # 事件类型见调度器映射: session_end → forget / memory_full → consolidate
        self.event_sink: Callable[[str, dict | None], object] | None = None

    def _emit_event(self, event_type: str, payload: dict | None = None) -> None:
        """事件转发(fire-and-forget): event_sink 未接线时空操作,失败只留痕."""
        if self.event_sink is None:
            return
        try:
            self.event_sink(event_type, payload)
        except Exception:  # noqa: BLE001 — 事件失败不影响写入/会话关闭
            get_logger("memory").warning("event_emit_failed", event=event_type)

    # ==================== 生命周期 ====================

    async def initialize(self) -> None:
        """初始化存储引擎与冷启动器(幂等: 已初始化则跳过)."""
        if not self._engine.is_initialized:
            await self._engine.initialize()
        from ..memory.scheduler.bootstrap.project_bootstrap import (
            ProjectBootstrap,  # 延迟导入: 避免启动期重依赖
        )

        self._bootstrap = ProjectBootstrap(
            self._engine, vector_store=self._vector_store
        )

    async def close(self) -> None:
        """关闭: flush 所有活跃会话到 L1(持久化闭环),再清 L0、关自建引擎.

        未 flush 的 L0 记忆会随进程消失 — 此前版本直接 clear() 丢弃,
        是"默认配置下短记忆不持久"的根因之一。
        """
        if self._engine.is_initialized:
            for sid in self._l0.session_ids():
                try:
                    await self.close_session(sid)
                except Exception:  # noqa: BLE001 — 单会话 flush 失败不阻断关闭
                    get_logger("memory").warning(
                        "session_flush_failed", session_id=sid
                    )
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
    def vector_store(self) -> VectorStore:
        """向量存储(与写入路径共享 embedder;调度器 semantic 任务注入用)."""
        return self._vector_store

    @property
    def concurrency(self) -> ConcurrencyController:
        """Layer 4 并发控制器(测试/诊断/任务链接入用)."""
        return self._concurrency

    @property
    def pii_masker(self) -> PIIMasker:
        """写入前 PII 脱敏器(审计层取脱敏后摘要用)."""
        return self._pii

    @property
    def current_scope_id(self) -> str | None:
        """最近一次 initialize_project 的项目 ID(工具层 scope 缺省用)."""
        return self._current_scope_id

    def _require_initialized(self) -> None:
        if not self._engine.is_initialized:
            raise RuntimeError("MemoryMiddleware 未初始化,请先调用 await initialize()")

    async def promote_memory(
        self,
        memory_id: str,
        *,
        session_id: str,
        scope_id: str | None = None,
    ) -> None:
        """把 L0 中的指定记忆立即晋升 L1(落库 + 向量;教训等高价值写入用).

        正常写入按 promotion_threshold 自动晋升;短而重要的内容(如错误教训)
        需要跨会话持久时显式调用 — 与 facts_bypass_l0 的直送路径同机制。
        """
        self._require_initialized()
        await self._promotion.promote(
            self._engine.conn,
            session_id,
            memory_id,
            scope_id=scope_id or self._current_scope_id,
        )
        await self._engine.conn.commit()

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

    # ==================== M.5 会话生命周期 ====================

    async def close_session(self, session_id: str, *, persist: bool = True) -> int:
        """关闭会话: 该会话全部 L0 记忆晋升 L1 持久化 + 清理热缓存.

        晋升按各记忆自身携带的 scope/scope_id 落库(支持一个会话内混合
        project/global 写入)。完成后经 event_sink 发 session_end 事件
        (调度器映射 → forget)。

        Args:
            session_id: 会话 ID
            persist: False 时只清理 L0 不晋升(显式丢弃)

        Returns:
            晋升到 L1 的记忆条数

        历史语义(2026-09 前)为 l0_snapshot 快照持久化,但快照只能恢复回
        L0、检索通道不可见,等于"存而不用" — 改为晋升后该路径废弃
        (restore_session/snapshot 模块保留,仅服务旧库快照)。
        """
        self._require_initialized()
        promoted = 0
        if persist:
            try:
                promoted = len(
                    await self._promotion.flush_session(self._engine.conn, session_id)
                )
                await self._engine.conn.commit()
            except Exception:
                await self._engine.conn.rollback()
                raise
        self._l0.clear_session(session_id)
        self._emit_event("session_end", {"session_id": session_id})
        return promoted

    async def restore_session(self, session_id: str) -> int:
        """从快照恢复会话到 L0(load_snapshot + import_session),返回恢复条数.

        快照不存在或已过期返回 0(快照保留,幂等可重复恢复).
        注: close_session 已不再产生新快照,本方法仅服务旧库遗留快照。
        """
        self._require_initialized()
        memories = await self._snapshots.load_snapshot(session_id)
        if not memories:
            return 0
        return self._l0.import_session(session_id, memories)


__all__ = ["MemoryMiddleware"]
