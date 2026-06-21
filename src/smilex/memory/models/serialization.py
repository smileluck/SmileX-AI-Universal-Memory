"""Serialization helpers — MessagePack with datetime Ext.

Per module doc 01:
- msgpack 热路径(cachebox 内部缓存)
- Ext type 1 for datetime
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import msgpack

from ...utils.timeutil import from_iso, to_iso

DATETIME_EXT_CODE = 1


def _default(obj: Any) -> Any:
    """msgpack default hook — handle datetime via Ext."""
    if isinstance(obj, datetime):
        return msgpack.ExtType(DATETIME_EXT_CODE, to_iso(obj).encode("utf-8"))
    raise TypeError(f"Object of type {type(obj).__name__} is not serializable")


def _ext_hook(code: int, data: bytes) -> Any:
    """msgpack ext_hook — reverse Ext back to datetime."""
    if code == DATETIME_EXT_CODE:
        return from_iso(data.decode("utf-8"))
    return msgpack.ExtType(code, data)


def packb(obj: Any) -> bytes:
    """Pack object to msgpack bytes."""
    return msgpack.packb(obj, default=_default, use_bin_type=True)


def unpackb(data: bytes) -> Any:
    """Unpack msgpack bytes."""
    return msgpack.unpackb(data, ext_hook=_ext_hook, raw=False)
