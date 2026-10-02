from __future__ import annotations

import os
import subprocess
import sys

import pytest

from commontrace import graph

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
