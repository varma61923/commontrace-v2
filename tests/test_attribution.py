from __future__ import annotations

import pytest

from commontrace import attribution


def test_planted_poison_and_replay_cost_cap(tmp_path):
    calls = []
    def replay(memories):
        calls.append(memories)
        return .8 - .7 * ("poison" in memories) + .1 * ("helpful" in memories)
    result = attribution.diagnose(str(tmp_path), "episode", ["neutral", "poison", "helpful"], replay,
        seed=16, sample_probability=1, budget_cents=100, cost_per_run_cents=2)
    assert result["attribution"]["poison"] < -.6
    assert result["poisoning_severity"] > .6 and len(calls) == 33 and result["cost_cents"] == 66
    assert attribution.gate_adjustment(str(tmp_path), "poison")["review_suggested"]
    with pytest.raises(ValueError, match="cost cap"):
        attribution.diagnose(str(tmp_path), "episode2", ["a"], replay, seed=3, sample_probability=1,
                             budget_cents=1, cost_per_run_cents=1)
    assert len(calls) == 33


def test_restore_audit_recommends_helpful_evicted_memory(tmp_path):
    result = attribution.restore_audit(str(tmp_path), "useful", ["neutral"],
        lambda ids: .9 if "useful" in ids else .1, seed=8, sample_probability=1,
        budget_cents=40, cost_per_run_cents=1)
    assert result["restore_recommended"]
    assert not attribution.diagnose(str(tmp_path), "skip", ["x"], lambda _: 1, seed=8,
        sample_probability=0, budget_cents=0, cost_per_run_cents=1)["sampled"]
