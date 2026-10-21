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

import copy
import math
import os
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Sequence

from unimem import _jsonl
from unimem import frontier_protocol as fp

THRESHOLDS = tuple(i / 100 for i in range(101)) + (1.01,)
GATE = "harm-gate.json"
MAX_WINDOWS = 64


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


def admit(calibration: dict[str, Any], score: float, *, context: str = "", hazard: bool = False,
          scorer: str | None = None) -> bool:
    """Admit a discretionary memory iff its score clears its stratum's threshold.

    Unknown strata abstain. When the calibration recorded the scorer whose scale
    produced its scores, a caller passing a different scorer abstains too: a
    threshold is meaningless on another relevance scale. Hazards always pass.
    """
    fp.number(score, 0, 1, "score")
    if hazard:
        return True
    scorers = calibration.get("scorers") or []
    if scorer is not None and scorers and scorer not in scorers:
        return False
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


def _load(root: str) -> dict[str, Any] | None:
    import json

    try:
        with open(fp.filename(root, GATE), "rb") as stream:
            data = stream.read(fp.MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(data) > fp.MAX_BYTES:
        raise ValueError("harm gate configuration exceeds 1 MiB")
    config = json.loads(data)
    if not isinstance(config, dict):
        raise ValueError("harm gate configuration must be an object")
    return config


def _gated(moment: Any, windows: Sequence[Sequence[Any]]) -> bool:
    from datetime import datetime

    if moment is None or not windows:
        return False
    when = moment if isinstance(moment, datetime) else datetime.fromisoformat(fp.moment(str(moment)))
    if when.tzinfo is None:
        return True  # An unzoned legacy timestamp cannot be placed outside a gated window.
    return any(datetime.fromisoformat(str(start)) <= when and (end is None or when < datetime.fromisoformat(str(end)))
               for start, end in windows)


def calibration_rows(root: str, *, windows: Sequence[Sequence[Any]] = (),
                     scorer: str | None = None) -> tuple[list[Calibration], list[str]]:
    """Single-memory randomized occasions logged while no harm gate filtered admission.

    Rows logged inside a gated window were admitted *because* they cleared the
    threshold then in force, so they under-represent low scores; using them would
    bias the loss of every lower threshold downward. They are excluded. Rows are
    restricted to one scorer label, the relevance scale the threshold is set on.
    """
    from unimem import holdout_io
    from unimem.commands.experiment_cmd import _load as load_assignments

    assignments = load_assignments(root)[0]
    # A shared outcome cannot be treated as independent calibration replicates.
    # Bundled policies need their own task-level randomized calibration trial.
    counts: dict[str, int] = {}
    for row in assignments:
        counts[row.occasion_id] = counts.get(row.occasion_id, 0) + 1
    eligible = [row for row in assignments if row.succeeded is not None
                and 0 < row.rate <= .99 and counts[row.occasion_id] == 1 and not _gated(row.at, windows)]
    labels: dict[str, int] = {}
    for row in eligible:
        labels[row.scorer or ""] = labels.get(row.scorer or "", 0) + 1
    if scorer is None:
        scorer = max(sorted(labels), key=lambda label: labels[label], default="")
    chosen = [row for row in eligible if (row.scorer or "") == scorer]
    scored = [row for row in chosen if row.relevance is not None]
    if not scored and chosen:
        # Provider policies (`CausalMemory`) log no comparable relevance and are
        # admitted at one fixed score; calibrate that policy as a whole.
        rows = [Calibration(.5, row.injected, not row.succeeded, row.rate,
                            holdout_io.context_label(row.context or "")) for row in chosen]
    else:
        rows = [Calibration(_score(float(row.relevance or 0.0)), row.injected, not row.succeeded, row.rate,
                            holdout_io.context_label(row.context or "")) for row in scored]
    return rows, [scorer] if rows else []


def _score(value: float) -> float:
    return score(value) if math.isfinite(value) else 0.0


def configure(root: str, *, alpha: float, per_context: bool = False, propensity_floor: float = .01,
              method: str = "crc", scorer: str | None = None) -> dict[str, Any]:
    """Enable (or recalibrate) the gate from ungated randomized evidence.

    The calibration is computed here, once, and snapshotted: admission on the
    retrieval hot path only reads it. Recalibrate by running this again.
    """
    fp.number(alpha, 1e-6, .99)
    fp.number(propensity_floor, .01, 1)
    if scorer is not None:
        fp.text(scorer, "scorer label", 256)
    with _jsonl.locked(fp.filename(root, GATE)):
        prior = _load(root) or {}
        windows = [list(window) for window in prior.get("gated_windows", [])]
        rows, scorers = calibration_rows(root, windows=windows, scorer=scorer)
        if not windows or windows[-1][1] is not None:
            fp.count(len(windows), 0, MAX_WINDOWS - 1, "gated windows")
            windows.append([fp.now(), None])
        calibration = calibrate(rows, alpha=alpha, per_context=per_context, method=method,
                                propensity_floor=propensity_floor)
        config = {"enabled": True, "alpha": alpha, "per_context": per_context, "method": method,
                  "propensity_floor": propensity_floor, "gated_windows": windows, "calibrated_at": fp.now(),
                  "calibration_n": len(rows), "scorers": scorers, "calibration": calibration}
        _jsonl.write_json(fp.filename(root, GATE), config)
    with fp._GUARD:
        _CURRENT.pop(os.path.realpath(root), None)
    return current(root)


def disable(root: str) -> dict[str, Any]:
    """Stop gating; rows logged from now on are ungated calibration evidence again."""
    with _jsonl.locked(fp.filename(root, GATE)):
        prior = _load(root)
        if prior is None or not prior.get("enabled"):
            return {"enabled": False}
        windows = [list(window) for window in prior.get("gated_windows", [])]
        if windows and windows[-1][1] is None:
            windows[-1][1] = fp.now()
        _jsonl.write_json(fp.filename(root, GATE), {"enabled": False, "gated_windows": windows})
    with fp._GUARD:
        _CURRENT.pop(os.path.realpath(root), None)
    return {"enabled": False, "gated_windows": windows}


_CURRENT: OrderedDict[str, tuple[Any, dict[str, Any]]] = OrderedDict()


def _after_fork() -> None:
    _CURRENT.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def current(root: str) -> dict[str, Any]:
    """The snapshotted gate, read once per file change (constant work per retrieval)."""
    path = fp.filename(root, GATE)
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return {"enabled": False}
    stamp = (info.st_ino, info.st_mtime_ns, info.st_size)
    key = os.path.realpath(root)
    with fp._GUARD:
        cached = _CURRENT.get(key)
        if cached is not None and cached[0] == stamp:
            _CURRENT.move_to_end(key)
            return copy.deepcopy(cached[1])
        config = _load(root) or {}
        if not config.get("enabled"):
            result: dict[str, Any] = {"enabled": False}
        elif "calibration" not in config:
            # A pre-snapshot configuration: refuse to guess a threshold on the hot path.
            result = {"enabled": True, "per_context": bool(config.get("per_context")), "strata": {},
                      "alpha": config.get("alpha"), "note": "recalibrate with `unimem retrieval --harm-budget`"}
        else:
            result = {"enabled": True, **config["calibration"], "scorers": config.get("scorers", []),
                      "calibrated_at": config.get("calibrated_at"), "calibration_n": config.get("calibration_n", 0)}
        _CURRENT[key] = (stamp, result)
        _CURRENT.move_to_end(key)
        while len(_CURRENT) > 64:
            _CURRENT.popitem(last=False)
        return copy.deepcopy(result)


def summary(gate: dict[str, Any], context: str = "") -> dict[str, Any]:
    """Compact per-response metadata: the threshold in force here, not the whole calibration."""
    if not gate.get("enabled"):
        return {"enabled": False}
    key = context if gate.get("per_context") else ""
    stratum = gate.get("strata", {}).get(key, {})
    return {"enabled": True, "alpha": gate.get("alpha"), "kind": gate.get("kind"),
            "per_context": bool(gate.get("per_context")), "context": key,
            "threshold": stratum.get("threshold", 1.01), "abstains": stratum.get("threshold", 1.01) > 1,
            "scorers": gate.get("scorers", []), "calibrated_at": gate.get("calibrated_at"),
            "hazards": "exempt from discretionary budget"}


def report(root: str) -> dict[str, Any]:
    """The calibrated thresholds plus the realized IPW injected-failure rate since gating began.

    The realized rate covers single-memory randomized occasions admitted while
    the gate was on; it is an estimate of the bounded loss, not of individual harm.
    """
    gate = current(root)
    if not gate.get("enabled"):
        return gate
    from unimem.commands.experiment_cmd import _load as load_assignments

    config = _load(root) or {}
    windows = config.get("gated_windows", [])
    assignments = load_assignments(root)[0]
    counts: dict[str, int] = {}
    for row in assignments:
        counts[row.occasion_id] = counts.get(row.occasion_id, 0) + 1
    gated = [row for row in assignments if row.succeeded is not None and 0 < row.rate < 1
             and counts[row.occasion_id] == 1 and _gated(row.at, windows)]
    loss = math.fsum(int(row.injected and not row.succeeded) / (1 - row.rate) for row in gated)
    gate["realized"] = {"n": len(gated), "ipw_injected_failure": loss / len(gated) if gated else None,
                        "kind": "realized estimate on gated occasions; not a guarantee"}
    return gate
