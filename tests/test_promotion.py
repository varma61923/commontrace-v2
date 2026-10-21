from __future__ import annotations

import json
import random

import pytest

from unimem import allocation, holdout_io, promotion
from unimem import frontier_protocol as fp


def traffic(rng, n, treatment=.8, control=.2):
    return [{"occasion_id": str(i), "injected": (a := rng.random() < .5),
             "outcome": int(rng.random() < (treatment if a else control)), "rate": .5,
             "context": "default", "at": fp.now()} for i in range(n)]


def test_seeded_worse_candidate_no_regression_and_better_candidate_power():
    rng = random.Random(99)
    false_promotions = 0
    for _ in range(40):
        result = promotion.evaluate(traffic(rng, 320, .45, .55), {"default": 1}, {"default": 0},
                                    propensity_floor=.5, minimum_support=10)
        false_promotions += result["promote"]
    assert false_promotions <= 4  # alpha plus a finite Monte Carlo tolerance.
    result = promotion.evaluate(traffic(rng, 1800, 1, 0), {"default": 1}, {"default": 0}, propensity_floor=.5)
    assert result["promote"] and result["confidence_sequence"][0] > 0


def test_spibb_fallback_and_offline_certificate(tmp_path):
    root = str(tmp_path)
    rng = random.Random(2)
    thin = promotion.evaluate(traffic(rng, 20), {"default": 1}, {"default": .5}, minimum_support=50)
    assert thin["effective_candidate"] == {"default": .5}
    assert not thin["promote"]
    holdout_io.configure(root, rate=.5, salt="promotion-trial")
    promotion.register(root, "upgrade", candidate={"default": 1}, incumbent={"default": 0},
                       allowed_changes=["ranker"], monitoring="monitor harm", rollback="restore baseline",
                       propensity_floor=.5)
    for i in range(1000):
        assignment = promotion.assign(root, "upgrade", str(i))
        holdout_io.record_outcome(root, str(i), assignment["injected"])
    certificate = promotion.certify(root, "upgrade", propensity_floor=.5)
    promotion.assign(root, "upgrade", "unresolved")
    with pytest.raises(ValueError, match="resolve every"):
        promotion.certify(root, "upgrade", propensity_floor=.5)
    assert promotion.verify(root, certificate)
    assert not promotion.verify(root, certificate, policy_hash="00" * 32)
    promotion.rollback(root, certificate, "new drift")
    assert not promotion.verify(root, certificate)


def test_preregistration_replay_forgery_and_surfaces(tmp_path):
    pytest.importorskip("mcp")
    from tests.test_mcp_server import cli

    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="promotion-trial")
    request = {"name": "upgrade", "candidate": {"default": 1}, "incumbent": {"default": 0},
               "allowed_changes": ["embedding"], "monitoring": "monitor drift", "rollback": "restore embedding"}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(request))
    result = cli("promote", "register", str(path), "--dest", root)
    assert result.returncode == 0, result.stderr
    with pytest.raises(ValueError, match="immutable"):
        promotion.register(root, **request)
    rows = traffic(random.Random(3), 50)
    rows[1]["occasion_id"] = rows[0]["occasion_id"]
    with pytest.raises(ValueError, match="one outcome"):
        promotion.evaluate(rows, {"default": 1}, {"default": 0})
    holdout_io.configure(root, rate=.5)
    allocation.enable(root, allocation.Policy(mode="thompson"))
    # Enable takes effect after a delay; advance the clock deterministically.
    from datetime import datetime, timedelta, timezone
    with pytest.raises(ValueError, match="certificate"):
        allocation.plan(root, now=datetime.now(timezone.utc) + timedelta(seconds=10), require_certificate=True)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1])
def test_nonfinite_and_invalid_probabilities(bad):
    with pytest.raises(ValueError):
        promotion.evaluate([], {"default": bad}, {"default": 0})


def test_certified_thompson_gate_can_publish_and_other_policies_cannot(tmp_path):
    from datetime import datetime, timedelta, timezone

    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="gate-trial")
    gate = allocation.Policy(mode="thompson")
    promotion.register(root, "thompson-gate", candidate={"default": 1}, incumbent={"default": 0},
                       allowed_changes=["allocation-policy"], monitoring="monitor harm",
                       rollback="disable adaptive allocation", propensity_floor=.5,
                       artifact_hash=allocation.policy_hash(gate))
    for i in range(1000):
        assignment = promotion.assign(root, "thompson-gate", f"trial-{i}")
        holdout_io.record_outcome(root, f"trial-{i}", assignment["injected"])
    certificate = promotion.certify(root, "thompson-gate", propensity_floor=.5)
    allocation.enable(root, gate)
    later = datetime.now(timezone.utc) + timedelta(seconds=10)
    schedule = allocation.plan(root, now=later, require_certificate=True)
    assert schedule.basis["promotion_certificate"] == fp.digest(certificate["certificate"])
    # A differently configured gate is a different, uncertified change.
    allocation.enable(root, allocation.Policy(mode="thompson", monitor=.02), now=later)
    with pytest.raises(ValueError, match="artifact_hash"):
        allocation.plan(root, now=later + timedelta(seconds=10), require_certificate=True)
    promotion.rollback(root, certificate, "revoked")
    allocation.enable(root, gate, now=later + timedelta(seconds=20))
    with pytest.raises(ValueError, match="certificate"):
        allocation.plan(root, now=later + timedelta(seconds=30), require_certificate=True)
