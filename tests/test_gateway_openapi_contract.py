"""Every gateway route is in the OpenAPI document, and every live response matches its schema."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys

import pytest

from benchmarks.robot_fleet_demo import _png
from commontrace import gateway, gateway_contract
from tests.test_ledger_views import _lesson, seeded_store

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "c" * 40
HEADERS = {"Authorization": "Bearer " + TOKEN, "Host": "localhost"}


def _doc(root):
    return gateway.Gateway(root, token=TOKEN)._openapi({}, {})


def _json_schema(node):
    """OpenAPI 3.0 schema objects to JSON Schema draft 4 (nullable becomes a null type)."""
    if isinstance(node, list):
        return [_json_schema(v) for v in node]
    if not isinstance(node, dict):
        return node
    out = {k: _json_schema(v) for k, v in node.items() if k not in ("nullable", "format", "example")}
    if node.get("nullable"):
        if "type" in out:
            out["type"] = [out["type"], "null"]
        if "enum" in out:
            out["enum"] = [*out["enum"], None]
        if "$ref" in out or "allOf" in out:
            out = {"anyOf": [{"type": "null"}, out]}
    return out


def test_every_registered_route_is_documented_once(tmp_path):
    doc = _doc(str(tmp_path))
    gw = gateway.Gateway(str(tmp_path), token=TOKEN)
    for method, path in gw.routes:
        operation = doc["paths"][path][method.lower()]
        assert operation["operationId"] and operation["tags"]
        if method == "POST":
            assert "requestBody" in operation, path
        assert "500" in operation["responses"]
        assert ("401" in operation["responses"]) == ("security" in operation), path
    ids = [op["operationId"] for item in doc["paths"].values() for op in item.values()]
    assert len(ids) == len(set(ids))
    assert set(gateway_contract.OPERATIONS) | {r for r in gw.routes if r[1].startswith("/v1/memory/")} == set(gw.routes)


def test_an_undocumented_route_refuses_to_build(tmp_path):
    gw = gateway.Gateway(str(tmp_path), token=TOKEN)
    gw._route("GET", "/v1/new-thing", lambda b, q: {}, summary="new")
    with pytest.raises(RuntimeError, match="undocumented"):
        gw._openapi({}, {})


def test_the_document_is_valid_openapi(tmp_path):
    validator = pytest.importorskip("openapi_spec_validator")
    validator.validate(_doc(str(tmp_path)))
    with open(os.path.join(REPO, "sdk", "openapi.json"), encoding="utf-8") as fh:
        validator.validate(json.load(fh))


def test_the_exported_documents_are_fresh():
    result = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "export_openapi.py"), "--check"],
                            capture_output=True, text=True, cwd=REPO)
    assert result.returncode == 0, result.stderr


def test_live_responses_match_the_declared_schemas(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    root = str(tmp_path)
    _first, second = seeded_store(root)
    _lesson(root, "draft", "## Rule\nDraft rule.\n", status="review")
    _lesson(root, "draft-two", "## Rule\nA second draft.\n", status="review")
    gw = gateway.Gateway(root, token=TOKEN, durable=False, allow_approval=True, allow_self_signup=True)
    doc = gw._openapi({}, {})
    components = {"components": {"schemas": _json_schema(doc["components"]["schemas"])}}
    checked: set[tuple[str, str]] = set()

    def call(method, target, body=None, *, status=200):
        response = gw.handle(method, target, HEADERS, json.dumps(body).encode() if body is not None else None)
        assert response.status == status, (target, response.body[:300])
        path = target.split("?", 1)[0]
        operation = doc["paths"][path][method.lower()]
        content = operation["responses"][str(status)]["content"]
        checked.add((method, path))
        if not response.content_type.startswith("application/json"):
            assert any(response.content_type.startswith(kind) for kind in content), (path, response.content_type)
            return response.body
        value = json.loads(response.body)
        schema = _json_schema(content["application/json"]["schema"])
        jsonschema.Draft4Validator({**schema, **components}).validate(value)
        if method == "POST" and status == 200:
            request = _json_schema(operation["requestBody"]["content"]["application/json"]["schema"])
            jsonschema.Draft4Validator({**request, **components}).validate(body)
        return value

    call("POST", "/v1/agent/signup", {"agent_id": "bot-1", "scopes": ["team"]})
    enrolled = call("POST", "/v1/agent/enroll", {})
    call("POST", "/v1/agent/claim", {"agent_id": enrolled["agent_id"], "owner": "alice"})
    call("POST", "/v1/agent/rotate", {"agent_id": "bot-1"})
    call("POST", "/v1/agent/plugin", {"agent_id": "bot-1"})
    call("POST", "/v1/agent/heartbeat", {"agent_id": "bot-1"})
    call("POST", "/v1/control/directive", {"text": "Never delete prod", "deny_tools": ["rm"], "context": ["team"]})
    question = call("POST", "/v1/control/question", {"text": "What breaks payments?", "context": ["team"]})
    call("POST", "/v1/control/refresh", {"id": question["id"]})
    fact = call("POST", "/v1/memory/add", {"text": "Office is Tokyo"})
    proposal = call("POST", "/v1/memory/propose", {"text": "Proposal", "sources": [fact["facts"][0]["id"]]})
    call("POST", "/v1/control/reject-proposal",
         {"id": proposal["id"], "expected_revision": proposal["revision"], "reason": "no"})
    for operation, body in (("search", {"query": "office"}), ("profile", {"query": "office"}),
                            ("reflect", {"query": "office", "occasion_id": "m1"}),
                            ("check-action", {"tool": "read"}), ("batch", {"items": [{"statement": "Osaka"}]}),
                            ("outcome", {"occasion_id": "m1", "succeeded": True})):
        call("POST", "/v1/memory/" + operation, body)
    call("POST", "/v1/control/review-foresight", {"id": "x", "expected_revision": "y", "approve": True}, status=400)
    call("POST", "/v1/control/skill-review", {"id": "x", "expected_revision": "y", "verdict": "approve"},
         status=400)
    call("GET", "/v1/palace")
    for target in ("/v1/health", "/v1/health/live", "/v1/health/ready", "/v1/capabilities", "/v1/whoami",
                   "/v1/metrics?format=json", "/v1/command-catalog", "/v1/status", "/v1/memories",
                   "/v1/occasions?limit=5", "/v1/agents", "/v1/lessons?status=review&limit=10",
                   "/v1/lesson?slug=draft", "/v1/ledger/executive", "/v1/ledger/releases",
                   f"/v1/ledger/releases?to={second.release_id}",
                   "/v1/ledger/design?baseline=0.5&effect=0.1&rate=0.2&daily=100",
                   "/v1/ledger/forensics?occasion=o1", "/v1/ledger/digest?days=7", "/v1/market/listings",
                   "/v1/openapi.json", "/v1/docs"):
        call("GET", target)
    call("GET", "/v1/metrics")
    call("GET", "/v1/ledger/releases?to=nope", status=404)
    call("POST", "/v1/resolve_tag", {"container_tag": "team-a"})
    call("POST", "/v1/explore", {"question": "idempotency key"})
    call("POST", "/v1/command", {"command": "doctor", "args": []})
    call("POST", "/v1/recall", {"occasion_id": "r1", "query": "payment retry", "agent_id": "bot-1"})
    call("POST", "/v1/recall", {"occasion_id": "r2", "items": [{"id": "a", "text": "Retry with backoff"}]})
    call("POST", "/v1/outcome", {"occasion_id": "r1", "succeeded": True})
    call("POST", "/v1/outcome", {"occasion_id": "r2", "signals": [
        {"detector": "from_test_exit_code", "args": {"returncode": 0}}]})
    call("POST", "/v1/episode", {"occasion_id": "e1", "summary": "picked", "sensors": {"grip_n": 12.5},
                                 "media": [{"kind": "image", "mime": "image/png",
                                            "data_b64": base64.b64encode(_png(4, 4, 9)).decode()}]})
    call("POST", "/v1/conversation/add", {"space": "u1", "session": "s1", "messages": [
        {"speaker": "A", "text": "I moved to Paris in May", "at": "2024-05-02"}]})
    call("POST", "/v1/conversation/recall", {"space": "u1", "question": "Where did A move?"})
    call("POST", "/v1/conversation/add", {"space": "u1", "session": "s1", "messages": [
        {"speaker": "A", "text": "The new flat near the river has two bedrooms", "at": "2024-05-03"}]})
    call("GET", "/v1/conversation/sessions?space=u1&limit=10")
    call("GET", "/v1/conversation/sessions?space=missing", status=404)
    call("POST", "/v1/conversation/summarize", {"space": "u1", "session": "s1"})
    call("POST", "/v1/conversation/summarize", {"space": "u1", "session": "s1", "mode": "extractive",
                                                "verify": "tail"})
    call("POST", "/v1/conversation/summarize", {"space": "u1", "session": "absent"}, status=404)
    call("POST", "/v1/working-memory", {"space": "u1", "session": "s1", "question": "Where did A move?",
                                        "budget": 400, "recent_turns": 1})
    call("POST", "/v1/working-memory", {"space": "u1", "session": "s1", "question": "x", "budget": 10},
         status=400)
    call("POST", "/v1/lesson/edit", {"slug": "draft", "rule": "Draft rule, edited."})
    call("POST", "/v1/lesson/approve", {"slug": "draft-two", "rationale": "measured"}, status=409)
    call("POST", "/v1/lesson/reject", {"slug": "draft", "reason": "duplicate"})
    call("POST", "/v1/market/listings", {"listing": {}}, status=422)
    call("POST", "/v1/market/install", {"id": "nope", "accept_licence": "x"}, status=404)
    call("POST", "/v1/connectors/github/push", {"zen": "hi"}, status=404)

    routes = set(gw.routes)
    assert routes - checked == set(), sorted(routes - checked)
