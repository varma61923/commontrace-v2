from __future__ import annotations

import json
import random

import pytest

from commontrace import conformal_gate as gate
from commontrace import dosage


def test_seeded_marginal_and_stratum_harm_budget():
    rng = random.Random(19)
    harmful = {"arm": .2, "suction": .5}
    rows = []
    for context, rate in harmful.items():
        for _ in range(1800):
            unsafe = rng.random() < rate
            rows.append(gate.Calibration(.1 if unsafe else .9, rng.random() > .3, unsafe, .3, context))
    calibrated = gate.calibrate(rows, alpha=.02, per_context=True, propensity_floor=.7)
    assert calibrated["kind"] == "expected marginal guarantee"
    for context, rate in harmful.items():
        harmed = admitted = 0
        for _ in range(1200):
            unsafe = rng.random() < rate
            inject = gate.admit(calibrated, .1 if unsafe else .9, context=context)
            harmed += inject and unsafe
            admitted += inject
        assert harmed / 1200 <= .02 + 1 / 1200
        assert admitted > 400
    assert not gate.admit(calibrated, 1, context="unseen")
    assert gate.admit(calibrated, 0, context="unseen", hazard=True)


def test_ltt_multiple_metrics_and_empty_calibration():
    rows = [gate.Calibration(.9, i % 2 == 0, False, .5, metric=metric)
            for metric in ("failure", "collision") for i in range(500)]
    result = gate.calibrate(rows, alpha=.2, method="ltt", propensity_floor=.5)
    assert set(result["strata"][""]["metrics"]) == {"failure", "collision"}
    assert gate.admit(result, .9)
    assert not gate.admit(gate.calibrate([]), 1)


def test_logged_propensity_is_essential_to_risk_bound():
    rng = random.Random(29)
    rows = [gate.Calibration(.9, rng.random() < .1, rng.random() < .5, .9) for _ in range(1000)]
    result = gate.calibrate(rows, alpha=.2, propensity_floor=.1)
    assert not gate.admit(result, .9)
    assert sum(row.injected and row.failed for row in rows) / len(rows) < .1


def test_anytime_trajectory_alarm_detects_change_with_bounded_delay():
    rng = random.Random(81)
    false_alarms = 0
    for _ in range(100):
        alarm = gate.TrajectoryAlarm()
        false_alarms += any(alarm.update(int(rng.random() < .02))["halt"] for _ in range(200))
    assert false_alarms <= 9  # 0.05 plus finite Monte Carlo tolerance.
    alarm = gate.TrajectoryAlarm()
    for _ in range(100):
        assert not alarm.update(0)["halt"]
    delay = next(i for i in range(1, 11) if alarm.update(1)["halt"])
    assert delay <= 6


def test_ambiguous_robot_actions_request_human():
    result = gate.action_set({"pick": .5, "stop": .5}, [.6] * 100)
    assert result["ask_human"] and len(result["actions"]) == 2
    assert not gate.action_set({"pick": .95, "stop": .05}, [.2] * 100)["ask_human"]
    assert "heuristic" in gate.online_threshold(.5, 1, step=2, alpha=.02)["kind"]


def test_default_unchanged_and_opt_in_dosage(tmp_path):
    pytest.importorskip("mcp")
    from tests.test_mcp_server import cli

    root = str(tmp_path)
    candidate = dosage.Candidate("lesson", "helpful memory", relevance=.9)
    assert dosage.select([candidate], root=root).admitted
    out = cli("retrieval", "--harm-budget", ".02", "--per-context", "--dest", root)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["enabled"]
    assert not dosage.select([candidate], root=root).admitted
    assert dosage.select([candidate], root=root).dropped[0].reason == "harm-budget gate"


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -1])
def test_malformed_calibration_refused(number):
    with pytest.raises(ValueError):
        gate.calibrate([], alpha=number)
