"""Conservative conformal harm control for optional memory injection.

Angelopoulos et al. CRC (arXiv:2208.02814); Learn-then-Test
(arXiv:2110.01052); conditional coverage limitations (arXiv:2305.12616);
adaptive conformal updates (arXiv:2402.01139). KnowNo-style action sets use
ordinary split conformal coverage, not a claim of physical safety.

Individual counterfactual harm is not identified by parallel-arm trials. We
upper-bound it by injected-arm failure, IPW-weighted by exact propensities.
CRC controls expected marginal loss over calibration and one fresh task under
exchangeability, bounded loss, consistent delivery, and no interference. It
does not guarantee the realized fraction on every finite stream. LTT gives a
simultaneous high-probability bound for a fixed finite threshold/metric family.
Per-context control requires exchangeability within each preregistered stratum.
Online adaptation under arbitrary drift is a heuristic, explicitly labelled.
Protected hazards bypass this discretionary-memory budget.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

from commontrace import _jsonl
from commontrace import frontier_protocol as fp

THRESHOLDS = tuple(i / 20 for i in range(21)) + (1.01,)


def score(value: float) -> float:
    """One fixed bounded transform, used identically at logging and admission."""
    fp.number(value, -1e6, 1e6, "retrieval relevance")
    return .5 + .5 * value / (1 + abs(value))


@dataclass(frozen=True)
class Calibration:
    score: float
    injected: bool
    failed: bool
    rate: float
    context: str = ""
    metric: str = "failure"

    def __post_init__(self) -> None:
        fp.number(self.score, 0, 1, "pre-treatment score")
        fp.number(self.rate, 0, .99, "holdout propensity")
        if type(self.injected) is not bool or type(self.failed) is not bool:
            raise ValueError("calibration assignment and loss must be binary")
        if len(self.context) > 128 or len(self.metric) > 128:
            raise ValueError("calibration label exceeds 128 characters")


def calibrate(rows: Sequence[Calibration], *, alpha: float = .02, beta: float = .05,
              per_context: bool = False, method: str = "crc", propensity_floor: float = .01) -> dict[str, Any]:
    fp.number(alpha, 1e-6, .99, "harm budget")
    fp.number(beta, 1e-6, .99, "failure probability")
    fp.count(len(rows), 0, fp.MAX_ROWS)
    fp.number(propensity_floor, .01, 1, "preregistered injection-propensity floor")
    if any(1 - row.rate < propensity_floor - 1e-12 for row in rows):
        raise ValueError("calibration violates the declared propensity floor")
    if method not in ("crc", "ltt"):
        raise ValueError("method must be crc or ltt")
    contexts = sorted({row.context if per_context else "" for row in rows}) or [""]
    metrics = sorted({row.metric for row in rows}) or ["failure"]
    fp.count(len(contexts) * len(metrics), 1, 256, "strata times metrics")
    result = {}
    for context in contexts:
        groups = [[row for row in rows if (not per_context or row.context == context) and row.metric == metric]
                  for metric in metrics]
        threshold, risks = 1.01, {}
        for candidate in THRESHOLDS:
            estimates = {}
            valid = True
            for metric, group in zip(metrics, groups):
                # This must be fixed by the design, not the sample minimum.
                bound = 1 / (propensity_floor - 1e-12)
                loss = math.fsum(int(row.injected and row.failed and row.score >= candidate) / (1 - row.rate)
                                 for row in group)
                n = len(group)
                # The reject-all rule has known zero discretionary loss.
                upper = 0.0 if candidate > 1 else (loss + bound) / (n + 1)
                if method == "ltt" and candidate <= 1:
                    correction = bound * math.sqrt(math.log(len(THRESHOLDS) * len(contexts) * len(metrics) / beta)
                                                    / (2 * max(n, 1)))
                    upper = loss / max(n, 1) + correction
                estimates[metric] = {"n": n, "observed_ipw_failure": loss / n if n else None,
                                     "risk_bound": min(1.0, upper), "loss_bound": bound}
                valid &= bool(n and upper <= alpha) or candidate > 1
            if valid:
                threshold, risks = candidate, estimates
                break
        result[context] = {"threshold": threshold, "metrics": risks}
    return {"alpha": alpha, "beta": beta, "method": method, "per_context": per_context, "strata": result,
            "propensity_floor": propensity_floor,
            "kind": "expected marginal guarantee" if method == "crc" else "high-probability simultaneous bound",
            "loss": "IPW injected failure; conservative upper bound on individual harm",
            "assumptions": "exchangeable calibration/future tasks within strata, exact propensities, no interference",
            "online_update": "heuristic under drift", "hazards": "exempt from discretionary budget"}


def admit(calibration: dict[str, Any], score: float, *, context: str = "", hazard: bool = False) -> bool:
    fp.number(score, 0, 1, "score")
    if hazard:
        return True
    key = context if calibration["per_context"] else ""
    threshold = calibration["strata"].get(key, {}).get("threshold", 1.01)
    fp.number(threshold, 0, 1.01, "calibrated threshold")
    return bool(score >= threshold)


@dataclass
class TrajectoryAlarm:
    """Mixture of predictable restart e-processes; bounded O(1) work per step."""
    alpha: float = .02
    beta: float = .05
    steps: int = 0
    wealth: float = 0
    allocated: float = 0

    def __post_init__(self) -> None:
        fp.number(self.alpha, 1e-6, .99)
        fp.number(self.beta, 1e-6, .99)

    def update(self, loss: float) -> dict[str, Any]:
        fp.number(loss, 0, 1, "step loss")
        fp.count(self.steps, 0, 9999, "episode step")
        self.steps += 1
        weight = 6 / (math.pi * math.pi * self.steps * self.steps)
        self.allocated += weight
        self.wealth = min(1e300, (self.wealth + weight) * (1 + .5 / self.alpha * (loss - self.alpha)))
        evidence = self.wealth + max(0, 1 - self.allocated)
        return {"halt": evidence >= 1 / self.beta, "e_value": evidence, "step": self.steps,
                "kind": "anytime alarm under conditional step risk <= alpha"}


def online_threshold(threshold: float, loss: float, *, step: int, alpha: float) -> dict[str, Any]:
    fp.number(threshold, 0, 1)
    fp.number(loss, 0, 1)
    fp.number(alpha, 0, 1)
    fp.count(step, 1, 1_000_000)
    return {"threshold": max(0, min(1, threshold + (loss - alpha) / math.sqrt(step))),
            "kind": "heuristic drift adaptation; recalibrate before claiming a guarantee"}


def action_set(probabilities: dict[str, float], calibration_scores: Sequence[float], *,
               alpha: float = .05) -> dict[str, Any]:
    fp.count(len(probabilities), 1, 128)
    fp.count(len(calibration_scores), 1, fp.MAX_ROWS)
    fp.number(alpha, 1e-6, .99)
    for action, probability in probabilities.items():
        fp.text(action)
        fp.number(probability, 0, 1)
    if not math.isclose(sum(probabilities.values()), 1, abs_tol=1e-6):
        raise ValueError("action probabilities must sum to one")
    scores = sorted(fp.number(score, 0, 1, "nonconformity score") for score in calibration_scores)
    rank = math.ceil((len(scores) + 1) * (1 - alpha))
    threshold = scores[rank - 1] if rank <= len(scores) else 1.0
    actions = sorted(action for action, probability in probabilities.items() if 1 - probability <= threshold)
    return {"actions": actions, "ask_human": len(actions) != 1, "coverage": 1 - alpha,
            "assumptions": "exchangeable held-out true-action scores and future action"}


def configure(root: str, *, alpha: float, per_context: bool = False,
              propensity_floor: float = .01) -> dict[str, Any]:
    fp.number(alpha, 1e-6, .99)
    fp.number(propensity_floor, .01, 1)
    config = {"alpha": alpha, "per_context": per_context, "propensity_floor": propensity_floor}
    _jsonl.write_json(fp.filename(root, "harm-gate.json"), config)
    return current(root)


def current(root: str) -> dict[str, Any]:
    import json

    try:
        with open(fp.filename(root, "harm-gate.json"), "rb") as stream:
            data = stream.read(4097)
        if len(data) > 4096:
            raise ValueError("harm gate config exceeds 4 KiB")
        config = json.loads(data)
    except FileNotFoundError:
        return {"enabled": False}
    from commontrace.commands.experiment_cmd import _load

    assignments = _load(root)[0]
    # A shared outcome cannot be treated as independent calibration replicates.
    # Bundled policies need their own task-level randomized calibration trial.
    counts: dict[str, int] = {}
    for row in assignments:
        counts[row.occasion_id] = counts.get(row.occasion_id, 0) + 1
    rows = [Calibration(score(row.relevance), row.injected, not row.succeeded, row.rate, row.context or "")
            for row in assignments if row.succeeded is not None and row.relevance is not None
            and 0 < row.rate <= .99 and counts[row.occasion_id] == 1]
    return {"enabled": True, **calibrate(rows, alpha=config["alpha"], per_context=config["per_context"],
                                         propensity_floor=config.get("propensity_floor", .01))}
