"""Gap filling preserves distinct facets and bounded, grounded model decisions."""
from __future__ import annotations

import json

import pytest

from commontrace import fact_store, hierarchical, search_recipes


def seed(root, identities):
    fact_store.migrate(root)
    now = hierarchical._now()
    with fact_store.transaction(root, write=True) as facts:
        for identity in identities:
            facts[identity] = hierarchical.AtomicFact(identity, identity, "general", [], .8, 1, now, None,
                created_at=now, updated_at=now)


def hit(identity, score=1):
    return {"id": identity, "text": identity, "score": score, "signals": {"bm25": score}}


def test_query_blocks_keep_each_facets_best_evidence(tmp_path, monkeypatch):
    seed(str(tmp_path), ["common-a", "common-b", "one", "two"])
    outputs = {"question": [hit("common-a"), hit("common-b")],
               "facet-one": [hit("one"), hit("common-a"), hit("common-b")],
               "facet-two": [hit("two"), hit("common-a"), hit("common-b")]}
    monkeypatch.setattr(search_recipes, "search", lambda root, query, **options: outputs[query])
    decisions = iter([{"grade": "PARTIAL", "queries": ["facet-one", "facet-two"]},
                      {"grade": "COMPLETE", "queries": []}])
    result = search_recipes.multi_round(str(tmp_path), "question", limit=3,
        complete=lambda prompt: (json.dumps(next(decisions)), {}))
    assert [row["id"] for row in result["results"]] == ["common-a", "one", "two"]
    assert result["trace"][1]["blocks"] == [
        {"query": "facet-one", "ids": ["one", "common-a", "common-b"]},
        {"query": "facet-two", "ids": ["two", "common-a", "common-b"]}]


def test_grounded_core_precedes_block_guarantees_and_repeated_queries_stop(tmp_path, monkeypatch):
    seed(str(tmp_path), ["common", "core", "facet"])
    searches, prompts = [], []

    def retrieve(root, query, **options):
        searches.append(query)
        return [hit("common"), hit("core")] if query == "question" else [hit("facet")]

    def judge(prompt):
        prompts.append(json.loads(prompt.split("\n", 1)[1]))
        return json.dumps({"grade": "PARTIAL", "queries": ["detail", " DETAIL ", "question"],
                           "core_ids": ["core", "core"]}), {}

    monkeypatch.setattr(search_recipes, "search", retrieve)
    result = search_recipes.multi_round(str(tmp_path), "question", complete=judge, limit=2)
    assert searches == ["question", "detail"]
    assert len(prompts) == len(result["trace"]) == 2
    assert [row["id"] for row in result["results"]] == ["core", "common"]
    assert prompts[1]["core_ids"] == ["core"]
    assert result["trace"][-1]["stopped"] == "no-new-queries"


@pytest.mark.parametrize("core", [["fabricated"], "core", [1]])
def test_unavailable_or_malformed_core_is_rejected(tmp_path, monkeypatch, core):
    seed(str(tmp_path), ["known"])
    monkeypatch.setattr(search_recipes, "search", lambda *args, **options: [hit("known")])
    with pytest.raises(ValueError, match="core"):
        search_recipes.multi_round(str(tmp_path), "question",
            complete=lambda prompt: (json.dumps({"grade": "COMPLETE", "queries": [], "core_ids": core}), {}))


def test_a_fact_withdrawn_by_the_judge_cannot_survive_an_earlier_block(tmp_path):
    root = str(tmp_path)
    fact_store.migrate(root)
    fact, _ = hierarchical.add_fact(root, "parser validates incoming grammar")

    def judge(prompt):
        hierarchical.forget_fact(root, fact.id)
        return json.dumps({"grade": "COMPLETE", "queries": [], "core_ids": [fact.id]}), {}

    result = search_recipes.multi_round(root, "parser", complete=judge, embedder="none", reranker="none")
    assert result["results"] == []
    assert result["trace"][0]["core_ids"] == []


def test_temporal_intent_and_scope_are_pinned_across_gap_rounds(tmp_path, monkeypatch):
    root = str(tmp_path)
    fact_store.migrate(root)
    fact, _ = hierarchical.add_fact(root, "parser validates grammar", scopes=["agent:a"],
        valid_from="2024-01-01T00:00:00Z", valid_until="2024-06-01T00:00:00Z")
    observed = []
    original = search_recipes.search

    def retrieve(root, query, **options):
        observed.append((options["as_of"], options["context"]))
        return original(root, query, **options)

    decisions = iter([{"grade": "PARTIAL", "queries": ["grammar"]}, {"grade": "COMPLETE", "queries": []}])
    monkeypatch.setattr(search_recipes, "search", retrieve)
    result = search_recipes.multi_round(root, "parser on 2024-03-01", scope="agent:a", embedder="none",
        reranker="none", complete=lambda prompt: (json.dumps(next(decisions)), {}))
    assert len(observed) == 2 and observed[0] == observed[1]
    assert observed[0][0].startswith("2024-03-01") and observed[0][1] == ["agent:a"]
    assert [row["id"] for row in result["results"]] == [fact.id]
