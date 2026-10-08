"""Exploration with known inclusion propensities and self-normalised IPW.

Selection alone cannot identify a treatment effect. Each explored memory is
independently randomised into delivery/control; missing outcomes invalidate an
estimate. These experimental estimates are never accepted as billing proof.
"""
from __future__ import annotations

import hashlib
import math
import os
import random
from collections.abc import Callable, Sequence

from commontrace import _jsonl, paths


def explore(eligible: Sequence[str], ranked: Sequence[str], *, slots: int, seed: int,
            treatment_probability: float = 0.5) -> list[dict]:
    if not isinstance(slots, int) or isinstance(slots, bool) or slots < 0:
        raise ValueError("slots must be a nonnegative integer")
    if not math.isfinite(treatment_probability) or not 0 < treatment_probability < 1:
        raise ValueError("treatment probability must be strictly between 0 and 1")
    pool = sorted(set(eligible) - set(ranked))
    k = min(slots, len(pool))
    rng = random.Random(seed)
    return [{"memory_id": mid, "selection_probability": k / len(pool),
             "treatment_probability": treatment_probability,
             "delivered": rng.random() < treatment_probability}
            for mid in rng.sample(pool, k)]


def snipw(rows: Sequence[dict]) -> dict:
    arms: dict[bool, list[tuple[float, float]]] = {True: [], False: []}
    for row in rows:
        if not isinstance(row.get("delivered"), bool):
            raise ValueError("delivery arm must be boolean")
        if row.get("outcome") is None:
            return {"identified": False, "reason": "missing outcomes", "effect": None}
        pi, p, outcome = (float(row[key]) for key in
                          ("selection_probability", "treatment_probability", "outcome"))
        if not all(math.isfinite(v) for v in (pi, p, outcome)) or not 0 < pi <= 1 or not 0 < p < 1:
            raise ValueError("invalid propensities or outcome")
        if not 0 <= outcome <= 1:
            raise ValueError("outcome must be in [0, 1]")
        delivered = row["delivered"]
        weight = 1 / (pi * (p if delivered else 1 - p))
        arms[delivered].append((weight, outcome))
    if not all(arms.values()):
        return {"identified": False, "reason": "both randomised arms required", "effect": None}
    means = {arm: sum(w * y for w, y in values) / sum(w for w, _ in values)
             for arm, values in arms.items()}
    ess = {str(arm): sum(w for w, _ in values) ** 2 / sum(w * w for w, _ in values)
           for arm, values in arms.items()}
    return {"identified": True, "effect": means[True] - means[False],
            "treated": means[True], "control": means[False], "effective_sample_size": ess,
            "billing_eligible": False, "rows": len(rows)}


def record(root: str, occasion_id: str, assignments: list[dict], outcome: float | None = None) -> None:
    if not isinstance(occasion_id, str) or not 1 <= len(occasion_id) <= 256:
        raise ValueError("occasion_id must be bounded nonempty text")
    if outcome is not None and (not math.isfinite(outcome) or not 0 <= outcome <= 1):
        raise ValueError("outcome must be finite in [0, 1]")
    if len({a.get("memory_id") for a in assignments}) != len(assignments):
        raise ValueError("assignment memory ids must be unique")
    path = os.path.join(paths.memory_dir(root), "exploration.jsonl")
    with _jsonl.locked(path):
        prior = {(r["occasion_id"], r["memory_id"]): r for r in read(root)}
        pending = []
        for assignment in assignments:
            if not isinstance(assignment.get("memory_id"), str) or not assignment["memory_id"]:
                raise ValueError("assignments require memory ids")
            # Validate before any append; no partially recorded assignment batch.
            snipw([{**assignment, "outcome": outcome if outcome is not None else 0}])
            row = {**assignment, "occasion_id": occasion_id, "outcome": outcome}
            key = (occasion_id, row["memory_id"])
            if key in prior:
                before = prior[key]
                if {k: v for k, v in before.items() if k != "outcome"} != {
                        k: v for k, v in row.items() if k != "outcome"}:
                    raise ValueError("occasion assignment is immutable")
                if outcome is not None:
                    if before["outcome"] is not None and before["outcome"] != outcome:
                        raise ValueError("recorded outcome is immutable")
                    if before["outcome"] is None:
                        pending.append({"event": "outcome", "occasion_id": occasion_id,
                                        "memory_id": row["memory_id"], "outcome": outcome})
            else:
                pending.append(row)
        for row in pending:
            _jsonl.append_row(path, row)


def record_outcome(root: str, occasion_id: str, outcome: float) -> bool:
    assignments = [{k: v for k, v in row.items() if k not in ("occasion_id", "outcome")}
                   for row in read(root) if row["occasion_id"] == occasion_id]
    if not assignments:
        return False
    record(root, occasion_id, assignments, outcome)
    return True


def read(root: str) -> list[dict]:
    """Project immutable assignment and eventual outcome events."""
    path = os.path.join(paths.memory_dir(root), "exploration.jsonl")
    rows = {}
    for row in _jsonl.read_rows(path):
        key = (row["occasion_id"], row["memory_id"])
        if row.get("event") == "outcome":
            if key not in rows:
                raise ValueError("outcome has no assignment")
            rows[key] = {**rows[key], "outcome": row["outcome"]}
        else:
            rows[key] = row
    return list(rows.values())


def ablation_probe(memory: Sequence[str], answer: Callable[[Sequence[str]], float], *,
                   seed: int, probes: int = 8) -> list[dict]:
    """Offline deletion-and-reanswer credits, paired with their raw baseline.

    The evaluator must score externally; these local probes are sensitivity
    evidence, not a replacement for randomised live-outcome measurement.
    """
    rng = random.Random(seed)
    full = float(answer(memory))
    results = []
    for idx in rng.sample(range(len(memory)), min(max(0, probes), len(memory))):
        score = float(answer([text for i, text in enumerate(memory) if i != idx]))
        results.append({"index": idx, "credit": full - score, "full": full, "ablated": score,
                        "memory_digest": hashlib.sha256(memory[idx].encode()).hexdigest()})
    return results


def attribute_failure(*, plan_valid: bool | None, execution_valid: bool | None) -> str:
    if plan_valid is False:
        return "plan"
    if plan_valid is True and execution_valid is False:
        return "execution"
    return "unidentified"


def utility_priors(root: str, *, minimum_rows: int = 30) -> dict[str, float]:
    """Use only complete, both-arm exploration estimates with adequate support."""
    groups: dict[str, list[dict]] = {}
    for row in read(root):
        groups.setdefault(row["memory_id"], []).append(row)
    result = {}
    for memory_id, rows in groups.items():
        if len(rows) < minimum_rows:
            continue
        estimate = snipw(rows)
        if estimate["identified"] and min(estimate["effective_sample_size"].values()) >= minimum_rows / 2:
            result[memory_id] = estimate["effect"]
    return result


def reward_dataset(root: str) -> list[dict]:
    """Optional learned policies train on observed randomised outcomes, not votes."""
    rows = read(root)
    return [{**row, "reward": row["outcome"], "billing_eligible": False}
            for row in rows if row["outcome"] is not None]
