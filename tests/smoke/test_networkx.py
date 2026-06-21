"""Smoke test: NetworkX graph algorithms (L3 semantic layer selection)."""

import networkx as nx


def test_networkx_shortest_path():
    """验证最短路径算法 — MVP 图谱遍历的备选方案."""
    g = nx.Graph()
    g.add_edge("A", "B")
    g.add_edge("B", "C")
    g.add_edge("A", "C", weight=10)  # 直接边但权重高
    g.add_edge("C", "D")

    path = nx.shortest_path(g, source="A", target="D")
    assert path == ["A", "B", "C", "D"] or path == ["A", "C", "D"]


def test_networkx_weighted_path():
    """验证加权最短路径(BFS vs Dijkstra)."""
    g = nx.Graph()
    g.add_edge("A", "B", weight=1)
    g.add_edge("B", "C", weight=1)
    g.add_edge("A", "C", weight=10)

    path = nx.dijkstra_path(g, source="A", target="C")
    assert path == ["A", "B", "C"], "权重路径应该走 B 而非直达"


def test_networkx_centrality():
    """验证中心性算法 — P2 社区检测会用到."""
    g = nx.Graph()
    g.add_edges_from([("A", "B"), ("B", "C"), ("C", "D"), ("D", "E")])
    deg = nx.degree_centrality(g)
    # 中间节点 C 的度数应该 >= 端点 A
    assert deg["C"] >= deg["A"]
