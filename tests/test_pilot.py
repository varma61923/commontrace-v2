"""Tests for commontrace/pilot.py's yes/no gate (`determine_result`) and the
`commontrace pilot` CLI command that bundles taxonomy + impact + the
baseline/current resolution rate into one report.

The property that matters most: a randomized-holdout (causal) result always
outranks a correlational one, and the gate never reports "yes" from
correlational data alone -- matching README.md's "Prove the lessons cause
the improvement" and STRATEGY.md §8's insistence that every other number in
this repo is correlational.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml

from commontrace import pilot
from commontrace.cli import main


def _effect(verdict, slug="lesson_x"):
    return SimpleNamespace(verdict=verdict, lesson_slug=slug)


class TestDetermineResultCausalOutranksCorrelational:
    def test_a_hurts_effect_is_no_even_with_a_positive_resolution_delta(self):
        v = pilot.determine_result(
            harmful_lesson_slugs=[],
            causal_effects=[_effect("HURTS", "bad_lesson")],
            resolution_delta=0.5,
            has_baseline=True,
        )
        assert v.level == pilot.RESULT_NO
        assert "bad_lesson" in v.explanation

    def test_a_helps_effect_is_yes(self):
        v = pilot.determine_result(
            harmful_lesson_slugs=[], causal_effects=[_effect("HELPS", "good_lesson")],
            resolution_delta=None, has_baseline=False,
        )
        assert v.level == pilot.RESULT_YES
        assert "good_lesson" in v.explanation

    def test_hurts_takes_priority_over_helps_when_both_present(self):
        v = pilot.determine_result(
            harmful_lesson_slugs=[],
            causal_effects=[_effect("HELPS", "a"), _effect("HURTS", "b")],
            resolution_delta=None, has_baseline=False,
        )
        assert v.level == pilot.RESULT_NO

    def test_causal_data_with_no_significant_effect_is_not_yet_conclusive(self):
        v = pilot.determine_result(
            harmful_lesson_slugs=[],
            causal_effects=[_effect("NO_MEASURABLE_EFFECT"), _effect("UNDERPOWERED")],
            resolution_delta=0.9, has_baseline=True,  # even a great correlational delta...
        )
        assert v.level == pilot.RESULT_UNKNOWN  # ...cannot be promoted to YES

    def test_empty_causal_effects_list_is_treated_as_holdout_data_with_no_signal_yet(self):
        """[] (data exists, nothing analyzable) is distinct from None (no
        holdout log at all) -- both land here, not in the correlational branch."""
        v = pilot.determine_result(
            harmful_lesson_slugs=["some_harmful_lesson"], causal_effects=[],
            resolution_delta=None, has_baseline=False,
        )
        assert v.level == pilot.RESULT_UNKNOWN


class TestDetermineResultCorrelationalFallback:
    def test_harmful_lesson_is_no_when_no_causal_data_exists(self):
        v = pilot.determine_result(
            harmful_lesson_slugs=["lesson_bad"], causal_effects=None,
            resolution_delta=0.5, has_baseline=True,
        )
        assert v.level == pilot.RESULT_NO
        assert "lesson_bad" in v.explanation

    def test_improved_resolution_rate_with_no_harmful_lesson_is_likely_not_yes(self):
        """Correlational improvement never earns an outright YES -- only causal does."""
        v = pilot.determine_result(
            harmful_lesson_slugs=[], causal_effects=None,
            resolution_delta=0.10, has_baseline=True,
        )
        assert v.level == pilot.RESULT_UNKNOWN
        assert "LIKELY" in v.label
        assert "causal" in v.explanation.lower()

    def test_flat_or_negative_resolution_rate_is_not_yet(self):
        v = pilot.determine_result(
            harmful_lesson_slugs=[], causal_effects=None,
            resolution_delta=-0.10, has_baseline=True,
        )
        assert v.level == pilot.RESULT_NO

    def test_no_baseline_and_no_causal_data_is_not_enough_data(self):
        v = pilot.determine_result(
            harmful_lesson_slugs=[], causal_effects=None,
            resolution_delta=None, has_baseline=False,
        )
        assert v.level == pilot.RESULT_UNKNOWN
        assert "NOT ENOUGH DATA" in v.label


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_trace_file(root, name, resolved, tokens_used, baseline):
    tdir = root / "memory" / "traces"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / f"{name}.md").write_text(
        "---\n"
        f"id: {name}\n"
        f"title: refund confusion {name}\n"
        "agent_type: support\n"
        "tags: [refunds]\n"
        "outcome:\n"
        f"  resolved: {'true' if resolved else 'false'}\n"
        f"  tokens_used: {tokens_used}\n"
        f"  baseline: {'true' if baseline else 'false'}\n"
        "---\n\n"
        "## Context\ncustomer confused about refund timeline\n\n## Solution\nfix\n",
        encoding="utf-8",
    )


def _write_episode(mem_dir, name, verdict, retrieved, hit):
    fm = {
        "name": name, "verdict": verdict,
        "lessons_retrieved_by_alpha": retrieved, "lessons_hit": hit,
    }
    (mem_dir / "episodes" / f"{name}.md").write_text(
        "---\n" + yaml.safe_dump(fm) + "---\n\nbody\n", encoding="utf-8",
    )


class TestPilotCLI:
    def test_empty_store_reports_not_enough_data(self, store, capsys):
        (store / "memory" / "traces").mkdir(parents=True)
        (store / "memory" / "lessons").mkdir(parents=True)
        (store / "memory" / "episodes").mkdir(parents=True)
        assert main(["pilot", "--dest", str(store)]) == 0
        out = capsys.readouterr().out
        assert "NOT ENOUGH DATA" in out

    def test_json_and_html_together_is_rejected(self, store, capsys):
        assert main(["pilot", "--dest", str(store), "--json", "--html"]) == 2
        assert "mutually exclusive" in capsys.readouterr().err

    def test_html_writes_a_full_report(self, store, capsys):
        for i in range(2):
            _write_trace_file(store, f"r{i}", resolved=(i % 2 == 0), tokens_used=100, baseline=False)
        capsys.readouterr()
        assert main(["pilot", "--dest", str(store), "--html"]) == 0
        reports = list((store / "memory" / "benchmark_reports").glob("pilot_report_*.html"))
        assert len(reports) == 1
        content = reports[0].read_text(encoding="utf-8")
        assert content.startswith("<!DOCTYPE html>")
        assert "THE RESULT" in content

    def test_harmful_lesson_from_episode_evidence_gates_the_result_to_no(self, store, capsys):
        eps = store / "memory" / "episodes"
        eps.mkdir(parents=True, exist_ok=True)
        for i in range(12):
            _write_episode(store / "memory", f"ok{i}", "CONFORM", ["fine"], ["fine"])
        for i in range(8):
            _write_episode(store / "memory", f"bad{i}", "ABANDON", ["bad"], ["bad"])
        capsys.readouterr()
        assert main(["pilot", "--dest", str(store)]) == 0
        out = capsys.readouterr().out
        assert "THE RESULT — NO" in out
        assert "bad" in out

    def test_json_output_contains_the_result_and_component_reports(self, store, capsys):
        _write_trace_file(store, "c1", resolved=True, tokens_used=100, baseline=False)
        capsys.readouterr()
        assert main(["pilot", "--dest", str(store), "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert "result" in data and "level" in data["result"]
        assert "taxonomy" in data
        assert "impact" in data
