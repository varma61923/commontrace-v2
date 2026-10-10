"""Adaptive budgets beyond conversations: multi-channel recall, reflect, the gateway and MCP."""
from __future__ import annotations

import json

import pytest

from commontrace import api_schema, hierarchical, holdout_io, memory_control, recall
from commontrace.client import MemoryClient
from commontrace.gateway import Gateway

PLACES = ("amsterdam", "berlin", "chicago", "dublin", "everett", "frankfurt", "geneva", "helsinki",
          "istanbul", "jakarta", "kyoto", "lisbon")
NOTES = ("replica lag", "failover drill", "runbook owner", "vacuum schedule", "index bloat", "wal archive",
         "pgbouncer pool", "disk alarm", "restore test", "upgrade plan", "audit trail", "parameter tuning")


@pytest.fixture
def facts(tmp_path):
    # Distinct facts, so diversity keeps them all as candidates.
    root = str(tmp_path)
    holdout_io.configure(root, rate=0)
    for place, note in zip(PLACES, NOTES):
        hierarchical.add_fact(root, f"Postgres cluster in {place} is backed up every hour; its {note} "
                                    f"is reviewed weekly by the {place} {note} team.")
    return root


def test_off_by_default_and_unchanged(facts):
    fixed = recall.recall(facts, "postgres backups", budget=60, channels=("facts",))
    assert fixed.budget == 60 and "budget" not in fixed.explain
    off = recall.recall(facts, "postgres backups", budget=60, channels=("facts",), adaptive_budget=False)
    assert off.context == fixed.context


def test_a_list_question_is_shaped_and_a_focused_one_is_not(facts):
    shaped = recall.recall(facts, "Summarize the postgres backups", budget=100, channels=("facts",),
                           adaptive_budget=True, max_budget=1000)
    decision = shaped.explain["budget"]
    assert decision["reason"] == "summary" and decision["shaped"] == 300 and shaped.budget >= 300
    assert shaped.tokens <= shaped.budget
    focused = recall.recall(facts, "how often is postgres cluster 3 backed up", budget=400, channels=("facts",),
                            adaptive_budget=True)
    assert focused.explain["budget"]["reason"] == "focused" and focused.explain["budget"]["grown"] == []
    assert focused.context == recall.recall(facts, "how often is postgres cluster 3 backed up", budget=400,
                                            channels=("facts",)).context


def test_weak_evidence_grows_the_budget_up_to_the_cap(facts):
    # "zebra" is a subject term nothing mentions: coverage stays incomplete, so the budget grows.
    result = recall.recall(facts, "postgres zebra", budget=60, channels=("facts",), adaptive_budget=True,
                           max_budget=200)
    decision = result.explain["budget"]
    assert [(g["from"], g["to"]) for g in decision["grown"]] == [(60, 120), (120, 200)]
    assert "zebra" in decision["grown"][0]["why"]
    assert decision["effective"] == result.budget == 200 and 120 < result.tokens <= 200
    assert result.explain["completeness"]["missing"] == ["zebra"]
    assert result.explain["completeness"]["bucket"] == "PARTIAL"


def test_growth_stops_when_every_candidate_already_fits(tmp_path):
    root = str(tmp_path)
    hierarchical.add_fact(root, "Postgres is backed up hourly.")
    result = recall.recall(root, "postgres zebra", budget=500, channels=("facts",), adaptive_budget=True)
    assert result.explain["budget"]["grown"] == [] and "already fits" in result.explain["budget"]["stopped"]
    assert result.budget == 500


def test_adaptive_settings_come_from_budgets_json(facts, tmp_path):
    (tmp_path / "memory").mkdir(exist_ok=True)
    (tmp_path / "memory" / "budgets.json").write_text(json.dumps(
        {"default": 60, "adaptive_budget": True, "max_budget": 120}), encoding="utf-8")
    result = recall.recall(facts, "postgres zebra", channels=("facts",))
    assert result.explain["budget"]["cap"] == 120 and result.budget == 120
    assert "budget" not in recall.recall(facts, "postgres zebra", channels=("facts",),
                                         adaptive_budget=False).explain
    (tmp_path / "memory" / "budgets.json").write_text(json.dumps({"adaptive_budget": "yes"}), encoding="utf-8")
    with pytest.raises(ValueError, match="adaptive_budget"):
        recall.recall(facts, "postgres", channels=("facts",))


def test_bad_adaptive_arguments(facts):
    with pytest.raises(ValueError):
        recall.recall(facts, "postgres", adaptive_budget="yes")
    with pytest.raises(ValueError):
        recall.recall(facts, "postgres", adaptive_budget=True, max_budget=10)


def test_reflect_adaptive_grows_while_coverage_is_thin(facts):
    fixed = memory_control.reflect(facts, "postgres zebra", budget=60, causal=False)
    assert fixed["budget"] == 60 and "budget_decision" not in fixed
    grown = memory_control.reflect(facts, "postgres zebra", budget=60, causal=False, adaptive_budget=True,
                                   max_budget=200)
    decision = grown["budget_decision"]
    assert decision["reason"] == "focused" and [g["to"] for g in decision["grown"]] == [120, 200]
    assert grown["budget"] == 200 and fixed["tokens_estimate"] < grown["tokens_estimate"] <= 200
    assert len(grown["evidence"]) > len(fixed["evidence"])


def test_reflect_adaptive_keeps_a_covered_answer_at_its_shaped_size(facts):
    result = memory_control.reflect(facts, "postgres backed", budget=400, causal=False, adaptive_budget=True)
    assert result["budget_decision"]["grown"] == [] and result["budget"] == 400
    with pytest.raises(ValueError):
        memory_control.reflect(facts, "postgres", adaptive_budget=1)


def test_local_client_threads_adaptive_budget(facts):
    result = MemoryClient(facts).reflect("postgres zebra", budget=60, adaptive_budget=True)
    assert result["budget_decision"]["requested"] == 60 and result["budget"] > 60


def test_remote_client_sends_the_flag_only_when_set(monkeypatch):
    sent = []
    client = MemoryClient(url="https://memory.example")
    monkeypatch.setattr(client, "_request", lambda op, body: sent.append((op, body)) or {})
    client.reflect("q")
    client.reflect("q", adaptive_budget=True)
    assert "adaptive_budget" not in sent[0][1] and sent[1][1]["adaptive_budget"] is True


def test_gateway_reflect_schema_and_route(facts):
    fields, _required = api_schema.FIELDS["reflect"]
    assert fields["adaptive_budget"] == {"type": "boolean"}
    with pytest.raises(ValueError):
        api_schema.validate_request("reflect", {"query": "q", "adaptive_budget": "yes"})
    gateway = Gateway(facts, token="owner")

    def post(body):
        response = gateway.handle("POST", "/v1/memory/reflect",
                                  {"Authorization": "Bearer owner", "Host": "localhost"}, json.dumps(body).encode())
        return response.status, json.loads(response.body)

    status, data = post({"query": "postgres zebra", "budget": 60, "adaptive_budget": True})
    assert status == 200 and data["budget_decision"]["requested"] == 60 and data["budget"] > 60
    status, data = post({"query": "postgres zebra", "budget": 60})
    assert status == 200 and data["budget"] == 60 and "budget_decision" not in data
    assert post({"query": "q", "adaptive_budget": "yes"})[0] == 400


def test_mcp_memory_recall_takes_adaptive_budget_and_reranker(facts, monkeypatch):
    pytest.importorskip("mcp")
    import asyncio

    from commontrace import mcp_server

    monkeypatch.setenv("COMMONTRACE_CONVERSATION_EMBEDDER", "none")
    server = mcp_server.build_server(facts)

    def call(**arguments):
        result = asyncio.run(server.call_tool("memory_recall", arguments))
        sc = getattr(result, "structured_content", None)
        return sc.get("result", sc) if sc else json.loads(result.content[0].text)

    got = call(question="postgres zebra", budget=60, channels=["facts"], adaptive_budget=True)
    assert got["ok"] and got["explain"]["budget"]["requested"] == 60 and got["budget"] > 60
    assert not call(question="postgres", reranker="nope")["ok"]
