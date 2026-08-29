"""Tests for commontrace/impact.py and `commontrace impact` -- the Impact
Dashboard ("errors avoided, lessons reused, value generated or saved").

The property that matters most: no dollar figure is ever produced unless
the caller explicitly supplies a rate. Fabricating one to fill in a
flattering number would contradict hub/plans.py's "no currency appears
anywhere in this repository, and that is deliberate."
"""
from __future__ import annotations

import json

import pytest

from commontrace import impact
from commontrace.cli import main
from commontrace.reliability import Evidence


def _trace(id, resolved=None, tokens_used=None, baseline=False):
    outcome = {"baseline": baseline}
    if resolved is not None:
        outcome["resolved"] = resolved
    if tokens_used is not None:
        outcome["tokens_used"] = tokens_used
    return {"id": id, "outcome": outcome}


class TestComputeImpact:
    def test_lessons_reused_counts_hits_within_retrieved(self):
        ev = [
            Evidence("e1", retrieved=["lesson_x"], hit=["lesson_x"], succeeded=True),
            Evidence("e2", retrieved=["lesson_x", "lesson_y"], hit=["lesson_x"], succeeded=True),
            Evidence("e3", retrieved=["lesson_y"], hit=[], succeeded=False),
        ]
        r = impact.compute_impact(ev, traces=[])
        assert r.lessons_reused == 2  # lesson_x hit twice; lesson_y never hit
        assert r.n_occasions_with_lesson == 3

    def test_a_hit_not_in_retrieved_does_not_count(self):
        """Mirrors reliability.score_lessons: a hit only counts if the lesson
        was actually retrieved on that occasion -- otherwise a stray hit
        entry could inflate the count past what was ever injected."""
        ev = [Evidence("e1", retrieved=["lesson_x"], hit=["lesson_never_retrieved"], succeeded=True)]
        r = impact.compute_impact(ev, traces=[])
        assert r.lessons_reused == 0

    def test_errors_avoided_counts_successful_hits_only(self):
        ev = [
            Evidence("e1", retrieved=["x"], hit=["x"], succeeded=True),   # avoided
            Evidence("e2", retrieved=["x"], hit=["x"], succeeded=False),  # hit but still failed
            Evidence("e3", retrieved=["x"], hit=[], succeeded=True),      # no hit -- excluded
            Evidence("e4", retrieved=["x"], hit=["x"], succeeded=None),   # unknown outcome -- excluded
        ]
        r = impact.compute_impact(ev, traces=[])
        assert r.errors_avoided == 1
        assert r.errors_avoided_basis == 2
        assert r.errors_avoided_rate == pytest.approx(0.5)

    def test_errors_avoided_rate_is_none_with_no_basis(self):
        r = impact.compute_impact([], traces=[])
        assert r.errors_avoided_basis == 0
        assert r.errors_avoided_rate is None

    def test_token_savings_computed_from_baseline_vs_current(self):
        traces = [
            _trace("b1", tokens_used=500, baseline=True),
            _trace("b2", tokens_used=300, baseline=True),
            _trace("c1", tokens_used=200, baseline=False),
            _trace("c2", tokens_used=200, baseline=False),
        ]
        r = impact.compute_impact([], traces)
        assert r.avg_tokens_baseline == pytest.approx(400.0)
        assert r.avg_tokens_current == pytest.approx(200.0)
        assert r.tokens_saved_per_task == pytest.approx(200.0)
        assert r.tokens_saved_total == pytest.approx(400.0)  # 200 * 2 current traces

    def test_negative_savings_are_reported_not_clipped(self):
        """Cost going UP must show as a negative number, never silently
        floored to zero -- this dashboard reports what happened, flattering
        or not."""
        traces = [_trace("b1", tokens_used=100, baseline=True), _trace("c1", tokens_used=300, baseline=False)]
        r = impact.compute_impact([], traces)
        assert r.tokens_saved_per_task == pytest.approx(-200.0)

    def test_no_baseline_traces_yields_no_token_savings(self):
        traces = [_trace("c1", tokens_used=200, baseline=False)]
        r = impact.compute_impact([], traces)
        assert r.avg_tokens_baseline is None
        assert r.tokens_saved_per_task is None
        assert r.tokens_saved_total is None

    def test_no_dollar_value_without_any_rate_supplied(self):
        ev = [Evidence("e1", retrieved=["x"], hit=["x"], succeeded=True)]
        traces = [_trace("b1", tokens_used=500, baseline=True), _trace("c1", tokens_used=200, baseline=False)]
        r = impact.compute_impact(ev, traces)
        assert r.dollar_value_tokens is None
        assert r.dollar_value_errors is None
        assert r.dollar_value_total is None

    def test_dollar_value_from_cost_per_1k_tokens_only(self):
        traces = [_trace("b1", tokens_used=2000, baseline=True), _trace("c1", tokens_used=1000, baseline=False)]
        r = impact.compute_impact([], traces, cost_per_1k_tokens=1.0)
        # saved 1000 tokens/task * 1 current trace = 1000 tokens saved total -> $1.00
        assert r.dollar_value_tokens == pytest.approx(1.0)
        assert r.dollar_value_errors is None
        assert r.dollar_value_total == pytest.approx(1.0)

    def test_dollar_value_from_value_per_error_avoided_only(self):
        ev = [Evidence(f"e{i}", retrieved=["x"], hit=["x"], succeeded=True) for i in range(3)]
        r = impact.compute_impact(ev, traces=[], value_per_error_avoided=10.0)
        assert r.dollar_value_errors == pytest.approx(30.0)
        assert r.dollar_value_tokens is None
        assert r.dollar_value_total == pytest.approx(30.0)

    def test_dollar_value_combines_both_components_when_both_supplied(self):
        ev = [Evidence("e1", retrieved=["x"], hit=["x"], succeeded=True)]
        traces = [_trace("b1", tokens_used=2000, baseline=True), _trace("c1", tokens_used=1000, baseline=False)]
        r = impact.compute_impact(ev, traces, cost_per_1k_tokens=1.0, value_per_error_avoided=10.0)
        assert r.dollar_value_total == pytest.approx(r.dollar_value_tokens + r.dollar_value_errors)


class TestRenderMarkdown:
    def test_no_rate_supplied_says_so_rather_than_a_number(self):
        r = impact.compute_impact([], traces=[])
        md = impact.render_markdown(r)
        assert "No dollar figure computed" in md

    def test_rate_supplied_shows_total(self):
        ev = [Evidence("e1", retrieved=["x"], hit=["x"], succeeded=True)]
        r = impact.compute_impact(ev, traces=[], value_per_error_avoided=50.0)
        md = impact.render_markdown(r)
        assert "$50.00" in md
        assert "estimate" in md.lower()


class TestToDict:
    def test_includes_computed_rate_and_is_json_serializable(self):
        ev = [Evidence("e1", retrieved=["x"], hit=["x"], succeeded=True)]
        r = impact.compute_impact(ev, traces=[])
        d = impact.to_dict(r)
        assert d["errors_avoided_rate"] == 1.0
        assert json.dumps(d)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_trace_file(root, name, resolved, tokens_used, baseline, retrieved=None, hit=None):
    tdir = root / "memory" / "traces"
    tdir.mkdir(parents=True, exist_ok=True)
    ext_lines = ""
    if retrieved is not None:
        ext_lines = (
            "extensions:\n"
            f"  lessons_retrieved: [{', '.join(retrieved)}]\n"
            f"  lessons_hit: [{', '.join(hit or [])}]\n"
        )
    (tdir / f"{name}.md").write_text(
        "---\n"
        f"id: {name}\n"
        f"title: {name}\n"
        "agent_type: support\n"
        "tags: []\n"
        "outcome:\n"
        f"  resolved: {'true' if resolved else 'false'}\n"
        f"  tokens_used: {tokens_used}\n"
        f"  baseline: {'true' if baseline else 'false'}\n"
        f"{ext_lines}"
        "---\n\n"
        "## Context\nctx\n\n## Solution\nfix\n",
        encoding="utf-8",
    )


class TestImpactCLI:
    def test_no_data_says_so_and_exits_zero(self, store, capsys):
        (store / "memory" / "traces").mkdir(parents=True)
        (store / "memory" / "episodes").mkdir(parents=True)
        assert main(["impact", "--dest", str(store)]) == 0
        assert "nothing to measure yet" in capsys.readouterr().err

    def test_reports_measured_counts_from_written_traces(self, store, capsys):
        _write_trace_file(store, "b1", resolved=False, tokens_used=500, baseline=True)
        _write_trace_file(store, "c1", resolved=True, tokens_used=200, baseline=False,
                           retrieved=["lesson_x"], hit=["lesson_x"])
        capsys.readouterr()
        assert main(["impact", "--dest", str(store)]) == 0
        out = capsys.readouterr().out
        assert "**Errors avoided**: 1" in out
        assert "**Lessons reused**: 1" in out

    def test_json_and_html_together_is_rejected(self, store, capsys):
        (store / "memory" / "traces").mkdir(parents=True)
        (store / "memory" / "episodes").mkdir(parents=True)
        _write_trace_file(store, "c1", resolved=True, tokens_used=200, baseline=False)
        assert main(["impact", "--dest", str(store), "--json", "--html"]) == 2
        assert "mutually exclusive" in capsys.readouterr().err

    def test_html_writes_a_report_file(self, store, capsys):
        _write_trace_file(store, "c1", resolved=True, tokens_used=200, baseline=False,
                           retrieved=["lesson_x"], hit=["lesson_x"])
        capsys.readouterr()
        assert main(["impact", "--dest", str(store), "--html"]) == 0
        reports = list((store / "memory" / "benchmark_reports").glob("impact_*.html"))
        assert len(reports) == 1
        assert reports[0].read_text(encoding="utf-8").startswith("<!DOCTYPE html>")

    def test_cli_rate_flags_produce_a_dollar_total(self, store, capsys):
        _write_trace_file(store, "c1", resolved=True, tokens_used=200, baseline=False,
                           retrieved=["lesson_x"], hit=["lesson_x"])
        capsys.readouterr()
        assert main([
            "impact", "--dest", str(store), "--json",
            "--value-per-error-avoided", "25",
        ]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["dollar_value_errors"] == pytest.approx(25.0)
