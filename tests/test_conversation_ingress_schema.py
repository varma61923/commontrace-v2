"""Transport validation rejects malformed batches before writing any memory."""
from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from commontrace import gateway, mcp_server
from commontrace.conversation.store import Store, db_path
from commontrace.conversation.validation import validate_messages


@contextlib.contextmanager
def running_gateway(root: Path) -> Iterator[tuple[str, int]]:
    app = gateway.Gateway(str(root), token="test-only-token")
    server = gateway.make_http_server(app, "127.0.0.1", 0)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        assert not worker.is_alive()


@pytest.mark.parametrize("field", ["text", "content", "role", "speaker", "name"])
@pytest.mark.parametrize("value", [{"arbitrary": "object"}, ["system"], True, 123])
def test_real_http_rejects_invalid_batch_before_creating_store(
    tmp_path: Path, field: str, value: Any,
) -> None:
    messages = [{"text": "This first valid turn must not be partly persisted."},
                {"text": "A second turn.", field: value}]
    with running_gateway(tmp_path) as address, contextlib.closing(http.client.HTTPConnection(*address)) as client:
        client.request("POST", "/v1/conversation/add", body=json.dumps({
            "space": "untrusted", "session": "batch", "messages": messages,
        }), headers={"Authorization": "Bearer test-only-token", "Content-Type": "application/json"})
        response = client.getresponse()
        payload = json.loads(response.read())
        assert response.status == 400
        assert payload["error"]["code"] == "bad_request"
    assert not Path(db_path(str(tmp_path), "untrusted")).exists()


def test_valid_nullable_messages_and_numeric_ids_keep_capture_compatibility(tmp_path: Path) -> None:
    messages = [{"text": None, "content": None, "role": None, "speaker": None, "name": None},
                {"content": "The team works in Oslo.", "id": 123, "role": "user"}]
    validate_messages(messages)
    assert messages[0]["text"] is None
    with running_gateway(tmp_path) as address, contextlib.closing(http.client.HTTPConnection(*address)) as client:
        for _ in range(2):
            client.request("POST", "/v1/conversation/add", body=json.dumps({
                "space": "compatible", "session": "batch", "messages": messages,
            }), headers={"Authorization": "Bearer test-only-token", "Content-Type": "application/json"})
            response = client.getresponse()
            payload = json.loads(response.read())
            assert response.status == 200
            assert payload["added"] == (1 if _ == 0 else 0)
    with Store(str(tmp_path), "compatible") as store:
        assert store.db.execute("SELECT count(*) FROM turns").fetchone()[0] == 1


def test_mcp_invalid_batch_is_refused_without_memory_write(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    server = mcp_server.build_server(str(tmp_path))
    result = asyncio.run(server.call_tool("conversation_add", {
        "space": "untrusted", "session": "batch",
        "messages": [{"text": "Valid first turn."}, {"text": {"not": "a string"}}],
    }))
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is False
    assert "must be a string or null" in payload["error"]
    assert not Path(db_path(str(tmp_path), "untrusted")).exists()
