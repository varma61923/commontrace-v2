from __future__ import annotations

import random

import pytest

from commontrace import frontier_protocol as fp
from commontrace import holdout_io, interference, lineage, search_recipes


def test_cluster_randomization_recovers_spillover_effect_and_flags_task_bias():
    rng = random.Random(71)
    rows = [interference.Cluster(str(i), (a := rng.random() < .5), .8 if a else .2, .5)
            for i in range(3000)]
    result = interference.cluster_effect(rows, propensity_floor=.5)
    # Task holdouts sharing a treated write-back cache both receive the benefit.
    contaminated_task_effect = 0.0
    assert abs(result["estimate"] - .6) < .04
    assert result["confidence_sequence"][0] <= .6 <= result["confidence_sequence"][1]
    assert interference.symbiosis_bias(contaminated_task_effect, result["estimate"], .01, .02)["flagged"]
    with pytest.raises(ValueError, match="one bounded"):
        interference.cluster_effect([rows[0], rows[0]])


def test_treated_derivations_cannot_cross_holdout_read_boundary(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="fleet")
    interference.publish(root, "design", "shared")
    assignments = [interference.assign_cluster(root, "design", str(i)) for i in range(20)]
    treated = next(row["cluster"] for row in assignments if row["treated"])
    control = next(row["cluster"] for row in assignments if not row["treated"])
    from commontrace import hierarchical
    with interference.reader_scope(root, "design", treated):
        fact, _ = hierarchical.add_fact(root, "Shared treated-arm learned workaround")
        body = {"id": fact.id, "statement": fact.statement}
        lineage.attest(root, body, fp.sign(root, body), inputs=[])
        unrelated = str(tmp_path / "unrelated")
        other = {"id": "independent", "statement": "An unrelated store observation"}
        lineage.attest(unrelated, other, fp.sign(unrelated, other), inputs=[])
        assert "experiment" not in lineage.record(unrelated, "independent")
    with interference.reader_scope(root, "design", control):
        assert not interference.read_allowed(root, fact.id)
        assert not search_recipes.admit(root, [fact.id])
    assert interference.read_allowed(root, fact.id)


def test_switchbacks_pin_blocks_and_exclude_burn_in(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="switchbacks")
    interference.publish(root, "sb", "policy", block_length=5, burn_in=2)
    rows = [interference.assign_cluster(root, "sb", "cell", period=i) for i in range(10)]
    assert [r["eligible"] for r in rows] == [False, False, True, True, True] * 2
    assert len({r["treated"] for r in rows[:5]}) == 1
    assert rows[0]["occasion_id"] != rows[5]["occasion_id"]
