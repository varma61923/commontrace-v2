from __future__ import annotations

import random
from datetime import datetime

import pytest

from unimem import compression, dosage, hierarchical, holdout_io, privacy_erasure, safety
from unimem import frontier_protocol as fp


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
    from unimem.mcp_server import build_server
    pytest.importorskip("mcp")
    from tests.test_mcp_server import call

    root = str(tmp_path)
    fact = protected_fact(root)
    assert call(build_server(root), "safety_context")["memories"][0]["id"] == fact.id
    review = safety.review(root, fact.id, reviewer="operator", reason="corrected review")
    review["record"]["id"] = "other"
    with pytest.raises(PermissionError):
        safety.mark(root, fact.id, "zone", review)


def test_property_hazards_survive_every_withholding_path(tmp_path):
    """Random experiment, gate, packing and budget configurations never withhold a hazard."""
    from unimem import conformal_gate, fact_consolidation, measure, recall, retention

    root = str(tmp_path)
    fact = protected_fact(root)
    hierarchical.add_fact(root, "Stop near the human work zone during maintenance")  # near-duplicates
    hierarchical.add_fact(root, "Stop near the human work zone at night")
    rng = random.Random(1234)
    holdout_io.configure(root, rate=.5, salt="property")
    memory = measure.CausalMemory(lambda query: [{"id": fact.id, "text": fact.statement},
                                                 {"id": "ordinary", "text": "Ordinary note"}], root=root)
    for i in range(60):
        rate = rng.choice([.05, .5, .95])
        holdout_io.configure(root, rate=rate, salt=f"property-{i % 3}")
        if rng.random() < .5:
            conformal_gate.configure(root, alpha=rng.choice([.001, .02, .5]), per_context=rng.random() < .5)
        else:
            conformal_gate.disable(root)
        if rng.random() < .5:
            dosage.configure_value_packing(root)
        # Assignment (direct and through a wrapped provider) never randomizes a hazard.
        withheld = holdout_io.assign_and_log(root, [fact.id, "ordinary"], occasion_id=f"a{i}", rate=rate,
                                             salt=f"property-{i % 3}", context=rng.choice(["", "robot"]))
        assert fact.id not in withheld
        items = memory.recall("work zone", occasion_id=f"m{i}", context=rng.choice(["", "robot"]))
        assert fact.id in [item["id"] for item in items]
        # Dosage under any budget, gate and packing configuration.
        budget = dosage.Budget(max_chars=rng.randrange(0, 40), max_lessons=rng.randrange(0, 3))
        candidates = [dosage.Candidate(fact.id, fact.statement, relevance=rng.random()),
                      dosage.Candidate("ordinary", "Ordinary note", relevance=rng.random())]
        assert fact.id in [c.slug for c in dosage.select(candidates, budget, root=root,
                                                         context=rng.choice(["", "robot"])).admitted]
        # Recall packing under a tiny budget keeps flagged hazards.
        packed = recall.pack([recall.Item("facts", "fact:" + fact.id, fact.statement * 20, .1,
                                          provenance={"hazard": True}),
                              recall.Item("facts", "fact:other", "other", .9)], rng.randrange(1, 8))
        assert "fact:" + fact.id in [item.id for item in packed]
    # Lifecycle paths: eviction, dreaming budget and consolidation skip hazards.
    with pytest.raises(PermissionError):
        retention.propose(root, fact.id, "rewritten hazard", author="dreamer")
    model = retention.UtilityModel((1.0,), 1.0, -1.0, 100)
    result = retention.evict(root, model, [{"id": fact.id, "tokens": 10, "future_retrieval": 0, "features": [-1]}],
                             token_budget=0)
    assert result["evicted"] == [] and fact.id in result["kept"]
    assert fact.id not in {row["id"] for row in retention.dream_budget(
        [{"id": fact.id, "effect": -1, "future_retrieval": 1, "tokens": 1}], tokens=10,
        protected=set(safety.protected(root)))["changes"]}
    clusters = fact_consolidation.cluster_facts(root, threshold=.1)["clusters"]
    assert clusters and all(fact.id not in str(cluster) for cluster in clusters)
