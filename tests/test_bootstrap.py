"""Tests for benchmarks/bootstrap.py (paired bootstrap 95% CIs and hypothesis testing)."""
from __future__ import annotations

import pytest

from benchmarks.bootstrap import (
    bootstrap_ci,
    compare_runs,
    format_comparison_markdown,
    paired_bootstrap_test,
    percentile,
)


def test_percentile_simple():
    data = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile(data, 0.0) == 1.0
    assert percentile(data, 100.0) == 5.0
    assert percentile(data, 50.0) == 3.0
    assert percentile(data, 25.0) == 2.0
    assert percentile(data, 75.0) == 4.0


def test_percentile_empty_raises():
    with pytest.raises(ValueError):
        percentile([], 50.0)


def test_bootstrap_ci_single_value():
    res = bootstrap_ci([0.75])
    assert res["mean"] == 0.75
    assert res["ci_lower"] == 0.75
    assert res["ci_upper"] == 0.75
    assert res["margin_of_error"] == 0.0


def test_bootstrap_ci_constant_array():
    res = bootstrap_ci([1.0, 1.0, 1.0, 1.0])
    assert res["mean"] == 1.0
    assert res["ci_lower"] == 1.0
    assert res["ci_upper"] == 1.0


def test_bootstrap_ci_bounds():
    data = [0.0] * 50 + [1.0] * 50
    res = bootstrap_ci(data, n_resamples=500, seed=123)
    assert res["mean"] == 0.5
    assert 0.35 <= res["ci_lower"] < 0.5
    assert 0.5 < res["ci_upper"] <= 0.65


def test_paired_bootstrap_no_difference():
    treatment = [1, 0, 1, 1, 0, 1, 0, 0, 1, 1]
    baseline = [1, 0, 1, 1, 0, 1, 0, 0, 1, 1]
    res = paired_bootstrap_test(treatment, baseline, seed=42)
    assert res["delta"] == 0.0
    assert res["significant"] is False
    assert res["p_value"] == 1.0


def test_paired_bootstrap_significant_improvement():
    # Treatment is consistently higher
    treatment = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1] * 10
    baseline = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0] * 10
    res = paired_bootstrap_test(treatment, baseline, n_resamples=500, seed=42)
    assert res["delta"] == 1.0
    assert res["ci_lower"] == 1.0
    assert res["ci_upper"] == 1.0
    assert res["significant"] is True
    assert res["p_value"] < 0.05


def test_paired_bootstrap_with_nones():
    treatment = [1.0, None, 1.0, 0.0]
    baseline = [0.0, 1.0, 0.0, 0.0]
    res = paired_bootstrap_test(treatment, baseline, n_resamples=200, seed=42)
    assert res["n"] == 3  # The None item was filtered out


def test_compare_runs_matching():
    run_a = {
        "rows": [
            {"id": "q1", "type": "temporal", "accuracy": 1.0, "evidence": 0.8},
            {"id": "q2", "type": "temporal", "accuracy": 1.0, "evidence": 1.0},
            {"id": "q3", "type": "preference", "accuracy": 1.0, "evidence": 0.5},
        ]
    }
    run_b = {
        "rows": [
            {"id": "q1", "type": "temporal", "accuracy": 0.0, "evidence": 0.4},
            {"id": "q2", "type": "temporal", "accuracy": 1.0, "evidence": 0.8},
            {"id": "q3", "type": "preference", "accuracy": 0.0, "evidence": 0.5},
        ]
    }
    comp = compare_runs(run_a, run_b, metrics=["accuracy", "evidence"], n_resamples=200, seed=42)
    assert comp["common_questions"] == 3
    assert "accuracy" in comp["overall"]
    assert "evidence" in comp["overall"]
    assert comp["overall"]["accuracy"]["delta"] > 0
    assert comp["overall"]["evidence"]["delta"] > 0
    assert "temporal" in comp["by_type"]
    assert "preference" in comp["by_type"]

    md = format_comparison_markdown(comp)
    assert "### Benchmark Comparison" in md
    assert "temporal" in md
