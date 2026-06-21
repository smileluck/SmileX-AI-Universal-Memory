"""Time helpers — UTC default, ISO 8601 formatting."""

from __future__ import annotations

from datetime import UTC, datetime


def now_utc() -> datetime:
    """Current time in UTC (timezone-aware)."""
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    """Serialize datetime to ISO 8601 string with microseconds + Z suffix."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def from_iso(s: str) -> datetime:
    """Parse ISO 8601 string back to timezone-aware datetime."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
