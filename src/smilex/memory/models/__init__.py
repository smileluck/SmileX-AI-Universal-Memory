"""Layer 0: Data models — 6 StrEnums + 7 dataclasses + serialization helpers.

See docs/design/agent-memory-design.md §5 for the authoritative spec.
"""

from .enums import (
    CertaintyLevel,
    ConflictType,
    LockType,
    MemoryLayer,
    MemoryScope,
    PreemptionPolicy,
)
from .fuzzy import FuzzyLocation, FuzzyMemory, TimeRange, UncertainValue
from .graph import CausalChain, Entity, Triple
from .scope import ScopeFilter
from .serialization import packb, unpackb

__all__ = [
    # Enums
    "CertaintyLevel",
    "ConflictType",
    "LockType",
    "MemoryLayer",
    "MemoryScope",
    "PreemptionPolicy",
    # Fuzzy
    "FuzzyLocation",
    "FuzzyMemory",
    "TimeRange",
    "UncertainValue",
    # Graph
    "CausalChain",
    "Entity",
    "Triple",
    # Scope
    "ScopeFilter",
    # Serialization
    "packb",
    "unpackb",
]
