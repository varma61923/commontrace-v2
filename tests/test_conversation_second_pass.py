"""Coverage-driven second retrieval pass: off by default, deterministic, bounded by the same budget."""
from __future__ import annotations

import pytest

from commontrace.conversation import Options, Store
from commontrace.conversation import recall as conversation_recall
from commontrace.conversation.search import SECOND_PASS_QUERIES, facet_queries

QUESTION = "Which tomatoes and peppers are on the trellis?"


@pytest.fixture
def store(tmp_path):
    # Two turns score strongly on "trellis tomatoes"; "peppers" lives only in weaker
    # turns of other sessions that a two-unit pool leaves out of the first pass.
    s = Store(str(tmp_path), "chat")
    for n in range(2):
        s.add(f"g{n}", [{"speaker": "Ana", "text": f"Trellis tomatoes, trellis tomatoes: trellis {n} of tomatoes."}],
              session_at=f"2024-05-{n + 1:02d}")
    for n in range(4):
        s.add(f"p{n}", [{"speaker": "Ana", "text": f"Long chatter {n} about the weekend, the weather, the traffic, "
                                                  "the neighbours, the news and also some peppers."}],
              session_at=f"2024-06-{n + 10:02d}")
    yield s
    s.close()


def opts(**kw):
    return Options(embedder=None, rerank=None, pool=2, budget=400, **kw)


def test_off_by_default_changes_nothing(store):
    assert Options().second_pass is False
    first = conversation_recall(store, QUESTION, options=opts())
    assert "second_pass" not in first.explain
    assert first.explain["coverage"]["missing_subject_terms"] == ["peppers"]


def test_second_pass_adds_turns_for_missing_subject_terms(store):
    first = conversation_recall(store, QUESTION, options=opts())
    second = conversation_recall(store, QUESTION, options=opts(second_pass=True))
    report = second.explain["second_pass"]
    assert report["terms"] == ["peppers"] and report["queries"][0] == "peppers"
    assert report["added_turns"] and set(report["added_turns"]).isdisjoint(first.turns)
    assert set(first.turns) <= set(second.turns)  # the first pass's evidence is kept
    assert "peppers" in second.context and second.tokens <= 400
    assert report["tokens_before"] == first.tokens and report["tokens_after"] == second.tokens
    assert report["missing_after"] == [] and second.explain["coverage"]["missing_subject_terms"] == []
    again = conversation_recall(store, QUESTION, options=opts(second_pass=True))
    assert again.turns == second.turns and again.explain["second_pass"] == report  # deterministic


def test_second_pass_skips_when_no_budget_remains(store):
    tight = conversation_recall(store, QUESTION, options=Options(embedder=None, rerank=None, pool=2, budget=20,
                                                                  second_pass=True))
    assert tight.explain["second_pass"]["skipped"] in ("no budget remains",
                                                       "the new turns did not fit the remaining budget")
    assert tight.tokens <= 20


def test_second_pass_skips_when_nothing_is_missing_or_it_abstains(store):
    covered = conversation_recall(store, "trellis tomatoes", options=opts(second_pass=True))
    assert covered.explain["second_pass"] == {"terms": [], "added_turns": [], "skipped": "no subject term is missing"}
    absent = conversation_recall(store, "What is my zebra called?", options=opts(second_pass=True))
    assert absent.explain["second_pass"]["added_turns"] == []
    assert "skipped" in absent.explain["second_pass"]


def test_facet_queries_are_bounded_and_deterministic():
    assert facet_queries("Where does Ana keep her bike?", []) == []
    queries = facet_queries("Where does Ana keep her bike?", ["bike", "helmet"])
    assert queries[:2] == ["bike", "ana bike"] and "helmet" in queries  # entities come back normalized
    assert queries == facet_queries("Where does Ana keep her bike?", ["bike", "helmet"])
    assert len(facet_queries("q", [f"term{n}" for n in range(30)])) == SECOND_PASS_QUERIES
