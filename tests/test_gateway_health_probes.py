"""Liveness and readiness probes for orchestrators (Kubernetes, Docker, load balancers)."""
import json
import os

from commontrace import gateway, holdout_io, paths

TOKEN = "t" * 40


def _get(g, path):
    response = g.handle("GET", path, {}, None)
    return response.status, json.loads(response.body)


def _gateway(tmp_path):
    root = str(tmp_path / "store")
    holdout_io.configure(root, rate=0.5, salt="probe-tests")
    return root, gateway.Gateway(root, token=TOKEN)


def test_live_needs_no_token_and_reports_no_storage_detail(tmp_path):
    _root, g = _gateway(tmp_path)
    status, body = _get(g, "/v1/health/live")
    assert status == 200 and body["status"] == "live"
    assert set(body) == {"ok", "status", "version"}


def test_ready_reports_named_checks_without_paths(tmp_path):
    root, g = _gateway(tmp_path)
    status, body = _get(g, "/v1/health/ready")
    assert status == 200 and body["ok"] is True
    assert body["checks"] == {"store": "ok", "schemas": "ok", "disk": "ok"}
    assert root not in json.dumps(body)


def test_ready_is_503_when_the_store_disappears(tmp_path):
    root, g = _gateway(tmp_path)
    memory = paths.memory_dir(root)
    os.rename(memory, memory + ".moved")
    try:
        status, body = _get(g, "/v1/health/ready")
    finally:
        os.rename(memory + ".moved", memory)
    assert status == 503 and body["status"] == "not_ready" and body["checks"]["store"] == "missing"


def test_ready_is_503_on_low_disk(tmp_path, monkeypatch):
    _root, g = _gateway(tmp_path)
    monkeypatch.setattr(gateway, "READY_MIN_FREE_BYTES", 1 << 62)
    status, body = _get(g, "/v1/health/ready")
    assert status == 503 and body["checks"]["disk"] == "low"


def test_probes_are_in_the_live_openapi_without_security(tmp_path):
    _root, g = _gateway(tmp_path)
    status, doc = _get(g, "/v1/openapi.json")
    assert status == 200
    for path in ("/v1/health/live", "/v1/health/ready"):
        assert "security" not in doc["paths"][path]["get"]


def test_conversation_recall_accepts_an_adaptive_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONTRACE_CONVERSATION_EMBEDDER", "none")
    _root, g = _gateway(tmp_path)
    auth = {"Authorization": f"Bearer {TOKEN}", "Host": "localhost:8787"}

    def post(body):
        response = g.handle("POST", "/v1/conversation/recall" if "question" in body else "/v1/conversation/add",
                            auth, json.dumps(body).encode())
        return response.status, json.loads(response.body)

    assert post({"space": "ana", "session": "s1", "messages": [
        {"speaker": "Ana", "text": "I painted a landscape and a portrait."}]})[0] == 200
    status, got = post({"space": "ana", "question": "What paintings has Ana made?", "budget": 400,
                        "adaptive_budget": True})
    assert status == 200 and got["explain"]["budget"] == {"requested": 400, "effective": 800, "reason": "list"}
    status, fixed = post({"space": "ana", "question": "What paintings has Ana made?", "budget": 400})
    assert status == 200 and "budget" not in fixed["explain"]
    assert post({"space": "ana", "question": "x", "adaptive_budget": "yes"})[0] == 400
