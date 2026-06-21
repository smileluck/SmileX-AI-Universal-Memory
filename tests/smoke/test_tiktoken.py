"""Smoke test: tiktoken token counting."""

import tiktoken


def test_tiktoken_encode():
    enc = tiktoken.get_encoding("cl100k_base")
    tokens = enc.encode("hello world")
    assert len(tokens) > 0


def test_tiktoken_count_differs_by_text():
    enc = tiktoken.get_encoding("cl100k_base")
    short = len(enc.encode("hi"))
    long = len(enc.encode("hello world, this is a longer text"))
    assert long > short


def test_tiktoken_chinese():
    """验证中文 token 计数(多个 token 表示一个汉字)."""
    enc = tiktoken.get_encoding("cl100k_base")
    tokens = enc.encode("你好")
    assert len(tokens) >= 2, "中文通常需要 2+ tokens"
