"""ProjectBootstrap — 项目冷启动入口(§9.5.3 / 04-layer3 §3.4).

六步流程(§9.5.2,MVP 范围 = 步骤 1/2/5,步骤 3/4 留 P1):
    [1] 初始化向导: 元信息(名称/描述/技术栈)+ 结构化向导答案 → 首批实体
    [2] 项目模板: 显式指定或按技术栈匹配 web/cli/data/agent,注入骨架
    [5] 主动学习: 基于谓词缺口生成待答问题,决定 stage
    [6] 首轮检索就绪: 实体/三元组 + (可选)向量写入完成

MVP 非交互: wizard_answers 为结构化 dict(见 onboarding.py);
LLM 提取默认规则兜底(EXECUTION_PLAN_GAPS §3.6),接口留 LLMProvider Protocol.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ....utils.ids import generate_id
from ...contracts import (
    PROJECT_TEMPLATES,
    BootstrapStage,
    ProjectContext,
    ProjectInitRequest,
)
from ...models import Entity, MemoryScope, Triple
from ...storage.storage_engine import StorageEngine, parse_scope_path
from .active_learner import ActiveLearner, LLMProvider
from .onboarding import WizardQuestion, process_answers, tech_seeds
from .readme_parser import extract_from_readme
from .seeds import (
    STATE_RELATION_TYPE,
    EntitySeed,
    ExtractionResult,
    TripleSeed,
    normalize_name,
)
from .templates import load_template, match_template

if TYPE_CHECKING:
    from ...storage.vector_store import VectorStore


class ProjectBootstrap:
    """项目冷启动器.

    Args:
        storage: Layer 1 StorageEngine(已 initialize)
        vector_store: 可选;提供时为每个新实体写入名称向量(knn 检索通道)
        llm: 可选 LLMProvider;None 时 README/主动学习走规则兜底

    使用方法:
        bootstrap = ProjectBootstrap(storage)
        ctx = await bootstrap.initialize(ProjectInitRequest(name="demo", ...))
        pending = await bootstrap.pending_questions(ctx.scope)
    """

    def __init__(
        self,
        storage: StorageEngine,
        *,
        vector_store: VectorStore | None = None,
        llm: LLMProvider | None = None,
    ) -> None:
        if not storage.is_initialized:
            raise RuntimeError("StorageEngine 未初始化,请先调用 await initialize()")
        self._storage = storage
        self._vector_store = vector_store
        self._llm = llm
        self._learner = ActiveLearner(llm)

    # ==================== 主流程(§9.5.2) ====================

    async def initialize(
        self,
        request: ProjectInitRequest,
        *,
        wizard_answers: dict | None = None,
    ) -> ProjectContext:
        """新项目冷启动: 创建 project scope + 写入初始实体/三元组.

        同名项目复用既有 scope(项目实体 entity_id 全局稳定为
        "project:{归一名}",取其所在 scope): 重复冷启动/批量导入不产生
        重复 scope,时序记忆按 (scope, fragment_id) 的幂等去重才能生效.

        Returns:
            ProjectContext(scope 全路径 "project:{ulid}", stage, 计数)
        """
        cursor = await self._storage.conn.execute(
            "SELECT scope FROM entities WHERE entity_id = ? AND valid_to IS NULL LIMIT 1",
            [f"project:{normalize_name(request.name)}"],
        )
        row = await cursor.fetchone()
        scope = str(row[0]) if row else f"project:{generate_id()}"

        # [1] 初始化向导: 项目实体 + 描述 + 技术栈
        result = ExtractionResult()
        result.entities.append(
            EntitySeed(
                name=request.name,
                entity_type="concept",
                entity_id=f"project:{normalize_name(request.name)}",
            )
        )
        if request.description:
            result.triples.append(
                TripleSeed(
                    subject=request.name,
                    predicate="has_description",
                    object_value=request.description,
                    relation_type=STATE_RELATION_TYPE,
                )
            )
        result.merge(tech_seeds(request.name, request.tech_stack))
        if wizard_answers:
            result.merge(process_answers(request.name, wizard_answers))

        # [1b] README 解析(规则版;有 LLM 时追加 LLM 提取)
        if request.readme_content:
            result.merge(extract_from_readme(request.name, request.readme_content))
            if self._llm is not None:
                result.merge(
                    await self._llm.extract(
                        request.readme_content, context=request.name
                    )
                )

        # [2] 项目模板: 显式指定,否则按技术栈 + README 技术名词匹配
        template_key = request.template or match_template(
            [
                *request.tech_stack,
                *(e.name for e in result.entities if e.entity_id.startswith("tech:")),
            ]
        )
        if template_key is not None:
            template = load_template(template_key)
            result.entities.extend(template.entities)
            result.triples.extend(template.triples)

        # [6] 持久化(去重 + 引用解析 + 向量)
        entity_count, triple_count = await self.apply_seeds(scope, result)

        # [5] 主动学习: 缺口分析决定 stage
        pending = await self.pending_questions(scope)
        stage = BootstrapStage.READY if not pending else BootstrapStage.ACTIVE_LEARNING

        return ProjectContext(
            scope=scope,
            name=request.name,
            stage=stage,
            tech_stack=list(request.tech_stack),
            entity_count=entity_count,
            triple_count=triple_count,
        )

    # ==================== 种子持久化 ====================

    async def apply_seeds(
        self,
        scope: str,
        result: ExtractionResult,
        *,
        known: dict[str, Any] | None = None,
    ) -> tuple[int, int]:
        """把种子集写入指定 scope(幂等: 已存在的实体/三元组跳过).

        Args:
            known: 可选的跨调用已存在键缓存(H7),结构
                {"name_to_id": dict[str, str], "seen": set[str], "triple_keys": set[tuple]}。
                传入时跳过该 scope 的全量重载,由本函数增量更新 —— 批量导入每批
                调用一次本方法,缓存由调用方持有可避免 O(批数 × scope 规模) 扫描。

        Returns:
            (实体总数, 三元组总数) — 该 scope 下当前总量(含已有)
        """
        scope_enum, scope_id = parse_scope_path(scope)
        if scope_enum is MemoryScope.GLOBAL:
            raise ValueError("冷启动种子只能写入 project/tenant scope")

        conn = self._storage.conn
        if known is None:
            known = {}
        if "name_to_id" not in known:
            # 已存在的实体: entity_id → (id, name)
            cursor = await conn.execute(
                "SELECT id, entity_id, name FROM entities WHERE scope = ?", [scope]
            )
            existing = {r["entity_id"]: (r["id"], r["name"]) for r in await cursor.fetchall()}
            known["name_to_id"] = {name: id_ for id_, name in existing.values()}
            known["seen"] = set(existing)
            cursor = await conn.execute(
                "SELECT subject_id, predicate, object_id, object_value "
                "FROM triples WHERE scope = ?",
                [scope],
            )
            known["triple_keys"] = {
                (r["subject_id"], r["predicate"], r["object_id"], r["object_value"])
                for r in await cursor.fetchall()
            }
        name_to_id: dict[str, str] = known["name_to_id"]
        seen: set[str] = known["seen"]
        triple_keys: set[tuple] = known["triple_keys"]

        # 1. 实体: 按 entity_id 去重(ExtractionResult.merge 已做,这里再兜底),
        #    批量单事务写入(H7: 代替逐条 write_entity 独立事务)
        new_entities: list[Entity] = []
        for seed in result.entities:
            if seed.entity_id in seen:
                continue
            seen.add(seed.entity_id)
            entity = Entity(
                entity_id=seed.entity_id,
                entity_type=seed.entity_type,
                name=seed.name,
                scope=scope_enum,
            )
            name_to_id[seed.name] = entity.id
            new_entities.append(entity)
        await self._storage.write_entities(new_entities, scope_id=scope_id)

        # 2. 向量(可选通道): 仅为新实体写名称向量
        if self._vector_store is not None and new_entities:
            for entity in new_entities:
                await self._vector_store.add_text(
                    conn, entity.name, entity_id=entity.id
                )
            await conn.commit()

        # 3. 三元组: 解析名称引用 → ULID;缺失端点自动补 concept 实体,
        #    批量单事务写入(H7: 代替逐条 write_triple 独立事务)
        new_triples: list[Triple] = []
        for seed in result.triples:
            subject_id = await self._resolve_entity(
                scope_enum, scope_id, seed.subject, name_to_id, seen
            )
            object_id = None
            if seed.object_name:
                object_id = await self._resolve_entity(
                    scope_enum, scope_id, seed.object_name, name_to_id, seen
                )
            key = (subject_id, seed.predicate, object_id, seed.object_value)
            if key in triple_keys:
                continue
            triple_keys.add(key)
            new_triples.append(
                Triple(
                    triple_id=f"{subject_id}|{seed.predicate}|{object_id or seed.object_value}",
                    subject_id=subject_id,
                    predicate=seed.predicate,
                    object_id=object_id,
                    object_value=seed.object_value,
                    scope=scope_enum,
                    relation_type=seed.relation_type,
                )
            )
        await self._storage.write_triples(new_triples, scope_id=scope_id)

        # 4. 返回该 scope 当前总量
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM entities WHERE scope = ?", [scope]
        )
        entity_total = int((await cursor.fetchone())[0])
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM triples WHERE scope = ?", [scope]
        )
        triple_total = int((await cursor.fetchone())[0])
        return entity_total, triple_total

    async def _resolve_entity(
        self,
        scope_enum: MemoryScope,
        scope_id: str | None,
        name: str,
        name_to_id: dict[str, str],
        seen: set[str] | None = None,
    ) -> str:
        """名称 → 实体 ULID;不存在时自动创建 concept 实体.

        seen 为 apply_seeds 的 entity_id 去重集合(H7 跨批缓存):
        新建实体的 entity_id 也要登记,避免后续批次重复创建同名 concept。
        """
        if name in name_to_id:
            return name_to_id[name]
        entity = Entity(
            entity_id=f"concept:{normalize_name(name)}",
            entity_type="concept",
            name=name,
            scope=scope_enum,
        )
        await self._storage.write_entity(entity, scope_id=scope_id)
        name_to_id[name] = entity.id
        if seen is not None:
            seen.add(entity.entity_id)
        if self._vector_store is not None:
            await self._vector_store.add_text(
                self._storage.conn, entity.name, entity_id=entity.id
            )
            await self._storage.conn.commit()
        return entity.id

    # ==================== 主动学习(缺口提问) ====================

    async def pending_questions(self, scope: str) -> list[WizardQuestion]:
        """基于该 scope 已有谓词的缺口,返回待答问题列表."""
        cursor = await self._storage.conn.execute(
            "SELECT DISTINCT predicate FROM triples WHERE scope = ?", [scope]
        )
        known = {r["predicate"] for r in await cursor.fetchall()}
        return self._learner.pending_questions(known)

    async def record_answer(
        self, scope: str, project_name: str, question_key: str, answer: str
    ) -> tuple[int, int]:
        """记录一轮主动学习答案(提取 + 写入),返回该 scope 当前总量."""
        seeds = await self._learner.answer_to_seeds(project_name, question_key, answer)
        return await self.apply_seeds(scope, seeds)


__all__ = [
    "PROJECT_TEMPLATES",
    "ProjectBootstrap",
]
