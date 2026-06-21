"""Smoke test: msgpack pack/unpack with datetime + nested dict."""

from datetime import UTC, datetime

import msgpack


def test_msgpack_roundtrip():
    original = {
        "id": "01HXXXXXX",
        "content": "hello",
        "created_at": datetime.now(UTC).isoformat(),
        "entities": ["e1", "e2"],
        "importance": 0.5,
    }
    packed = msgpack.packb(original, use_bin_type=True)
    unpacked = msgpack.unpackb(packed, raw=False)
    assert unpacked == original


def test_msgpack_more_compact_than_json():
    """msgpack 应该比 JSON 紧凑(对长字符串,header 开销小)."""
    import json

    data = {"content": "x" * 10_000}
    packed = msgpack.packb(data, use_bin_type=True)
    as_json = json.dumps(data).encode("utf-8")
    assert len(packed) < len(as_json), (
        f"msgpack ({len(packed)}) 应该比 JSON ({len(as_json)}) 紧凑"
    )
