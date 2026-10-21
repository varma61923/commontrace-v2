from __future__ import annotations

import pytest

from tests.test_safety import protected_fact
from unimem import holdout_io, recovery

CONTEXT = {"site": "warehouse", "robot": "r7", "embodiment": "parallel-jaw", "environment": "sim"}


def test_fleet_recoveries_measured_ranked_and_harm_withdrawn(tmp_path):
    import random

    root = str(tmp_path)
    rng = random.Random(16)
    lifts = {"regrasp from above": .45, "slow approach": .2, "increase speed sharply": -.3}
    ids = {name: recovery.register(root, "slip", name, context=CONTEXT)["record"]["id"] for name in lifts}
    holdout_io.configure(root, rate=.5, salt="recovery-fleet")
    for i in range(400):  # interleaved fleet episodes, stochastic outcomes (end-to-end, fsynced)
        for name, lift in lifts.items():
            occasion = f"{ids[name]}-{i}"
            assigned = recovery.trial(root, ids[name], occasion, context=CONTEXT)
            holdout_io.record_outcome(root, occasion, rng.random() < .4 + (lift if assigned["injected"] else 0))
    result = recovery.recall(root, {"failure_type": "slip"}, CONTEXT)
    ranked = [row["id"] for row in result["recoveries"]]
    # Real lift rises to the top in order; the harmful recovery is withdrawn.
    assert ranked == [ids["regrasp from above"], ids["slow approach"]]
    top = result["recoveries"][0]
    assert top["interval"][0] > 0 and abs(top["effect"] - .45) < .12 and top["n"] == 400
    with pytest.raises(PermissionError):
        recovery.trial(root, ids["slow approach"], "foreign", context={**CONTEXT, "site": "other"})
    assert recovery.recall(root, {"failure_type": "slip"}, {**CONTEXT, "embodiment": "suction"})["recoveries"] == []
    hazard = protected_fact(root)
    assert recovery.trial(root, hazard.id, "hazard", context=CONTEXT)["injected"]


def test_calibration_and_intervention_metric():
    result = recovery.signature("collision", .8, [(.8, True)] * 40,
                               explanation={"cause": "occlusion", "step": 3}, scene={"objects": ["crate"]})
    assert .9 < result["calibrated_score"] < 1
    assert recovery.interventions_per_hour([
        {"timestamp": "2026-01-01T00:00:00Z", "duration_hours": 2, "interventions": 3}
    ])["interventions_per_hour"] == 1.5
