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
    graph.add_node(store, "service:api", "service", "API Service")
    graph.add_node(store, "tool:cache", "tool", "Cache")

    e1 = graph.add_edge(
        store, "service:api", "tool:cache", "uses",
        weight=0.5, valid_at="2024-01-01T00:00:00Z"
    )

    e2 = graph.add_edge(
        store, "service:api", "tool:cache", "uses",
        weight=0.9, valid_at="2024-01-02T00:00:00Z"
    )

    edges = graph.load_edges(store)
    api_cache_edges = [e for e in edges if e.source == "service:api" and e.target == "tool:cache"]

    assert len(api_cache_edges) == 2

    active_edges = [e for e in api_cache_edges if e.invalid_at is None]
    assert len(active_edges) == 1
    assert active_edges[0].weight == 0.9


def test_bitemporal_expired_edges(store):
    graph.add_node(store, "service:search", "service", "Search Service")
    graph.add_node(store, "tool:elasticsearch", "tool", "Elasticsearch")

    graph.add_edge(
        store, "service:search", "tool:elasticsearch", "uses",
        valid_at="2024-01-01T00:00:00Z",
        expired_at="2024-01-10T00:00:00Z"
    )

    neighbors = graph.get_neighbors(store, "service:search")
    assert len(neighbors) == 0

    neighbors = graph.get_neighbors(store, "service:search", as_of="2024-01-05T00:00:00Z")
    assert len(neighbors) == 1


def test_version_chain_creation(store):
    node1 = graph.add_node(
        store, "memory:auth_flow", "memory", "Authentication Flow",
        properties={"status": "draft"}
    )

    assert node1.version == 1
    assert node1.is_latest is True
    assert node1.parent_id is None
    assert node1.root_id is None

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

    nodes = graph.load_nodes(store)
    parent = nodes["memory:auth_flow"]
    assert parent.is_latest is False
    assert parent.version == 1

    chain = graph.get_version_chain(store, "memory:auth_flow")
    assert len(chain) == 2
    assert chain[0].version == 1
    assert chain[1].version == 2


def test_version_chain_with_root_id(store):
    node1 = graph.add_node(
        store, "memory:payment", "memory", "Payment System",
        properties={"version": "1.0"}
    )
    nodes = graph.load_nodes(store)
    nodes["memory:payment"].root_id = "memory:payment_root"
    save_nodes(store, nodes)

    node2 = graph.create_memory_version(
        store, "memory:payment",
        new_properties={"version": "2.0"}
    )

    assert node2.root_id == "memory:payment_root"

    chain = graph.get_version_chain(store, "memory:payment")
    assert len(chain) == 2
    assert all(n.root_id == "memory:payment_root" for n in chain)


def test_forget_node_and_invalidate_edges(store):
    graph.add_node(store, "node:a", "service", "Service A")
    graph.add_node(store, "node:b", "service", "Service B")
    graph.add_edge(store, "node:a", "node:b", "depends_on")

    forgotten = graph.forget_node(store, "node:a", reason="deprecated_architecture")
    assert forgotten is not None
    assert forgotten.is_forgotten is True
    assert forgotten.properties["forget_reason"] == "deprecated_architecture"
    assert "forgotten_at" in forgotten.properties

    edges = graph.load_edges(store)
    conn_edge = next(e for e in edges if e.source == "node:a" and e.target == "node:b")
    assert conn_edge.invalid_at is not None


def test_list_version_chains(store):
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

