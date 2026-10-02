from __future__ import annotations

import json
import os

from commontrace import graph
from e2e_tests.harness.cli_runner import run_cli


def test_t1_graph_add_nodes(isolated_store: str):
    """E2E-T1-KG-1: Add entity nodes across types and verify persistence in nodes.jsonl."""
    # Add service node
    res_node1 = run_cli(
        "graph", "node", "service:billing",
        "--type", "service",
        "--name", "Stripe Payment Gateway",
        dest=isolated_store,
    )
    res_node1.assert_success()
    assert "Saved node 'service:billing'" in res_node1.stdout

    # Add error node
    res_node2 = run_cli(
        "graph", "node", "error:timeout",
        "--type", "error",
        "--name", "HTTP 504 Gateway Timeout",
        dest=isolated_store,
    )
    res_node2.assert_success()

    # Add lesson node
    res_node3 = run_cli(
        "graph", "node", "lesson:retry-backoff",
        "--type", "lesson",
        "--name", "Exponential Backoff on Gateway Timeout",
        dest=isolated_store,
    )
    res_node3.assert_success()

    # Verify nodes.jsonl on disk
    nodes_file = os.path.join(isolated_store, "memory", "graph", "nodes.jsonl")
    assert os.path.exists(nodes_file), "nodes.jsonl must exist on disk"

    with open(nodes_file, encoding="utf-8") as f:
        node_records = [json.loads(line) for line in f if line.strip()]
    node_ids = {n["id"] for n in node_records}
    assert "service:billing" in node_ids
    assert "error:timeout" in node_ids
    assert "lesson:retry-backoff" in node_ids


def test_t1_graph_add_edges(isolated_store: str):
    """E2E-T1-KG-2: Add directed relationship edges and verify persistence in edges.jsonl."""
    # Add nodes first
    run_cli("graph", "node", "service:auth", "--type", "service", dest=isolated_store).assert_success()
    run_cli("graph", "node", "service:redis", "--type", "service", dest=isolated_store).assert_success()

    # Add edge
    res_edge = run_cli(
        "graph", "edge", "service:auth", "depends_on", "service:redis",
        "--weight", "0.95",
        dest=isolated_store,
    )
    res_edge.assert_success()
    assert "Saved edge:" in res_edge.stdout
    assert "depends_on" in res_edge.stdout

    # Verify edges.jsonl on disk
    edges_file = os.path.join(isolated_store, "memory", "graph", "edges.jsonl")
    assert os.path.exists(edges_file), "edges.jsonl must exist on disk"

    with open(edges_file, encoding="utf-8") as f:
        edge_records = [json.loads(line) for line in f if line.strip()]
    assert len(edge_records) >= 1
    e0 = edge_records[0]
    assert e0["source"] == "service:auth"
    assert e0["target"] == "service:redis"
    assert e0["relation"] == "depends_on"
    assert e0["weight"] == 0.95


def test_t1_graph_query_multihop_traversal(isolated_store: str):
    """E2E-T1-KG-3: Perform multi-hop BFS neighborhood query via CLI."""
    # Topology: A -> depends_on -> B -> depends_on -> C
    graph.add_node(isolated_store, "service:frontend", entity_type="service", name="Frontend UI")
    graph.add_node(isolated_store, "service:gateway", entity_type="service", name="API Gateway")
    graph.add_node(isolated_store, "service:auth", entity_type="service", name="Auth Service")

    graph.add_edge(isolated_store, "service:frontend", "service:gateway", "depends_on")
    graph.add_edge(isolated_store, "service:gateway", "service:auth", "depends_on")

    # 1 hop query from frontend should find gateway
    res_hop1 = run_cli("graph", "query", "service:frontend", "--hops", "1", dest=isolated_store)
    res_hop1.assert_success()
    assert "service:gateway" in res_hop1.stdout

    # 2 hop query from frontend should find gateway AND auth
    res_hop2 = run_cli("graph", "query", "service:frontend", "--hops", "2", dest=isolated_store)
    res_hop2.assert_success()
    assert "service:gateway" in res_hop2.stdout
    assert "service:auth" in res_hop2.stdout


def test_t1_graph_entity_extraction_from_text(isolated_store: str):
    """E2E-T1-KG-4: Extract entity mentions from free-form task text based on known graph entities."""
    graph.add_node(isolated_store, "service:payment", entity_type="service", name="Stripe Billing API")
    graph.add_node(isolated_store, "error:ratelimit", entity_type="error", name="429 Too Many Requests")

    task = "Investigate 429 Too Many Requests errors when calling Stripe Billing API in production"
    matched = graph.extract_entities_from_text(isolated_store, task)
    assert "service:payment" in matched
    assert "error:ratelimit" in matched


def test_t1_graph_distance_decayed_boost(isolated_store: str):
    """E2E-T1-KG-5: Verify distance-decayed relevance boost formula for candidate lessons."""
    # Setup: error:oom -> causes -> service:worker -> affects -> lesson:memlimit
    graph.add_node(isolated_store, "error:oom", entity_type="error", name="Out of Memory")
    graph.add_node(isolated_store, "service:worker", entity_type="service", name="Batch Worker")
    graph.add_node(isolated_store, "lesson:memlimit", entity_type="lesson", name="Configure Container Memory Limits")
    graph.add_node(isolated_store, "lesson:distant", entity_type="lesson", name="Unrelated Lesson")

    graph.add_edge(isolated_store, "error:oom", "service:worker", "affects")
    graph.add_edge(isolated_store, "service:worker", "lesson:memlimit", "resolves")

    task = "Fix Out of Memory errors causing worker crash"
    boosts = graph.graph_boost_for_lessons(
        isolated_store,
        task,
        ["memlimit", "distant"],
    )

    # memlimit should receive a boost > 0, distant should be 0.0
    assert boosts.get("memlimit", 0.0) > 0.0, "Connected lesson must receive graph boost"
    assert boosts.get("distant", 0.0) == 0.0, "Unconnected lesson must receive zero boost"


def test_t1_graph_render_mermaid_and_json(isolated_store: str):
    """E2E-T1-KG-6: Render knowledge graph as Mermaid diagram and structured JSON."""
    graph.add_node(isolated_store, "service:db", entity_type="service", name="Postgres")
    graph.add_node(isolated_store, "service:api", entity_type="service", name="FastAPI")
    graph.add_edge(isolated_store, "service:api", "service:db", "depends_on")

    # Render as Mermaid
    res_mermaid = run_cli("graph", "render", "--format", "mermaid", dest=isolated_store)
    res_mermaid.assert_success()
    assert "graph TD" in res_mermaid.stdout
    assert "depends_on" in res_mermaid.stdout

    # Render as JSON
    res_json = run_cli("graph", "render", "--format", "json", dest=isolated_store)
    res_json.assert_success()
    data = res_json.json()
    assert "nodes" in data
    assert "edges" in data
    assert len(data["nodes"]) >= 2
    assert len(data["edges"]) >= 1
