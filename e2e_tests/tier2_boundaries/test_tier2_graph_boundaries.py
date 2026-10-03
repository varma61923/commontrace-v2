from __future__ import annotations

import pytest

from commontrace import graph
from e2e_tests.harness.cli_runner import run_cli


def test_t2_graph_cycle_resilience(isolated_store: str):
    graph.add_node(isolated_store, "service:a", entity_type="service")
    graph.add_node(isolated_store, "service:b", entity_type="service")
    graph.add_node(isolated_store, "service:c", entity_type="service")

    graph.add_edge(isolated_store, "service:a", "service:b", "depends_on")
    graph.add_edge(isolated_store, "service:b", "service:c", "depends_on")
    graph.add_edge(isolated_store, "service:c", "service:a", "depends_on")

    subgraph = graph.multi_hop_subgraph(isolated_store, ["service:a"], max_hops=3)
    visited_ids = set(subgraph["hop_distances"].keys())
    assert len(visited_ids) == 3
    assert "service:a" in visited_ids
    assert "service:b" in visited_ids
    assert "service:c" in visited_ids


def test_t2_graph_disconnected_subgraph(isolated_store: str):
    graph.add_node(isolated_store, "service:island", entity_type="service")

    neighbors = graph.get_neighbors(isolated_store, "service:island")
    assert len(neighbors) == 0

    subgraph = graph.multi_hop_subgraph(isolated_store, ["service:island"], max_hops=2)
    assert len(subgraph["nodes"]) == 1
    assert subgraph["nodes"][0]["id"] == "service:island"
    assert len(subgraph["edges"]) == 0


def test_t2_graph_duplicate_edge_deduplication(isolated_store: str):
    graph.add_node(isolated_store, "service:src", entity_type="service")
    graph.add_node(isolated_store, "service:dst", entity_type="service")

    graph.add_edge(isolated_store, "service:src", "service:dst", "depends_on", weight=0.5)
    graph.add_edge(isolated_store, "service:src", "service:dst", "depends_on", weight=0.9)
    matching = [e for e in graph.load_edges(isolated_store) if e.source == "service:src"]
    assert len(matching) == 1 and matching[0].weight == 0.9

    graph.add_edge(isolated_store, "service:src", "service:dst", "depends_on", weight=0.4,
                   valid_at="2099-01-01T00:00:00Z")
    matching = [e for e in graph.load_edges(isolated_store) if e.source == "service:src"]
    assert len(matching) == 2
    active = [e for e in matching if e.invalid_at is None]
    assert len(active) == 1 and active[0].weight == 0.4


def test_t2_graph_empty_entity_id_rejection(isolated_store: str):
    with pytest.raises((ValueError, Exception)):
        graph.add_node(isolated_store, "   ")

    res = run_cli("graph", "node", "   ", dest=isolated_store)
    res.assert_failure()


def test_t2_graph_unknown_entity_query(isolated_store: str):
    subgraph = graph.multi_hop_subgraph(isolated_store, ["service:nonexistent"], max_hops=2)
    assert len(subgraph["nodes"]) == 0
    assert len(subgraph["hop_distances"]) == 0

    res = run_cli("graph", "query", "service:nonexistent", dest=isolated_store)
    res.assert_success()
