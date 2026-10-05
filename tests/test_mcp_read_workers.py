"""Blocking document and SQL reads retain scope and leave MCP responsive."""
import asyncio
import json
import threading

import pytest

from commontrace import mcp_server, sql_guard
from commontrace.ingest import catalog

pytest.importorskip("mcp")


def _payload(result):
    return json.loads(result.content[0].text)


@pytest.mark.parametrize("tool", ["ingest_document_get", "sql_guarded_query"])
def test_read_worker_does_not_block_other_requests(tmp_path, monkeypatch, tool):
    started, release = threading.Event(), threading.Event()

    def held(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return {"content": "registered"} if tool == "ingest_document_get" else {"rows": []}

    if tool == "ingest_document_get":
        monkeypatch.setattr(catalog, "get_document", held)
        arguments = {"doc_id_or_path": "registered-id"}
    else:
        monkeypatch.setattr(sql_guard, "execute_guarded_sql", held)
        arguments = {"db_path": str(tmp_path / "data.sqlite"), "sql": "SELECT 1"}
    server = mcp_server.build_server(str(tmp_path))

    async def drive():
        pending = asyncio.create_task(server.call_tool(tool, arguments))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert not pending.done()
            status = await asyncio.wait_for(server.call_tool("store_status", {}), 1)
            assert _payload(status)["ok"]
        finally:
            release.set()
        assert _payload(await pending)["ok"]

    asyncio.run(drive())


def test_document_tool_never_reads_uncataloged_files(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    internal, external = root / "unregistered.txt", tmp_path / "private.txt"
    internal.write_text("internal secret")
    external.write_text("external secret")
    server = mcp_server.build_server(str(root))
    for source in (internal, external):
        result = _payload(asyncio.run(server.call_tool("ingest_document_get", {"doc_id_or_path": str(source)})))
        assert result["ok"] is False
        assert "secret" not in json.dumps(result)
    doc = catalog.record_document(str(root), str(external), "authorized snapshot")
    external.write_text("changed source secret")
    result = _payload(asyncio.run(server.call_tool("ingest_document_get", {"doc_id_or_path": doc.id})))
    assert result["document"]["content"] == "authorized snapshot"


def test_sql_scope_is_checked_before_worker_executes(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("out-of-scope database reached execution")

    monkeypatch.setattr(sql_guard, "execute_guarded_sql", forbidden)
    root = tmp_path / "store"
    root.mkdir()
    server = mcp_server.build_server(str(root))
    result = _payload(asyncio.run(server.call_tool("sql_guarded_query", {
        "db_path": str(tmp_path / "outside.sqlite"), "sql": "SELECT 1",
    })))
    assert result["ok"] is False
    assert result["code"] == "scope_error"
