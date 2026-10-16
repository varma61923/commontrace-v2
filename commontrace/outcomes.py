"""Surrogate outcomes, prediction-powered labels and explicit sim/real calibration.

Athey et al. surrogate indices (arXiv:1603.09326, 2309.07893);
PPI/PPI++ (arXiv:2301.09633, 2311.01453); active gold labels
(arXiv:2403.03208). Gold residual correction is unbiased with exact nonzero
gold-inclusion probabilities. The conservative finite-population interval assumes
independent Bernoulli gold sampling, conditional on all outcomes/predictions.
Surrogacy and sim-to-real trust weights are validated estimates/heuristics, not
guarantees that an early signal preserves a treatment's long-term causal effect.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

from commontrace import frontier_protocol as fp


def prediction_powered(predictions: Sequence[float], gold: dict[int, float], probabilities: Sequence[float], *,
                        alpha: float = .05, low: float = 0, high: float = 1) -> dict[str, Any]:
    n = fp.count(len(predictions), 1, fp.MAX_ROWS)
    if len(probabilities) != n or not low < high:
        raise ValueError("one gold-inclusion propensity per prediction and a positive outcome range required")
    fp.number(low, -1000, 1000)
    fp.number(high, -1000, 1000)
    fp.number(alpha, 1e-6, .99)
    for value in predictions:
        fp.number(value, low, high)
    for probability in probabilities:
        fp.number(probability, .001, 1, "gold-inclusion propensity")
    correction = 0.0
    for index, actual in gold.items():
        fp.count(index, 0, n - 1, "gold index")
        fp.number(actual, low, high, "gold outcome")
        correction += (actual - predictions[index]) / probabilities[index]
    naive = math.fsum(predictions) / n
    estimate = naive + correction / n
    radius = (high - low) * math.sqrt(.5 * math.log(2 / alpha) *
                                     math.fsum(1 / p**2 for p in probabilities)) / n
    return {"estimate": estimate, "naive": naive, "gold_n": len(gold), "n": n,
            "interval": [max(low, estimate - radius), min(high, estimate + radius)],
            "kind": "unbiased finite-population estimate under exact independent Bernoulli gold sampling"}


def corrected_effect(rows: Sequence[dict[str, Any]], *, alpha: float = .05,
                     propensity_floor: float = .01) -> dict[str, Any]:
    fp.count(len(rows), 1, fp.MAX_ROWS)
    predictions, gold, probabilities = [], {}, []
    floor = fp.number(propensity_floor, .001, .5)
    environments = {row.get("environment", "real") for row in rows}
    if len(environments) != 1 or not environments <= {"sim", "real"}:
        raise ValueError("sim and real outcomes cannot be pooled silently")
    for i, row in enumerate(rows):
        p = fp.number(1 - row["rate"], floor, 1 - floor, "injection propensity")
        if type(row["injected"]) is not bool:
            raise ValueError("logged binary action required")
        prediction = fp.number(row["prediction"], 0, 1)
        weight = 1 / p if row["injected"] else -1 / (1 - p)
        predictions.append(weight * prediction)
        probabilities.append(row["gold_probability"])
        if row.get("gold") is not None:
            gold[i] = weight * fp.number(row["gold"], 0, 1)
    bound = 1 / floor
    result = prediction_powered(predictions, gold, probabilities, alpha=alpha / 2, low=-bound, high=bound)
    # Add assignment uncertainty separately; PPI alone covers the realized
    # full-data IPW mean, not the target treatment effect.
    radius = bound * math.sqrt(2 * math.log(4 / alpha) / len(rows))
    result["interval"] = [max(-1, result["interval"][0] - radius), min(1, result["interval"][1] + radius)]
    result["environment"] = next(iter(environments))
    result["assumptions"] = "exact treatment/gold propensities, independent sampling, bounded outcomes, no interference"
    return result


@dataclass(frozen=True)
class Surrogate:
    weights: tuple[float, ...]
    agreement: float
    validation_n: int
    environment: str
    minimum_agreement: float

    def predict(self, signals: Sequence[float]) -> dict[str, Any]:
        if len(signals) != len(self.weights):
            raise ValueError("surrogate signal dimensions differ")
        linear = math.fsum(w * fp.number(x, -1, 1, "early signal") for w, x in zip(self.weights, signals))
        return {"prediction": 1 / (1 + math.exp(-max(-30, min(30, linear)))),
                "use_final": self.agreement < self.minimum_agreement,
                "agreement": self.agreement, "kind": "validated surrogate estimate; not a final observed outcome"}

    def monitor(self, pairs: Sequence[tuple[float, bool]]) -> dict[str, Any]:
        fp.count(len(pairs), 1, 10_000)
        for prediction, final in pairs:
            fp.number(prediction, 0, 1)
            if type(final) is not bool:
                raise ValueError("final outcome must be binary")
        agreement = sum((prediction >= .5) == final for prediction, final in pairs) / len(pairs)
        return {"agreement": agreement, "use_final": agreement < self.minimum_agreement,
                "n": len(pairs), "kind": "surrogate monitoring estimate"}


def fit_surrogate(rows: Sequence[dict[str, Any]], *, minimum_agreement: float = .8) -> Surrogate:
    fp.count(len(rows), 4, 10_000)
    fp.number(minimum_agreement, .5, 1)
    environments = {row["environment"] for row in rows}
    if len(environments) != 1 or not environments <= {"sim", "real"}:
        raise ValueError("surrogate training requires one explicit environment")
    experiments = sorted({fp.text(row["experiment_id"]) for row in rows})
    if len(experiments) < 2:
        raise ValueError("hold out at least one entire experiment for surrogate validation")
    held = experiments[-1]
    training = [row for row in rows if row["experiment_id"] != held]
    validation = [row for row in rows if row["experiment_id"] == held]
    dimensions = fp.count(len(rows[0]["signals"]), 1, 16)
    weights = [0.0] * dimensions
    for row in rows:
        if len(row["signals"]) != dimensions or type(row["final"]) is not bool:
            raise ValueError("bounded equal-sized early signals and binary final outcomes required")
        for x in row["signals"]:
            fp.number(x, -1, 1)
    for epoch in range(20):
        for row in training:
            linear = math.fsum(w * x for w, x in zip(weights, row["signals"]))
            probability = 1 / (1 + math.exp(-max(-30, min(30, linear))))
            for j, x in enumerate(row["signals"]):
                weights[j] -= .1 / math.sqrt(epoch + 1) * (probability - int(row["final"])) * x
    model = Surrogate(tuple(weights), 1, len(validation), next(iter(environments)), minimum_agreement)
    agreement = sum((model.predict(row["signals"])["prediction"] >= .5) == row["final"]
                    for row in validation) / len(validation)
    return Surrogate(tuple(weights), agreement, len(validation), model.environment, minimum_agreement)


def record_discrepancy(root: str, condition: str, simulated: float, observed: float, *,
                       valid_from: str) -> dict[str, Any]:
    from commontrace import hierarchical

    fp.text(condition, "condition", 128)
    fp.number(simulated, 0, 1)
    fp.number(observed, 0, 1)
    statement = f"Sim-to-real discrepancy for {condition}: simulated={simulated:.6f}; real={observed:.6f}"
    fact, _ = hierarchical.add_fact(root, statement, scopes=["env:real", "condition:" + fp.digest(condition)[:16]],
                                    valid_from=fp.moment(valid_from))
    entry = {"condition": condition, "valid_from": fp.moment(valid_from),
             "fact_id": fact.id, "simulated": simulated, "observed": observed}
    fp.append(root, "sim-real-discrepancies.jsonl", entry)
    return {"fact_id": fact.id, "simulated": simulated, "observed": observed,
            "trust_weight": 1 - abs(simulated - observed),
            "kind": "bitemporal discrepancy; trust weight is a heuristic"}


def simulation_weight(root: str, condition: str, *, as_of: str | None = None) -> dict[str, Any]:
    """Predictable empirical shrinkage from *prior* paired discrepancies only.

    Call before a simulation signal is observed; never convert a simulation into
    a real outcome. Unknown conditions receive zero trust until calibrated.
    """
    fp.text(condition, "condition", 128)
    cutoff = fp.moment(as_of) if as_of else fp.now()
    rows = [r for r in fp.journal(root, "sim-real-discrepancies.jsonl")
            if r["condition"] == condition and r["recorded_at"] <= cutoff and r["valid_from"] <= cutoff]
    n = len(rows)
    weight = sum(1 - abs(r["simulated"] - r["observed"]) for r in rows) / (n + 5)
    return {"condition": condition, "trust_weight": weight, "paired_n": n, "as_of": cutoff,
            "kind": "empirical discrepancy shrinkage heuristic; environments remain separate"}
