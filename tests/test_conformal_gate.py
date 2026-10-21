from __future__ import annotations

import json
import random

import pytest

from unimem import conformal_gate as gate
from unimem import dosage


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


def _log_trial(root, rng, n, *, start=0, scorer="lexical", context="Support"):
    from unimem import holdout_io

    for i in range(start, start + n):
        relevance = rng.random()
        occasion = f"occ-{i}"
        holdout_io.assign_and_log(root, [f"m{i}"], occasion_id=occasion, rate=.3, salt="gate",
                                  relevance={f"m{i}": relevance}, scorer=scorer, context=context)
        # Low-relevance memories are the harmful ones in this synthetic trial.
        holdout_io.record_outcome(root, occasion, relevance > .3 or rng.random() < .1)


def test_gate_snapshot_excludes_gated_rows_and_is_cached(tmp_path, monkeypatch):
    from unimem import holdout_io

    root = str(tmp_path)
    holdout_io.configure(root, rate=.3, salt="gate")
    rng = random.Random(3)
    _log_trial(root, rng, 400)
    configured = gate.configure(root, alpha=.05, propensity_floor=.7, per_context=True)
    assert configured["enabled"] and configured["calibration_n"] == 400
    assert configured["scorers"] == ["lexical"]
    threshold = configured["strata"]["support"]["threshold"]
    assert gate.score(.2) < threshold <= gate.score(.35)
    # Admission reads the snapshot: no assignment log is parsed on the hot path.
    import unimem.commands.experiment_cmd as experiment_cmd

    monkeypatch.setattr(experiment_cmd, "_load", lambda root: (_ for _ in ()).throw(AssertionError("hot path")))
    candidate = dosage.Candidate("memory", "text", relevance=.9)
    dose = dosage.select([candidate], root=root, context="  SUPPORT ", scorer="lexical")
    assert dose.admitted and dose.harm_budget["threshold"] == threshold
    assert not dosage.select([candidate], root=root, context="support", scorer="dense").admitted
    monkeypatch.undo()
    # Rows logged while gated are excluded from recalibration (they only cover admitted scores).
    _log_trial(root, rng, 200, start=400)
    assert gate.configure(root, alpha=.05, propensity_floor=.7, per_context=True)["calibration_n"] == 400
    realized = gate.report(root)["realized"]
    assert realized["n"] == 200 and realized["ipw_injected_failure"] is not None
    assert gate.disable(root)["enabled"] is False
    assert dosage.select([dosage.Candidate("memory", "text", relevance=.01)], root=root).admitted


def test_core_and_hazard_memories_bypass_the_budget(tmp_path):
    root = str(tmp_path)
    gate.configure(root, alpha=.02)
    core = dosage.Candidate("pinned", "operator rule", core=True, relevance=0)
    optional = dosage.Candidate("optional", "memory", relevance=1)
    dose = dosage.select([core, optional], root=root)
    assert [c.slug for c in dose.admitted] == ["pinned"]
    assert dose.harm_budget["abstains"]


def test_recall_gates_only_the_calibrated_lessons_channel(tmp_path):
    pytest.importorskip("mcp")
    from unimem import hierarchical, recall
    from tests.test_mcp_server import _write_lesson

    root = str(tmp_path)
    _write_lesson(root, "torque", body="Limit gripper torque to five newton metres on glass.")
    hierarchical.add_fact(root, "Gripper torque on glass is limited to five newton metres")
    before = recall.recall(root, "gripper torque glass")
    assert {item.channel for item in before.items} >= {"lessons", "facts"}
    gate.configure(root, alpha=.02)  # no ungated calibration rows: lessons must abstain
    after = recall.recall(root, "gripper torque glass")
    assert "lessons" not in {item.channel for item in after.items}
    assert "facts" in {item.channel for item in after.items}
    assert after.explain["harm_budget"]["scope"].startswith("lessons channel")
    assert recall._memory_id("lesson:torque") == "torque" and recall._memory_id("fact:f1") == "f1"
    assert recall._memory_id("edge:a->b:rel") == "edge:a->b:rel"
