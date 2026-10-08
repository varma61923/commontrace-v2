"""Falsifiable original retrieval metrics and actual durable governance workflows."""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from benchmarks.fact_retrieval import (
    SCORERS,
    Case,
    Fact,
    Mutation,
    Outcome,
    RankingMetrics,
    execute_case,
    fixtures,
    inspect_result,
    main,
    ranking_metrics,
    summarize,
    validate_case,
)
from commontrace import hierarchical


def populate(root: str, case: Case) -> dict[str, str]:
    keys = {}
    for row in case.facts:
        fact, _ = hierarchical.add_fact(root, row.statement, confidence=row.confidence,
                                        scopes=list(row.scopes), category=row.category, stability=row.stability,
                                        valid_from=row.valid_from, valid_until=row.valid_until, expires_at=row.expires_at)
        keys[row.key] = fact.id
    return keys


def test_grades_affect_ndcg_but_not_binary_recall_or_reciprocal_rank():
    observed = ranking_metrics(["irrelevant", "weak", "strong"], {"strong": 3, "weak": 1}, k=3)
    dcg = 1 / math.log2(3) + 7 / math.log2(4)
    ideal = 7 + 1 / math.log2(3)
    assert observed.recall_at_k == 1
    assert observed.mrr == 0.5
    assert observed.ndcg == pytest.approx(dcg / ideal)
    perfect = ranking_metrics(["strong", "weak"], {"strong": 3, "weak": 1}, k=3)
    assert perfect == RankingMetrics(1, 1, 1)


def test_recall_denominator_includes_relevant_documents_beyond_k():
    observed = ranking_metrics(["first", "second", "third"], {"first": 3, "second": 2, "third": 1}, k=1)
    assert observed.recall_at_k == pytest.approx(1 / 3)
    assert observed.mrr == observed.ndcg == 1


def test_absent_gold_is_undefined_instead_of_a_fabricated_perfect_rank():
    assert ranking_metrics([], {}, k=3) == RankingMetrics(None, None, None)
    assert ranking_metrics(["irrelevant"], {}, k=3) == RankingMetrics(None, None, None)
    assert ranking_metrics([], {"gold": 3}, k=3) == RankingMetrics(0, 0, 0)


@pytest.mark.parametrize("grades", [{"gold": 0}, {"gold": 4}, {"gold": True}, {"gold": 1.5},
                                    {"gold": float("nan")}, {"": 3}])
def test_metric_grades_are_bounded_typed_input(grades):
    with pytest.raises(ValueError):
        ranking_metrics(["gold"], grades, k=3)


@pytest.mark.parametrize("k", [0, -1, 1001, True, 1.5])
def test_ranking_cutoff_rejects_unbounded_or_ambiguous_values(k):
    with pytest.raises(ValueError):
        ranking_metrics([], {}, k=k)


def test_duplicate_ranked_ids_cannot_inflate_recall_even_after_cutoff():
    with pytest.raises(ValueError):
        ranking_metrics(["gold", "other", "gold"], {"gold": 3}, k=1)
    with pytest.raises(ValueError):
        ranking_metrics(["x"] * 1001, {"gold": 3}, k=3)
    with pytest.raises(ValueError):
        ranking_metrics("gold", {"gold": 3}, k=3)


@pytest.mark.parametrize("values", [(math.nan, 1, 1), (1, math.inf, 1), (-1, 0, 0), (1.1, 1, 1),
                                     (None, 1, 1), (True, 1, 1)])
def test_metric_objects_cannot_carry_nonfinite_or_inconsistent_values(values):
    with pytest.raises(ValueError):
        RankingMetrics(*values)


@pytest.mark.parametrize("case", fixtures(), ids=lambda case: case.name)
def test_persisted_production_retrieval_pairs_preserve_governance(case):
    outcomes = execute_case(case)
    assert len(outcomes) == 2
    assert outcomes[0].eligible_corpus_sha256 == outcomes[1].eligible_corpus_sha256
    assert len(outcomes[0].eligible_corpus_sha256) == 64
    for outcome in outcomes:
        assert outcome.error is None, outcome
        assert outcome.contracts_passed, outcome
        assert set(outcome.returned).isdisjoint(case.forbidden)
        for value in outcome.metrics.to_dict().values():
            assert value is None or math.isfinite(value) and 0 <= value <= 1


def test_fixture_set_includes_benefits_and_downside_stressors_before_comparison():
    cases = fixtures()
    assert len(cases) == 23
    names = {case.name for case in cases}
    assert {"english-inflection", "cjk-retrieval", "stemming-collision", "stopword-technical-term",
            "numeric-short-term", "short-identifier", "long-identifier", "long-source"} <= names
    assert {case.ability for case in cases} >= {"erasure", "scope", "stability", "knowledge-update", "abstention"}


@pytest.mark.parametrize("name", ["tenant-isolation", "stable-tier", "category-isolation"])
def test_actual_wrong_view_results_fail_even_if_all_relevant_facts_are_present(tmp_path, name):
    case = next(case for case in fixtures() if case.name == name)
    root = str(tmp_path)
    keys = populate(root, case)
    # Perform a real privileged/unfiltered read, then evaluate it as the
    # intended restricted caller; this is not a mocked search implementation.
    results = hierarchical.search_facts(root, case.query, as_of=case.as_of, limit=10, scorer="bm25-v1")
    outcome = inspect_result(root, case, keys, results, scorer="bm25-v1", k=10)
    assert outcome.metrics.recall_at_k == 1
    assert not outcome.contracts_passed
    assert "ineligible result" in outcome.violations
    assert "forbidden or erased fact returned" in outcome.violations


@pytest.mark.parametrize("operation", ["forget", "delete"])
def test_real_erasure_invalidates_previously_read_fact_representations(tmp_path, operation):
    case = Case("erasure", "erasure", "Orion rollback", (Fact("erased", "Orion rollback key is ERASED"),),
                (), forbidden=("erased",), as_of=None)
    root = str(tmp_path)
    keys = populate(root, case)
    previous = hierarchical.search_facts(root, case.query, scorer="overlap-v1")
    if operation == "forget":
        hierarchical.forget_fact(root, keys["erased"])
    else:
        hierarchical.delete_fact(root, keys["erased"])
    outcome = inspect_result(root, case, keys, previous, scorer="overlap-v1", k=3)
    assert not outcome.contracts_passed
    assert "ineligible result" in outcome.violations
    assert "unknown or changed source attribution" in outcome.violations


def test_relevant_fact_under_old_body_cannot_pass_source_attribution(tmp_path):
    case = Case("correction", "update", "Orion limit", (Fact("current", "Orion limit is 16"),), (("current", 3),))
    root = str(tmp_path)
    keys = populate(root, case)
    previous = hierarchical.search_facts(root, case.query, scorer="overlap-v1")
    hierarchical.update_fact(root, keys["current"], statement="Orion limit is 42")
    outcome = inspect_result(root, case, keys, previous, scorer="overlap-v1", k=3)
    assert outcome.metrics.recall_at_k == 1
    assert not outcome.contracts_passed
    assert "unknown or changed source attribution" in outcome.violations


def test_missing_durable_fixture_cannot_masquerade_as_safe_zero_recall(tmp_path):
    case = Case("missing", "storage", "Orion limit", (Fact("current", "Orion limit is 16"),), (("current", 3),))
    root = str(tmp_path)
    keys = populate(root, case)
    Path(hierarchical._facts_file(root)).unlink()
    outcome = inspect_result(root, case, keys, [], scorer="overlap-v1", k=3)
    assert outcome.metrics.recall_at_k == 0
    assert not outcome.contracts_passed
    assert "durable fixture facts are missing or unreadable" in outcome.violations


def test_private_fact_body_copied_under_an_eligible_id_still_fails_leakage(tmp_path):
    case = Case("copy", "scope", "Orion key", (
        Fact("private", "Orion key is PRIVATE_FINANCE_SECRET", scopes=("finance",)),
        Fact("copied", "Orion key is PRIVATE_FINANCE_SECRET"),
    ), (), forbidden=("private",), scope="payments")
    root = str(tmp_path)
    keys = populate(root, case)
    results = hierarchical.search_facts(root, case.query, scope="payments", scorer="bm25-v1")
    outcome = inspect_result(root, case, keys, results, scorer="bm25-v1", k=3)
    assert "ineligible result" not in outcome.violations
    assert not outcome.contracts_passed
    assert "forbidden fact content returned" in outcome.violations


def test_irrelevant_actual_data_does_not_pass_an_abstention_contract(tmp_path):
    case = next(case for case in fixtures() if case.name == "unknown-query")
    root = str(tmp_path)
    keys = populate(root, case)
    previous = hierarchical.search_facts(root, "Orion", scorer="overlap-v1")
    outcome = inspect_result(root, case, keys, previous, scorer="overlap-v1", k=3)
    assert outcome.metrics.recall_at_k is None
    assert not outcome.contracts_passed
    assert "unknown-query abstention failed" in outcome.violations


def test_duplicate_real_results_are_rejected_as_an_invalid_ranking(tmp_path):
    case = Case("duplicate", "ranking", "Orion limit", (Fact("current", "Orion limit is 16"),), (("current", 3),))
    root = str(tmp_path)
    keys = populate(root, case)
    result = hierarchical.search_facts(root, case.query, scorer="overlap-v1")
    outcome = inspect_result(root, case, keys, [*result, *result], scorer="overlap-v1", k=3)
    assert not outcome.contracts_passed
    assert outcome.metrics.recall_at_k == 0
    assert "ranked identities must be unique" in outcome.violations


def test_failed_real_evidence_admission_is_kept_as_an_error_in_both_scorers():
    case = Case("unsafe-setup", "scope", "Orion timeout", (
        Fact("private", "Private timeout measurements", scopes=("finance",)),
        Fact("global", "Orion timeout is 5 seconds", evidence=("private",)),
    ), (("global", 3),))
    outcomes = execute_case(case)
    assert len(outcomes) == 2
    for outcome in outcomes:
        assert not outcome.contracts_passed
        assert outcome.error is not None and "setup failed" in outcome.error
        assert outcome.metrics == RankingMetrics(0, 0, 0)
    summary = summarize(outcomes)["scorers"]
    assert summary["bm25-v1"]["errors"] == 1
    assert summary["bm25-v1"]["mean_metrics"]["mrr"] == 0


def test_summary_displays_losses_and_keeps_failures_in_metric_denominator():
    baseline = Outcome("loss", "ranking", "overlap-v1", 3, (), (), RankingMetrics(1, 1, 1), (), "same")
    candidate = replace(baseline, scorer="bm25-v1", metrics=RankingMetrics(1, 0.5, 0.6))
    failed = replace(candidate, case="failed", metrics=RankingMetrics(0, 0, 0), error="actual query failed")
    summary = summarize([baseline, candidate, failed])
    assert summary["bm25_ndcg_losses"] == 1
    assert summary["scorers"]["bm25-v1"]["mean_metrics"]["mrr"] == 0.25
    assert summary["scorers"]["bm25-v1"]["contract_failures"] == 1


def test_failed_real_contradiction_mutation_is_retained_as_an_error():
    case = Case("unsafe-resolution", "scope", "Orion timeout", (
        Fact("old", "Orion timeout is 5 seconds", scopes=("payments",)),
        Fact("new", "Orion timeout is 9 seconds", scopes=("finance",), valid_from="2026-01-01T00:00:00Z"),
    ), (("new", 3),), mutations=(Mutation("resolve", "old", "new"),))
    outcomes = execute_case(case)
    assert all(outcome.error is not None and "mutation failed" in outcome.error for outcome in outcomes)
    assert all(not outcome.contracts_passed and outcome.metrics == RankingMetrics(0, 0, 0) for outcome in outcomes)


@pytest.mark.parametrize("case", [
    Case("duplicates", "input", "Orion", (Fact("x", "Orion one"), Fact("x", "Orion two")), ()),
    Case("forward", "input", "Orion", (Fact("x", "Orion one", evidence=("later",)),), ()),
    Case("unknown-gold", "input", "Orion", (Fact("x", "Orion one"),), (("missing", 3),)),
    Case("conflict", "input", "Orion", (Fact("x", "Orion one"),), (("x", 3),), forbidden=("x",)),
    Case("unknown-mutation", "input", "Orion", (Fact("x", "Orion one"),), (),
         mutations=(Mutation("delete", "missing"),)),
])
def test_malformed_fixture_labels_are_rejected_before_ingestion(case):
    with pytest.raises(ValueError):
        validate_case(case)


def test_actual_benchmark_cli_is_stdout_only_and_all_original_cases_are_reported(tmp_path):
    repository = str(Path(__file__).resolve().parents[1])
    environment = {**os.environ, "PYTHONPATH": repository, "TMPDIR": str(tmp_path)}
    completed = subprocess.run([sys.executable, "-m", "benchmarks.fact_retrieval"],
                               capture_output=True, text=True, check=False, cwd=tmp_path, env=environment)
    assert completed.returncode == 0, completed.stderr
    manifest = json.loads(completed.stdout)
    assert manifest["case_count"] == 23
    assert len(manifest["outcomes"]) == 46
    assert len(manifest["cases"]) == 23
    assert manifest["contracts_passed"]
    assert len(manifest["fixture_sha256"]) == 64
    assert manifest["comparison"]["bm25_ndcg_wins"] + manifest["comparison"]["bm25_ndcg_ties"] \
        + manifest["comparison"]["bm25_ndcg_losses"] == 22
    assert list(tmp_path.iterdir()) == []


def test_stdout_gate_does_not_require_curated_mrr_improvement(capsys):
    assert main(["--case", "stemming-collision"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["contracts_passed"]
    assert manifest["case_count"] == 1
    assert manifest["scorers"] == list(SCORERS)
    assert "downstream answer accuracy" in " ".join(manifest["limitations"])
