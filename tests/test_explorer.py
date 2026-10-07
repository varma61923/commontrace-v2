"""Actual authenticated retrieval inspection, isolation and mutation boundaries."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from commontrace import explorer, gateway, hierarchical

TOKEN = "explorer-test-token-01234567890123456789"


def call(root: Path, data: dict, *, tag: str = "", authenticated: bool = True):
    service = gateway.Gateway(str(root), token=TOKEN)
    headers = {"Authorization": f"Bearer {TOKEN}"} if authenticated else {}
    if tag:
        headers["X-Container-Tag"] = tag
    response = service.handle("POST", "/v1/explore", headers, json.dumps(data).encode())
    return response.status, json.loads(response.body)


@pytest.mark.parametrize("data", [
    {}, {"question": " "}, {"question": "x" * 2001}, {"question": 12},
    {"question": "Lumen", "budget": True}, {"question": "Lumen", "budget": 8001},
    {"question": "Lumen", "evidence_budget": -1}, {"question": "Lumen", "channels": []},
    {"question": "Lumen", "channels": ["facts", "facts"]},
    {"question": "Lumen", "channels": ["graph"]},
    {"question": "Lumen", "channels": ["conversations"]},
    {"question": "Lumen", "as_of": "yesterday-ish"},
    {"question": "Lumen", "as_of": False}, {"question": "Lumen", "as_of": 0},
    {"question": "Lumen", "as_of": []}, {"question": "Lumen", "as_of": {}},
])
def test_invalid_requests_fail_before_retrieval(tmp_path, data):
    status, body = call(tmp_path, data)
    assert status == 400
    assert body["error"]["code"] == "bad_request"


def test_authentication_precedes_inspection(tmp_path):
    status, body = call(tmp_path, {"question": "private memory"}, authenticated=False)
    assert status == 401 and "items" not in body


def test_real_fact_retrieval_is_read_only_and_budgeted(tmp_path):
    fact, _ = hierarchical.add_fact(str(tmp_path), "The Lumen database timeout is 5 seconds")
    before = hierarchical.load_facts(str(tmp_path))[fact.id].to_dict()
    status, body = call(tmp_path, {"question": "Lumen database timeout", "channels": ["facts"], "budget": 200})
    assert status == 200
    assert body["read_only"] is True and body["answer_accuracy_measured"] is False
    assert f"fact:{fact.id}" in {i["id"] for i in body["items"]}
    assert body["tokens"] <= 200
    assert body["elapsed_ms"] >= 0
    assert hierarchical.load_facts(str(tmp_path))[fact.id].to_dict() == before
    assert not list(tmp_path.rglob("holdout*.jsonl"))


def test_server_scope_cannot_be_overridden_by_request_body(tmp_path):
    a, _ = hierarchical.add_fact(str(tmp_path), "The Lumen timeout is 5 seconds", scopes=["container:alpha"])
    b, _ = hierarchical.add_fact(str(tmp_path), "The Lumen timeout is 90 seconds", scopes=["container:beta"])
    status, body = call(tmp_path, {"question": "Lumen timeout", "channels": ["facts"],
                                    "scope": "container:beta"}, tag="alpha")
    assert status == 200 and body["scope"] == "container:alpha"
    ids = {i["id"] for i in body["items"]}
    assert f"fact:{a.id}" in ids and f"fact:{b.id}" not in ids
    assert "90 seconds" not in json.dumps(body)


def test_unknown_detail_abstains_without_discarding_context(tmp_path):
    hierarchical.add_fact(str(tmp_path), "Mira maintains the Lumen database")
    status, body = call(tmp_path, {"question": "What is Mira's professional license number?", "channels": ["facts"]})
    assert status == 200 and body["assessment"]["abstain"] is True
    assert "Mira" in body["context"]


def test_deleted_memory_disappears_on_next_inspection(tmp_path):
    fact, _ = hierarchical.add_fact(str(tmp_path), "The Lumen timeout is 5 seconds")
    data = {"question": "Lumen timeout", "channels": ["facts"]}
    assert call(tmp_path, data)[1]["items"]
    with hierarchical.mutate_facts(str(tmp_path)) as facts:
        del facts[fact.id]
    status, body = call(tmp_path, data)
    assert status == 200 and not body["items"] and body["assessment"]["abstain"]


def test_conversation_space_is_namespaced_before_retrieval(tmp_path):
    service = gateway.Gateway(str(tmp_path), token=TOKEN)
    for tag, day in (("alpha", "Friday"), ("beta", "Tuesday")):
        added = service.handle("POST", "/v1/conversation/add", {"Authorization": f"Bearer {TOKEN}",
                                                                 "X-Container-Tag": tag},
                               json.dumps({"space": "chat", "session": "session", "messages": [
                                   {"speaker": "Mira", "text": f"The Lumen release ships {day}"}]}).encode())
        assert added.status == 200
    response = service.handle("POST", "/v1/explore", {"Authorization": f"Bearer {TOKEN}",
                                                        "X-Container-Tag": "alpha"},
                              json.dumps({"question": "Lumen release", "channels": ["conversations"],
                                          "space": "chat"}).encode())
    assert response.status == 200
    text = response.body.decode()
    assert "Friday" in text and "Tuesday" not in text


def test_explorer_has_no_model_dependency(tmp_path):
    with pytest.raises(explorer.ExplorerError):
        explorer.inspect(str(tmp_path), {"question": "test", "channels": ["dense"]})


def test_bound_fact_exposes_actual_quote_and_withdraws_after_source_change(tmp_path):
    from commontrace.fact_evidence import bind_evidence

    premise, _ = hierarchical.add_fact(str(tmp_path), "The Lumen database timeout log records 5 seconds")
    fact, _ = hierarchical.add_fact(str(tmp_path), "The Lumen database timeout is 5 seconds",
                                    evidence=[bind_evidence(str(tmp_path), "fact", premise.id)])
    data = {"question": "Lumen database timeout", "channels": ["facts"], "evidence_budget": 512}
    status, body = call(tmp_path, data)
    assert status == 200
    hit = next(item for item in body["items"] if item["id"] == f"fact:{fact.id}")
    sources = hit["provenance"]["evidence_context"]["nodes"]
    assert any(node["quote"] == premise.statement for node in sources)
    with hierarchical.mutate_facts(str(tmp_path)) as facts:
        facts[premise.id].statement = "The Lumen database timeout log records 9 seconds"
    status, body = call(tmp_path, data)
    assert status == 200 and f"fact:{fact.id}" not in {item["id"] for item in body["items"]}
