from __future__ import annotations

import hashlib
import random

import pytest

from unimem import hierarchical
from unimem import world_model_trust as world

MODEL = hashlib.sha256(b"demo simulator build").hexdigest()


class ActionAware:
    def rollout(self, state, action):
        value = float(action["force"])
        return {"safe": value < 5, "distance": value * 2}


class ActionBlind:
    def rollout(self, state, action):
        return {"safe": True, "distance": 1.0}


def setup_world(root):
    memory = hierarchical.append_facts(root, [{"statement": "Use low force with the padded cup tray."}])[0][0]
    world.register(root, "countertop-sim", MODEL, embodiment="arm-v4", tasks=["place-cup"], adapter="local")
    return memory


def test_action_ignoring_model_is_vetoed_and_low_trust_never_promotes(tmp_path):
    root = str(tmp_path)
    memory = setup_world(root)
    result = world.gate(root, memory.id, model_digest=MODEL, embodiment="arm-v4", task="place-cup",
        state={"surface": "counter"}, action={"force": 2}, perturbations=[{"force": 3}], adapter=ActionBlind())
    assert result["decision"] == "veto"
    assert result["reason"] == "world model did not respond to a perturbed action"
    held = world.gate(root, memory.id, model_digest=MODEL, embodiment="arm-v4", task="place-cup",
        state={"surface": "counter"}, action={"force": 2}, perturbations=[{"force": 3}], adapter=ActionAware())
    assert held["decision"] == "hold"
    assert held["trust"]["trusted_for_veto"] is False


def test_trust_isolated_by_task_model_and_embodiment_and_trials_cover_endpoints(tmp_path):
    root = str(tmp_path)
    setup_world(root)
    for index in range(240):
        world.record_outcome(root, model_digest=MODEL, embodiment="arm-v4", task="place-cup",
                             episode_id=f"episode-{index}", predicted_safe=True, real_success=True)
    report = world.trust(root, MODEL, embodiment="arm-v4", task="place-cup", minimum_trials=20)
    assert report["trusted_for_veto"] is True
    with pytest.raises(PermissionError, match="does not cover"):
        world.trust(root, MODEL, embodiment="arm-v5", task="place-cup")
    with pytest.raises(ValueError, match="already has"):
        world.record_outcome(root, model_digest=MODEL, embodiment="arm-v4", task="place-cup",
                             episode_id="episode-0", predicted_safe=True, real_success=True)
    priority = world.prioritize_real_trials([{"id": "low", "predicted_score": -.8},
        {"id": "middle", "predicted_score": .1}, {"id": "high", "predicted_score": .9}])
    assert priority["bottom"]["id"] == "low" and priority["top"]["id"] == "high"


def test_anytime_agreement_interval_has_simulated_fixed_horizon_coverage():
    rng = random.Random(240923)
    covered = 0
    for trial in range(120):
        scores = [1 if rng.random() < .8 else -1 for _ in range(80)]
        estimate, low, high = world._interval(scores, .05)
        covered += low <= .6 <= high
        assert -1 <= estimate <= 1
    assert covered >= 110
