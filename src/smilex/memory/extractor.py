"""FactExtractor — 写入时事实抽取(core 保持零 LLM,LLM 后端为可选注入).

设计(对齐 mem0 的记忆形成方式,补齐计数/整合类问题的架构短板):
- FactExtractor Protocol + 两个后端
    - PassThroughExtractor(默认,零依赖): 返回 [content] 原文整块,
      行为与未引入抽取完全一致
    - LLMFactExtractor(可选 extra `llm`): OpenAI 兼容 API 把长对话块
      抽取为原子事实列表(一句一事、保留日期/实体/待办/拥有物/偏好)
- 工厂: get_extractor() / ExtractorConfig,默认返回 PassThroughExtractor
- 失败语义: LLM 调用失败时降级返回 [content](不让 write 崩)
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .observability import get_logger

EXTRACTOR_BACKENDS = ("passthrough", "llm")
DEFAULT_EXTRACT_MODEL = "glm-4.5-flash"

# mem0 风格原子事实抽取 prompt: 一句一事、自包含、保留可检索要素
_EXTRACT_SYSTEM_PROMPT = """\
You are a precise memory extraction engine. Extract self-contained atomic \
facts from the conversation chunk below.

Rules:
1. One fact per line; each fact must be understandable without the conversation context.
2. Preserve specific details: dates, names, quantities, places, item names.
3. Capture especially: tasks/errands (things to do, pick up, return, buy), \
possessions, preferences, habits, plans, and updates that supersede earlier facts.
4. Attribute facts to the speaker when relevant (e.g., "The user ...").
5. Do NOT summarize or merge multiple facts into one line; do NOT add facts \
not present in the text.
6. Output ONLY a JSON array of strings, one fact per element. No other text."""


_logger = get_logger("extractor")


@runtime_checkable
class FactExtractor(Protocol):
    """写入时事实抽取协议.

    实现约定:
    - extract() 返回非空字符串列表(至少 1 条;失败时降级返回 [原文])
    - 实现应是幂等纯函数式的(同一输入恒得同一输出,LLM 后端 temperature=0)
    """

    async def extract(self, content: str) -> list[str]:
        """把一段原始内容抽取为原子事实列表."""
        ...


class PassThroughExtractor:
    """默认实现 — 不抽取,原 content 整块返回(行为与现状一致)."""

    async def extract(self, content: str) -> list[str]:
        return [content]


class LLMFactExtractor:
    """LLM 事实抽取后端(OpenAI 兼容 API)— 可选依赖.

    安装: ``pip install "smilex-ai-memory[llm]"``
    配置(环境变量,均可被构造参数覆盖):
    - SMILEX_EXTRACT_API_KEY   必填(未设置时 extract 直接降级)
    - SMILEX_EXTRACT_BASE_URL  可选(兼容代理/私有网关)
    - SMILEX_EXTRACT_MODEL     默认 glm-4.5-flash(便宜模型即可胜任抽取)
    """

    def __init__(
        self,
        model: str | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 4096,
    ) -> None:
        self._model = model or os.environ.get(
            "SMILEX_EXTRACT_MODEL", DEFAULT_EXTRACT_MODEL
        )
        self._api_key = api_key or os.environ.get("SMILEX_EXTRACT_API_KEY")
        if not self._api_key:
            _logger.warning(
                "llm_extractor_no_key",
                hint="SMILEX_EXTRACT_API_KEY 未配置,抽取降级为原文直通",
            )
        self._base_url = base_url or os.environ.get("SMILEX_EXTRACT_BASE_URL")
        self._max_tokens = max_tokens
        self._client = None  # 懒加载

    def _load_client(self):
        if self._client is not None:
            return self._client
        if not self._api_key:
            # 未配置 key: 不 raise(保持 write 可用),每次降级返回原文
            return None
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise ImportError(
                "LLMFactExtractor 需要 openai SDK,但未安装. "
                "安装方式: pip install 'smilex-ai-memory[llm]' "
                "(或 uv add --optional llm openai). "
                "无 LLM 环境请改用 PassThroughExtractor(默认)."
            ) from e
        self._client = AsyncOpenAI(
            api_key=self._api_key, base_url=self._base_url or None, max_retries=3
        )
        return self._client

    async def extract(self, content: str) -> list[str]:
        """LLM 抽取原子事实;任何失败(未配置/网络/解析)降级返回 [content]."""
        client = self._load_client()
        if client is None:
            # 未配 key: 构造时已 warn 过,这里保持静默零开销降级
            return [content]
        try:
            resp = await client.chat.completions.create(
                model=self._model,
                temperature=0.0,
                max_tokens=self._max_tokens,
                messages=[
                    {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
            )
            text = (resp.choices[0].message.content or "").strip()
            facts = self._parse_facts(text)
            return facts or [content]
        except Exception as exc:  # noqa: BLE001 — 降级不阻断写入,但必须可观测
            _logger.warning(
                "llm_extract_degraded", error=str(exc)[:200], fallback="content"
            )
            return [content]

    @staticmethod
    def _parse_facts(text: str) -> list[str]:
        """解析模型输出: 优先 JSON 数组,失败退化为按行切分."""
        # 容忍 markdown 代码块包裹
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            try:
                arr = json.loads(m.group(0))
                facts = [str(x).strip() for x in arr if str(x).strip()]
                if facts:
                    return facts
            except (json.JSONDecodeError, ValueError):
                pass
        return [
            line.strip().lstrip("-*0123456789. ").strip()
            for line in text.splitlines()
            if line.strip()
        ]


@dataclass(frozen=True)
class ExtractorConfig:
    """FactExtractor 工厂配置.

    Attributes:
        backend: "passthrough"(默认,零依赖)或 "llm"(可选 extra `llm`)
        model: LLM 模型名(默认读 SMILEX_EXTRACT_MODEL,再缺省 glm-4.5-flash)
    """

    backend: str = "passthrough"
    model: str | None = None


def get_extractor(config: ExtractorConfig | None = None) -> FactExtractor:
    """工厂: 按 config.backend 返回 FactExtractor(默认 PassThroughExtractor)."""
    config = config or ExtractorConfig()
    if config.backend == "passthrough":
        return PassThroughExtractor()
    if config.backend == "llm":
        return LLMFactExtractor(config.model)
    raise ValueError(
        f"未知 extractor backend {config.backend!r},可选: {EXTRACTOR_BACKENDS}"
    )
