"""Exercise real client transports, negotiation and authority pinning."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

from unimem.client import LEARNING_SCHEMA, MemoryClient
from unimem.hub_client import HubLearningClient

OPS = ["capabilities", "configure", "assign", "delivery", "outcome", "run", "status", "rollback"]


@pytest.fixture
def peer():
    requests = []
    state = {"schema": LEARNING_SCHEMA, "operations": OPS}

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            requests.append((self.command, self.path, body, self.headers.get("Authorization")))
            reply = {"schema": state["schema"], "operations": state["operations"], "request": body}
            raw = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = handle_request
        do_POST = handle_request

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_http_learning_uses_versioned_routes_and_pins_context(peer):
    url, calls, _ = peer
    client = MemoryClient(url=url, token="key", agent_id="robot", context={"user": "alice"})
    client.learning.configure("observe", min_arm=4)
    client.learning.assign("occasion", ["memory"], experiment_id="experiment", task_context="pick")
    client.learning.delivery("occasion", [], experiment_id="experiment")
    client.learning.outcome("occasion", reward=0.7, experiment_id="experiment")
    client.learning.run(limit=2)
    client.learning.status(experiment_id="experiment")
    client.learning.rollback("regression")
    for method, path, body, auth in calls:
        assert auth == "Bearer key"
        assert path.startswith("/v1/learning/")
        if method == "GET":
            assert set(parse_qs(urlsplit(path).query)["context"]) == {"agent:robot", "user:alice"}
            assert body == {}
        else:
            assert set(body["context"]) == {"agent:robot", "user:alice"}
    receipts = [body for _, path, body, _ in calls if path.endswith("/delivery")]
    assert receipts == [
        {"occasion_id": "occasion", "memory_ids": [], "experiment_id": "experiment", "context": client.context}
    ]
    assert any("experiment_id=experiment" in path for _, path, _, _ in calls if "/status?" in path)
    outcomes = [body for _, path, body, _ in calls if path.endswith("/outcome")]
    assert outcomes[0]["reward"] == 0.7 and "succeeded" not in outcomes[0]


def test_http_learning_refuses_old_schema_or_missing_operation(peer):
    url, calls, state = peer
    client = MemoryClient(url=url)
    state["schema"] = "unimem.learning.v0"
    with pytest.raises(RuntimeError, match="support"):
        client.learning.assign("occasion", ["m"])
    assert len(calls) == 1
    state["schema"] = LEARNING_SCHEMA
    state["operations"] = ["capabilities"]
    with pytest.raises(RuntimeError, match="support"):
        client.learning.outcome("occasion", True)
    assert len(calls) == 2
    state["operations"] = "outcome"
    with pytest.raises(RuntimeError, match="malformed"):
        client.learning.outcome("occasion", True)
    assert len(calls) == 3
    with pytest.raises(ValueError, match="unknown"):
        client.learning.call("../outcome")


def test_local_facade_passes_trusted_scope_separately(monkeypatch, tmp_path):
    from unimem import learning

    calls = []

    def operate(root, operation, payload, **authority):
        calls.append((root, operation, payload, authority))
        return {"schema": LEARNING_SCHEMA, "operations": OPS}

    monkeypatch.setattr(learning, "operate", operate)
    client = MemoryClient(str(tmp_path), agent_id="worker", context={"user": "owner"})
    client.learning.assign("occasion", ["m"], context=["agent:attacker"])
    assert calls[-1][3] == {"context": client.context, "untrusted": True}
    assert calls[-1][2]["context"] == ["agent:attacker"]
    assert calls[-1][3]["context"] != calls[-1][2]["context"]


def test_hub_learning_negotiates_and_disables_unsafe_transport_retries():
    import asyncio

    async def scenario():
        call = AsyncMock(return_value={"schema": LEARNING_SCHEMA, "operations": OPS})
        learning = HubLearningClient(SimpleNamespace(call=call))
        await learning.outcome("occasion", metrics={"progress": 0.8}, experiment_id="e")
        assert call.call_args.args == (
            "learning_outcome",
            {"occasion_id": "occasion", "metrics": {"progress": 0.8}, "experiment_id": "e"},
        )
        assert call.call_args.kwargs == {"retry_transport": True}
        await learning.rollback("regression")
        assert call.call_args.kwargs == {"retry_transport": False}
        call.return_value = {"schema": "old", "operations": OPS}
        with pytest.raises(RuntimeError, match="support"):
            await learning.status()
        call.side_effect = [
            {"schema": LEARNING_SCHEMA, "operations": OPS},
            {"error": "forbidden", "detail": "owner required"},
        ]
        with pytest.raises(RuntimeError, match="forbidden.*owner required"):
            await learning.configure("observe")

    asyncio.run(scenario())


def test_local_learning_client_runs_real_durable_lifecycle(tmp_path):
    from unimem.memory_journal import Journal
    from unimem.memory_record import MemoryRecord

    root = str(tmp_path)
    Journal(root).put(MemoryRecord(id="m", kind="fact", version=1, payload={"text": "governed memory"}))
    client = MemoryClient(root)
    configured = client.learning.configure("observe")
    experiment = configured["experiment_id"]
    assignment = client.learning.assign("o", ["m"], experiment_id=experiment, task_context="pick")
    client.learning.delivery("o", assignment["inject"], experiment_id=experiment)
    accepted = client.learning.outcome("o", reward=0.8, experiment_id=experiment)
    assert accepted["outcome"]["reward"] == 0.8
    # Reopen the SDK and repeat without changing accepted evidence.
    reopened = MemoryClient(root)
    assert reopened.learning.outcome("o", reward=0.8, experiment_id=experiment) == accepted
    assert reopened.learning.status()["pending_work"]
    assert reopened.learning.run()["applied"] is False
    assert reopened.learning.status()["occasions"] == 1


def test_robot_http_transport_supports_versioned_read_and_outcome_routes(peer):
    from unimem.robotics.bridge import HTTPGateway

    url, calls, _ = peer
    transport = HTTPGateway(url, "robot-key")
    transport.get("/v1/learning/capabilities", ["agent:r1"])
    transport.post("/v1/learning/outcome", {"occasion_id": "o", "experiment_id": "e", "reward": 0.5})
    assert calls[0][0] == "GET"
    assert parse_qs(urlsplit(calls[0][1]).query) == {"context": ["agent:r1"]}
    assert calls[1][1] == "/v1/learning/outcome"
    assert calls[1][3] == "Bearer robot-key"
    with pytest.raises(ValueError):
        transport.post("/v1/learning/configure", {"mode": "adaptive"})


def test_async_learning_snapshots_request_before_capability_await():
    import asyncio

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        received = []

        async def call(tool, body, **options):
            if tool == "learning_capabilities":
                entered.set()
                await release.wait()
                return {"schema": LEARNING_SCHEMA, "operations": OPS}
            received.append(body)
            return {"schema": LEARNING_SCHEMA}

        client = HubLearningClient(SimpleNamespace(call=call))
        metrics = {"completion": 0.8}
        task = asyncio.create_task(client.outcome("task", metrics=metrics))
        await asyncio.wait_for(entered.wait(), timeout=2)
        metrics["completion"] = 0.1
        release.set()
        await task
        assert received == [{"occasion_id": "task", "metrics": {"completion": 0.8}}]
        assert metrics == {"completion": 0.1}

    asyncio.run(scenario())
