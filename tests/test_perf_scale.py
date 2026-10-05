from __future__ import annotations

import inspect

from commontrace import graph


def _build_chain_graph(root: str):
    graph.add_node(root, "n0", entity_type="service", name="n0")
    prev = "n0"
    for i in range(1, 6):
        nid = f"n{i}"
        graph.add_node(root, nid, entity_type="service", name=nid)
        graph.add_edge(root, prev, nid, relation="depends_on")
        prev = nid
    for i in range(10):
        leaf = f"x{i}"
        graph.add_node(root, leaf, entity_type="tool", name=leaf)
        graph.add_edge(root, "n0", leaf, relation="uses")


class TestGraphCaps:
    def test_default_behavior_unchanged(self, tmp_path):
        root = str(tmp_path)
        _build_chain_graph(root)
        default = graph.multi_hop_subgraph(root, ["n0"])
        explicit = graph.multi_hop_subgraph(root, ["n0"], max_hops=2, max_edges=None)
        assert default == explicit

    def test_no_signature_breakage(self):
        sig = inspect.signature(graph.multi_hop_subgraph)
        assert sig.parameters["max_hops"].default == 2
        assert sig.parameters["max_edges"].default is None
        assert callable(graph.multi_hop_subgraph)
        sig_b = inspect.signature(graph.graph_boost_for_lessons)
        assert sig_b.parameters["max_hops"].default == 2
        assert sig_b.parameters["max_edges"].default is None

    def test_max_edges_cap_respected(self, tmp_path):
        root = str(tmp_path)
        _build_chain_graph(root)
        full = graph.multi_hop_subgraph(root, ["n0"])
        assert len(full["edges"]) > 2
        capped = graph.multi_hop_subgraph(root, ["n0"], max_hops=2, max_edges=2)
        assert len(capped["edges"]) <= 2
        empty = graph.multi_hop_subgraph(root, ["n0"], max_hops=2, max_edges=0)
        assert empty["edges"] == []
        assert {n["id"] for n in empty["nodes"]} >= {"n0"}

    def test_max_hops_cap_respected(self, tmp_path):
        root = str(tmp_path)
        _build_chain_graph(root)
        one_hop = graph.multi_hop_subgraph(root, ["n0"], max_hops=1)
        assert one_hop["hop_distances"], "expected visited nodes"
        assert max(one_hop["hop_distances"].values()) <= 1
        two_hop = graph.multi_hop_subgraph(root, ["n0"], max_hops=2)
        assert max(two_hop["hop_distances"].values()) <= 2
        assert len(one_hop["edges"]) <= len(two_hop["edges"])

    def test_boost_default_unchanged_and_caps_pass_through(self, tmp_path):
        root = str(tmp_path)
        graph.add_node(root, "service:search", entity_type="service", name="search")
        graph.add_node(root, "lesson:mylesson", entity_type="lesson", name="mylesson")
        graph.add_edge(root, "service:search", "lesson:mylesson", relation="relates_to")
        candidates = ["mylesson", "otherlesson"]
        query = "how to use search effectively"

        default = graph.graph_boost_for_lessons(root, query, candidates)
        explicit = graph.graph_boost_for_lessons(root, query, candidates, max_hops=2, max_edges=None)
        assert default == explicit
        assert default["mylesson"] > 0.0
        assert default["otherlesson"] == 0.0

        zero_hop = graph.graph_boost_for_lessons(root, query, candidates, max_hops=0)
        assert zero_hop == {"mylesson": 0.0, "otherlesson": 0.0}

        zero_edge = graph.graph_boost_for_lessons(root, query, candidates, max_edges=0)
        assert zero_edge == {"mylesson": 0.0, "otherlesson": 0.0}

    def test_adjacency_memoized(self, tmp_path):
        root = str(tmp_path)
        _build_chain_graph(root)
        assert len(graph._GRAPH_INDEX_CACHE) == 0
        first = graph.multi_hop_subgraph(root, ["n0"])
        assert len(graph._GRAPH_INDEX_CACHE) >= 1
        size_before = len(graph._GRAPH_INDEX_CACHE)
        second = graph.multi_hop_subgraph(root, ["n0"])
        assert second == first
        assert len(graph._GRAPH_INDEX_CACHE) == size_before
        graph.add_edge(root, "n1", "x0", relation="relates_to")
        third = graph.multi_hop_subgraph(root, ["n0"])
        assert third != first or len(third["edges"]) >= len(first["edges"])
