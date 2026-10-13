"""A retrieval plan cannot mix historical snapshots across its evidence arms."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from commontrace import fact_store, hierarchical, query_plan, search_recipes, temporal_intent

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("query, expected", [
    ("office as of 2025-01-01T18:30:00+05:30", "2025-01-01T13:00:00+00:00"),
    ("office as of 2025-01-01T18:30:00+0530", "2025-01-01T13:00:00+00:00"),
    ("office at 2025-01-01T23:59:59.123Z", "2025-01-01T23:59:59.123000+00:00"),
    ("office on 2025-01-01", "2025-01-01T00:00:00+00:00"),
    ("office as of 2025-01-01 and at 2025-01-01T00:00Z", "2025-01-01T00:00:00+00:00"),
])
def test_complete_iso_instants_preserve_offsets_and_precision(query, expected):
    assert temporal_intent.resolve(query, now=NOW) == {"mode": "historical", "as_of": expected}


@pytest.mark.parametrize("query", ["compare yesterday and tomorrow", "compare last week and yesterday",
                                   "compare today and yesterday", "compare now and tomorrow",
                                   "office as of 2024-01-01 and on 2025-01-01"])
def test_multiple_distinct_instants_never_silently_select_one(query):
    assert temporal_intent.resolve(query, now=NOW) == {"mode": "unspecified", "as_of": None}


def test_naive_reference_is_utc_and_invalid_calendar_date_is_rejected():
    assert temporal_intent.resolve("office as of 2025-01-01", now=NOW.replace(tzinfo=None))["mode"] == "historical"
    with pytest.raises(ValueError):
        temporal_intent.resolve("office as of 2025-02-30", now=NOW)


@pytest.mark.parametrize("suffix", ["T18:30:00+bad", "T18:30:00.1234567Z", "T18:30:00Zjunk"])
def test_unsupported_timestamp_suffix_never_silently_truncates(suffix):
    assert temporal_intent.resolve("office as of 2025-01-01" + suffix, now=NOW)["as_of"] is None


def test_plan_graph_category_and_subqueries_share_original_snapshot(tmp_path, monkeypatch):
    seen = []

    class Backend:
        def neighbors(self, node_id, *, as_of=None):
            seen.append(("graph", as_of))
            return []

    def list_facts(root, **options):
        seen.append(("category", options["as_of"]))
        return []

    def search(root, part, **options):
        seen.append(("search", options["as_of"]))
        assert options["infer_temporal"] is False
        return []

    monkeypatch.setattr(hierarchical, "list_facts", list_facts)
    monkeypatch.setattr(search_recipes, "search", search)
    planned = {"subqueries": ["office location", "office personnel"], "category": "general",
               "hops": 1, "entity_ids": ["office"]}
    query_plan.retrieve(str(tmp_path), "office as of 2025-01-01", backend=Backend(),
                        complete=lambda prompt: json.dumps(planned))
    assert {moment for _, moment in seen} == {"2025-01-01T00:00:00+00:00"}
    assert [kind for kind, _ in seen] == ["graph", "category", "search", "search"]


def test_explicit_caller_snapshot_wins_over_query_and_model_dates(tmp_path, monkeypatch):
    def search(root, part, **options):
        assert options["as_of"] == "2023-01-01" and options["infer_temporal"] is False
        return []

    monkeypatch.setattr(search_recipes, "search", search)
    query_plan.retrieve(str(tmp_path), "office as of 2025-01-01", as_of="2023-01-01",
                        complete=lambda _: json.dumps({"subqueries": ["office as of 2020-01-01"],
                                                        "category": "", "hops": 0, "entity_ids": []}))


def test_direct_search_caller_snapshot_bypasses_invalid_textual_hint(tmp_path, monkeypatch):
    seen = []

    def list_facts(root, **options):
        seen.append(options["as_of"])
        return []

    monkeypatch.setattr(hierarchical, "list_facts", list_facts)
    assert search_recipes.search(str(tmp_path), "office as of 2025-02-30", as_of="2023-01-01", embedder="none") == []
    assert seen == ["2023-01-01T00:00:00+00:00"]


def test_model_followup_date_cannot_turn_current_recall_into_historical_recall(tmp_path, monkeypatch):
    seen = []

    def search(root, part, **options):
        seen.append((part, options["as_of"], options["infer_temporal"]))
        return []

    monkeypatch.setattr(search_recipes, "search", search)
    decisions = iter([{"grade": "PARTIAL", "queries": ["office as of 2020-01-01"]},
                      {"grade": "COMPLETE", "queries": []}])
    search_recipes.multi_round(str(tmp_path), "office location",
                               complete=lambda _: (json.dumps(next(decisions)), {}))
    assert seen == [("office location", None, False), ("office as of 2020-01-01", None, False)]


@pytest.mark.parametrize("indexed", [False, True])
def test_decomposed_historical_retrieval_returns_old_category_fact_in_both_stores(tmp_path, indexed):
    root = str(tmp_path)
    old = hierarchical.add_fact(root, "Office is located in Paris", category="general",
                                valid_from="2024-01-01", valid_until="2025-01-01")[0]
    new = hierarchical.add_fact(root, "Office is located in Tokyo", category="general", valid_from="2025-01-01")[0]
    if indexed:
        fact_store.migrate(root)
    rows = query_plan.retrieve(root, "Office as of 2024-06-01", embedder="none", reranker="none",
                               complete=lambda _: json.dumps({"subqueries": ["Office location"],
                                                               "category": "general", "hops": 0, "entity_ids": []}))
    assert [row["id"] for row in rows] == [old.id]
    assert new.id not in {row["id"] for row in rows}
