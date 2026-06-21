"""Unit tests for serialization helpers (msgpack + datetime Ext)."""

from datetime import UTC, datetime

from smilex.memory.models import FuzzyMemory, TimeRange, packb, unpackb


def test_packb_unpackb_primitive():
    data = {"a": 1, "b": [1, 2, 3], "c": "hello"}
    packed = packb(data)
    assert isinstance(packed, bytes)
    unpacked = unpackb(packed)
    assert unpacked == data


def test_packb_datetime_roundtrip():
    """datetime 通过 Ext type 1 序列化往返一致."""
    data = {"now": datetime.now(UTC)}
    packed = packb(data)
    unpacked = unpackb(packed)
    assert unpacked["now"] == data["now"]


def test_packb_nested_datetime():
    """嵌套 datetime 序列化."""
    data = {
        "outer": {
            "inner": datetime.now(UTC),
            "list": [datetime.now(UTC), datetime.now(UTC)],
        }
    }
    packed = packb(data)
    unpacked = unpackb(packed)
    assert unpacked["outer"]["inner"] == data["outer"]["inner"]
    assert unpacked["outer"]["list"] == data["outer"]["list"]


def test_packb_fuzzy_memory_dict():
    """FuzzyMemory.to_dict() 后 msgpack 序列化往返."""
    m = FuzzyMemory(
        content="hello",
        time_range=TimeRange(exact=datetime.now(UTC)),
    )
    d = m.to_dict()
    # datetime 字段先转 ISO 字符串(to_dict 内部已转),msgpack 直接处理字符串
    packed = packb(d)
    unpacked = unpackb(packed)
    assert unpacked["content"] == "hello"
    assert unpacked["id"] == m.id


def test_packb_rejects_unknown_type():
    """未知类型应该 raise TypeError."""
    import pytest

    class Foo:
        pass

    with pytest.raises(TypeError, match="not serializable"):
        packb({"obj": Foo()})


def test_packb_binary_smaller_than_json_for_long_strings():
    """msgpack 对长字符串应该比 JSON 紧凑(虽然差距小)."""
    import json

    data = {"content": "x" * 10_000}
    assert len(packb(data)) < len(json.dumps(data).encode("utf-8"))
