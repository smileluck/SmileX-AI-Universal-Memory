"""Reranker — 检索结果精排(Embedder 同款架构: Protocol + 默认 no-op + 可选 cross-encoder).

设计(对齐 docs/analyse/mempalace-optimization-roadmap.md 的 rerank 草图):
- Reranker Protocol + 两个后端
    - NoopReranker(默认,零依赖): 不干预排序,recall 行为与现状完全一致
    - CrossEncoderReranker(可选 extra `rerank`): BAAI/bge-reranker-v2-m3 精排
- 工厂: get_reranker() / RerankerConfig,默认返回 NoopReranker
- 插入点: ContextBuilder.build_context 在 _collect 之后对 L1/L2 候选按层精排
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

RERANKER_BACKENDS = ("noop", "cross-encoder")
DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


@runtime_checkable
class Reranker(Protocol):
    """查询-文档相关性精排协议.

    实现约定:
    - rerank() 返回与 documents 等长的相关性分数列表(越大越相关);
      NoopReranker 返回空列表表示"不干预排序",调用方保留原分数
    - 实现应可安全在事件循环中 await(CPU 推理需经 asyncio.to_thread)
    """

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        """对 (query, doc) 对打分,返回与 documents 等长的分数列表."""
        ...


class NoopReranker:
    """默认实现 — 不精排,返回空列表让调用方保留原 RRF 分数."""

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        return []


class CrossEncoderReranker:
    """cross-encoder 精排后端(bge-reranker-v2-m3)— 可选依赖.

    安装: ``pip install "smilex-ai-memory[rerank]"''
    懒加载: 首次 rerank() 时才 import 并加载模型
    (v2-m3 约 2.3GB,首次下载后缓存到 ``~/.cache/``).
    """

    def __init__(
        self,
        model_name: str = DEFAULT_RERANK_MODEL,
        *,
        device: str | None = None,
        max_candidates: int = 50,
        batch_size: int = 16,
    ) -> None:
        if max_candidates <= 0:
            raise ValueError(f"max_candidates 必须为正整数,得到 {max_candidates!r}")
        self._model_name = model_name
        self._device = device
        self._max_candidates = max_candidates
        self._batch_size = batch_size
        self._model: Any | None = None  # 懒加载

    @property
    def max_candidates(self) -> int:
        """单次精排的候选上限(超出部分保留原分数,排在前面的优先)."""
        return self._max_candidates

    def _load_model(self) -> Any:
        """懒加载模型;未安装 sentence-transformers 时给出清晰错误."""
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as e:
            raise ImportError(
                "CrossEncoderReranker 需要 sentence-transformers,但未安装. "
                "安装方式: pip install 'smilex-ai-memory[rerank]' "
                "(或 uv add --optional rerank sentence-transformers). "
                "无模型环境请改用 NoopReranker(默认)."
            ) from e
        self._model = CrossEncoder(self._model_name, device=self._device)
        return self._model

    def _predict(self, query: str, documents: list[str]) -> list[float]:
        model = self._load_model()
        pairs = [(query, doc) for doc in documents]
        scores = model.predict(pairs, batch_size=self._batch_size)
        return [float(s) for s in scores]

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        """CPU/GPU 推理经 to_thread,避免阻塞事件循环;空输入直接返回."""
        if not documents:
            return []
        return await asyncio.to_thread(self._predict, query, documents)


@dataclass(frozen=True)
class RerankerConfig:
    """Reranker 工厂配置(对齐 EmbedderConfig 模式).

    Attributes:
        backend: "noop"(默认,零依赖)或 "cross-encoder"(可选 extra `rerank`)
        model_name: cross-encoder 模型名(默认 BAAI/bge-reranker-v2-m3,多语)
        device: 推理设备(None 自动;MPS 大批次有卡死风险,batch_size 已限 16)
        max_candidates: 单次精排候选上限
        batch_size: 推理批大小
    """

    backend: str = "noop"
    model_name: str = DEFAULT_RERANK_MODEL
    device: str | None = None
    max_candidates: int = 50
    batch_size: int = 16


def get_reranker(config: RerankerConfig | None = None) -> Reranker:
    """工厂: 按 config.backend 返回 Reranker 实例(默认 NoopReranker)."""
    config = config or RerankerConfig()
    if config.backend == "noop":
        return NoopReranker()
    if config.backend == "cross-encoder":
        return CrossEncoderReranker(
            config.model_name,
            device=config.device,
            max_candidates=config.max_candidates,
            batch_size=config.batch_size,
        )
    raise ValueError(
        f"未知 reranker backend {config.backend!r},可选: {RERANKER_BACKENDS}"
    )
