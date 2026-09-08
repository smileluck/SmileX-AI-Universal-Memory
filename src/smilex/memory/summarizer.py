"""Summarizer — 长记忆摘要压缩(Embedder/Extractor 同款架构: Protocol + 默认规则 + 可选 LLM).

设计(对齐 03-layer2 §5 演进路径 "P2: L3 语义 + LLM 摘要压缩"):
- Summarizer Protocol + 两个后端
    - RuleSummarizer(默认,零依赖): 截取前 N 字 + 句末标点回退(原任务内联
      逻辑原样迁入,行为逐字一致)
    - LLMSummarizer(可选 extra ``llm``): OpenAI 兼容 API 生成摘要,
      temperature=0;GLM 系模型附 ``{"thinking": {"type": "disabled"}}``
      防推理吃 max_tokens(复刻 benchmarks/_shared/llm_client 的处理)
- 降级语义(与 LLMFactExtractor 一致): 未配置 key / 未装 openai / 调用失败
  一律回退规则摘要,绝不抛 — 调度器会把 FAILED 任务的 checkpoint 删除,
  摘要任务必须自愈
- 工厂: get_summarizer() / SummarizerConfig,默认返回 RuleSummarizer
- 插入点: scheduler/tasks/summarize.py(批内逐条调用;【摘要】前缀与
  ":summary" 幂等键由任务侧负责,本组件只产出正文)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .observability import get_logger

_logger = get_logger("summarizer")

SUMMARIZER_BACKENDS = ("rule", "llm")
DEFAULT_SUMMARIZE_MODEL = "glm-4.5-flash"

_SUMMARY_SYSTEM_PROMPT = (
    "你是记忆压缩器。把给定的长篇记忆条目压缩为一段忠实、信息密集的中文摘要,"
    "保留:关键实体、结论、决定性事实、时间与数量;"
    "丢弃:寒暄、重复、代码块细节(保留一句话说明其作用)。"
    "只输出摘要正文,不要任何前缀、标题或解释,不超过 120 字。"
)


@runtime_checkable
class Summarizer(Protocol):
    """摘要协议: 输入原文,输出摘要正文(不带【摘要】前缀)."""

    async def summarize(self, content: str) -> str:
        """压缩 content 为摘要文本."""
        ...


def rule_summary(content: str, summary_length: int = 120) -> str:
    """规则版摘要:截取前 summary_length 字,优先在句末标点处截断.

    (原 summarize 任务内联逻辑原样迁入,行为逐字一致。)
    """
    if len(content) <= summary_length:
        return content
    head = content[:summary_length]
    cut = max(head.rfind(p) for p in ("。", "!", "?", "!", "?"))
    if cut > 0:
        head = head[: cut + 1]
    return f"{head}…(原文共 {len(content)} 字)"


class RuleSummarizer:
    """默认实现 — 零依赖规则截取."""

    def __init__(self, summary_length: int = 120) -> None:
        if summary_length <= 0:
            raise ValueError(f"summary_length 必须为正整数,得到 {summary_length!r}")
        self.summary_length = summary_length

    async def summarize(self, content: str) -> str:
        return rule_summary(content, self.summary_length)


class LLMSummarizer:
    """LLM 摘要后端 — 可选依赖(extra ``llm``),失败全量降级规则摘要.

    环境变量(构造参数优先): ``SMILEX_SUMMARIZE_API_KEY``(必填,未设置时
    永久降级)/ ``SMILEX_SUMMARIZE_BASE_URL``(可选,OpenAI 兼容代理)/
    ``SMILEX_SUMMARIZE_MODEL``(默认 glm-4.5-flash)。
    """

    def __init__(
        self,
        model: str | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 512,
        summary_length: int = 120,
    ) -> None:
        self._model = model or os.environ.get("SMILEX_SUMMARIZE_MODEL") or DEFAULT_SUMMARIZE_MODEL
        self._api_key = api_key or os.environ.get("SMILEX_SUMMARIZE_API_KEY")
        self._base_url = base_url or os.environ.get("SMILEX_SUMMARIZE_BASE_URL")
        self._max_tokens = max_tokens
        self._fallback = RuleSummarizer(summary_length)
        self._client: Any | None = None  # 懒加载

    def _load_client(self) -> Any | None:
        """懒加载 AsyncOpenAI;无 key 返回 None(永久降级),缺包给清晰错误."""
        if self._client is not None:
            return self._client
        if not self._api_key:
            return None
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise ImportError(
                "LLMSummarizer 需要 openai,但未安装. "
                "安装方式: pip install 'smilex-ai-memory[llm]'. "
                "无 LLM 环境请保持默认 RuleSummarizer."
            ) from e
        self._client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._base_url or None,
            max_retries=3,
        )
        return self._client

    async def summarize(self, content: str) -> str:
        client = self._load_client()
        if client is None:
            return await self._fallback.summarize(content)
        extra_body: dict[str, Any] | None = None
        if "glm" in self._model.lower():
            extra_body = {"thinking": {"type": "disabled"}}
        try:
            resp = await client.chat.completions.create(
                model=self._model,
                temperature=0.0,
                max_tokens=self._max_tokens,
                messages=[
                    {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                **({"extra_body": extra_body} if extra_body else {}),
            )
            text = (resp.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001 — 摘要失败降级规则版,不拖垮任务
            _logger.warning(
                "llm_summarize_failed", error=str(exc), fallback="rule"
            )
            return await self._fallback.summarize(content)
        return text or await self._fallback.summarize(content)


@dataclass(frozen=True)
class SummarizerConfig:
    """Summarizer 工厂配置(对齐 ExtractorConfig 模式).

    Attributes:
        backend: "rule"(默认,零依赖)或 "llm"(可选 extra `llm` +
            SMILEX_SUMMARIZE_* 环境变量)
        model: LLM 模型名(None 走 SMILEX_SUMMARIZE_MODEL → glm-4.5-flash)
        summary_length: 规则摘要截取长度(也是 LLM 降级的兜底长度)
    """

    backend: str = "rule"
    model: str | None = None
    summary_length: int = 120


def get_summarizer(config: SummarizerConfig | None = None) -> Summarizer:
    """工厂: 按 config.backend 返回 Summarizer 实例(默认 RuleSummarizer)."""
    config = config or SummarizerConfig()
    if config.backend == "rule":
        return RuleSummarizer(config.summary_length)
    if config.backend == "llm":
        return LLMSummarizer(
            config.model, summary_length=config.summary_length
        )
    raise ValueError(
        f"未知 summarizer backend {config.backend!r},可选: {SUMMARIZER_BACKENDS}"
    )
