from __future__ import annotations

import random

import pytest

from unimem import holdout_io, valuation


def test_seeded_factorial_values_and_complementarity_verify_offline(tmp_path):
    rng = random.Random(61)
    rows = []
    for i in range(8000):
        a, b = rng.random() < .5, rng.random() < .5
        y = .3 + .1 * a - .1 * b + .2 * a * b
        rows.append({"occasion_id": str(i), "included": {"a": a, "b": b},
                     "probabilities": {"a": .5, "b": .5}, "outcome": y})
    report = valuation.factorial(rows, ["a", "b"], propensity_floor=.5)
    estimates = {tuple(v["memories"]): v["estimate"] for v in report["values"]}
    assert abs(estimates['a',] - .2) < .03
    assert abs(estimates['b',]) < .03
    assert abs(estimates['a', 'b'] - .2) < .04
    bundle = valuation.certificate(str(tmp_path), report)
    assert valuation.verify(str(tmp_path), bundle)
    assert not valuation.verify(str(tmp_path), bundle, require_attested=True)
    bundle["certificate"]["record"]["report"]["n"] = 1
    assert not valuation.verify(str(tmp_path), bundle)


def test_canonical_certificate_and_warranty_ledger(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="valuation")
    for i in range(180):
        held = holdout_io.assign_and_log(root, ["memory"], occasion_id=str(i), rate=.5, salt="valuation",
                                         revisions={"memory": "fixture-r1"})
        holdout_io.record_outcome(root, str(i), "memory" in held)
    bundle = valuation.certify_logged(root, ["memory"], propensity_floor=.5)
    assert valuation.verify(root, bundle, require_attested=True)
    credit = valuation.warranty_credit(root, bundle, contract="c", memory="memory", minimum_lift=.2, paid_cents=500)
    assert credit["credit_cents"] == 500
    assert valuation.warranty_credit(root, bundle, contract="c", memory="memory", minimum_lift=.2, paid_cents=500) == credit
    holdout_io.assign_and_log(root, ["memory"], occasion_id="unresolved", rate=.5, salt="valuation",
                              revisions={"memory": "fixture-r1"})
    with pytest.raises(ValueError, match="resolve every"):
        valuation.certify_logged(root, ["memory"], propensity_floor=.5)
    with pytest.raises(PermissionError):
        valuation.warranty_credit(root, valuation.certificate(root, {}), contract="c", memory="memory",
                                  minimum_lift=.2, paid_cents=500)


def test_ranked_training_metadata():
    export = valuation.data_value_export([
        {"episode_id": "a", "success": True, "memory_effect": .2, "interventions": [[1, 2]]},
        {"episode_id": "b", "success": False, "memory_effect": -.4}])
    assert export["episodes"][0]["episode_index"] == "a"
    with pytest.raises(ValueError):
        valuation.data_value_export([], layout="unknown")
