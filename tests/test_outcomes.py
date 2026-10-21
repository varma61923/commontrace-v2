from __future__ import annotations

import random

import pytest

from unimem import outcomes


def test_seeded_prediction_powered_effect_removes_judge_bias():
    rng = random.Random(21)
    rows = []
    for _ in range(4000):
        injected = rng.random() < .5
        actual = rng.random() < (.7 if injected else .5)
        rows.append({"injected": injected, "rate": .5, "prediction": .95 if injected else .2,
                     "gold_probability": .3, "gold": int(actual) if rng.random() < .3 else None,
                     "environment": "real"})
    result = outcomes.corrected_effect(rows)
    assert abs(result["estimate"] - .2) < .1
    assert result["naive"] - .2 > .4
    assert result["interval"][0] <= .2 <= result["interval"][1]


def test_active_gold_sampling_requires_propensity_weights():
    rng = random.Random(15)
    predictions = [.9] * 2000 + [.1] * 2000
    actual = [1] * 2000 + [0] * 2000
    probabilities = [.8] * 2000 + [.05] * 2000
    gold = {i: value for i, value in enumerate(actual) if rng.random() < probabilities[i]}
    result = outcomes.prediction_powered(predictions, gold, probabilities)
    assert abs(result["estimate"] - .5) < .02
    naive_gold = sum(actual[i] for i in gold) / len(gold)
    assert naive_gold > .9


def test_deterministic_gold_sampling_has_no_gold_noise_radius():
    complete = outcomes.prediction_powered([.2, .8], {0: .1, 1: .9}, [1.0, 1.0])
    assert complete["interval"] == [complete["estimate"], complete["estimate"]]
    sampled = outcomes.prediction_powered([.2, .8], {0: .1}, [.5, .5])
    assert sampled["interval"][0] < sampled["estimate"] < sampled["interval"][1]
    with pytest.raises(ValueError, match="deterministic gold"):
        outcomes.prediction_powered([.2], {}, [1.0])


def test_surrogate_validation_and_degraded_agreement_fallback():
    rows = [{"experiment_id": "training" if i < 80 else "validation", "signals": [1, (-1 if i % 2 else 1)],
             "final": i % 2 == 0, "environment": "real"} for i in range(120)]
    model = outcomes.fit_surrogate(rows)
    assert model.agreement > .95 and not model.predict([1, 1])["use_final"]
    assert model.monitor([(.9, False)] * 30)["use_final"]
    with pytest.raises(ValueError, match="environment"):
        outcomes.fit_surrogate([*rows, {**rows[0], "environment": "sim"}])


def test_discrepancy_is_bitemporal(tmp_path):
    from unimem import hierarchical
    result = outcomes.record_discrepancy(str(tmp_path), "wet-floor", .9, .4, valid_from="2026-01-01T00:00:00Z")
    fact = hierarchical.load_facts(str(tmp_path))[result["fact_id"]]
    assert fact.valid_from.startswith("2026-01-01") and result["trust_weight"] == .5
