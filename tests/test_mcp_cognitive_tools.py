from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest

from commontrace import mcp_server

pytest.importorskip("mcp")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "commontrace.cli", *argv],
        capture_output=True, text=True, cwd=REPO_ROOT, check=False,
    )


def _payload(result) -> dict:
    if getattr(result, "structured_content", None):
        sc = result.structured_content
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


def call(server, tool_name: str, **arguments) -> dict:
    return _payload(asyncio.run(server.call_tool(tool_name, arguments)))


@pytest.fixture
def store(tmp_path):
    root = str(tmp_path / "fleet")
    assert cli("init", "--dest", root, "--agent-type", "support").returncode == 0
    return root


@pytest.fixture
def server(store):
    return mcp_server.build_server(store)


def test_mcp_memory_blocks(server):
    # Set block
    out = call(server, "memory_block_update", name="persona", content="Autonomous support agent.")
    assert out["ok"]
    assert out["block"]["name"] == "persona"
    assert out["block"]["content"] == "Autonomous support agent."

    # Read block
    read_out = call(server, "memory_block_read", name="persona")
    assert read_out["ok"]
    assert read_out["block"]["content"] == "Autonomous support agent."

    # List blocks
    list_out = call(server, "memory_block_list")
    assert list_out["ok"]
    assert list_out["count"] == 1

    # Delete block
    del_out = call(server, "memory_block_delete", name="persona")
    assert del_out["ok"] is True
    assert del_out["deleted"] is True

    # Read deleted block fails
    read_deleted = call(server, "memory_block_read", name="persona")
    assert read_deleted["ok"] is False

    # Delete non-existent block fails
    del_nonexistent = call(server, "memory_block_delete", name="nonexistent")
    assert del_nonexistent["ok"] is False


def test_mcp_facts(server):
    # Record fact
    rec_out = call(server, "record_fact", statement="VIP customers get 1h response SLA", category="preference", scope="support")
    assert rec_out["ok"]
    assert rec_out["action"] == "ADD"
    assert rec_out["fact"]["confidence"] == 0.8

    # Query facts
    q_out = call(server, "query_facts", query="VIP SLA")
    assert q_out["ok"]
    assert q_out["count"] >= 1
    assert "VIP" in q_out["facts"][0]["fact"]["statement"]


def test_mcp_graph(server):
    from commontrace import graph
    store = server.name  # we can use graph module directly or add nodes
    # let's add via graph python module into server root
    root = [t for t in asyncio.run(server.list_tools()) if t.name == "store_status"]
    # server closures capture `root`
    out = call(server, "store_status")
    srv_root = out["root"]

    graph.add_node(srv_root, "service:zendesk", "service", "Zendesk")
    graph.add_node(srv_root, "error:rate_limited", "error", "Rate Limited")
    graph.add_edge(srv_root, "service:zendesk", "error:rate_limited", "causes")

    neighbors_out = call(server, "graph_neighbors", entity="service:zendesk")
    assert neighbors_out["ok"]
    assert neighbors_out["count"] == 1
    assert neighbors_out["neighbors"][0]["neighbor_id"] == "error:rate_limited"

    query_out = call(server, "graph_query", entity="zendesk", hops=1)
    assert query_out["ok"]
    assert len(query_out["nodes"]) == 2
