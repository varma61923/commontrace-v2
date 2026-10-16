from __future__ import annotations

import random

from commontrace import uplift


def traffic(seed, n, effects=True):
    rng = random.Random(seed)
    result = []
    for i in range(n):
        jaw = rng.random() < .5
        action = rng.random() < .5
        success = (action if jaw else not action) if effects else rng.random() < .5
        result.append({"occasion_id": str(i), "features": {"gripper": "jaw" if jaw else "suction", "site": "depot"},
                       "injected": action, "outcome": int(success), "rate": .5, "environment": "real"})
    return result


def test_embodiment_targeting_rate_and_exploration(tmp_path):
    model = uplift.fit(traffic(12, 12000), training_size=1000, propensity_floor=.5)
    assert model.valid and model.rate_lower > 0
    jaw = model.decide({"gripper": "jaw", "site": "depot"}, exploration_draw=.8)
    suction = model.decide({"gripper": "suction", "site": "depot"}, exploration_draw=.8)
    assert jaw["injection_probability"] == .95 and suction["injection_probability"] == .05
    assert model.decide({"gripper": "jaw", "site": "depot"}, exploration_draw=.01)["exploration"]
    from datetime import datetime, timedelta, timezone

    from commontrace import allocation, holdout_io
    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="targeting")
    uplift.activate(root, "memory", model, {"jaw": {"gripper": "jaw", "site": "depot"},
                                            "suction": {"gripper": "suction", "site": "depot"}})
    allocation.enable(root, allocation.Policy(mode="thompson"))
    schedule = allocation.plan(root, now=datetime.now(timezone.utc) + timedelta(seconds=10))
    assert schedule.rate_for("memory", "jaw") < .5
    assert schedule.rate_for("memory", "suction") >= .9


def test_noise_rule_rejected_and_falls_back():
    model = uplift.fit(traffic(13, 3000, False), training_size=500, propensity_floor=.5)
    result = model.decide({"gripper": "jaw", "site": "depot"}, exploration_draw=.9, fallback_probability=.63)
    assert not model.valid and result["fallback"] and result["injection_probability"] == .63
