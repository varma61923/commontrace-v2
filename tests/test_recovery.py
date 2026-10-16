from __future__ import annotations

import pytest

from commontrace import holdout_io, recovery
from tests.test_safety import protected_fact

CONTEXT = {"site": "warehouse", "robot": "r7", "embodiment": "parallel-jaw", "environment": "sim"}


def test_fleet_recoveries_measured_ranked_and_harm_withdrawn(tmp_path):
    root = str(tmp_path)
    good = recovery.register(root, "slip", "regrasp from above", context=CONTEXT)["record"]["id"]
    bad = recovery.register(root, "slip", "increase speed sharply", context=CONTEXT)["record"]["id"]
    holdout_io.configure(root, rate=.5, salt="recovery-fleet")
    for identity, beneficial in ((good, True), (bad, False)):
        for i in range(180):
            occasion = identity + str(i)
            assigned = recovery.trial(root, identity, occasion, context=CONTEXT)
            holdout_io.record_outcome(root, occasion, assigned["injected"] == beneficial)
    result = recovery.recall(root, {"failure_type": "slip"}, CONTEXT)
    assert [r["id"] for r in result["recoveries"]] == [good]
    assert result["recoveries"][0]["effect"] > .9
    assert result["recoveries"][0]["n"] == 180
    with pytest.raises(PermissionError):
        recovery.trial(root, good, "foreign", context={**CONTEXT, "site": "other"})
    hazard = protected_fact(root)
    assert recovery.trial(root, hazard.id, "hazard", context=CONTEXT)["injected"]


def test_calibration_and_intervention_metric():
    result = recovery.signature("collision", .8, [(.8, True)] * 40,
                               explanation={"cause": "occlusion", "step": 3}, scene={"objects": ["crate"]})
    assert .9 < result["calibrated_score"] < 1
    assert recovery.interventions_per_hour([
        {"timestamp": "2026-01-01T00:00:00Z", "duration_hours": 2, "interventions": 3}
    ])["interventions_per_hour"] == 1.5
