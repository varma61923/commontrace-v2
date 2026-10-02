from __future__ import annotations

import pytest

from commontrace import graph
from e2e_tests.harness.cli_runner import run_cli


def test_t2_graph_cycle_resilience(isolated_store: str):
    """E2E-T2-KG-1: Cyclical graphs (A -> B -> C -> A) do not cause infinite recursion during BFS traversal."""
    graph.add_node(isolated_store, "service:a", entity_type="service")
    graph.add_node(isolated_store, "service:b", entity_type="service")
    graph.add_node(isolated_store, "service:c", entity_type="service")

    graph.add_edge(isolated_store, "service:a", "service:b", "depends_on")
    graph.add_edge(isolated_store, "service:b", "service:c", "depends_on")
    graph.add_edge(isolated_store, "service:c", "service:a", "depends_on")

    # Multi-hop query with list of start nodes should complete cleanly and not loop infinitely
    subgraph = graph.multi_hop_subgraph(isolated_store, ["service:a"], max_hops=3)
    visited_ids = set(subgraph["hop_distances"].keys())
    assert len(visited_ids) == 3
    assert "service:a" in visited_ids
    assert "service:b" in visited_ids
    assert "service:c" in visited_ids


def test_t2_graph_disconnected_subgraph(isolated_store: str):
    """E2E-T2-KG-2: Querying an isolated node returns empty neighborhood."""
    graph.add_node(isolated_store, "service:island", entity_type="service")

    neighbors = graph.get_neighbors(isolated_store, "service:island")
    assert len(neighbors) == 0

    subgraph = graph.multi_hop_subgraph(isolated_store, ["service:island"], max_hops=2)
    assert len(subgraph["nodes"]) == 1
    assert subgraph["nodes"][0]["id"] == "service:island"
    assert len(subgraph["edges"]) == 0


def test_t2_graph_duplicate_edge_deduplication(isolated_store: str):
    """E2E-T2-KG-3: Adding the same edge updates weight with bi-temporal contradiction resolution.

    With bi-temporal support, adding the same edge twice creates two entries:
    - The first edge is invalidated (invalid_at set)
    - The second edge is active (invalid_at is None)
    This implements the 'latest valid_at wins' policy for fact evolution.
    """
    graph.add_node(isolated_store, "service:src", entity_type="service")
    graph.add_node(isolated_store, "service:dst", entity_type="service")

    graph.add_edge(isolated_store, "service:src", "service:dst", "depends_on", weight=0.5)
    graph.add_edge(isolated_store, "service:src", "service:dst", "depends_on", weight=0.9)

    edges = graph.load_edges(isolated_store)
    matching = [e for e in edges if e.source == "service:src" and e.target == "service:dst"]
    # Bi-temporal: we keep both edges (one invalidated, one active)
    assert len(matching) == 2, "Bi-temporal keeps both edges (invalidated + active)"

    # The active edge should have the higher weight
    active_edges = [e for e in matching if e.invalid_at is None]
    assert len(active_edges) == 1, "Exactly one edge should be active"
    assert active_edges[0].weight == 0.9, "Active edge should have the latest weight"

    # The invalidated edge should have the lower weight
    invalidated_edges = [e for e in matching if e.invalid_at is not None]
    assert len(invalidated_edges) == 1, "Exactly one edge should be invalidated"
    assert invalidated_edges[0].weight == 0.5, "Invalidated edge should have the original weight"


def test_t2_graph_empty_entity_id_rejection(isolated_store: str):
    """E2E-T2-KG-4: Empty entity IDs must be rejected."""
    with pytest.raises((ValueError, Exception)):
        graph.add_node(isolated_store, "   ")

    res = run_cli("graph", "node", "   ", dest=isolated_store)
    res.assert_failure()


def test_t2_graph_unknown_entity_query(isolated_store: str):
    """E2E-T2-KG-5: Querying a nonexistent entity returns clean empty result without crashing."""
    subgraph = graph.multi_hop_subgraph(isolated_store, ["service:nonexistent"], max_hops=2)
    assert len(subgraph["nodes"]) == 0
    assert len(subgraph["hop_distances"]) == 0

    res = run_cli("graph", "query", "service:nonexistent", dest=isolated_store)
    res.assert_success()
