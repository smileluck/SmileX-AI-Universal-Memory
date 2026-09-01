"""TokenCounter — tiktoken cl100k_base token 计数.

Per module doc 03 §2.2:
- tiktoken 主方案(Rust 实现,100 万 tokens/s,离线)
- 加载失败时 fallback 到 chars/4 粗略估算(±20%)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import FuzzyMemory

DEFAULT_ENCODING = "cl100k_base"
ESTIMATE_CHARS_PER_TOKEN = 4


class TokenCounter:
    """Token 计数器 — cl100k_base BPE,失败时降级为估算.

    使用方法:
        counter = TokenCounter()
        n = counter.count("hello world")
        n = counter.count_memory(fuzzy_memory)
    """

    def __init__(self, encoding_name: str = DEFAULT_ENCODING) -> None:
        self._encoding_name = encoding_name
        try:
            import tiktoken

            self._enc = tiktoken.get_encoding(encoding_name)
            self._mode = "tiktoken"
        except Exception:  # 任何加载失败都降级为估算
            self._enc = None
            self._mode = "estimate"

    @property
    def mode(self) -> str:
        """当前计数模式: "tiktoken" 或 "estimate"."""
        return self._mode

    def count(self, text: str) -> int:
        """计算文本 token 数."""
        if not text:
            return 0
        if self._mode == "tiktoken":
            # disallowed_special=(): 文本中出现的 <|endoftext|> 等特殊 token 字面量
            # 按普通文本编码计数,否则 encode 直接抛异常(计数不应因内容崩掉)
            return len(self._enc.encode(text, disallowed_special=()))
        return max(1, len(text) // ESTIMATE_CHARS_PER_TOKEN)

    def count_memory(self, memory: FuzzyMemory) -> int:
        """计算 FuzzyMemory.content 的 token 数(content 为 None 返回 0)."""
        if memory.content is None:
            return 0
        return self.count(memory.content)

    def count_memories(self, memories: list[FuzzyMemory]) -> int:
        """批量计算多条记忆的总 token 数."""
        return sum(self.count_memory(m) for m in memories)


__all__ = ["TokenCounter"]
