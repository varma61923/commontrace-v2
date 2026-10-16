from __future__ import annotations

import random

import pytest

from commontrace import frontier_protocol as fp
from commontrace import holdout_io, interference, lineage, search_recipes


def _fleet(rng, clusters, tasks, *, design, direct=.1, spillover=.3, base=.3):
    """Simulate write-back contamination: a treated run writes a learned workaround
    into its cluster's shared cache, and the workaround helps every later task in
    that cluster whatever its own arm. Returns per-task rows and per-cluster rows."""
    task_rows, cluster_rows = [], []
    for c in range(clusters):
        cluster_treated = rng.random() < .5
        cache = False
        outcomes = []
        for _ in range(tasks):
            treated = cluster_treated if design == "cluster" else rng.random() < .5
            # Write-path diversion: holdout-arm tasks cannot read treated derivations.
            visible = cache and (treated or design != "diverted")
            success = rng.random() < base + direct * treated + spillover * visible
            task_rows.append((treated, success))
            outcomes.append(success)
            cache = cache or treated
        cluster_rows.append(interference.Cluster(str(c), cluster_treated, sum(outcomes) / tasks, .5))
    return task_rows, cluster_rows


def _difference(rows):
    treated = [y for a, y in rows if a]
    control = [y for a, y in rows if not a]
    effect = sum(treated) / len(treated) - sum(control) / len(control)
    se = (sum(treated) / len(treated) * (1 - sum(treated) / len(treated)) / len(treated)
          + sum(control) / len(control) * (1 - sum(control) / len(control)) / len(control)) ** .5
    return effect, se


def test_write_back_contamination_biases_task_estimates_and_is_flagged():
    from commontrace import integrity

    rng = random.Random(71)
    tasks = 20
    # Fleet-wide deployment effect: direct lift on every task, plus the cache from
    # the first treated task onward (tasks 2..20 of a treated cluster).
    total = .1 + .3 * (tasks - 1) / tasks
    contaminated, _ = _fleet(rng, 400, tasks, design="task")
    task_effect, task_se = _difference(contaminated)
    _, clusters = _fleet(rng, 1500, tasks, design="cluster")
    clustered = interference.cluster_effect(clusters, propensity_floor=.5)
    diverted, _ = _fleet(rng, 400, tasks, design="diverted")
    diverted_effect, _ = _difference(diverted)
    assert abs(task_effect - total) > .15  # both arms read the treated cache: biased toward the direct effect
    assert abs(clustered["estimate"] - total) < .05
    assert clustered["confidence_sequence"][0] <= total <= clustered["confidence_sequence"][1]
    assert abs(diverted_effect - total) < abs(task_effect - total)  # diversion removes most of the bias
    cluster_se = (clustered["confidence_sequence"][1] - clustered["estimate"]) / 4
    flag = interference.symbiosis_bias(task_effect, clustered["estimate"], task_se, cluster_se)
    assert flag["flagged"]
    report = integrity.audit([], cluster_comparison={"task_effect": task_effect,
                                                     "cluster_effect": clustered["estimate"],
                                                     "task_se": task_se, "cluster_se": cluster_se})
    finding = next(f for f in report.findings if f.check == "symbiosis_bias")
    assert finding.severity == integrity.SEVERITY_WEAKENS
    # Mutation guard: an unequal design needs Horvitz-Thompson weights.
    skewed = []
    for c in range(3000):
        treated = rng.random() < .2
        skewed.append(interference.Cluster(str(c), treated, min(1, (.6 if treated else .3) + rng.random() * .1), .2))
    weighted = interference.cluster_effect(skewed, propensity_floor=.2)["estimate"]
    unweighted = sum(c.outcome if c.treated else -c.outcome for c in skewed) / len(skewed)
    assert abs(weighted - .3) < .05 and abs(unweighted - .3) > .2
    with pytest.raises(ValueError, match="one bounded"):
        interference.cluster_effect([clusters[0], clusters[0]])


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
    neutral, _ = hierarchical.add_fact(root, "An operator note written outside the experiment")
    derived = {"id": "derived-summary", "statement": "Summary of the treated workaround"}
    lineage.attest(root, derived, fp.sign(root, derived), inputs=[fact.id])
    with interference.reader_scope(root, "design", control):
        assert not interference.read_allowed(root, fact.id)
        assert not search_recipes.admit(root, [fact.id])
        assert not interference.read_allowed(root, "derived-summary")  # laundering through a summary
        # Diversion hides only treated derivations; unrelated new memory stays visible to both arms.
        assert interference.read_allowed(root, neutral.id) and search_recipes.admit(root, [neutral.id])
    assert interference.read_allowed(root, fact.id)


def test_switchbacks_pin_blocks_and_exclude_burn_in(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="switchbacks")
    interference.publish(root, "sb", "policy", block_length=5, burn_in=2)
    rows = [interference.assign_cluster(root, "sb", "cell", period=i) for i in range(10)]
    assert [r["eligible"] for r in rows] == [False, False, True, True, True] * 2
    assert len({r["treated"] for r in rows[:5]}) == 1
    assert rows[0]["occasion_id"] != rows[5]["occasion_id"]
