"""Query modules — temporal/spatial/graph/causal/hybrid/scope (per §6.5).

MVP 完成: temporal(L1.8), spatial(L1.9), graph(L1.10), causal(L1.11),
         scope(L1.12), hybrid(L1.13)
"""

from .causal import trace_causal_chain
from .graph import find_n_degree_relations, find_path
from .hybrid import HybridQuery, hybrid_memory_search, rrf_fusion
from .scope import (
    apply_scope_filter,
    build_scope_clause,
    cross_project_scope,
    global_only_scope,
    project_scope,
    tenant_scope,
    to_iso,
)
from .spatial import query_in_area, query_in_location, upsert_location_with_rtree
from .temporal import build_entity_timeline, query_at_time, query_in_range

__all__ = [
    # temporal
    "build_entity_timeline",
    "query_at_time",
    "query_in_range",
    # spatial
    "query_in_area",
    "query_in_location",
    "upsert_location_with_rtree",
    # graph
    "find_path",
    "find_n_degree_relations",
    # causal
    "trace_causal_chain",
    # scope (L1.12)
    "apply_scope_filter",
    "build_scope_clause",
    "project_scope",
    "cross_project_scope",
    "global_only_scope",
    "tenant_scope",
    "to_iso",
    # hybrid (L1.13)
    "HybridQuery",
    "hybrid_memory_search",
    "rrf_fusion",
]
