"""ID generation helpers (ULID)."""

from __future__ import annotations

from ulid import ULID


def generate_id() -> str:
    """Generate a 26-char ULID string (time-ordered, sortable)."""
    return str(ULID())


def parse_id(s: str) -> ULID:
    """Parse a ULID string back to ULID object (raises ValueError on bad input)."""
    return ULID.from_str(s)


def is_valid_id(s: str) -> bool:
    """Check whether a string is a valid ULID."""
    try:
        ULID.from_str(s)
        return True
    except (ValueError, TypeError):
        return False
