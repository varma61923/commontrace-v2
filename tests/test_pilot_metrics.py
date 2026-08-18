"""Tests for benchmark/pilot_metrics.py — the deck's five business-outcome metrics."""
import pytest

import pilot_metrics as pm


def _trace(name, resolved=None, escalated=None, repeated_error=None,
           frustration_signal=None, tokens_used=None, llm_calls=None, baseline=False):
    outcome = {}
    if resolved is not None:
        outcome["resolved"] = resolved
    if escalated is not None:
        outcome["escalated"] = escalated
    if repeated_error is not None:
        outcome["repeated_error"] = repeated_error
    if frustration_signal is not None:
        outcome["frustration_signal"] = frustration_signal
    if tokens_used is not None:
        outcome["tokens_used"] = tokens_used
    if llm_calls is not None:
        outcome["llm_calls"] = llm_calls
    if baseline:
        outcome["baseline"] = True
    return {"id": name, "outcome": outcome} if outcome else {"id": name}


class TestRateAndMean:
    def test_rate_all_true(self):
        traces = [_trace("a", resolved=True), _trace("b", resolved=True)]
        val, n = pm._rate(traces, "resolved")
        assert val == pytest.approx(1.0)
        assert n == 2

    def test_rate_mixed(self):
        traces = [_trace("a", resolved=True), _trace("b", resolved=False)]
        val, n = pm._rate(traces, "resolved")
        assert val == pytest.approx(0.5)

    def test_rate_ignores_missing_field(self):
        """A trace with no outcome.resolved at all should not count toward n."""
        traces = [_trace("a", resolved=True), _trace("b")]
        val, n = pm._rate(traces, "resolved")
        assert val == pytest.approx(1.0)
        assert n == 1

    def test_rate_no_data(self):
        traces = [_trace("a")]
        val, n = pm._rate(traces, "resolved")
        assert val is None
        assert n == 0

    def test_mean_tokens(self):
        traces = [_trace("a", tokens_used=100), _trace("b", tokens_used=300)]
        val, n = pm._mean(traces, "tokens_used")
        assert val == pytest.approx(200.0)
        assert n == 2

    def test_rate_ignores_non_bool_values(self):
        """Regression: a malformed/hand-edited value like the string 'false' is Python-
        truthy and would otherwise invert the rate if not filtered to real bools."""
        traces = [{"id": "a", "outcome": {"resolved": "false"}}, _trace("b", resolved=True)]
        val, n = pm._rate(traces, "resolved")
        assert val == pytest.approx(1.0)  # only the real bool counts
        assert n == 1

    def test_mean_ignores_non_numeric_and_bool_values(self):
        """Regression: sum() on a mix of int and non-numeric string values raised TypeError."""
        traces = [
            {"id": "a", "outcome": {"tokens_used": 300}},
            {"id": "b", "outcome": {"tokens_used": "1500.5"}},
            {"id": "c", "outcome": {"tokens_used": True}},
        ]
        val, n = pm._mean(traces, "tokens_used")
        assert val == pytest.approx(300.0)
        assert n == 1


class TestSplitBaseline:
    def test_splits_by_outcome_baseline_flag(self):
        traces = [
            _trace("a", resolved=True, baseline=True),
            _trace("b", resolved=True, baseline=False),
        ]
        baseline, current = pm.split_baseline(traces)
        assert [t["id"] for t in baseline] == ["a"]
        assert [t["id"] for t in current] == ["b"]

    def test_no_outcome_counts_as_current(self):
        traces = [_trace("a")]
        baseline, current = pm.split_baseline(traces)
        assert baseline == []
        assert [t["id"] for t in current] == ["a"]


class TestComputeBucket:
    def test_full_bucket(self):
        traces = [
            _trace("a", resolved=True, escalated=False, repeated_error=False,
                    frustration_signal=False, tokens_used=100, llm_calls=1),
            _trace("b", resolved=False, escalated=True, repeated_error=True,
                    frustration_signal=True, tokens_used=300, llm_calls=3),
        ]
        r = pm.compute_bucket(traces)
        assert r["n_traces"] == 2
        assert r["resolution_rate"]["value"] == pytest.approx(0.5)
        assert r["escalation_rate"]["value"] == pytest.approx(0.5)
        assert r["repeated_error_rate"]["value"] == pytest.approx(0.5)
        assert r["frustration_rate"]["value"] == pytest.approx(0.5)
        assert r["avg_tokens_used"]["value"] == pytest.approx(200.0)
        assert r["avg_llm_calls"]["value"] == pytest.approx(2.0)

    def test_empty_bucket_all_none(self):
        r = pm.compute_bucket([])
        assert r["n_traces"] == 0
        assert r["resolution_rate"]["value"] is None


class TestPctDelta:
    def test_improvement_is_negative(self):
        # Deck framing: current lower than baseline -> negative % (e.g. -53%)
        assert pm._pct_delta(600.0, 300.0) == pytest.approx(-0.5)

    def test_regression_is_positive(self):
        assert pm._pct_delta(0.3, 0.9) == pytest.approx(2.0)

    def test_none_when_before_missing(self):
        assert pm._pct_delta(None, 1.0) is None

    def test_none_when_before_zero(self):
        assert pm._pct_delta(0.0, 1.0) is None


class TestRenderMarkdown:
    def test_renders_baseline_vs_current_table(self):
        traces = [
            _trace("a", resolved=True, baseline=True),
            _trace("b", resolved=True, baseline=False),
        ]
        baseline, current = pm.split_baseline(traces)
        report = {
            "schema_version": pm.SCHEMA_VERSION,
            "timestamp": "2026-08-18T00:00:00",
            "n_traces_total": 2,
            "baseline": pm.compute_bucket(baseline),
            "current": pm.compute_bucket(current),
        }
        md = pm.render_markdown(report)
        assert "Baseline vs. current" in md
        assert "Resolution rate" in md

    def test_renders_no_baseline_notice(self):
        traces = [_trace("a", resolved=True)]
        baseline, current = pm.split_baseline(traces)
        report = {
            "schema_version": pm.SCHEMA_VERSION,
            "timestamp": "2026-08-18T00:00:00",
            "n_traces_total": 1,
            "baseline": pm.compute_bucket(baseline),
            "current": pm.compute_bucket(current),
        }
        md = pm.render_markdown(report)
        assert "nothing to compare against" in md
