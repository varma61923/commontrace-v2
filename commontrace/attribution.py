"""Budgeted ContextCite-style randomized episode ablations (arXiv:2409.00729).

Sparse coordinate-descent surrogates are diagnostic approximations, not observed
causal effects of a live robot action. Replay may use a digital twin. The caller
must supply an isolated runner and explicitly attest the execution environment.
Only the canonical holdout analysis may issue a causal release verdict.
"""
from __future__ import annotations

import math
import random
from typing import Any, Callable, Sequence

from commontrace import frontier_protocol as fp
from commontrace import transparency_log as log


def sparse_fit(masks: Sequence[Sequence[bool]], outcomes: Sequence[float], *, penalty: float = .01) -> list[float]:
    n = fp.count(len(masks), 4, 64)
    d = fp.count(len(masks[0]), 1, 32)
    fp.number(penalty, 0, 1)
    if len(outcomes) != n or any(len(row) != d or any(type(x) is not bool for x in row) for row in masks):
        raise ValueError("bounded binary ablation matrix and one outcome per mask required")
    for y in outcomes:
        fp.number(y, 0, 1)
    xs = [[int(masks[i][j]) - sum(int(row[j]) for row in masks) / n for i in range(n)] for j in range(d)]
    residuals = [y - sum(outcomes) / n for y in outcomes]
    weights = [0.0] * d
    for _ in range(100):
        for j in range(d):
            column = xs[j]
            variance = sum(x*x for x in column)
            if variance == 0:
                continue
            for i in range(n):
                residuals[i] += column[i] * weights[j]
            cov = sum(column[i] * residuals[i] for i in range(n))
            weights[j] = math.copysign(max(0, abs(cov) - penalty * n), cov) / variance
            for i in range(n):
                residuals[i] -= column[i] * weights[j]
    return weights


def diagnose(root: str, episode: str, memories: Sequence[str], runner: Callable[[tuple[str, ...]], float], *,
             seed: int, budget_cents: int, cost_per_run_cents: int, ablations: int = 32,
             sample_probability: float = .1, environment: str = "digital-twin") -> dict[str, Any]:
    ids = tuple(dict.fromkeys(fp.text(x) for x in memories))
    fp.count(len(ids), 1, 32)
    fp.text(episode)
    fp.count(seed, 0, 2**63 - 1)
    fp.count(ablations, 4, 64)
    fp.count(cost_per_run_cents, 1, 100_000)
    fp.count(budget_cents, 0, 1_000_000)
    fp.number(sample_probability, 0, 1)
    if environment not in ("digital-twin", "isolated-agent", "operator-approved-robot"):
        raise ValueError("declare an isolated replay environment")
    rng = random.Random(seed)
    if rng.random() >= sample_probability:
        return {"sampled": False, "cost_cents": 0}
    total = (ablations + 1) * cost_per_run_cents
    if total > budget_cents:
        raise ValueError("attribution replay exceeds the configured cost cap")
    fp.append(root, "attribution-costs.jsonl", {"episode": episode, "reserved_cents": total,
                                               "seed": seed, "environment": environment})
    masks = [tuple(rng.random() < .5 for _ in ids) for _ in range(ablations)]
    outcomes = [fp.number(runner(tuple(x for x, included in zip(ids, mask) if included)), 0, 1)
                for mask in masks]
    baseline = fp.number(runner(ids), 0, 1)
    coefficients = sparse_fit(masks, outcomes)
    regret = max(0, max(outcomes) - baseline)
    body = {"episode": episode, "memories": list(ids), "attribution": dict(zip(ids, coefficients)),
            "counterfactual_regret": regret, "poisoning_severity": regret,
            "n": ablations, "cost_cents": total, "sampled": True,
            "environment": environment, "seed": seed, "mask_digest": fp.digest(masks),
            "step_scope": "episode summary; use separate step runners for step attribution",
            "kind": "sparse counterfactual diagnostic; not a release certificate"}
    signed = fp.append(root, "attribution-signals.jsonl", body)
    return {**body, "anchor": log.append(root, "counterfactual-attribution", signed)}


def restore_audit(root: str, eviction: str, memories: Sequence[str], runner: Callable[[tuple[str, ...]], float],
                  **options: Any) -> dict[str, Any]:
    """Restore the evicted item only inside an isolated ablation replay."""
    result = diagnose(root, "restore:" + fp.text(eviction), [*memories, eviction], runner, **options)
    return {**result, "restore_recommended": result.get("attribution", {}).get(eviction, 0) > .05}


def gate_adjustment(root: str, memory: str) -> dict[str, Any]:
    """Bounded diagnostic input for operator review; cannot manufacture HELPS/HURTS."""
    signals = [r["attribution"][memory] for r in fp.journal(root, "attribution-signals.jsonl")
               if memory in r["attribution"]]
    estimate = sum(signals) / len(signals) if signals else 0
    return {"memory": memory, "diagnostic_score": estimate, "n": len(signals),
            "review_suggested": estimate < -.05, "automatic_causal_verdict": False}
