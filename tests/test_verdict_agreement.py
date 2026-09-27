"""Every local surface gives one lesson the same verdict.

`retrieve`/`experiment_status` (commontrace/evidence.py), `commontrace
experiment`, its `--strict` CI gate, and `commontrace pilot` all read the
same running experiment. They used to read it two ways: the MCP tools
against a boundary valid at every sample size, the CLI and the pilot
against a fixed 5% threshold. So one store could fail its CI gate on a
HURTS its own agents were being told was UNDERPOWERED, and the pilot report
a sponsor reads could disagree with both.

The store here holds a modest harm (about 40% success with the lesson
against 60% without) at a sample where a fixed test already calls HURTS
and an anytime-valid one does not yet. That is exactly where the two
readings part, so it is where agreement has to be pinned.
"""
from __future__ import annotations

import json

import pytest

from commontrace import evidence, experiment
from tests.test_harm_policy import BAD, _store
from tests.test_mcp_server import cli

pytest.importorskip("mcp", reason="`commontrace serve` needs the MCP SDK: pip install 'commontrace[serve]'")


@pytest.fixture(autouse=True)
def _fresh_cache():
    evidence._cache.clear()
    yield
    evidence._cache.clear()


@pytest.fixture
def modest_harm(tmp_path):
    root, _server = _store(
        tmp_path, n=160,
        report=lambda i, injected: (i % 5) < (2 if BAD in injected else 3),
    )
    return root


def _cli_verdicts(root, *extra):
    out = cli("experiment", "--json", "--dest", root, *extra)
    assert out.returncode == 0, out.stderr
    return {e["lesson_slug"]: e["verdict"] for e in json.loads(out.stdout)["effects"]}


def test_the_fixture_sits_where_the_two_readings_disagree(modest_harm):
    assert _cli_verdicts(modest_harm, "--fixed-horizon")[BAD] == experiment.VERDICT_HURTS
    assert evidence.for_lessons(modest_harm)["by_lesson"][BAD]["verdict"] == (
        experiment.VERDICT_UNDERPOWERED
    )


def test_the_experiment_report_agrees_with_what_agents_are_told(modest_harm):
    by_lesson = evidence.for_lessons(modest_harm)["by_lesson"]
    assert _cli_verdicts(modest_harm) == {slug: ev["verdict"] for slug, ev in by_lesson.items()}


def test_the_ci_gate_does_not_fail_on_a_verdict_a_watched_run_has_not_earned(modest_harm):
    """`--strict` runs on every build: each is another look. Failing a
    build on a fixed-threshold HURTS is failing it on the verdict that
    repeated looking manufactures."""
    assert cli("experiment", "--strict", "--dest", modest_harm).returncode == 0
    # And the one-shot reading, for a run that really was looked at once.
    assert cli("experiment", "--strict", "--fixed-horizon", "--dest", modest_harm).returncode == 1


def test_the_pilot_report_agrees_with_the_experiment_report(modest_harm):
    out = cli("pilot", "--json", "--dest", modest_harm)
    assert out.returncode == 0, out.stderr
    pilot = {e["lesson_slug"]: e["verdict"] for e in json.loads(out.stdout)["causal_effects"]}
    assert pilot == _cli_verdicts(modest_harm)


def test_a_strong_effect_is_still_reported_by_default(tmp_path):
    """The default must not have simply stopped concluding: a lesson that
    decides the outcome is HURTS on every surface."""
    root, _server = _store(tmp_path, n=160, report=lambda i, injected: BAD not in injected)
    assert _cli_verdicts(root)[BAD] == experiment.VERDICT_HURTS
    assert cli("experiment", "--strict", "--dest", root).returncode == 1
