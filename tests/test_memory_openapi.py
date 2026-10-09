"""Standard-contract and live-handler agreement, including refusal and idempotency."""
from __future__ import annotations

import json

import pytest

from commontrace import api_schema, holdout_io
from commontrace.gateway import Gateway


def test_openapi_models_match_live_memory_operations(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0, salt="contract-test")
    gateway = Gateway(root, token="operator-token")
    doc = gateway._openapi({}, {})
    assert doc["openapi"] == "3.0.3"
    headers = {"Host": "localhost", "Authorization": "Bearer operator-token"}
    requests = {
        "add": {"text": "Office is Tokyo"}, "batch": {"items": [{"statement": "Office is Osaka"}]},
        "search": {"query": "office"}, "profile": {"query": "office", "occasion_id": "profile"},
        "reflect": {"query": "office", "occasion_id": "reflection", "budget": 100},
        "check-action": {"tool": "read"}, "outcome": {"occasion_id": "reflection", "succeeded": True},
        "propose": {"text": "A review proposal", "sources": ["pending-source"]},
    }
    fact_id = ""
    for operation, body in requests.items():
        if operation == "propose":
            assert fact_id
            body["sources"] = [fact_id]
        path = "/v1/memory/"+operation
        schema = doc["components"]["schemas"][api_schema.model_name(operation)+"Request"]
        assert set(schema["required"]) <= body.keys()
        response = gateway.handle("POST", path, headers, json.dumps(body).encode())
        assert response.status == 200, (operation, response.body)
        value = json.loads(response.body)
        if operation == "add":
            fact_id = value["facts"][0]["id"]
        model = doc["components"]["schemas"][api_schema.model_name(operation)+"Response"]
        assert set(model.get("required", [])) <= value.keys()
        # Optional JSON Schema tooling performs full recursive validation;
        # native generated clients in CI also compile these typed models.
        try:
            import jsonschema
        except ImportError:
            pass
        else:
            jsonschema.Draft4Validator({"allOf": [{"$ref": "#/components/schemas/"+
                api_schema.model_name(operation)+"Response"}], "components": doc["components"]}).validate(value)
        assert doc["paths"][path]["post"]["security"] == [{"bearer": []}]
    again = gateway.handle("POST", "/v1/memory/add", headers, json.dumps(requests["add"]).encode())
    assert json.loads(again.body)["facts"][0]["action"] == "NOOP"
    assert api_schema.components()["Fact"]["properties"]["action"]["enum"] == ["ADD", "NOOP"]
    assert schema["type"] == "object"


def test_gateway_enforces_the_schema_before_any_write(tmp_path):
    gateway = Gateway(str(tmp_path), token="operator-token")
    headers = {"Host": "localhost", "Authorization": "Bearer operator-token"}
    for operation, body in (("add", {"text": "fact", "unknown": True}),
                            ("propose", {"text": "proposal", "sources": "untyped"}),
                            ("search", {"query": "office", "limit": True}),
                            ("reflect", {"query": "office", "exploration_slots": 101})):
        response = gateway.handle("POST", "/v1/memory/"+operation, headers, json.dumps(body).encode())
        assert response.status == 400
    assert not (tmp_path/"memory"/"facts.jsonl").exists()


def test_swagger_ui_assets_are_pinned_and_cannot_send_credentials_off_origin(tmp_path):
    gateway = Gateway(str(tmp_path), token="operator-token")
    response = gateway.handle("GET", "/v1/docs", {"Host": "localhost"}, None)
    assert response.status == 200 and "text/html" in response.content_type
    text = response.body.decode()
    assert 'url:"/v1/openapi.json"' in text and 'persistAuthorization:false' in text
    assert text.count('integrity="sha384-') == 2 and "@5.30.0" in text
    assert "connect-src 'self'" in response.headers["Content-Security-Policy"]
    assert "unsafe-eval" not in response.headers["Content-Security-Policy"]
    assert "operator-token" not in text


def test_recursive_response_schema_accepts_actual_withdrawal_maps(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    document = Gateway(str(tmp_path))._openapi({}, {})
    model = document["components"]["schemas"]["ReflectResponse"]
    schema = {**model, "components": document["components"]}
    value = {"context": "", "tokens_estimate": 0, "budget": 100, "evidence": [], "occasion_id": "task",
             "withheld": [], "withdrawn": {"harmful": {"verdict": "HURTS", "reason": "harm"}},
             "exploration": [], "authority_sources": []}
    jsonschema.Draft4Validator(schema).validate(value)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft4Validator(schema).validate({**value, "withdrawn": []})
