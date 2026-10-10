"""Contradiction detection on write, supersede mode, and transaction-time (known_at) queries."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from commontrace import fact_conflicts, hierarchical


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("COMMONTRACE_FACT_CONFLICTS", "COMMONTRACE_FACT_CONFLICT_JUDGE", "COMMONTRACE_FACT_DEDUP",
                 "COMMONTRACE_FACT_EMBEDDER", "COMMONTRACE_FACT_EMBEDDER_PATH"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("words", [("like", "likes", "liked", "liking"), ("work", "works", "worked", "working"),
                                   ("study", "studies"), ("use", "uses", "used", "using")])
def test_inflections_share_one_stem(words):
    assert len({fact_conflicts._stem(w) for w in words}) == 1


@pytest.mark.parametrize("older, newer, kind", [
    ("Alice likes spicy food a lot", "Alice does not like spicy food a lot", "negation"),
    ("Bob never used vim at work", "Bob uses vim at work", "negation"),
    ("Bob has 2 cats at home", "Bob has 3 cats at home", "value"),
    ("Bob works at Acme", "Bob worked at Initech", None),
    ("Alice likes spicy food", "Alice likes spicy food", None),
    ("Not here", "Here", None),  # too few content words to name a slot
])
def test_classify(older, newer, kind):
    assert fact_conflicts.classify(older, newer) == kind


def test_a_negation_is_flagged_by_default_and_changes_nothing(tmp_path):
    root = str(tmp_path)
    old, _ = hierarchical.add_fact(root, "Alice likes spicy food a lot")
    new, action = hierarchical.add_fact(root, "Alice does not like spicy food a lot")
    assert action == "ADD"
    [conflict] = fact_conflicts.pending(root)
    assert (conflict.older, conflict.newer, conflict.kind, conflict.action) == (old.id, new.id, "negation", "flagged")
    facts = hierarchical.load_facts(root)
    assert facts[old.id].status == facts[new.id].status == "active"
    assert facts[old.id].retracted_at is None


def test_supersede_mode_closes_the_contradicted_fact(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONTRACE_FACT_CONFLICTS", "supersede")
    root = str(tmp_path)
    old, _ = hierarchical.add_fact(root, "Alice likes spicy food a lot")
    new, _ = hierarchical.add_fact(root, "Alice does not like spicy food a lot")
    stored = hierarchical.load_facts(root)[old.id]
    assert stored.status == "superseded" and stored.superseded_by == new.id
    assert stored.retracted_at and stored.valid_until
    assert fact_conflicts.pending(root) == []  # resolved, so not waiting for review
    assert [f.id for f in hierarchical.list_facts(root)] == [new.id]


def test_value_changes_are_only_flagged_even_in_supersede_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONTRACE_FACT_CONFLICTS", "supersede")
    root = str(tmp_path)
    old, _ = hierarchical.add_fact(root, "Bob has 2 cats at home")
    new, _ = hierarchical.add_fact(root, "Bob has 3 cats at home")
    [conflict] = fact_conflicts.pending(root)
    assert (conflict.kind, conflict.action) == ("value", "flagged")
    assert hierarchical.load_facts(root)[old.id].status == "active" and new.id == conflict.newer


def test_numbered_templates_are_identifiers_not_contradictions(tmp_path):
    root = str(tmp_path)
    hierarchical.add_facts(root, [{"statement": f"Customer {i} prefers invoices by email"} for i in range(20)])
    hierarchical.append_facts(root, [{"statement": f"Order {i} ships from depot {i % 3}"} for i in range(20)])
    hierarchical.add_fact(root, "Customer 21 prefers invoices by email")
    assert fact_conflicts.pending(root) == []


def test_a_batch_compares_each_fact_only_with_earlier_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONTRACE_FACT_CONFLICTS", "supersede")
    root = str(tmp_path)
    [(old, _), (new, _)] = hierarchical.add_facts(root, [{"statement": "Alice likes spicy food a lot"},
                                                         {"statement": "Alice does not like spicy food a lot"}])
    facts = hierarchical.load_facts(root)
    assert facts[old.id].status == "superseded" and facts[new.id].status == "active"
    assert facts[old.id].superseded_by == new.id


def test_off_mode_and_scope_isolation(tmp_path, monkeypatch):
    root = str(tmp_path)
    hierarchical.add_fact(root, "Alice likes spicy food a lot", scopes=["tenant-a"])
    hierarchical.add_fact(root, "Alice does not like spicy food a lot", scopes=["tenant-b"])
    assert fact_conflicts.pending(root) == []
    monkeypatch.setenv("COMMONTRACE_FACT_CONFLICTS", "off")
    hierarchical.add_fact(root, "Carol enjoys long morning runs")
    hierarchical.add_fact(root, "Carol does not enjoy long morning runs")
    assert fact_conflicts.pending(root) == []
    monkeypatch.setenv("COMMONTRACE_FACT_CONFLICTS", "always")
    with pytest.raises(ValueError, match="COMMONTRACE_FACT_CONFLICTS"):
        hierarchical.add_fact(root, "Dave reads every evening")


def test_append_only_admission_flags_but_never_supersedes(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONTRACE_FACT_CONFLICTS", "supersede")
    root = str(tmp_path)
    [(old, _)] = hierarchical.append_facts(root, [{"statement": "Alice likes spicy food a lot"}])
    [(new, _)] = hierarchical.append_facts(root, [{"statement": "Alice does not like spicy food a lot"}])
    [conflict] = fact_conflicts.pending(root)
    assert (conflict.older, conflict.newer, conflict.action) == (old.id, new.id, "flagged")
    assert hierarchical.load_facts(root)[old.id].status == "active"


def test_the_llm_judge_sees_candidates_and_only_offered_ids_count(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONTRACE_FACT_CONFLICT_JUDGE", "llm")
    monkeypatch.setenv("COMMONTRACE_FACT_CONFLICTS", "supersede")
    root = str(tmp_path)
    old, _ = hierarchical.add_fact(root, "Bob has 2 cats at home")
    seen, judge = [], fact_conflicts.judge

    def fake(newer, candidates, complete=None):
        seen.append((newer, [c.id for c in candidates]))
        return {c.id for c in candidates}

    monkeypatch.setattr(fact_conflicts, "judge", fake)
    new, _ = hierarchical.add_fact(root, "Bob has 3 cats at home")
    assert seen == [("Bob has 3 cats at home", [old.id])]
    stored = hierarchical.load_facts(root)[old.id]
    assert stored.status == "superseded" and stored.superseded_by == new.id
    # The judge itself refuses ids it was not offered and survives a broken reply.
    facts = [stored]
    assert judge("x", facts, complete=lambda p: '{"replaces": ["%s", "f-other"]}' % old.id) == {old.id}
    assert judge("x", facts, complete=lambda p: "not json") == set()
    assert judge("x", [], complete=lambda p: "{}") == set()


def test_known_at_reconstructs_what_the_store_believed(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONTRACE_FACT_CONFLICTS", "supersede")
    root = str(tmp_path)
    old, _ = hierarchical.add_fact(root, "Alice likes spicy food a lot")
    time.sleep(0.02)
    before = datetime.now(timezone.utc).isoformat()
    time.sleep(0.02)
    new, _ = hierarchical.add_fact(root, "Alice does not like spicy food a lot")
    then = [f.id for f in hierarchical.list_facts(root, known_at=before)]
    now = [f.id for f in hierarchical.list_facts(root, known_at=datetime.now(timezone.utc).isoformat())]
    assert then == [old.id] and now == [new.id]
    hits = hierarchical.search_facts(root, "spicy food", known_at=before)
    assert [f.id for f, _s in hits] == [old.id]
    assert [f.id for f, _s in hierarchical.search_facts(root, "spicy food")] == [new.id]


def test_known_at_falls_back_for_rows_written_before_retracted_at(tmp_path):
    fact = hierarchical._coerce_fact({"id": "f1", "statement": "x y z", "status": "deleted",
                                      "valid_from": "2026-01-01T00:00:00+00:00",
                                      "valid_until": "2026-02-01T00:00:00+00:00",
                                      "created_at": "2026-01-01T00:00:00+00:00",
                                      "updated_at": "2026-02-01T00:00:00+00:00"})
    assert fact.retracted_at is None
    jan = datetime(2026, 1, 15, tzinfo=timezone.utc)
    mar = datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert hierarchical._known_at(fact, jan) and not hierarchical._known_at(fact, mar)
