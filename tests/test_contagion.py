"""UniMem visible graph closure, bounded traversal and correlated-majority checks."""
from __future__ import annotations

import pytest

from commontrace import contagion, hierarchical, memory_authority
from commontrace import nextwave_store as store


def fact(root, text, *, sources=(), scopes=(), writer="root-a"):
    with memory_authority.writer(writer, "operator"):
        return hierarchical.append_facts(root, [{"statement": text, "source_trace_id": next(iter(sources), ""),
                                                 "scopes": list(scopes)}])[0][0]


def test_complete_visible_radius_includes_reads_derivatives_and_exports(tmp_path, monkeypatch):
    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    source = fact(root, "Read signature before deploying production packages.")
    derived = fact(root, "Deployment reviews require signed package evidence.", sources=[source.id])
    secret = fact(root, "Private deployment key must remain confidential.", scopes=["private"], sources=[source.id])
    read = contagion.record_read(root, [derived.id], "reviewer", "episode-1", "answer-1")
    store.put(root, "capsules", "capsule-a", {"model": "test"}, sources=[derived.id])
    store.put(root, "rl-exports", "export-a", {"digest": "test"}, sources=[source.id])
    result = contagion.radius(root, source.id)
    ids = {n["id"] for n in result["nodes"]}
    assert {source.id, derived.id, "agent:reviewer", "episode:episode-1", "output:answer-1",
            store.reference("read-graph", read["id"]), store.reference("capsules", "capsule-a"),
            store.reference("rl-exports", "export-a")} <= ids
    assert secret.id not in ids and not result["truncated"]
    with pytest.raises(PermissionError):
        contagion.radius(root, secret.id)
    assert contagion.radius(root, source.id, max_nodes=2)["truncated"]
    assert len(contagion.radius(root, source.id, max_nodes=2)["nodes"]) == 2
    assert contagion.radius(root, source.id, max_depth=1)["truncated"]


def test_single_root_majority_is_one_source_and_reretrieval_rechecks_admission(tmp_path, monkeypatch):
    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    memories = [fact(root, f"Vendor alpha release checklist item number {i} needs verification.").id for i in range(5)]
    other = fact(root, "Independent vendor beta deployment verification.", writer="root-b")
    denied = fact(root, "Private vendor gamma audit checklist.", scopes=["secret"], writer="root-c")
    annotation = contagion.independence(root, memories, downweight=True)
    assert annotation["n_eff"] == pytest.approx(1)
    assert all(r["agreement_weight"] == pytest.approx(.2) for r in annotation["records"])
    def retrieve(*args, **kwargs):
        return [{"id": other.id}, {"id": denied.id}]
    result = contagion.reretrieve(root, "deployment", memories, retriever=retrieve)
    assert result["triggered"] and 1 < result["n_eff"] < 2
    assert denied.id not in {r["id"] for r in result["records"]}
    assert result["threshold_met"] is False
    with pytest.raises(PermissionError):
        contagion.record_read(root, [denied.id], "agent", "episode", "answer")


def test_retention_and_deletion_remove_read_graph_edges(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    from commontrace import frontier_protocol as fp

    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    memory = fact(root, "Inspect robot wrist current before the next task.")
    contagion.record_read(root, [memory.id], "robot-a", "episode", "action", retention_days=1)
    future = (datetime.fromisoformat(fp.now()) + timedelta(days=2)).isoformat()
    monkeypatch.setattr(fp, "now", lambda: future)
    assert {n["id"] for n in contagion.radius(root, memory.id)["nodes"]} == {memory.id}
    assert len(contagion.prune(root)["expired"]) == 1
    assert not list((tmp_path / "memory" / "unimem" / "read-graph").glob("*.json"))
    with pytest.raises(ValueError):
        contagion.radius(root, memory.id, max_nodes=2001)


def test_cli_and_gateway_independence_are_additive_and_read_graph_is_opt_in(tmp_path, monkeypatch, capsys):
    import json

    from commontrace import cli, gateway

    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    memory = fact(root, "Check deployment signatures before action.")
    assert cli.main(["lineage", "contagion", memory.id, "--dest", root]) == 0
    assert json.loads(capsys.readouterr().out)["source"] == memory.id
    app = gateway.Gateway(root, token="test-owner-token", durable=False)
    headers = {"Authorization": "Bearer test-owner-token", "Host": "localhost"}
    body = {"occasion_id": "occasion", "agent_id": "reviewer", "items": [{"id": memory.id, "text": memory.statement}]}
    default = app.handle("POST", "/v1/recall", headers, json.dumps(body).encode())
    assert default.status == 200 and "independence" not in json.loads(default.body)
    assert not store.latest(root, contagion.KIND)
    annotated = app.handle("POST", "/v1/recall", headers, json.dumps({**body, "independence": True,
                           "read_graph": {"output": "answer"}}).encode())
    assert annotated.status == 200
    result = json.loads(annotated.body)
    assert result["independence"]["n_eff"] == 1 and result["read_graph"]["id"]
    bad = app.handle("POST", "/v1/recall", headers, json.dumps({**body, "independence": "true"}).encode())
    assert bad.status == 400
    again = contagion.record_read(root, [memory.id], "reviewer", "occasion", "answer")
    assert again["id"] == result["read_graph"]["id"]


def test_mcp_contagion_omits_private_nodes(tmp_path, monkeypatch):
    import asyncio
    import json

    pytest.importorskip("mcp")
    from commontrace import mcp_server

    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    memory = fact(root, "Inspect actuator temperature before picking.")
    server = mcp_server.build_server(root)
    response = asyncio.run(server.call_tool("contagion_radius", {"identity": memory.id}))
    payload = json.loads(response.content[0].text)
    assert payload["ok"] and payload["nodes"][0]["id"] == memory.id
