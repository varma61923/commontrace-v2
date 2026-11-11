from __future__ import annotations

import random

from unimem import uplift


def traffic(seed, n, effects=True):
    rng = random.Random(seed)
    result = []
    for i in range(n):
        jaw = rng.random() < 0.5
        action = rng.random() < 0.5
        success = (action if jaw else not action) if effects else rng.random() < 0.5
        result.append(
            {
                "occasion_id": str(i),
                "features": {"gripper": "jaw" if jaw else "suction", "site": "depot"},
                "injected": action,
                "outcome": int(success),
                "rate": 0.5,
                "environment": "real",
            }
        )
    return result


def test_embodiment_targeting_rate_and_exploration(tmp_path):
    model = uplift.fit(traffic(12, 12000), training_size=1000, propensity_floor=0.5)
    assert model.valid and model.rate_lower > 0
    jaw = model.decide({"gripper": "jaw", "site": "depot"}, exploration_draw=0.8)
    suction = model.decide({"gripper": "suction", "site": "depot"}, exploration_draw=0.8)
    assert jaw["injection_probability"] == 0.95 and suction["injection_probability"] == 0.05
    assert model.decide({"gripper": "jaw", "site": "depot"}, exploration_draw=0.01)["exploration"]
    from datetime import datetime, timedelta, timezone

    from unimem import allocation, holdout_io

    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="targeting")
    uplift.activate(
        root,
        "memory",
        model,
        {"jaw": {"gripper": "jaw", "site": "depot"}, "suction": {"gripper": "suction", "site": "depot"}},
    )
    allocation.enable(root, allocation.Policy(mode="thompson"))
    schedule = allocation.plan(root, now=datetime.now(timezone.utc) + timedelta(seconds=10))
    assert schedule.rate_for("memory", "jaw") < 0.5
    assert schedule.rate_for("memory", "suction") >= 0.9


def test_noise_rule_rejected_and_falls_back():
    model = uplift.fit(traffic(13, 3000, False), training_size=500, propensity_floor=0.5)
    result = model.decide({"gripper": "jaw", "site": "depot"}, exploration_draw=0.9, fallback_probability=0.63)
    assert not model.valid and result["fallback"] and result["injection_probability"] == 0.63


def test_only_the_latest_policy_in_the_active_experiment_can_override_rates(tmp_path):
    from unimem import frontier_protocol as fp
    from unimem import holdout_io

    root = str(tmp_path)
    holdout_io.configure(root, salt="current", rate=0.5)
    for salt, rates in [("current", {"old-class": 0.1}), ("other", {"foreign": 0.1}), ("current", {"new-class": 0.2})]:
        fp.append(root, "uplift-policies.jsonl", {"lesson": "m", "salt": salt, "rates": rates, "rate_lower": 0.1})
    assert uplift.apply_rates(root, {}, 0.05) == {"m": {"new-class": 0.2}}
    holdout_io.configure(root, salt="reset", rate=0.5)
    assert uplift.apply_rates(root, {}, 0.05) == {}
