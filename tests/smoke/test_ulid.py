"""Smoke test: python-ulid generation and monotonicity."""

from ulid import ULID


def test_ulid_generation():
    u = ULID()
    assert len(str(u)) == 26


def test_ulid_monotonicity():
    ids = [str(ULID()) for _ in range(100)]
    assert ids == sorted(ids), "ULID 应该时间有序(monotonic)"


def test_ulid_parse_roundtrip():
    s = str(ULID())
    parsed = ULID.from_str(s)
    assert str(parsed) == s
