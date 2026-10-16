"""DR uplift with disjoint training/validation and conservative RATE admission.

Kennedy (arXiv:2004.14497), Yadlowsky et al. (arXiv:2111.07966).
A categorical, shrinkage DR learner is fit on a fixed training slice. Validation
uses a fixed top-half TOC (a two-point, prespecified RATE weighting), with a
Hoeffding lower bound. This is not the full continuous AUTOC estimator. The
ranking rule and group definitions must be fixed before validation outcomes.
Failure to validate falls back to the existing empirical-Bayes gate; a permanent
untargeted exploration slice retains positivity. No silent sim/real pooling.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

from commontrace import frontier_protocol as fp

FEATURES = ("context", "tool", "model_version", "embodiment", "gripper", "site", "object_class")


def _key(features: dict[str, str]) -> str:
    if not set(features) <= set(FEATURES):
        raise ValueError("uplift features must be declared pre-treatment context fields")
    for key, value in features.items():
        fp.text(value, key, 64)
    return fp.digest(features)


def _score(row: dict[str, Any], means: dict[tuple[str, bool], float], floor: float) -> float:
    p = fp.number(1 - row["rate"], floor, 1 - floor)
    y = fp.number(row["outcome"], 0, 1)
    if type(row["injected"]) is not bool:
        raise ValueError("binary randomized action required")
    key = _key(row["features"])
    m0, m1 = means.get((key, False), .5), means.get((key, True), .5)
    return m1 - m0 + ((y - m1) / p if row["injected"] else -(y - m0) / (1 - p))


@dataclass(frozen=True)
class Targeting:
    effects: dict[str, float]
    positive_classes: frozenset[str]
    rate: float
    rate_lower: float
    validation_n: int
    exploration: float
    environment: str
    valid: bool
    feature_hash: str

    def decide(self, features: dict[str, str], *, exploration_draw: float,
               fallback_probability: float = .5) -> dict[str, Any]:
        fp.number(exploration_draw, 0, 1)
        fp.number(fallback_probability, .01, .99)
        key = _key(features)
        exploring = exploration_draw < self.exploration
        target = self.valid and key in self.positive_classes
        probability = fallback_probability if not self.valid or exploring else (.95 if target else .05)
        return {"injection_probability": probability, "targeted": self.valid and not exploring,
                "exploration": exploring, "fallback": not self.valid,
                "estimated_effect": self.effects.get(key), "rate_lower": self.rate_lower,
                "kind": "validated uplift policy" if self.valid else "empirical-Bayes fallback required"}


def fit(rows: Sequence[dict[str, Any]], *, training_size: int, propensity_floor: float = .1,
        alpha: float = .05, exploration: float = .1) -> Targeting:
    fp.count(len(rows), training_size + 20, 20_000)
    fp.count(training_size, 20, 10_000)
    fp.number(propensity_floor, .01, .5)
    fp.number(alpha, .0001, .2)
    fp.number(exploration, .01, .5)
    environments = {r["environment"] for r in rows}
    if len(environments) != 1 or not environments <= {"sim", "real"}:
        raise ValueError("uplift cannot pool sim and real")
    if len({fp.text(r["occasion_id"]) for r in rows}) != len(rows):
        raise ValueError("training and validation tasks must be disjoint")
    training, validation = rows[:training_size], rows[training_size:]
    cells: dict[tuple[str, bool], list[float]] = {}
    for row in training:
        _score(row, {}, propensity_floor)
        cells.setdefault((_key(row["features"]), row["injected"]), []).append(row["outcome"])
    means = {cell: (sum(ys) + 1) / (len(ys) + 2) for cell, ys in cells.items()}
    effects: dict[str, float] = {}
    counts: dict[str, int] = {}
    for row in training:
        key = _key(row["features"])
        effects[key] = effects.get(key, 0) + _score(row, means, propensity_floor)
        counts[key] = counts.get(key, 0) + 1
    effects = {key: value / (counts[key] + 20) for key, value in effects.items()}
    # Stable ties use the pre-treatment occasion id, never outcomes.
    ranked = sorted(validation, key=lambda r: (-effects.get(_key(r["features"]), 0), r["occasion_id"]))
    half = len(ranked) // 2
    scores = [_score(r, means, propensity_floor) for r in ranked]
    rate = sum(scores[:half]) / half - sum(scores) / len(scores)
    bound = 1 + 1 / propensity_floor
    radius = bound * math.sqrt(2 * math.log(4 / alpha) * (1 / half + 1 / len(scores)))
    lower = rate - radius
    # Simultaneous positive-effect tests prevent broadening into harmful classes.
    eligible = set()
    for key in effects:
        subgroup = [s for r, s in zip(ranked, scores) if _key(r["features"]) == key]
        if subgroup:
            radius_group = bound * math.sqrt(2 * math.log(2 * len(effects) / alpha) / len(subgroup))
            low = sum(subgroup) / len(subgroup) - radius_group
            if low > 0:
                eligible.add(key)
    return Targeting(effects, frozenset(eligible), rate, lower, len(validation), exploration,
                     next(iter(environments)), lower > 0,
                     fp.digest({"features": FEATURES, "training_size": training_size}))


def activate(root: str, lesson: str, model: Targeting, contexts: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Publish only validated classes for the *next* signed allocation schedule.

    The Hub can carry this signed scope map alongside a tenant-owned lesson.
    It never promotes a lesson into an unmeasured site/embodiment class.
    """
    fp.text(lesson)
    fp.count(len(contexts), 1, 64)
    if not model.valid:
        raise PermissionError("RATE has not validated targeting; retain empirical-Bayes allocation")
    rates = {}
    classes = {}
    for label, features in contexts.items():
        from commontrace.holdout_io import context_label
        context_label(label)
        if _key(features) in model.positive_classes:
            rates[label] = model.exploration * .5 + (1 - model.exploration) * .05
            classes[label] = features
    if not rates:
        raise PermissionError("no site/embodiment class has a positive simultaneous lower bound")
    return fp.append(root, "uplift-policies.jsonl", {"type": "validated-uplift-policy-v1",
        "lesson": lesson, "rates": rates, "classes": classes,
        "rate_lower": model.rate_lower, "environment": model.environment, "feature_hash": model.feature_hash,
        "validation_n": model.validation_n, "exploration": model.exploration})


def apply_rates(root: str, contextual: dict[str, dict[str, float]], floor: float, *,
                lesson_rates: dict[str, float] | None = None) -> dict[str, dict[str, float]]:
    policies = fp.journal(root, "uplift-policies.jsonl")
    result = {lesson: dict(rates) for lesson, rates in contextual.items()}
    for row in policies:
        if row["rate_lower"] <= 0:
            continue
        if lesson_rates is not None:
            # Outside validated classes retain only the exploration floor;
            # global empirical-Bayes lift cannot broaden the fleet rollout.
            lesson_rates[row["lesson"]] = 1 - floor
        result.setdefault(row["lesson"], {}).update({label: max(floor, min(1 - floor, rate))
                                                    for label, rate in row["rates"].items()})
    return result
