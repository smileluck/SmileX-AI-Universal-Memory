"""Smoke test: python-ulid generation and ordering."""

import time

from ulid import ULID


def test_ulid_generation():
    u = ULID()
    assert len(str(u)) == 26


def test_ulid_monotonicity_across_milliseconds():
    """ULID 时间戳部分(前 10 字符)严格单调 — 跨毫秒生成时验证.

    注: 同一毫秒内的随机后缀不保证单调(ULID 规范允许),所以这里
    显式 sleep 1ms 跨毫秒生成。
    """
    ids = []
    for _ in range(10):
        ids.append(str(ULID()))
        time.sleep(0.002)  # 2ms,确保时间戳部分不同
    assert ids == sorted(ids), "跨毫秒 ULID 应该严格单调"


def test_ulid_timestamp_portion_extractable():
    """ULID 前 10 字符是时间戳(Crockford Base32 编码的毫秒)."""
    u = ULID()
    s = str(u)
    assert len(s) == 26
    # ULID 时间戳属性(.timestamp) 是 float,可读
    assert u.timestamp > 0


def test_ulid_parse_roundtrip():
    s = str(ULID())
    parsed = ULID.from_str(s)
    assert str(parsed) == s
