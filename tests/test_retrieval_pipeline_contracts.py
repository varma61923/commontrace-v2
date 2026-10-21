"""Public retrieval behavior across the canonical JSONL and SQLite stores."""
from __future__ import annotations

import asyncio
import json

import pytest

from unimem import fact_store, graph, hierarchical, search_recipes
from unimem.client import MemoryClient
from unimem.gateway import Gateway


@pytest.fixture(params=["jsonl", "sqlite"])
def root(tmp_path, request):
    (tmp_path / "unimem.yaml").write_text(
        "profiles:\n  local:\n    embedding: none\n    reranker: none\n"
    )
    if request.param == "sqlite":
        fact_store.migrate(str(tmp_path))
    return str(tmp_path)


def add(root, text, **options):
    fact, _ = hierarchical.add_fact(root, text, valid_from="2025-01-01T00:00:00Z", **options)
    return fact


def test_hybrid_consensus_beats_either_channel_alone(root):
    lexical = add(root, "quartz approval")
    consensus = add(root, "quartz release policy")
    semantic = add(root, "automobile service procedure")
    explanation = {}
    rows = MemoryClient(root).search(
        "quartz approval", dense_scores={consensus.id: 0.8, semantic.id: 0.9},
        reranker="none", graph_hops=0, explain=explanation,
    )
    assert rows[0]["id"] == consensus.id
    assert {row["id"] for row in rows} == {lexical.id, consensus.id, semantic.id}
    assert explanation["fusion"] == "rrf" and explanation["k"] == 60
    assert explanation["graph_candidates"] == 0


def test_ingestion_entities_boost_only_admitted_candidates(root):
    from unimem import entity_store

    fact = add(root, "Alice owns the release checklist")
    private = add(root, "Alice owns a private checklist", scopes=["agent:other"])
    client = MemoryClient(root, agent_id="reader")
    options = {"graph_hops": 0, "reranker": "none"}
    boosted = client.search("Alice", **options)
    assert [row["id"] for row in boosted] == [fact.id]
    assert boosted[0]["signals"]["entity_boost"] > 0
    entity_store.unlink_memory(root, fact.id)
    baseline = client.search("Alice", **options)
    assert baseline[0]["score"] < boosted[0]["score"]
    assert "entity_boost" not in baseline[0]["signals"]
    assert private.id not in {row["id"] for row in baseline}


def test_multi_hop_preserves_feedback_weight_and_prefers_stronger_paths(root):
    weak = add(root, "the weaker answer")
    strong = add(root, "the stronger answer")

    class Backend:
        def neighbors(self, node_id, *, as_of=None):
            return {
                "Alice": [{"neighbor_id": "Bridge", "weight": .8}],
                "Bridge": [{"neighbor_id": weak.id, "weight": .1},
                           {"neighbor_id": strong.id, "weight": .9}],
            }.get(node_id, [])

    rows = MemoryClient(root).search(
        "Alice", entity_ids=["Alice"], backend=Backend(), reranker="none",
    )
    assert [row["id"] for row in rows] == [strong.id, weak.id]
    assert rows[0]["signals"]["entity"] == pytest.approx(.72)
    assert rows[1]["signals"]["entity"] == pytest.approx(.08)


def test_mcp_uses_the_governed_hybrid_and_temporal_pipeline(root, monkeypatch):
    pytest.importorskip("mcp")
    from unimem import mcp_server

    monkeypatch.setattr(hierarchical, "_now", lambda: "2025-02-01T00:00:00Z")
    monkeypatch.setattr(graph, "_now", lambda: "2025-02-01T00:00:00Z")
    answer = add(root, "the registered office is in bristol", scopes=["agent:reader"])
    graph.add_node(root, "person:alice", "person", "Alice")
    graph.add_node(root, "company:meridian", "organization", "Meridian")
    graph.add_node(root, answer.id, "memory", answer.statement)
    graph.add_edge(root, "person:alice", "company:meridian", "works_at")
    graph.add_edge(root, "company:meridian", answer.id, "mentions")
    server = mcp_server.build_server(root)

    def call(**options):
        result = asyncio.run(server.call_tool("memory_search", {
            "query": "Alice", "context": ["agent:reader"], "reranker": "none", **options,
        }))
        structured = getattr(result, "structured_content", None)
        return structured.get("result", structured) if structured else json.loads(result.content[0].text)

    result = call(graph_hops=2, as_of="2025-03-01", known_at="2025-03-01")
    assert result["ok"] and result["results"][0]["id"] == answer.id
    assert call(graph_hops=1)["results"] == []
    assert call(context=["agent:other"])["results"] == []
    assert call(known_at="2020-01-01")["results"] == []
    assert call(graph_hops=5)["ok"] is False
    assert call(reranker="unknown")["ok"] is False


def test_query_entities_seed_two_hop_evidence_without_lexical_overlap(root):
    answer = add(root, "the registered office is in bristol")
    graph.add_node(root, "person:alice", "person", "Alice")
    graph.add_node(root, "company:meridian", "organization", "Meridian")
    graph.add_node(root, answer.id, "memory", answer.statement)
    graph.add_edge(root, "person:alice", "company:meridian", "works_at")
    graph.add_edge(root, "company:meridian", answer.id, "mentions")

    client = MemoryClient(root)
    assert not client.search("Alice", graph_hops=1, reranker="none")
    assert client.search("Alice", graph_hops=2, reranker="none")[0]["id"] == answer.id
    assert client.search("Alice", retriever="planned", reranker="none")[0]["id"] == answer.id
    assert client.search("Alice", retriever="graph-completion", reranker="none")[0]["id"] == answer.id


@pytest.mark.parametrize("prefix", ["", "memory:"])
@pytest.mark.parametrize("retriever", ["hybrid", "planned", "graph-completion", "multi-hop", "ppr", "expanded", "hyde"])
def test_a_private_fact_cannot_bridge_to_otherwise_visible_evidence(root, prefix, retriever):
    private = add(root, "a private relationship", scopes=["agent:other"])
    target = add(root, "a hidden route to public evidence", scopes=["agent:reader"])
    bridge = prefix + private.id
    graph.add_node(root, "person:alice", "person", "Alice")
    graph.add_node(root, bridge, "memory", private.statement)
    graph.add_node(root, target.id, "memory", target.statement)
    graph.add_edge(root, "person:alice", bridge, "mentions")
    graph.add_edge(root, bridge, target.id, "derived_from")

    rows = MemoryClient(root, agent_id="reader").search(
        "Alice", retriever=retriever, graph_hops=2, reranker="none",
    )
    assert rows == []


def test_valid_time_and_record_time_are_independent_public_search_filters(root, monkeypatch):
    monkeypatch.setattr(hierarchical, "_now", lambda: "2025-02-01T00:00:00Z")
    earlier = add(root, "quartz approval was recorded in february")
    monkeypatch.setattr(hierarchical, "_now", lambda: "2025-03-01T00:00:00Z")
    later = add(root, "quartz approval correction was recorded in march")
    client = MemoryClient(root)
    options = {"as_of": "2025-01-15", "graph_hops": 0, "reranker": "none"}

    before_correction = client.search("quartz approval", known_at="2025-02-15", **options)
    after_correction = client.search("quartz approval", known_at="2025-03-15", **options)
    assert [row["id"] for row in before_correction] == [earlier.id]
    assert {row["id"] for row in after_correction} == {earlier.id, later.id}

    gateway = Gateway(root, token="t" * 40)
    result = gateway.handle(
        "POST", "/v1/memory/search",
        body=json.dumps({"query": "quartz approval", "known_at": "2025-02-15", **options}).encode(),
        headers={"Authorization": "Bearer " + "t" * 40, "Host": "localhost"},
    )
    assert result.status == 200, result.body
    assert [row["id"] for row in json.loads(result.body)["results"]] == [earlier.id]


def test_mmr_keeps_a_relevant_but_distinct_supporting_fact(root):
    first = add(root, "quartz policy retry")
    duplicate = add(root, "quartz policy retries")
    distinct = add(root, "quartz policy rollback for archival export")
    rows = MemoryClient(root).search(
        "quartz policy", reranker="mmr", graph_hops=0, limit=2,
        dense_scores={first.id: 0.95, duplicate.id: 0.94, distinct.id: 0.93},
    )
    assert [row["id"] for row in rows] == [first.id, distinct.id]


def test_reranking_cannot_redeliver_a_concurrently_forgotten_fact(root):
    from unimem.reranking import TextReranker

    target = add(root, "quartz policy for the primary service")
    retained = add(root, "quartz policy for the backup service")

    class Forgetting(TextReranker):
        name = "forget-during-ranking"

        def score(self, query, documents):
            hierarchical.forget_fact(root, target.id)
            return [1.0] * len(documents)

    rows = MemoryClient(root).search("quartz policy", reranker=Forgetting(), graph_hops=0)
    assert [row["id"] for row in rows] == [retained.id]


@pytest.mark.parametrize("value", [-1, 5, True, "2"])
def test_hop_budget_rejects_unbounded_or_ambiguous_values(root, value):
    with pytest.raises(ValueError, match="graph_hops"):
        search_recipes.search(root, "Alice", graph_hops=value)
