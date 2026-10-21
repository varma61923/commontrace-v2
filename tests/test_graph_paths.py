"""Temporal path evidence, traversal bounds and typed local query contracts."""
from __future__ import annotations

import json

import pytest

from unimem import graph
from unimem.cli import main

BASE = "2024-01-01T00:00:00+00:00"


def seed(root):
    nodes = {nid: graph.GraphNode(nid, "concept", nid, {}, BASE, BASE) for nid in ("a", "b", "c", "d", "x")}
    edges = [graph.GraphEdge(source, target, relation, 1, BASE, None, None,
                             properties={"source_document": f"document:{source}-{target}"}, created_at=BASE)
             for source, target, relation in (("a", "b", "uses"), ("b", "c", "uses"),
                                              ("a", "d", "depends_on"), ("d", "c", "depends_on"),
                                              ("c", "a", "relates_to"))]
    graph.save_nodes(root, nodes)
    graph.save_edges(root, edges)
    return nodes, edges


def test_shortest_path_retains_original_evidence_and_reverse_direction(tmp_path):
    root = str(tmp_path)
    _, edges = seed(root)
    result = graph.shortest_path(root, " A ", "c")
    assert result["status"] == "found" and result["hops"] == 1
    assert result["edges"] == [edges[-1].to_dict()]
    assert result["steps"] == [{"from": "a", "to": "c", "direction": "in"}]
    assert [node["id"] for node in result["nodes"]] == ["a", "c"]
    result["edges"][0]["properties"].clear()
    assert graph.shortest_path(root, "a", "c")["edges"][0]["properties"]


def test_direction_relation_hop_and_identity_paths(tmp_path):
    root = str(tmp_path)
    seed(root)
    result = graph.shortest_path(root, "a", "c", direction="out", relations=["uses"])
    assert [node["id"] for node in result["nodes"]] == ["a", "b", "c"]
    assert result["hops"] == 2
    assert graph.shortest_path(root, "a", "c", relations=[])["status"] == "not-found"
    assert graph.shortest_path(root, "a", "c", direction="out", relations=["uses"], max_hops=1)["status"] == "not-found"
    assert graph.shortest_path(root, "a", "a", max_hops=0)["hops"] == 0
    assert graph.shortest_path(root, "a", "x")["status"] == "not-found"


def test_paths_respect_both_time_axes_and_half_open_expiry(tmp_path):
    root = str(tmp_path)
    _, edges = seed(root)
    edges[0].invalid_at = "2024-06-01T00:00:00Z"
    edges[0].properties["closed_recorded_at"] = "2024-07-01T00:00:00Z"
    edges[0].created_at = "2024-02-01T00:00:00Z"
    graph.save_edges(root, edges)
    options = {"relations": ["uses"], "as_of": "2024-06-01"}
    assert graph.shortest_path(root, "a", "c", **options)["status"] == "not-found"
    assert graph.shortest_path(root, "a", "c", known_at="2024-06-15", **options)["status"] == "found"
    assert graph.shortest_path(root, "a", "c", known_at="2024-01-15", **options)["status"] == "not-found"
    edges[0].expired_at = "2024-06-01T00:00:00Z"
    graph.save_edges(root, edges)
    assert graph.shortest_path(root, "a", "c", known_at="2024-06-15", **options)["status"] == "not-found"


def test_forgotten_and_missing_nodes_never_bridge_paths(tmp_path):
    root = str(tmp_path)
    nodes, _ = seed(root)
    nodes["b"].is_forgotten = True
    graph.save_nodes(root, nodes)
    assert graph.shortest_path(root, "a", "c", relations=["uses"], as_of=BASE)["status"] == "not-found"
    assert graph.shortest_path(root, "a", "b")["reason"] == "missing-or-forgotten-endpoint"
    del nodes["b"]
    graph.save_nodes(root, nodes)
    assert graph.shortest_path(root, "a", "c", relations=["uses"])["status"] == "not-found"


def test_work_budgets_are_inconclusive_and_bound_actual_work(tmp_path):
    root = str(tmp_path)
    seed(root)
    result = graph.shortest_path(root, "a", "x", max_examined_edges=1)
    assert result["status"] == "budget-exhausted" and result["reason"] == "edge-budget"
    assert result["examined_edges"] == 1
    result = graph.shortest_path(root, "a", "c", max_nodes=1)
    assert result["status"] == "budget-exhausted" and result["visited_nodes"] == 1


@pytest.mark.parametrize("options", [{"max_nodes": True}, {"max_nodes": 0}, {"max_nodes": 100001},
                                     {"max_examined_edges": 0}, {"max_hops": 5}, {"max_hops": -1},
                                     {"direction": "sideways"}, {"relations": "uses"},
                                     {"relations": [1]}, {"relations": ["uses"] * 65}])
def test_invalid_controls_fail_before_loading(tmp_path, monkeypatch, options):
    monkeypatch.setattr(graph, "_full_index", lambda *_: pytest.fail("invalid controls touched the store"))
    with pytest.raises(ValueError):
        graph.shortest_path(str(tmp_path), "a", "c", **options)


def test_typed_neighbors_and_subgraphs_preserve_journal_order(tmp_path):
    root = str(tmp_path)
    _, edges = seed(root)
    assert [row["neighbor_id"] for row in graph.get_neighbors(root, "a", relations=["uses", "depends_on"])] == ["b", "d"]
    assert graph.get_neighbors(root, "a", relation="uses", relations=["depends_on"]) == []
    sub = graph.multi_hop_subgraph(root, ["a"], relations=["uses"], direction="out")
    assert sub["edges"] == [edge.to_dict() for edge in edges[:2]]
    assert sub["hop_distances"] == {"a": 0, "b": 1, "c": 2}
    assert graph.multi_hop_subgraph(root, ["a"], relations=[])["edges"] == []


def test_rare_relation_warm_queries_never_scan_other_incident_edges(tmp_path, monkeypatch):
    root = str(tmp_path)
    _, edges = seed(root)
    graph.get_neighbors(root, "a", relation="uses")
    original = graph._is_active_edge
    examined = []

    def active(edge, *args, **kwargs):
        examined.append(edge.relation)
        return original(edge, *args, **kwargs)

    monkeypatch.setattr(graph, "_is_active_edge", active)
    assert graph.get_neighbors(root, "a", relation="uses")[0]["neighbor_id"] == "b"
    assert examined == ["uses"]
    edges.append(graph.GraphEdge("a", "x", "uses", 1, BASE, None, None, created_at=BASE))
    graph.save_edges(root, edges)
    assert [row["neighbor_id"] for row in graph.get_neighbors(root, "a", relation="uses")] == ["b", "x"]


def test_cli_exposes_path_evidence_and_inconclusive_exit_status(tmp_path, capsys):
    root = str(tmp_path)
    seed(root)
    assert main(["graph", "path", "a", "c", "--relation", "uses", "--json", "--dest", root]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["hops"] == 2 and result["edges"][0]["properties"]["source_document"] == "document:a-b"
    assert main(["graph", "path", "a", "c", "--max-nodes", "1", "--dest", root]) == 2
    assert "budget-exhausted" in capsys.readouterr().out
