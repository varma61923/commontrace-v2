"""Interference diagnostics for fleet memory experiments.

Symbiosis bias (arXiv:2309.07107): disagreement between task and cluster effects
is a warning of possible write-back contamination, not proof of its cause.
The early diagnostic is descriptive; cluster-randomized designs must supply
their own design-based variance and define their interference boundary.
"""
from __future__ import annotations

import math
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

from commontrace import frontier_protocol as fp

_READER: ContextVar[tuple[str, str, str] | None] = ContextVar("commontrace_cluster_reader", default=None)


@dataclass(frozen=True)
class Cluster:
    id: str
    treated: bool
    outcome: float
    propensity: float


def cluster_effect(rows: Sequence[Cluster], *, propensity_floor: float = .05,
                   alpha: float = .05) -> dict[str, Any]:
    """HT mean across clusters with a design-based time-uniform Hoeffding bound.

    Fixed/predictable cluster potential outcomes, independent randomized clusters,
    and no interference across clusters are required. Unequal cluster sizes target
    an equal-cluster effect, not an equal-task effect. arXiv:2210.08639.
    """
    fp.count(len(rows), 1, fp.MAX_ROWS)
    fp.number(propensity_floor, .001, .5)
    fp.number(alpha, 1e-6, .99)
    seen, scores = set(), []
    for row in rows:
        if row.id in seen:
            raise ValueError("one bounded aggregate outcome per randomized cluster required")
        seen.add(fp.text(row.id))
        if type(row.treated) is not bool:
            raise ValueError("cluster treatment must be binary")
        y = fp.number(row.outcome, 0, 1)
        p = fp.number(row.propensity, propensity_floor, 1 - propensity_floor)
        scores.append(y / p if row.treated else -y / (1 - p))
    n = len(scores)
    estimate = math.fsum(scores) / n
    radius = 1 / propensity_floor * math.sqrt(2 * math.log(2 * n * (n + 1) / alpha) / n)
    return {"estimate": estimate, "confidence_sequence": [max(-1, estimate - radius), min(1, estimate + radius)],
            "clusters": n, "target": "equal-cluster average treatment effect",
            "kind": "finite-sample design-based guarantee under declared randomization/interference assumptions"}


def switchback(lesson: str, cluster: str, period: int, *, salt: str, block_length: int,
                burn_in: int = 1, rate: float = .5) -> dict[str, Any]:
    """Clustered random switchbacks with explicit carryover exclusion.

    arXiv:2312.15574 and 2009.00148. Washout sufficiency is a design assumption,
    not learned from the data. Blocks, not steps, are the randomized units.
    """
    from commontrace import experiment

    fp.count(period, 0, 10_000_000)
    fp.count(block_length, 2, 100_000)
    fp.count(burn_in, 0, block_length - 1)
    fp.number(rate, .001, .999)
    unit = fp.text(cluster) + ":block:" + str(period // block_length)
    treated = not experiment.is_held_out(fp.text(lesson), unit, rate, fp.text(salt))
    return {"treated": treated, "eligible": period % block_length >= burn_in,
            "block": unit, "propensity": 1 - rate, "burn_in": burn_in}


def publish(root: str, identity: str, lesson: str, *, rate: float = .5,
            block_length: int | None = None, burn_in: int = 1) -> dict[str, Any]:
    from commontrace import allocation, holdout_io, lineage

    fp.text(identity)
    fp.text(lesson)
    fp.number(rate, .001, .999)
    config = holdout_io.load_config(root)
    if not config.running or allocation.active(root, config.salt) is not None:
        raise ValueError("cluster designs require a configured fixed-rate experiment, without adaptive allocation")
    if config.rate != rate:
        raise ValueError("cluster rate must match the configured experiment")
    if block_length is not None:
        fp.count(block_length, 2, 100_000)
        fp.count(burn_in, 0, block_length - 1)
    prior = fp.journal(root, "cluster-designs.jsonl")
    if any(row["id"] == identity for row in prior):
        raise ValueError("cluster design is immutable")
    # The lineage position pins which derivations predate the design (an audit
    # anchor, never an enumeration: a store of any size can publish a design).
    return fp.append(root, "cluster-designs.jsonl", {"id": identity, "lesson": lesson, "rate": rate,
        "salt": config.salt, "lineage_position": fp.checkpoint(root, lineage.JOURNAL)[0],
        "block_length": block_length, "burn_in": burn_in})


def _design(root: str, identity: str) -> dict[str, Any]:
    rows = [row for row in fp.journal(root, "cluster-designs.jsonl") if row["id"] == identity]
    if len(rows) != 1:
        raise ValueError("publish the cluster design before its occasions")
    return dict(rows[0])


def assign_cluster(root: str, design: str, cluster: str, *, period: int | None = None) -> dict[str, Any]:
    from commontrace import allocation, holdout_io, safety

    row = _design(root, design)
    eligible = True
    if row.get("block_length") is not None:
        if period is None:
            raise ValueError("a switchback requires its period")
        fp.count(period, 0, 10_000_000)
        eligible = period % row["block_length"] >= row["burn_in"]
        cluster = fp.text(cluster) + ":block:" + str(period // row["block_length"])
    elif period is not None:
        raise ValueError("periods require a published switchback design")
    if row["lesson"] in safety.protected(root):
        return {"cluster": cluster, "treated": True, "hazard": True}
    if allocation.active(root, row["salt"]) is not None:
        raise ValueError("adaptive schedules cannot reshuffle a running cluster design")
    occasion = "cluster-" + fp.digest({"design": design, "cluster": fp.text(cluster)})[:32]
    withheld = holdout_io.assign_and_log(root, [row["lesson"]], occasion_id=occasion,
                                        rate=row["rate"], salt=row["salt"], context="cluster")
    return {"cluster": cluster, "occasion_id": occasion, "treated": row["lesson"] not in withheld,
            "eligible": eligible, "propensity": 1 - row["rate"], "design_hash": fp.digest(row)}


@contextmanager
def reader_scope(root: str, design: str, cluster: str, *, period: int | None = None) -> Iterator[dict[str, Any]]:
    assignment = assign_cluster(root, design, cluster, period=period)
    token = _READER.set((os.path.realpath(root), design, "treated" if assignment["treated"] else "holdout"))
    try:
        yield assignment
    finally:
        _READER.reset(token)


def active_context(root: str | None = None) -> tuple[str, str] | None:
    context = _READER.get()
    return (context[1], context[2]) if context and (root is None or context[0] == os.path.realpath(root)) else None


def read_allowed(root: str, identity: str) -> bool:
    """Write-path diversion: a holdout-arm reader never sees treated-arm derivations.

    A memory is hidden from a holdout reader of a design exactly when it, or
    any authenticated ancestor, was derived inside that design's treated arm;
    everything else (including memories written outside the design) stays
    visible to both arms, so diversion itself cannot create a contrast.
    """
    from commontrace import writer_holdout

    if not writer_holdout.read_allowed(root, identity):
        return False
    context = active_context(root)
    if context is None or context[1] == "treated":
        return True
    from commontrace import lineage, safety

    if identity in safety.protected(root):
        return True
    _design(root, context[0])
    for source in (identity, *lineage.ancestors(root, identity)):
        row = lineage.record(root, source)
        if row.get("experiment") == context[0] and row.get("arm") == "treated":
            return False
    return True


def symbiosis_bias(task_effect: float, cluster_effect: float, task_se: float, cluster_se: float,
                   *, tolerance: float = .1) -> dict[str, Any]:
    fp.number(task_effect, -1, 1)
    fp.number(cluster_effect, -1, 1)
    fp.number(task_se, 0, 10)
    fp.number(cluster_se, 0, 10)
    fp.number(tolerance, 0, 1)
    gap = abs(task_effect - cluster_effect)
    # Sum of SEs is conservative even when the two estimates are dependent.
    threshold = max(tolerance, 1.96 * (task_se + cluster_se))
    return {"check": "symbiosis_bias", "flagged": gap > threshold, "gap": gap,
            "threshold": threshold, "kind": "diagnostic heuristic, not a causal attribution",
            "scaled_gap": gap / max(math.ulp(1.0), task_se + cluster_se)}
