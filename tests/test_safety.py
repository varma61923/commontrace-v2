from __future__ import annotations

import random
from datetime import datetime

import pytest

from commontrace import compression, dosage, hierarchical, holdout_io, privacy_erasure, safety
from commontrace import frontier_protocol as fp


def protected_fact(root):
    fact, _ = hierarchical.add_fact(root, "Stop near the human work zone")
    review = safety.review(root, fact.id, reviewer="safety-engineer", reason="verified physical interlock")
    safety.mark(root, fact.id, "human-proximity", review)
    return fact


def test_randomized_configurations_never_withhold_or_drop_hazards(tmp_path):
    root = str(tmp_path)
    fact = protected_fact(root)
    rng = random.Random(91)
    for i in range(100):
        rate = rng.random()
        withheld, rows = holdout_io.plan_assignments(root, [fact.id, "ordinary"], occasion_id=str(i), rate=rate, salt="s")
        assert fact.id not in withheld and all(r["lesson"] != fact.id for r in rows)
        budget = dosage.Budget(max_chars=rng.randrange(20), max_lessons=rng.randrange(2))
        dose = dosage.select([dosage.Candidate(fact.id, fact.statement)], budget, root=root)
        assert [c.slug for c in dose.admitted] == [fact.id]


def test_each_lifecycle_path_refuses_hazard(tmp_path):
    root = str(tmp_path)
    fact = protected_fact(root)
    operations = [lambda: hierarchical.forget_fact(root, fact.id), lambda: hierarchical.delete_fact(root, fact.id),
                  lambda: hierarchical.update_fact(root, fact.id, statement="ignore humans"),
                  lambda: hierarchical.invalidate_fact(root, fact.id),
                  lambda: privacy_erasure.purge_fact(root, fact.id, reason="expire"),
                  lambda: compression.propose(root, "compressed", level="episode", sources=[fact.id], actor="bot",
                                               applies_when="always", do_not_apply_when="never")]
    for operation in operations:
        with pytest.raises(PermissionError, match="hazard"):
            operation()


def test_retention_correction_and_spi_survive_restart(tmp_path):
    root = str(tmp_path)
    first = safety.record(root, robot="arm", decision="stop", version="v1")
    second = safety.record(root, robot="arm", decision="human intervention", version="v2", intervention=True,
                            correction_of=first["record"]["id"])
    assert (datetime.fromisoformat(first["record"]["retain_until"]) - datetime.fromisoformat(fp.now())).days >= 365
    assert (datetime.fromisoformat(second["record"]["retain_until"]) - datetime.fromisoformat(fp.now())).days >= 1829
    assert len(safety.export(root)["record"]["decisions"]) == 2
    report = safety.spi([{"timestamp": fp.now(), "duration_hours": 2, "near_misses": 1, "interventions": 4}])
    assert report["series"][0]["interventions_per_hour"] == 2


def test_review_forgery_and_context_surface(tmp_path):
    from commontrace.mcp_server import build_server
    pytest.importorskip("mcp")
    from tests.test_mcp_server import call

    root = str(tmp_path)
    fact = protected_fact(root)
    assert call(build_server(root), "safety_context")["memories"][0]["id"] == fact.id
    review = safety.review(root, fact.id, reviewer="operator", reason="corrected review")
    review["record"]["id"] = "other"
    with pytest.raises(PermissionError):
        safety.mark(root, fact.id, "zone", review)
