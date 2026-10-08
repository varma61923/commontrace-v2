"""Original bounded facet fixtures and exact-source, finite-budget contracts."""
from __future__ import annotations

from dataclasses import replace

import pytest

from commontrace.conversation import Options, Store, recall
from commontrace.conversation.context_selection import (
    MAX_CANDIDATES,
    MAX_FACETS,
    MAX_PASSAGE_CHARS,
    MAX_QUESTION_CHARS,
    Candidate,
    prioritize,
)
from commontrace.conversation.search import _excerpt


def candidate(identity: int, body: str, cost: int = 20) -> Candidate:
    return Candidate(identity, (body,), cost)


def test_distinct_facets_outrank_redundant_high_ranked_hits_with_same_anchor():
    rows = [candidate(1, "Zircon timeout is seven seconds", 50),
            candidate(2, "Zircon timeout remains seven seconds", 40),
            candidate(3, "Zircon timeout dashboard has seven seconds", 35),
            candidate(4, "Zircon retention is thirty days", 20),
            candidate(5, "Zircon recovery requires a verified snapshot", 25)]
    plan = prioritize("What are Zircon timeout retention and recovery settings?", rows)
    assert plan.priority == (1, 4, 5)
    assert plan.order == (1, 4, 5, 2, 3)
    assert {"timeout", "retention", "recovery"} <= set(plan.covered_facets)
    assert "settings" in plan.missing_facets
    # Same token allowance: diversity includes three independently sourced
    # facets; default rank spends it on repeated evidence about one facet.
    def emitted(order):
        spent, facets = 0, set()
        indexed = {row.turn: row for row in rows}
        for identity in order:
            row = indexed[identity]
            if spent + row.token_cost <= 100:
                spent += row.token_cost
                facets.update(word for word in ("timeout", "retention", "recovery")
                              if any(word in text for text in row.passages))
        return spent, facets
    assert emitted(plan.order) == (95, {"timeout", "retention", "recovery"})
    assert emitted(tuple(row.turn for row in rows))[1] == {"timeout"}


def test_recency_or_reranking_anchor_cannot_be_replaced_by_a_shorter_old_claim():
    rows = [candidate(5, "The timeout is now twelve seconds instead of six", 40),
            candidate(1, "The timeout used to be six seconds", 5),
            candidate(3, "Retention lasts thirty days", 10)]
    plan = prioritize("What are the timeout and retention now?", rows)
    assert plan.order[0] == plan.priority[0] == 5
    assert set(plan.order) == {1, 3, 5}


def test_existing_top_hit_quota_survives_before_any_facet_diversification():
    rows = [candidate(11, "timeout is now twelve seconds", 100),
            candidate(7, "timeout is NOT six seconds anymore", 100),
            candidate(3, "timeout correction was confirmed after the deployment", 100),
            candidate(6, "timeout older dashboard", 20),
            candidate(5, "retention thirty days", 10), candidate(9, "recovery snapshot", 20)]
    plan = prioritize("timeout retention recovery", rows, anchor_count=3)
    assert plan.order[:3] == plan.priority[:3] == (11, 7, 3)
    assert plan.order == (11, 7, 3, 5, 9, 6)
    assert plan.quoted_tokens == 330
    assert {"timeout", "retention", "recovery"} == set(plan.covered_facets)


def test_anchor_count_larger_than_available_candidates_keeps_all_eligible_anchors():
    rows = [candidate(7, "timeout"), candidate(5, "retention")]
    plan = prioritize("timeout retention", rows, anchor_count=MAX_CANDIDATES)
    assert plan.order == plan.priority == (7, 5)
    assert plan.quoted_tokens == 40


def test_all_bounded_candidates_can_be_protected_without_reordering_the_tail():
    rows = [candidate(i + 1, f"timeout facet{i}") for i in range(MAX_CANDIDATES)]
    plan = prioritize("timeout facet199", rows, anchor_count=MAX_CANDIDATES)
    assert plan.order == plan.priority == tuple(range(1, MAX_CANDIDATES + 1))


def test_explicit_zero_anchor_quota_does_not_force_an_unrelated_first_candidate():
    rows = [candidate(1, "unrelated gardening", 100), candidate(3, "retention", 10),
            candidate(2, "timeout", 10)]
    plan = prioritize("timeout retention", rows, anchor_count=0)
    assert plan.priority == (3, 2)
    assert plan.order == (3, 2, 1)


def test_empty_query_and_empty_candidates_preserve_safe_anchor_boundaries():
    assert prioritize("what is the and", [candidate(7, "timeout")], anchor_count=3).order == (7,)
    assert prioritize("what is the and", [candidate(7, "timeout")], anchor_count=3).priority == ()
    assert prioritize("timeout", [], anchor_count=3).order == ()


@pytest.mark.parametrize("count", [-1, MAX_CANDIDATES + 1, True, False, 1.5, float("nan"), None])
def test_anchor_quota_is_typed_and_bounded_even_for_empty_inputs(count):
    with pytest.raises(ValueError, match="anchor_count"):
        prioritize("timeout", [], anchor_count=count)


def test_equal_utility_preserves_original_rank_without_float_tie_noise():
    plan = prioritize("timeout retention recovery", [candidate(8, "timeout", 10),
                      candidate(2, "retention", 10), candidate(9, "recovery", 10)])
    assert plan.order == (8, 2, 9)
    assert prioritize("timeout retention", [candidate(8, "timeout"), candidate(9, "retention"),
                                            candidate(2, "retention")]).order == (8, 9, 2)


def test_utility_includes_all_required_graph_ancestor_costs():
    costly = Candidate(2, ("Retention is thirty days", "This is the parent connecting passage",
                           "This is another required ancestor"), 100)
    cheap = candidate(3, "Recovery requires a verified snapshot", 20)
    plan = prioritize("timeout retention recovery", [candidate(1, "timeout"), costly, cheap])
    assert plan.priority == (1, 3, 2)
    assert plan.quoted_tokens == 140
    assert costly.passages[1:] == ("This is the parent connecting passage", "This is another required ancestor")


def test_atomic_ancestor_passages_can_supply_a_distinct_lexical_facet():
    chain = Candidate(2, ("The recovery owner is Mira", "Mira manages snapshot retention"), 35)
    plan = prioritize("timeout retention recovery", [candidate(1, "timeout"), chain])
    assert {"retention", "recovery"} <= set(plan.covered_facets)
    assert plan.order == (1, 2)


def test_complete_tail_is_kept_even_when_only_a_few_candidates_add_information():
    rows = [candidate(i, "timeout") for i in range(1, MAX_CANDIDATES)]
    rows.append(candidate(MAX_CANDIDATES, "retention"))
    plan = prioritize("timeout retention", rows)
    assert plan.priority == (1, MAX_CANDIDATES)
    assert plan.order[2:] == tuple(range(2, MAX_CANDIDATES))
    assert len(plan.order) == len(set(plan.order)) == MAX_CANDIDATES


def test_missing_attribute_is_reported_without_inventing_source_text():
    row = candidate(1, "Nia bought a bicycle in Porto")
    plan = prioritize("What is Nia's bicycle serial identifier?", [row])
    assert {"serial", "identifier"} <= set(plan.missing_facets)
    assert not {"serial", "identifier"}.intersection(plan.covered_facets)
    assert "unverified" not in row.passages[0]
    assert plan.as_dict()["meaning"].startswith("candidate excerpt lexical diversity")


def test_actual_excerpt_cannot_credit_hidden_full_turn_identifier(tmp_path):
    with Store(str(tmp_path), "excerpt-selection") as store:
        text = "Nia bought a bicycle in Porto. " * 30 + "The bicycle serial identifier is CT-492."
        store.add("purchase", [{"speaker": "Nia", "text": text}], session_at="2025-01-01",
                  extract_profile=False)
        turn = next(iter(store.turns([1]).values()))
        quote = _excerpt(turn, "bicycle", 20).removeprefix(turn.speaker + ": ")
        assert "serial" not in quote and "CT-492" not in quote
        plan = prioritize("What is the bicycle serial identifier?", [candidate(turn.id, quote)])
        assert {"serial", "identifier"} <= set(plan.missing_facets)
        assert "CT-492" in turn.text


def test_negation_correction_and_history_quotes_are_preserved_verbatim():
    rows = [candidate(7, "Retention is NOT thirty days now; it is seven days."),
            candidate(2, "Retention used to be thirty days."),
            candidate(8, "Recovery must NOT use the previous snapshot.")]
    original = tuple(row.passages for row in rows)
    plan = prioritize("What are retention and recovery now?", rows)
    assert plan.order == (7, 8, 2)
    assert tuple(row.passages for row in rows) == original


def test_unicode_and_short_scalar_facets_survive_lexical_selection():
    rows = [candidate(1, "超时保护 port x"), candidate(2, "重试策略 rack 7"),
            candidate(3, "règle du café")]
    plan = prioritize("超时 重试 port x rack 7 café règle", rows)
    assert {"x", "7", "café", "règle"} <= set(plan.covered_facets)
    assert not plan.missing_facets
    assert plan.priority == (1, 2, 3)


def test_empty_or_stopword_queries_keep_original_rank_without_fake_coverage():
    rows = [candidate(2, "timeout"), candidate(1, "retention")]
    for question in ("", "what is the and"):
        plan = prioritize(question, rows)
        assert plan.order == (2, 1)
        assert plan.priority == ()
        assert plan.covered_facets == ()
        assert plan.quoted_tokens == 0
    empty = prioritize("timeout retention", [])
    assert empty.order == empty.priority == ()
    assert set(empty.missing_facets) == {"timeout", "retention"}


def test_query_facet_bound_is_explicit_and_deterministic():
    question = " ".join(f"facet{i:04}" for i in range(MAX_FACETS + 9))
    plan = prioritize(question, [candidate(1, "facet0000")])
    assert len(plan.facets) == MAX_FACETS
    assert plan.omitted_facets == 9
    assert plan == prioritize(question, [candidate(1, "facet0000")])


@pytest.mark.parametrize("identity", [0, -1, True, 1.5, "1"])
def test_candidate_id_must_be_an_unambiguous_positive_turn(identity):
    with pytest.raises(ValueError, match="turn"):
        candidate(identity, "timeout")


@pytest.mark.parametrize("cost", [0, -1, True, 1.5, 1_000_001, float("inf"), float("nan")])
def test_cost_is_bounded_typed_and_finite(cost):
    with pytest.raises(ValueError, match="token cost"):
        candidate(1, "timeout", cost)


@pytest.mark.parametrize("passages", [(), ("one", "two", "three", "four"), ["one"],
                                       (None,), ("one", 2)])
def test_passages_bound_atomic_support_and_reject_nontext(passages):
    with pytest.raises(ValueError, match="passages"):
        Candidate(1, passages, 20)


def test_total_passage_size_is_bounded_before_lexical_work():
    with pytest.raises(ValueError, match="character limit"):
        Candidate(1, ("x" * MAX_PASSAGE_CHARS, "x"), 20)


def test_planner_validates_candidates_and_duplicate_source_identities():
    with pytest.raises(ValueError, match="unique"):
        prioritize("timeout", [candidate(1, "timeout"), candidate(1, "retention")])
    for rows in ([candidate(i + 1, "timeout") for i in range(MAX_CANDIDATES + 1)], "timeout", [None]):
        with pytest.raises(ValueError, match="sequence"):
            prioritize("timeout", rows)
    with pytest.raises(ValueError, match="bounded string"):
        prioritize("x" * (MAX_QUESTION_CHARS + 1), [])
    with pytest.raises(ValueError, match="bounded string"):
        prioritize(None, [])


def test_public_diagnostics_do_not_contain_unquoted_source_bodies():
    row = candidate(1, "Original source body containing timeout evidence")
    plan = prioritize("timeout retention", [row])
    metadata = plan.as_dict()
    assert metadata["strategy"] == "coverage-v1"
    assert metadata["order"] == [1]
    assert metadata["priority"] == [1]
    assert "Original source body" not in str(metadata)
    assert prioritize("timeout retention", [replace(row, token_cost=40)]).quoted_tokens == 40


def _persist_facets(root):
    with Store(root, "facet-quality") as store:
        store.add("timeouts", [{"speaker": "Nia", "text":
                   f"Zircon timeout is seven seconds in region {i}. "
                   + "The connector is checked by operators and monitored during deployments. " * 2}
                   for i in range(3)], session_at="2025-01-05", extract_profile=False)
        store.add("retention", [{"speaker": "Nia", "text": "Zircon retention lasts thirty days."}],
                  session_at="2025-01-02", extract_profile=False)
        store.add("recovery", [{"speaker": "Nia", "text": "Zircon recovery requires a verified snapshot."}],
                  session_at="2025-01-01", extract_profile=False)


@pytest.mark.parametrize("budget", [120, 150])
def test_reopened_real_recall_preserves_more_facets_at_the_same_smaller_budget(tmp_path, budget):
    root = str(tmp_path)
    _persist_facets(root)
    question = "What are Zircon timeout retention and recovery now?"
    options = Options(budget=budget, primary_hits=1, embedder=None, rerank=None, graph_hops=0,
                      instructions=0, profile_facts=0, summaries=False)
    with Store(root, "facet-quality", create=False, read_only=True) as store:
        legacy = recall(store, question, options=options)
        selected = recall(store, question, options=replace(options, context_strategy="coverage-v1"))
        assert selected.ranked == legacy.ranked  # identical retrieval inputs; only context packing differs
        assert legacy.turns[0] == selected.turns[0] == selected.ranked[0]
        assert "Zircon retention lasts thirty days." in selected.context
        assert "Zircon recovery requires a verified snapshot." in selected.context
        assert "timeout is seven seconds" in selected.context
        assert "recovery" not in legacy.context
        assert selected.tokens < legacy.tokens <= budget
        assert not {"retention", "recovery"}.intersection(selected.explain["coverage"]["missing_subject_terms"])
        assert "recovery" in legacy.explain["coverage"]["missing_subject_terms"]
        # one of the three equivalent timeout turns, then the retention and recovery turns
        priority = selected.explain["context_selection"]["priority"]
        assert priority[0] in {1, 2, 3} and priority[1:] == [4, 5]
        sources = store.turns(selected.turns)
        assert {turn.session for turn in sources.values()} == {"timeouts", "retention", "recovery"}
        assert all(turn.text in selected.context for turn in sources.values())
        assert legacy.explain["context_selection"]["strategy"] == "legacy"


def test_unaffordable_candidate_coverage_cannot_be_claimed_as_emitted_evidence(tmp_path):
    root = str(tmp_path)
    _persist_facets(root)
    with Store(root, "facet-quality", create=False) as store:
        result = recall(store, "What are Zircon timeout retention and recovery now?", options=Options(
            budget=100, primary_hits=1, embedder=None, rerank=None, graph_hops=0,
            instructions=0, profile_facts=0, summaries=False, context_strategy="coverage-v1"))
        assert result.tokens <= 100
        assert "recovery" not in result.context
        assert "recovery" in result.explain["context_selection"]["covered_facets"]
        assert "recovery" in result.explain["coverage"]["missing_subject_terms"]


def test_scoped_real_selection_cannot_admit_filtered_or_future_facet_sources(tmp_path):
    root = str(tmp_path)
    _persist_facets(root)
    with Store(root, "facet-quality", create=False) as store:
        store.add("future", [{"speaker": "Nia", "text": "Zircon future recovery uses an unrelated archive."}],
                  session_at="2026-01-01", extract_profile=False)
        result = recall(store, "What are Zircon timeout retention and recovery now?", now="2025-01-05",
                        options=Options(budget=180, embedder=None, rerank=None, graph_hops=0,
                                        instructions=0, profile_facts=0, summaries=False,
                                        sessions=("timeouts", "retention"), context_strategy="coverage-v1"))
        assert "recovery" not in result.context
        assert "recovery" in result.explain["coverage"]["missing_subject_terms"]
        assert all(turn.session in {"timeouts", "retention"} for turn in store.turns(result.turns).values())
        assert set(result.explain["context_selection"]["order"]) <= {1, 2, 3, 4}
