"""Unit tests for TokenCounter (L2.6, tiktoken cl100k_base).

验证:
- count() 基本计数(tiktoken 精确值)
- 空字符串 / None content
- count_memory / count_memories
- estimate fallback 模式
"""

from __future__ import annotations

import sys
from unittest.mock import patch

from smilex.memory.lifecycle import TokenCounter
from smilex.memory.models import FuzzyMemory


def test_default_mode_is_tiktoken():
    counter = TokenCounter()
    assert counter.mode == "tiktoken"


def test_count_english():
    """cl100k_base 下 "hello world" 是 2 个 token."""
    counter = TokenCounter()
    assert counter.count("hello world") == 2


def test_count_chinese():
    counter = TokenCounter()
    n = counter.count("你好世界")
    assert n > 0
    # 与 tiktoken 直接编码结果一致
    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")
    assert n == len(enc.encode("你好世界"))


def test_count_empty_string():
    counter = TokenCounter()
    assert counter.count("") == 0


def test_count_memory():
    counter = TokenCounter()
    m = FuzzyMemory(content="hello world")
    assert counter.count_memory(m) == 2


def test_count_memory_none_content():
    counter = TokenCounter()
    m = FuzzyMemory(content=None)
    assert counter.count_memory(m) == 0


def test_count_memories():
    counter = TokenCounter()
    memories = [
        FuzzyMemory(content="hello world"),  # 2
        FuzzyMemory(content=None),  # 0
        FuzzyMemory(content="hello"),  # 1
    ]
    assert counter.count_memories(memories) == 3


def test_estimate_fallback_mode():
    """tiktoken 不可用时降级为 chars/4 估算."""
    with patch.dict(sys.modules, {"tiktoken": None}):
        counter = TokenCounter()
    assert counter.mode == "estimate"
    assert counter.count("a" * 40) == 10
    assert counter.count("") == 0
    # 不足 4 字符的非空文本至少算 1 个 token
    assert counter.count("ab") == 1
