"""Analytic ranking oracles, clustered inference and fail-closed input contracts."""
from __future__ import annotations

import copy
import json
import math
import random
from pathlib import Path
from typing import Any

import pytest

from benchmarks.compare import check_regressions, clustered_estimates, compare_runs, load_runs, main, ranking_metrics


def _run() -> dict[str, Any]:
    return {
        "comparison_schema": 1, "dataset": "fixture", "budget": 1500, "mode": "memory",
        "provenance": {
            "dataset_sha256": "a" * 64, "adapter_sha256": "b" * 64,
            "product_sha256": "c" * 64, "sampling": {"seed": 42, "limit": 0, "personas": ""},
        },
        "rows": [
            {"id": f"q{i}", "type": "temporal", "cluster_id": f"c{i}", "question_sha256": str(i) * 64,
             "mode": "memory", "gold_turn_ids": ["t1", "t2"], "gold_session_ids": ["s1"],
             "ranked_turn_ids": ["t1", "other", "t2"], "ranked_session_ids": ["s1", "s2"],
             "evidence": .5, "complete": False, "session": 1., "tokens": 100,
             "answer_in_context": True, "correct": True, "score": 1.}
            for i in range(3)
        ],
    }


def _oracle_percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    pos = (len(ordered) - 1) * p
    return ordered[math.floor(pos)] * (math.ceil(pos) - pos) + ordered[math.ceil(pos)] * (pos - math.floor(pos)) if pos % 1 else ordered[int(pos)]


def test_rank_metrics_have_analytic_binary_relevance_oracle() -> None:
    metrics = ranking_metrics(["x", "a", "b", "z"], ["a", "b", "missing"])
    assert metrics["recall_at_5"] == metrics["recall_at_10"] == 2 / 3
    assert metrics["ndcg_at_10"] == pytest.approx((1 / math.log2(3) + 1 / math.log2(4)) / (1 + 1 / math.log2(3) + 1 / math.log2(4)))


def test_rank_cutoffs_and_ideal_cap_at_ten() -> None:
    ranked = [f"t{i}" for i in range(20)]
    metrics = ranking_metrics(ranked, ranked)
    assert metrics == {"recall_at_5": .25, "recall_at_10": .5, "ndcg_at_10": 1.}
    assert ranking_metrics([], ["missing"]) == {"recall_at_5": 0., "recall_at_10": 0., "ndcg_at_10": 0.}
    assert all(value is None for value in ranking_metrics(["anything"], []).values())


@pytest.mark.parametrize("ranked,gold", [(["a", "a"], ["a"]), (["a"], ["a", "a"]), ([""], ["a"])])
def test_rank_duplicate_or_empty_identities_rejected(ranked: list[str], gold: list[str]) -> None:
    with pytest.raises(ValueError):
        ranking_metrics(ranked, gold)


def test_zero_mean_delta_retains_nonzero_uncertainty() -> None:
    base = _run()
    candidate = copy.deepcopy(base)
    base["rows"][0]["evidence"], base["rows"][1]["evidence"] = 0., 1.
    candidate["rows"][0]["evidence"], candidate["rows"][1]["evidence"] = 1., 0.
    result = compare_runs(base, candidate, n_resamples=2000)
    metric = result["overall"]["evidence"]  # type: ignore[index]
    assert metric["delta"] == 0
    assert metric["ci_lower"] < 0 < metric["ci_upper"]
    assert metric["significant"] is False


def test_cluster_bootstrap_matches_independent_weighted_oracle() -> None:
    base = _run()
    candidate = copy.deepcopy(base)
    for row in base["rows"]:
        row["evidence"] = 0.
    # Two questions in c0, one in c1. Question-weighted mean is 2/3,
    # not the average of the cluster means (1/2).
    for i, row in enumerate(candidate["rows"]):
        cluster = "c0" if i < 2 else "c1"
        row["cluster_id"] = base["rows"][i]["cluster_id"] = cluster
        row["evidence"] = 1. if i < 2 else 0.
    count, seed = 137, 19
    rng = random.Random(seed)
    expected = []
    for _ in range(count):
        sample = rng.choices([0, 1], k=2)
        observations = [v for group in sample for v in ([1., 1.] if group == 0 else [0.])]
        expected.append(sum(observations) / len(observations))
    metric = compare_runs(base, candidate, n_resamples=count, seed=seed)["overall"]["evidence"]  # type: ignore[index]
    assert metric["baseline_mean"] == 0.
    assert metric["candidate_mean"] == metric["delta"] == 2 / 3
    assert metric["n_questions"] == 3 and metric["n_clusters"] == 2
    assert metric["ci_lower"] == pytest.approx(_oracle_percentile(expected, .025))
    assert metric["ci_upper"] == pytest.approx(_oracle_percentile(expected, .975))


def test_input_order_and_product_revision_do_not_change_pairing() -> None:
    baseline = _run()
    candidate = copy.deepcopy(baseline)
    candidate["rows"].reverse()
    candidate["provenance"]["product_sha256"] = "d" * 64
    first = compare_runs(baseline, candidate, n_resamples=50)
    candidate["rows"].reverse()
    assert first == compare_runs(baseline, candidate, n_resamples=50)
    assert first["sample_unit"] == "conversation" and first["mean_weighting"] == "question"


def test_single_conversation_has_no_confidence_or_statistical_pass() -> None:
    run = _run()
    for row in run["rows"]:
        row["cluster_id"] = "only"
    result = compare_runs(run, run, n_resamples=50)
    metric = result["overall"]["evidence"]  # type: ignore[index]
    assert metric["n_questions"] == 3 and metric["n_clusters"] == 1
    assert metric["ci_lower"] is None and metric["ci_upper"] is None
    assert metric["significant"] is False and metric["ci_status"] == "insufficient_clusters"
    assert any("insufficient" in message for message in check_regressions(result))


def test_category_intervals_use_only_their_conversations() -> None:
    run = _run()
    run["rows"][2]["type"] = "single-hop"
    result = compare_runs(run, run, n_resamples=30)
    assert result["by_type"]["temporal"]["evidence"]["n_clusters"] == 2  # type: ignore[index]
    assert result["by_type"]["single-hop"]["evidence"]["ci_status"] == "insufficient_clusters"  # type: ignore[index]
    assert any(message.startswith("single-hop.") for message in check_regressions(result))


def test_no_gold_explicitly_excluded_and_unresolved_gold_stays_a_miss() -> None:
    run = _run()
    run["rows"][0]["gold_turn_ids"] = []
    run["rows"][1]["gold_turn_ids"] = ["unresolved"]
    result = compare_runs(run, run, n_resamples=50)
    metric = result["overall"]["turn_recall_at_5"]  # type: ignore[index]
    assert metric["n_questions"] == 2 and metric["excluded_questions"] == 1
    assert metric["baseline_mean"] == .5
    for row in run["rows"]:
        row["gold_turn_ids"] = []
    result = compare_runs(run, run, n_resamples=50)
    assert result["overall"]["turn_recall_at_5"]["ci_status"] == "no_scored_questions"  # type: ignore[index]
    assert check_regressions(result) == []


@pytest.mark.parametrize("field,value", [
    ("dataset", "other"), ("budget", 4000), ("mode", "full-context"),
    ("comparison_schema", 2), ("comparison_schema", True), ("budget", 0), ("budget", True), ("mode", "unknown"),
])
def test_incompatible_or_invalid_run_metadata_rejected(field: str, value: object) -> None:
    baseline = _run()
    candidate = copy.deepcopy(baseline)
    candidate[field] = value
    with pytest.raises(ValueError):
        compare_runs(baseline, candidate)


@pytest.mark.parametrize("field,value", [
    ("type", "changed"), ("cluster_id", "other"), ("question_sha256", "d" * 64),
    ("gold_turn_ids", ["different"]), ("gold_session_ids", ["different"]),
    ("ranked_turn_ids", ["duplicate", "duplicate"]), ("ranked_session_ids", ["dup", "dup"]),
    ("gold_turn_ids", ["same", "same"]), ("id", "q1"), ("mode", "no-memory"),
    ("tokens", -1), ("evidence", 1.1), ("evidence", float("inf")), ("score", float("nan")),
    ("correct", "true"), ("question_sha256", "short"), ("cluster_id", ""),
])
def test_incompatible_or_invalid_question_metadata_rejected(field: str, value: object) -> None:
    baseline = _run()
    candidate = copy.deepcopy(baseline)
    candidate["rows"][0][field] = value
    with pytest.raises(ValueError):
        compare_runs(baseline, candidate)


@pytest.mark.parametrize("action", ["remove", "extra", "empty", "missing", "null"])
def test_missing_failed_or_extra_questions_cannot_disappear(action: str) -> None:
    baseline = _run()
    candidate = copy.deepcopy(baseline)
    if action == "remove":
        candidate["rows"].pop()
    elif action == "extra":
        extra = copy.deepcopy(candidate["rows"][0])
        extra["id"] = "new-question"
        candidate["rows"].append(extra)
    elif action == "empty":
        candidate["rows"] = []
    elif action == "missing":
        del candidate["rows"][0]["correct"]
    else:
        candidate["rows"][0]["correct"] = None
    with pytest.raises(ValueError):
        compare_runs(baseline, candidate)


@pytest.mark.parametrize("field", ["dataset_sha256", "adapter_sha256", "sampling"])
def test_dataset_sampling_or_adapter_provenance_mismatch_rejected(field: str) -> None:
    baseline = _run()
    candidate = copy.deepcopy(baseline)
    if field == "sampling":
        candidate["provenance"][field]["seed"] += 1
    else:
        candidate["provenance"][field] = "d" * 64
    with pytest.raises(ValueError, match="provenance"):
        compare_runs(baseline, candidate)


def test_quality_gate_rejects_negative_ci_but_ignores_more_tokens() -> None:
    baseline = _run()
    candidate = copy.deepcopy(baseline)
    for row in candidate["rows"]:
        row["evidence"] = 0.
        row["tokens"] = 500
    problems = check_regressions(compare_runs(baseline, candidate, n_resamples=50))
    assert problems and all("evidence" in message for message in problems)
    for row in candidate["rows"]:
        row["evidence"] = .5
    assert check_regressions(compare_runs(baseline, candidate, n_resamples=50)) == []


@pytest.mark.parametrize("option,value", [("n_resamples", 0), ("seed", -1), ("confidence_level", 1.), ("confidence_level", float("nan")), ("n_resamples", True)])
def test_bootstrap_configuration_invalid(option: str, value: object) -> None:
    with pytest.raises(ValueError):
        compare_runs(_run(), _run(), **{option: value})  # type: ignore[arg-type]


def test_cli_stdout_success_regression_and_missing_budget(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    baseline = tmp_path / "before.json"
    candidate = tmp_path / "after.json"
    run = _run()
    baseline.write_text(json.dumps({"1500": run}), encoding="utf-8")
    candidate.write_text(json.dumps({"1500": run}), encoding="utf-8")
    arguments = ["--baseline", str(baseline), "--candidate", str(candidate), "--resamples", "50", "--check"]
    assert main(arguments) == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["comparisons"]["1500"]["n_questions"] == 3
    for row in run["rows"]:
        row["correct"] = False
    candidate.write_text(json.dumps({"1500": run}), encoding="utf-8")
    assert main(arguments) == 1
    assert "negative confidence interval" in capsys.readouterr().out
    assert main(arguments + ["--budget", "4000"]) == 2
    assert "requested budget missing" in capsys.readouterr().err


def test_cli_requires_exact_all_budget_sets_even_with_selector(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    baseline = tmp_path / "before.json"
    candidate = tmp_path / "after.json"
    baseline.write_text(json.dumps({"1500": _run()}), encoding="utf-8")
    other = _run()
    other["budget"] = 4000
    candidate.write_text(json.dumps({"1500": _run(), "4000": other}), encoding="utf-8")
    assert main(["--baseline", str(baseline), "--candidate", str(candidate), "--budget", "1500"]) == 2
    assert "budget sets differ" in capsys.readouterr().err


@pytest.mark.parametrize("payload", ['{"1500":{},"1500":{}}', '{"x":NaN}', '{"x":Infinity}', '{}'])
def test_loader_rejects_ambiguous_nonfinite_and_empty_json(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "run.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        load_runs(str(path), "memory")


def test_loader_selects_actual_reference_mode_and_checks_budget(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    reference = _run()
    reference["mode"] = "no-memory"
    path.write_text(json.dumps({"1500": {**_run(), "modes_detail": {"no-memory": reference}}}), encoding="utf-8")
    assert load_runs(str(path), "no-memory")["1500"]["mode"] == "no-memory"
    with pytest.raises(ValueError, match="mode"):
        load_runs(str(path), "memory")
    path.write_text(json.dumps({"4000": _run()}), encoding="utf-8")
    with pytest.raises(ValueError, match="budget"):
        load_runs(str(path), "memory")


@pytest.mark.parametrize("field", ["correct", "score", "tokens"])
def test_failed_measurements_in_both_runs_cannot_be_dropped(field: str) -> None:
    run = _run()
    run["rows"][0][field] = None
    with pytest.raises(ValueError, match="missing measured value"):
        compare_runs(run, run)


@pytest.mark.parametrize("value", [True, 1.5])
def test_context_token_count_requires_integer(value: object) -> None:
    run = _run()
    run["rows"][0]["tokens"] = value
    with pytest.raises(ValueError, match="integer"):
        compare_runs(run, run)


def test_unscorable_lexical_answer_and_completeness_are_explicit_exclusions() -> None:
    run = _run()
    for row in run["rows"]:
        row["answer_in_context"] = None
        row["completeness_score"] = None
    result = compare_runs(run, run, n_resamples=50)
    for key in ("answer_in_context", "completeness_score"):
        metric = result["overall"][key]  # type: ignore[index]
        assert metric["n_questions"] == 0 and metric["excluded_questions"] == 3
    assert check_regressions(result) == []


def test_gate_rejects_zero_scored_quality_when_all_quality_fields_removed() -> None:
    run = _run()
    for row in run["rows"]:
        for key in ("evidence", "complete", "session", "answer_in_context", "correct", "score"):
            del row[key]
        row["gold_turn_ids"] = row["gold_session_ids"] = []
    assert check_regressions(compare_runs(run, run, n_resamples=50)) == ["overall: no scored quality metrics"]


@pytest.mark.parametrize("key", ["judge", "judge_profile", "judge_model", "answer_model"])
def test_changed_or_missing_evaluation_configuration_rejected(key: str) -> None:
    baseline = _run()
    candidate = copy.deepcopy(baseline)
    baseline[key] = "profile-a"
    with pytest.raises(ValueError, match="evaluation"):
        compare_runs(baseline, candidate)
    candidate[key] = "profile-b"
    with pytest.raises(ValueError, match="evaluation"):
        compare_runs(baseline, candidate)


def test_stale_question_digest_does_not_hide_changed_rubric() -> None:
    baseline = _run()
    candidate = copy.deepcopy(baseline)
    baseline["rows"][0]["rubric"] = {"weight": 1}
    candidate["rows"][0]["rubric"] = {"weight": 2}
    with pytest.raises(ValueError, match="rubric"):
        compare_runs(baseline, candidate)


def test_category_named_overall_cannot_overwrite_overall_quality_gate() -> None:
    baseline = _run()
    candidate = copy.deepcopy(baseline)
    for i, row in enumerate(baseline["rows"]):
        row["type"] = candidate["rows"][i]["type"] = "overall" if i == 0 else "other"
        candidate["rows"][i]["correct"] = False
    problems = check_regressions(compare_runs(baseline, candidate, n_resamples=50))
    assert "overall.accuracy: negative confidence interval" in problems


@pytest.mark.parametrize("mode", ["full-context", "budgeted-history", "no-memory"])
def test_reference_modes_never_claim_retrieval_ranking_quality(mode: str) -> None:
    run = _run()
    run["mode"] = mode
    for row in run["rows"]:
        row["mode"] = mode
    result = compare_runs(run, run, n_resamples=50)
    for level in ("turn", "session"):
        for rank_metric in ("recall_at_5", "recall_at_10", "ndcg_at_10"):
            metric = result["overall"][f"{level}_{rank_metric}"]  # type: ignore[index]
            assert metric["n_questions"] == 0 and metric["excluded_questions"] == 3
            assert metric["baseline_mean"] is None


def test_absolute_constant_mean_uses_clustered_estimates_without_significance() -> None:
    run = _run()
    result = clustered_estimates(run, n_resamples=100)
    metric = result["overall"]["evidence"]  # type: ignore[index]
    assert metric == {
        "mean": .5, "ci_lower": .5, "ci_upper": .5, "ci_status": "available",
        "n_questions": 3, "n_clusters": 3, "excluded_questions": 0,
    }
    assert "significant" not in metric
    assert result["sample_unit"] == "conversation" and result["mean_weighting"] == "question"
    assert result["by_type"]["temporal"]["evidence"] == metric  # type: ignore[index]


def test_absolute_unequal_clusters_remain_question_weighted() -> None:
    run = _run()
    for i, row in enumerate(run["rows"]):
        row["cluster_id"] = "shared" if i < 2 else "other"
        row["evidence"] = 1. if i < 2 else 0.
    metric = clustered_estimates(run, n_resamples=1000)["overall"]["evidence"]  # type: ignore[index]
    assert metric["mean"] == 2 / 3
    assert metric["n_questions"] == 3 and metric["n_clusters"] == 2
    assert metric["ci_lower"] == 0 and metric["ci_upper"] == 1


def test_absolute_no_gold_and_single_cluster_have_explicit_unavailable_intervals() -> None:
    run = _run()
    for row in run["rows"]:
        row["gold_turn_ids"] = []
        row["cluster_id"] = "shared"
    result = clustered_estimates(run, n_resamples=20)
    evidence = result["overall"]["evidence"]  # type: ignore[index]
    assert evidence["mean"] == .5 and evidence["ci_lower"] is None
    assert evidence["ci_status"] == "insufficient_clusters" and evidence["n_clusters"] == 1
    ranking = result["overall"]["turn_recall_at_5"]  # type: ignore[index]
    assert ranking["mean"] is None and ranking["ci_lower"] is None
    assert ranking["ci_status"] == "no_scored_questions" and ranking["excluded_questions"] == 3


@pytest.mark.parametrize("option,value", [("n_resamples", 0), ("seed", -1), ("confidence_level", 1.), ("confidence_level", float("nan")), ("n_resamples", True)])
def test_absolute_bootstrap_configuration_invalid(option: str, value: object) -> None:
    with pytest.raises(ValueError):
        clustered_estimates(_run(), **{option: value})  # type: ignore[arg-type]


def test_cli_accepts_named_budgeted_history_reference(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = _run()
    run["mode"] = "budgeted-history"
    for row in run["rows"]:
        row["mode"] = "budgeted-history"
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"1500": run}), encoding="utf-8")
    assert main(["--baseline", str(path), "--candidate", str(path), "--mode", "budgeted-history", "--resamples", "50"]) == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["comparisons"]["1500"]["overall"]["turn_recall_at_5"]["baseline_mean"] is None
