from __future__ import annotations

import os
import subprocess
import sys

import pytest

from commontrace import graph
from commontrace.graph import save_nodes

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "commontrace.cli", *argv],
        capture_output=True, text=True, cwd=REPO_ROOT, check=False,
    )


@pytest.fixture
def store(tmp_path):
    root = str(tmp_path / "fleet")
    assert cli("init", "--dest", root, "--agent-type", "coding").returncode == 0
    return root


def test_graph_nodes_and_edges(store):
    n1 = graph.add_node(store, "service:auth", "service", "Authentication Service")
    n2 = graph.add_node(store, "tool:jwt_validator", "tool", "JWT Validator")
    n3 = graph.add_node(store, "error:expired_token", "error", "Expired Token Error")

    e1 = graph.add_edge(store, "service:auth", "tool:jwt_validator", "uses", weight=1.0)
    e2 = graph.add_edge(store, "tool:jwt_validator", "error:expired_token", "causes", weight=0.8)

    assert e1.relation == "uses"
    assert e2.relation == "causes"
    assert e1.valid_at is not None
    assert e1.invalid_at is None

    neighbors = graph.get_neighbors(store, "tool:jwt_validator")
    assert len(neighbors) == 2

    # Multi-hop subgraph
    sub = graph.multi_hop_subgraph(store, ["service:auth"], max_hops=2)
    assert len(sub["nodes"]) == 3
    assert len(sub["edges"]) == 2


def test_graph_boost_and_entity_extraction(store):
    graph.add_node(store, "service:payments", "service", "Payments Gateway")
    graph.add_node(store, "error:card_declined", "error", "Card Declined Error")
    graph.add_node(store, "lesson:handle_declined_card", "lesson", "Gracefully handle declined card")
    graph.add_edge(store, "service:payments", "error:card_declined", "causes")
    graph.add_edge(store, "lesson:handle_declined_card", "error:card_declined", "resolves")

    extracted = graph.extract_entities_from_text(store, "When payments fail due to error")
    assert "service:payments" in extracted

    boosts = graph.graph_boost_for_lessons(
        store, "payments error", ["handle_declined_card", "other_slug"],
    )
    assert boosts["handle_declined_card"] > 0.0
    assert boosts["other_slug"] == 0.0


def test_graph_cli(store):
    res = cli("graph", "node", "service:billing", "--type", "service", "--name", "Billing", "--dest", store)
    assert res.returncode == 0
    assert "Saved node 'service:billing'" in res.stdout

    res = cli("graph", "node", "error:insufficient_funds", "--type", "error", "--dest", store)
    assert res.returncode == 0

    res = cli("graph", "edge", "service:billing", "causes", "error:insufficient_funds", "--dest", store)
    assert res.returncode == 0
    assert "Saved edge" in res.stdout

    res = cli("graph", "query", "billing", "--dest", store)
    assert res.returncode == 0
    assert "insufficient_funds" in res.stdout

    res = cli("graph", "render", "--format", "mermaid", "--dest", store)
    assert res.returncode == 0
    assert "graph TD" in res.stdout


def test_bitemporal_contradiction_resolution(store):
    """Test 'latest valid_at wins' policy for contradiction resolution."""
    graph.add_node(store, "service:api", "service", "API Service")
    graph.add_node(store, "tool:cache", "tool", "Cache")

    # Add first edge
    e1 = graph.add_edge(
        store, "service:api", "tool:cache", "uses",
        weight=0.5, valid_at="2024-01-01T00:00:00Z"
    )

    # Add second edge with later valid_at (should win)
    e2 = graph.add_edge(
        store, "service:api", "tool:cache", "uses",
        weight=0.9, valid_at="2024-01-02T00:00:00Z"
    )

    # The first edge should be invalidated
    edges = graph.load_edges(store)
    api_cache_edges = [e for e in edges if e.source == "service:api" and e.target == "tool:cache"]

    # Should have 2 edges (one invalidated, one active)
    assert len(api_cache_edges) == 2

    # The edge with later valid_at should be active (no invalid_at)
    active_edges = [e for e in api_cache_edges if e.invalid_at is None]
    assert len(active_edges) == 1
    assert active_edges[0].weight == 0.9


def test_bitemporal_interval_query(store):
    """Test temporal retriever with interval queries."""
    graph.add_node(store, "service:db", "service", "Database")
    graph.add_node(store, "tool:backup", "tool", "Backup")

    # Add edge with specific valid_at
    graph.add_edge(
        store, "service:db", "tool:backup", "uses",
        valid_at="2024-01-01T00:00:00Z",
        invalid_at="2024-01-10T00:00:00Z"
    )

    # Query interval that overlaps with edge's active period
    edges = graph.query_edges_by_interval(
        store,
        starts_at="2024-01-05T00:00:00Z",
        ends_at="2024-01-15T00:00:00Z"
    )
    assert len(edges) == 1

    # Query interval before edge was valid
    edges = graph.query_edges_by_interval(
        store,
        starts_at="2023-12-01T00:00:00Z",
        ends_at="2023-12-31T00:00:00Z"
    )
    assert len(edges) == 0

    # Query interval after edge was invalidated
    edges = graph.query_edges_by_interval(
        store,
        starts_at="2024-01-20T00:00:00Z",
        ends_at="2024-01-30T00:00:00Z"
    )
    assert len(edges) == 0


def test_bitemporal_expired_edges(store):
    """Test that expired edges are not considered active."""
    graph.add_node(store, "service:search", "service", "Search Service")
    graph.add_node(store, "tool:elasticsearch", "tool", "Elasticsearch")

    # Add edge with expiration
    graph.add_edge(
        store, "service:search", "tool:elasticsearch", "uses",
        valid_at="2024-01-01T00:00:00Z",
        expired_at="2024-01-10T00:00:00Z"
    )

    # Query as of now (after expiration) - should not return the edge
    neighbors = graph.get_neighbors(store, "service:search")
    assert len(neighbors) == 0

    # Query as of time before expiration - should return the edge
    neighbors = graph.get_neighbors(store, "service:search", as_of="2024-01-05T00:00:00Z")
    assert len(neighbors) == 1


def test_version_chain_creation(store):
    """Test version chain creation for memory evolution."""
    # Create initial memory node
    node1 = graph.add_node(
        store, "memory:auth_flow", "memory", "Authentication Flow",
        properties={"status": "draft"}
    )

    assert node1.version == 1
    assert node1.is_latest is True
    assert node1.parent_id is None
    assert node1.root_id is None

    # Create version 2
    node2 = graph.create_memory_version(
        store, "memory:auth_flow",
        new_properties={"status": "reviewed"},
        new_name="Authentication Flow v2"
    )

    assert node2.version == 2
    assert node2.is_latest is True
    assert node2.parent_id == "memory:auth_flow"
    assert node2.root_id == "memory:auth_flow"
    assert node2.name == "Authentication Flow v2"

    # Verify parent is no longer latest
    nodes = graph.load_nodes(store)
    parent = nodes["memory:auth_flow"]
    assert parent.is_latest is False
    assert parent.version == 1

    # Get version chain
    chain = graph.get_version_chain(store, "memory:auth_flow")
    assert len(chain) == 2
    assert chain[0].version == 1
    assert chain[1].version == 2


def test_memory_relationships(store):
    """Test memory-specific relationships (updates, extends, derives)."""
    graph.add_node(store, "memory:api_v1", "memory", "API v1")
    graph.add_node(store, "memory:api_v2", "memory", "API v2")
    graph.add_node(store, "memory:api_v3", "memory", "API v3")

    # Add memory relationships
    graph.add_memory_relationship(store, "memory:api_v2", "memory:api_v1", "updates")
    graph.add_memory_relationship(store, "memory:api_v3", "memory:api_v2", "extends")

    # Get relationships
    rels = graph.get_memory_relationships(store, "memory:api_v2")
    assert rels["updates"] == "memory:api_v1"

    rels = graph.get_memory_relationships(store, "memory:api_v3")
    assert rels["extends"] == "memory:api_v2"

    # Test invalid relation type
    with pytest.raises(ValueError, match="Invalid memory relation"):
        graph.add_memory_relationship(store, "memory:api_v1", "memory:api_v2", "invalid_relation")


def test_version_chain_with_root_id(store):
    """Test version chain when nodes have explicit root_id."""
    # Create initial node with explicit root_id
    node1 = graph.add_node(
        store, "memory:payment", "memory", "Payment System",
        properties={"version": "1.0"}
    )
    # Manually set root_id by loading, modifying, and saving
    nodes = graph.load_nodes(store)
    nodes["memory:payment"].root_id = "memory:payment_root"
    save_nodes(store, nodes)

    # Create version
    node2 = graph.create_memory_version(
        store, "memory:payment",
        new_properties={"version": "2.0"}
    )

    # Verify root_id is preserved
    assert node2.root_id == "memory:payment_root"

    # Get chain should find all nodes with same root_id
    chain = graph.get_version_chain(store, "memory:payment")
    assert len(chain) == 2
    assert all(n.root_id == "memory:payment_root" for n in chain)


def test_resolve_contradictions(store):
    """Test 'latest valid_at wins' contradiction resolution policy."""
    e1 = graph.GraphEdge(
        source="srv1",
        target="srv2",
        relation="depends_on",
        weight=0.5,
        valid_at="2024-01-01T00:00:00Z",
        invalid_at=None,
        expired_at=None,
        properties={},
        created_at="2024-01-01T00:00:00Z",
    )
    e2 = graph.GraphEdge(
        source="srv1",
        target="srv2",
        relation="depends_on",
        weight=0.9,
        valid_at="2024-02-01T00:00:00Z",
        invalid_at=None,
        expired_at=None,
        properties={},
        created_at="2024-02-01T00:00:00Z",
    )
    e3 = graph.GraphEdge(
        source="srv1",
        target="srv3",
        relation="uses",
        weight=1.0,
        valid_at="2024-01-15T00:00:00Z",
        invalid_at=None,
        expired_at=None,
        properties={},
        created_at="2024-01-15T00:00:00Z",
    )

    resolved = graph.resolve_contradictions([e1, e2, e3])
    assert len(resolved) == 2
    # Winner for (srv1, srv2, depends_on) should be e2 (latest valid_at)
    dep_edge = next(e for e in resolved if e.target == "srv2")
    assert dep_edge.weight == 0.9
    assert dep_edge.valid_at == "2024-02-01T00:00:00Z"


def test_forget_node_and_invalidate_edges(store):
    """Test soft-delete / automatic forgetting with edge invalidation."""
    graph.add_node(store, "node:a", "service", "Service A")
    graph.add_node(store, "node:b", "service", "Service B")
    graph.add_edge(store, "node:a", "node:b", "depends_on")

    # Forget node:a
    forgotten = graph.forget_node(store, "node:a", reason="deprecated_architecture")
    assert forgotten is not None
    assert forgotten.is_forgotten is True
    assert forgotten.properties["forget_reason"] == "deprecated_architecture"
    assert "forgotten_at" in forgotten.properties

    # Edge connected to node:a should now be invalidated
    edges = graph.load_edges(store)
    conn_edge = next(e for e in edges if e.source == "node:a" and e.target == "node:b")
    assert conn_edge.invalid_at is not None


def test_list_version_chains(store):
    """Test grouping all nodes by version chains."""
    graph.add_node(store, "chain_a", "concept", "Concept A")
    graph.create_memory_version(store, "chain_a", new_name="Concept A v2")
    graph.add_node(store, "chain_b", "concept", "Concept B")

    chains = graph.list_version_chains(store)
    assert "chain_a" in chains
    assert len(chains["chain_a"]) == 2
    assert chains["chain_a"][0].version == 1
    assert chains["chain_a"][1].version == 2
    assert "chain_b" in chains
    assert len(chains["chain_b"]) == 1

