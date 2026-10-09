"""Conditional joint-policy evaluation; never multiply unknown marginal propensities.

The logged experiment randomizes exploration delivery conditional on its sampled
subset and baseline context. Changing that subset, ranker, prompt or baseline
requires a separate experiment with logged joint support.
"""
from __future__ import annotations

import hashlib
import math
import os

from commontrace import _jsonl, memory_authority, origin, paths


def digest(value) -> str:
    return hashlib.sha256(origin._bytes(value)).hexdigest()


def _path(root):
    return os.path.join(paths.memory_dir(root), "policy_events.jsonl")


def record_assignment(root: str, occasion: str, assignments: list[dict], *, baseline: list[dict], scopes: list[str]):
    if not assignments:
        return
    arms = [{k: a[k] for k in ("memory_id", "delivered", "treatment_probability", "source_sha256")}
            for a in assignments]
    record = {"id": occasion, "kind": "conditional-exploration", "arms": arms,
              "baseline": baseline, "scopes": scopes, "selection_condition": digest(assignments)}
    filename = _path(root)
    with _jsonl.locked(filename):
        prior = next((r for r in _jsonl.read_rows(filename) if r.get("id") == occasion), None)
        if prior:
            if {k: v for k, v in prior.items() if k not in ("origin", "sequence")} != record:
                raise ValueError("occasion context and joint assignment are immutable")
            return
        record["sequence"] = len(_jsonl.read_rows(filename))
        _jsonl.append_row(filename, {**record, "origin": memory_authority.bind(root, record)})


def records(root: str) -> list[dict]:
    rows = {}
    for row in _jsonl.read_rows(_path(root)):
        record = {k: v for k, v in row.items() if k != "origin"}
        if not memory_authority.verify(root, row.get("origin", {}), record):
            raise PermissionError("policy event origin is invalid")
        if row.get("event") == "outcome":
            if row["id"] not in rows:
                raise ValueError("policy outcome has no assignment")
            assignment = {k: v for k, v in rows[row["id"]].items() if k not in ("origin", "outcome")}
            if row.get("assignment_sha256") != digest(assignment):
                raise ValueError("policy outcome assignment binding is invalid")
            if rows[row["id"]]["outcome"] is not None:
                raise ValueError("duplicate policy outcome")
            rows[row["id"]]["outcome"] = row["outcome"]
        else:
            record = {k: v for k, v in row.items() if k != "origin"}
            if not memory_authority.verify(root, row.get("origin", {}), record):
                raise PermissionError("policy assignment origin is invalid")
            rows[row["id"]] = {**row, "outcome": None}
    return list(rows.values())


def outcome(root: str, occasion: str, value: float) -> bool:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 1):
        raise ValueError("policy outcome must be finite in [0,1]")
    with _jsonl.locked(_path(root)):
        row = next((r for r in records(root) if r["id"] == occasion), None)
        if row is None:
            return False
        if row["outcome"] is not None:
            if row["outcome"] != value:
                raise ValueError("policy outcome is immutable")
            return True
        event = {"event": "outcome", "id": occasion, "outcome": value,
                 "assignment_sha256": digest({k: v for k, v in row.items() if k not in ("origin", "outcome")})}
        _jsonl.append_row(_path(root), {**event, "origin": memory_authority.bind(root, event)})
    return True


def _probability(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 1):
        raise ValueError("candidate probabilities must be finite in [0,1]")
    return float(value)


def evaluate(root: str, candidate: dict, *, minimum_samples: int = 30, alpha: float | None = None) -> dict:
    if candidate.get("kind") != "exploration-delivery" or set(candidate) - {
            "kind", "default_probability", "probabilities", "predictions", "training_occasions"}:
        raise ValueError("only conditional exploration-delivery policies have logged joint support")
    if not isinstance(minimum_samples, int) or minimum_samples < 2 or (alpha is not None and not 0 < alpha < 1):
        raise ValueError("invalid policy evaluation precision")
    probabilities = candidate.get("probabilities", {})
    if not isinstance(probabilities, dict):
        raise ValueError("probabilities must map memory ids to delivery probabilities")
    default = _probability(candidate.get("default_probability", .5))
    probabilities = {k: _probability(v) for k, v in probabilities.items()}
    training_ids = candidate.get("training_occasions", [])
    if not isinstance(training_ids, list) or not all(isinstance(i, str) for i in training_ids):
        raise ValueError("training occasions must be a list of ids")
    training = set(training_ids)
    registrations = _jsonl.read_rows(os.path.join(paths.memory_dir(root), "policy-candidates.jsonl"))
    registered = None
    for row in registrations:
        if not memory_authority.verify(root, row.get("origin", {}), {k: v for k, v in row.items() if k != "origin"}):
            raise PermissionError("policy preregistration signature is invalid")
        if row["candidate_sha256"] == digest(candidate):
            registered = row
            break
    all_rows = records(root)
    rows = [r for r in all_rows if r["id"] not in training and registered
            and r["sequence"] >= registered["start_sequence"]]
    report = {"candidate_sha256": digest(candidate), "log_sha256": digest(rows),
              "estimand": "delivery conditional on sampled subset and unchanged baseline",
              "billing_eligible": False, "identified": False, "safe_to_release": False}
    if registered is None:
        return {**report, "reason": "freeze candidate before fresh independent evaluation traffic"}
    prefix = _jsonl.read_rows(_path(root))[:registered["start_sequence"]]
    if digest({"events": prefix}) != registered["prefix_sha256"]:
        raise PermissionError("policy evaluation journal prefix differs from preregistration")
    horizon = registered["evaluation_samples"]
    if alpha is not None and alpha != registered["alpha"]:
        raise ValueError("evaluation alpha differs from the preregistration")
    alpha = registered["alpha"]
    rows = rows[:horizon]
    report["log_sha256"] = digest(rows)
    report["evaluation_samples"] = horizon
    report["multiplicity"] = "Bonferroni .05/10 across at most ten registered policies per store"
    if len(rows) < horizon or any(r["outcome"] is None for r in rows):
        return {**report, "reason": "independent complete held-out outcomes required"}
    weights, rewards, limits, maxima, dr = [], [], [], [], []
    predictions = candidate.get("predictions", {})
    if not isinstance(predictions, dict):
        raise ValueError("predictions must map held-out occasions to joint outcome predictions")
    for row in rows:
        weight = low = high = 1.
        for arm in row["arms"]:
            p = _probability(arm["treatment_probability"])
            if not 0 < p < 1 or not isinstance(arm["delivered"], bool):
                raise ValueError("logged joint assignment has insufficient support")
            q = probabilities.get(arm["memory_id"], default)
            ratios = (q / p, (1 - q) / (1 - p))
            weight *= ratios[0 if arm["delivered"] else 1]
            low *= min(ratios)
            high *= max(ratios)
        if not math.isfinite(high) or high > 1e6:
            return {**report, "reason": "joint importance weights exceed supported precision"}
        maxima.append(high)
        weights.append(weight)
        rewards.append(row["outcome"])
        limits.append((min(0., low - 1), max(0., high - 1)))
        if row["id"] in predictions:
            model = predictions[row["id"]]
            ids = sorted(a["memory_id"] for a in row["arms"])
            if (not isinstance(model, dict) or set(model) != {"memory_ids", "values"}
                    or model["memory_ids"] != ids or len(ids) > 8):
                raise ValueError("DR requires one frozen complete joint-action prediction table")
            table = model["values"]
            keys = [format(i, "0"+str(len(ids))+"b") for i in range(2**len(ids))]
            if not isinstance(table, dict) or set(table) != set(keys):
                raise ValueError("DR joint-action model lacks complete support")
            table = {k: _probability(v) for k, v in table.items()}
            target = 0.
            for bits, value in table.items():
                probability = 1.
                for mid, bit in zip(ids, bits):
                    q = probabilities.get(mid, default)
                    probability *= q if bit == "1" else 1-q
                target += probability*value
            arm_map = {a["memory_id"]: a["delivered"] for a in row["arms"]}
            observed = table["".join("1" if arm_map[mid] else "0" for mid in ids)]
            dr.append(target + weight*(row["outcome"]-observed)-row["outcome"])
    total = sum(weights)
    ess = total ** 2 / sum(w * w for w in weights) if total else 0.
    if ess < minimum_samples:
        return {**report, "reason": "insufficient joint overlap/effective samples", "effective_samples": ess}
    n = len(rows)
    baseline = sum(rewards) / n
    ips = sum(w * y for w, y in zip(weights, rewards)) / n
    snips = sum(w * y for w, y in zip(weights, rewards)) / total
    effect = ips - baseline
    span = max(hi for _lo, hi in limits) - min(lo for lo, _hi in limits)
    radius = span * math.sqrt(math.log(2 / alpha) / (2 * n))
    bound = max(maxima)
    ips_radius = bound * math.sqrt(math.log(2 / alpha) / (2 * n))
    ratio_radius = bound * math.sqrt(math.log(4 / alpha) / (2 * n))
    numerator = sum(w * y for w, y in zip(weights, rewards)) / n
    denominator = total / n
    snips_ci = [max(0., (numerator-ratio_radius)/(denominator+ratio_radius)),
                min(1., (numerator+ratio_radius)/(denominator-ratio_radius))
                if denominator > ratio_radius else 1.]
    report.update(ips_ci=[max(0., min(1., ips-ips_radius)), max(0., min(1., ips+ips_radius))], snips_ci=snips_ci,
                  identified=True, observations=n, effective_samples=ess, baseline=baseline,
                  ips=ips, snips=snips, effect=effect, ci_low=effect-radius, ci_high=effect+radius,
                  interval="fixed-time Hoeffding, independent occasions, bounded rewards",
                  safe_to_release=effect-radius >= -1e-12)
    if len(dr) == n and training:
        report["dr"] = baseline + sum(dr) / n
        dr_radius = (1+2*bound) * math.sqrt(math.log(2/alpha)/(2*n))
        report["dr_ci"] = [max(0., report["dr"]-dr_radius), min(1., report["dr"]+dr_radius)]
    else:
        report["dr"] = None
        report["dr_reason"] = "disjoint training ids and complete external joint-outcome predictions required"
    return report


def train(root: str, *, minimum_cases: int = 30) -> dict:
    """Optional Bayesian delivery policy learned from instrumented reward data."""
    from commontrace import causal_policy

    rows = causal_policy.reward_dataset(root)
    groups = {}
    for row in rows:
        arms = groups.setdefault(row["memory_id"], {True: [1., 1.], False: [1., 1.]})
        arm = arms[row["delivered"]]
        arm[0] += row["reward"]
        arm[1] += 1 - row["reward"]
    probabilities = {}
    for mid, arms in groups.items():
        if min(sum(v) - 2 for v in arms.values()) < minimum_cases:
            continue
        effect = arms[True][0]/sum(arms[True]) - arms[False][0]/sum(arms[False])
        probabilities[mid] = min(.9, max(.1, .5 + effect))
    return {"kind": "exploration-delivery", "default_probability": .5, "probabilities": probabilities,
            "training_occasions": sorted({r["occasion_id"] for r in rows})}


def install(root: str, candidate: dict, *, minimum_samples: int = 30) -> dict:
    if memory_authority.WRITER.get()[1] != "operator":
        raise PermissionError("policy changes require an operator")
    for probability in [candidate.get("default_probability", .5), *candidate.get("probabilities", {}).values()]:
        if not 0 < _probability(probability) < 1:
            raise ValueError("installed delivery probabilities require strict full support")
    report = evaluate(root, candidate, minimum_samples=minimum_samples)
    if not report["safe_to_release"]:
        raise ValueError("candidate policy lacks a nonnegative held-out lower bound")
    record = {"id": "exploration-policy", "candidate": candidate, "report": report}
    _jsonl.write_json(os.path.join(paths.memory_dir(root), "exploration-policy.json"),
                      {**record, "origin": memory_authority.bind(root, record)})
    return report


def delivery_probabilities(root: str) -> dict:
    filename = os.path.join(paths.memory_dir(root), "exploration-policy.json")
    if not os.path.isfile(filename):
        return {}
    import json

    with open(filename, encoding="utf-8") as fh:
        row = json.load(fh)
    record = {k: v for k, v in row.items() if k != "origin"}
    if not memory_authority.verify(root, row.get("origin", {}), record):
        raise PermissionError("installed delivery policy signature is invalid")
    return row["candidate"]


def preregister(root: str, candidate: dict, *, evaluation_samples: int = 300, alpha: float = .05) -> dict:
    if memory_authority.WRITER.get()[1] != "operator":
        raise PermissionError("policy preregistration requires an operator")
    if (isinstance(evaluation_samples, bool) or not isinstance(evaluation_samples, int)
            or not 30 <= evaluation_samples <= 1000000 or not 0 < alpha < 1):
        raise ValueError("policy evaluation requires a fixed horizon and alpha")
    # Validate configuration even before data exist.
    evaluate(root, candidate)
    path = os.path.join(paths.memory_dir(root), "policy-candidates.jsonl")
    with _jsonl.locked(_path(root)), _jsonl.locked(path):
        prior = next((r for r in _jsonl.read_rows(path) if r["candidate_sha256"] == digest(candidate)), None)
        if prior:
            return prior
        if alpha != .05:
            raise ValueError("the store policy family uses a fixed .05 error budget")
        if len(_jsonl.read_rows(path)) >= 10:
            raise ValueError("preregistered ten-policy family budget exhausted")
        events = _jsonl.read_rows(_path(root))
        if candidate.get("predictions"):
            complete_ids = {r["id"] for r in records(root) if r["outcome"] is not None}
            training = set(candidate.get("training_occasions", []))
            if not training or not training <= complete_ids:
                raise ValueError("DR model fit requires signed complete pre-registration training occasions")
        record = {"id": "candidate:"+digest(candidate), "candidate_sha256": digest(candidate),
                  "candidate": candidate, "evaluation_samples": evaluation_samples, "alpha": alpha/10,
                  "start_sequence": len(events), "prefix_sha256": digest({"events": events})}
        row = {**record, "origin": memory_authority.bind(root, record)}
        _jsonl.append_row(path, row)
        return row
