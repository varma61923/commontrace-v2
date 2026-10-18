"""UniMem directional transfer tests, factorial forgetting and drift alarms.

Misevolution (arXiv:2509.26354) and experience-poisoning motivate checking both
origin benefit AND out-of-segment harm. Intersection-union e-values use their
minimum, then e-BH (Wang and Ramdas, arXiv:2009.02824). No shared-data products
or maxima enter e-BH. The null is conditional per occasion, not an unrestricted
time-average null. Repeated releases require the I/O layer's alpha accounting.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from commontrace import aipw, eprocess
from commontrace import frontier_protocol as fp

PATHWAYS = ("model", "memory", "tool", "workflow")


def observations(rows: Sequence[Mapping[str, Any]]) -> list[aipw.Observation]:
    fp.count(len(rows), 0, fp.MAX_ROWS, "UniMem sentinel observations")
    seen = set()
    result = []
    for row in rows:
        identity = fp.text(row["occasion_id"], "occasion", 256)
        if identity in seen:
            raise ValueError("UniMem sentinel requires one observation per unit and occasion")
        seen.add(identity)
        if type(row["injected"]) is not bool or row["outcome"] not in (0, 1) or type(row["outcome"]) is not int:
            raise ValueError("UniMem sentinel requires binary actual assignments and outcomes")
        rate = fp.number(row["rate"], .05, .95, "holdout rate")
        result.append(aipw.Observation(row["injected"], bool(row["outcome"]), rate, str(row.get("context", ""))))
    return result


def transfer(rows: Sequence[Mapping[str, Any]], *, origin: str, segments: Sequence[str],
             alpha: float = .05) -> dict[str, Any]:
    """One fixed segment family at one common stopping time, including origin."""
    fp.number(alpha, 1e-12, .5)
    fp.count(len(segments), 2, 16, "sentinel segment family")
    names = [fp.text(segment, "pre-treatment segment", 64) for segment in segments]
    if len(set(names)) != len(names) or origin not in names:
        raise ValueError("UniMem sentinel requires distinct preregistered segments including the origin")
    observations(rows)
    if any(row.get("context") not in names for row in rows):
        raise ValueError("UniMem sentinel encountered an unregistered segment")
    groups = {name: observations([row for row in rows if row.get("context") == name]) for name in names}
    benefit = eprocess.process(groups[origin]).e_value
    tests: list[dict[str, Any]] = []
    for segment in names:
        if segment == origin:
            continue
        values = groups[segment]
        harm = eprocess.process(values, direction="hurts").e_value
        scores = aipw.aipw_scores(values)
        bound = max((1/min(row.rate, 1-row.rate) for row in values), default=20)
        estimate, low, high = eprocess.interval(scores, bound=bound, alpha=alpha/(len(names)-1))
        tests.append({"segment": segment, "origin_benefit_e": benefit, "transfer_harm_e": harm,
                      "crossing_e": min(benefit, harm), "estimate": estimate,
                      "confidence_sequence": [max(-1., low), min(1., high)], "n": len(values),
                      "minimum_detectable_effect": min(2., 2.802*bound/math.sqrt(max(1, len(values))))})
    rejected = eprocess.ebh([test["crossing_e"] for test in tests], alpha=alpha)
    for test, reject in zip(tests, rejected):
        test["crossing"] = reject
    return {"quarantine": any(rejected), "origin": origin, "tests": tests, "alpha": alpha,
            "classification": "guarantee under conditional directional null and exact propensities",
            "assumptions": ["origin and segment family fixed before these outcomes", "complete canonical outcomes",
                            "common stopping time for this e-BH release", "no interference"],
            "power_note": "MDE is normal-approximation planning; absent rejection does not establish transfer safety"}


def interference(rows: Sequence[Mapping[str, Any]], *, tolerance: float = .1, alpha: float = .05,
                 minimum_support: int = 5) -> dict[str, Any]:
    """Effect(canary|update) minus effect(canary|control), using actual 2x2 arms.

    The interaction parameter lies in [-2,2]. The HT score bound includes BOTH
    randomization propensities; neither the parameter range nor a main-effect
    bound may replace it. Independent factors and complete occasions are required.
    """
    fp.count(len(rows), 0, fp.MAX_ROWS)
    fp.count(minimum_support, 1, 1000)
    fp.number(tolerance, 0, 2)
    fp.number(alpha, 1e-12, .5)
    cells = {(u, c): 0 for u in (False, True) for c in (False, True)}
    scores, seen, bound = [], set(), 4.
    for row in rows:
        identity = fp.text(row["occasion_id"], "factorial occasion", 256)
        if identity in seen:
            raise ValueError("UniMem update ledger repeats a factorial occasion")
        seen.add(identity)
        u, c, y = row["update"], row["canary"], row["outcome"]
        if type(u) is not bool or type(c) is not bool or type(y) is not int or y not in (0, 1):
            raise ValueError("UniMem update ledger requires binary actual factorial arms/outcome")
        pu = 1-fp.number(row["update_rate"], .05, .95)
        pc = 1-fp.number(row["canary_rate"], .05, .95)
        score = y*(1/pu if u else -1/(1-pu))*(1/pc if c else -1/(1-pc))
        bound = max(bound, 1/(min(pu, 1-pu)*min(pc, 1-pc)))
        scores.append(score)
        cells[u, c] += 1
    # interval inverts a mean in [-1, 1]. Scale both the observations and
    # their design bound, then restore the interaction's [-2, 2] units.
    estimate, low, high = eprocess.interval([score/2 for score in scores], bound=bound/2, alpha=alpha)
    estimate, low, high = 2*estimate, 2*low, 2*high
    supported = all(count >= minimum_support for count in cells.values())
    lower, upper = max(-2., low), min(2., high)
    return {"estimate": estimate, "confidence_sequence": [lower, upper], "score_bound": bound,
            "supported": supported, "allow": supported and lower >= -tolerance,
            "regression": supported and upper < -tolerance, "tolerance": tolerance,
            "cells": {f"update:{int(u)}|canary:{int(c)}": count for (u, c), count in cells.items()},
            "classification": "guarantee under stationary independent-factor trial assumptions",
            "assumptions": ["actual independent logged factors", "complete canonical outcomes",
                            "certified canary content held fixed", "no cross-occasion interference"]}


def drift(rows: Sequence[Mapping[str, Any]], *, baseline_failure: float, alpha: float = .05) -> dict[str, Any]:
    """Conditional failure-rate alarms; these do not identify the cause of drift."""
    fp.number(baseline_failure, 0, 1)
    fp.number(alpha, 1e-12, .5)
    fp.count(len(rows), 0, fp.MAX_ROWS)
    scores: dict[str, list[float]] = {name: [] for name in PATHWAYS}
    seen = set()
    for row in rows:
        key = row["pathway"], fp.text(row["occasion_id"])
        if key[0] not in PATHWAYS or key in seen:
            raise ValueError("UniMem drift observation has an unknown pathway or repeated occasion")
        seen.add(key)
        failure = row["failure"]
        if type(failure) is not int or failure not in (0, 1):
            raise ValueError("UniMem drift monitor requires binary failures")
        scores[key[0]].append(failure-baseline_failure)
    alarms = {}
    for name, values in scores.items():
        evidence = eprocess._wealth(values, [1.]*len(values), delta=0, direction="helps")
        alarms[name] = {"e_value": evidence.e_value, "maximum": evidence.maximum,
                        "alarm": evidence.maximum >= len(PATHWAYS)/alpha, "n": len(values)}
    return {"pathways": alarms, "alpha": alpha, "baseline_failure": baseline_failure,
            "classification": "conditional failure-rate guarantee; cause attribution is a heuristic",
            "assumptions": ["baseline frozen before monitoring", "fresh complete one-per-occasion outcomes",
                            "conditional failure rate at most baseline under each pathway's null"]}
