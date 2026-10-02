from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp", reason="`commontrace serve` needs the MCP SDK: pip install 'commontrace[serve]'")

from commontrace import evidence, experiment  # noqa: E402
from tests.test_harm_policy import BAD, _store  # noqa: E402
from tests.test_mcp_server import cli  # noqa: E402


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
    assert cli("experiment", "--strict", "--dest", modest_harm).returncode == 0
    assert cli("experiment", "--strict", "--fixed-horizon", "--dest", modest_harm).returncode == 1


def test_the_pilot_report_agrees_with_the_experiment_report(modest_harm):
    out = cli("pilot", "--json", "--dest", modest_harm)
    assert out.returncode == 0, out.stderr
    pilot = {e["lesson_slug"]: e["verdict"] for e in json.loads(out.stdout)["causal_effects"]}
    assert pilot == _cli_verdicts(modest_harm)


def test_a_strong_effect_is_still_reported_by_default(tmp_path):
    root, _server = _store(tmp_path, n=160, report=lambda i, injected: BAD not in injected)
    assert _cli_verdicts(root)[BAD] == experiment.VERDICT_HURTS
    assert cli("experiment", "--strict", "--dest", root).returncode == 1
