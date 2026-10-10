"""Target E: how fast each holdout design proves a +5pp memory, and whether its intervals stay honest.

A seeded simulation of one memory over 2,000 occasions, repeated many times per
scenario. Each occasion falls in a stratum (a pre-treatment label such as task
difficulty) whose base success rate differs; the memory adds the same true
effect in every stratum. Designs compared:

- ``fixed10-mixture``: the default 10% holdout read by the shipped anytime
  interval (beta-mixture per arm, Bonferroni);
- ``fixed50-mixture``: the same interval at a 50% holdout;
- ``fixed50-aipw``: 50% holdout, AIPW scores, asymptotic confidence sequence;
- ``fixed50-aipw-strata``: the same, with the stratum label in the outcome model;
- ``adaptive-aipw-strata``: adaptive allocation in eras of 250 occasions
  (explore at 50% until proven, then monitor at 5%), AIPW with strata.

Reported per design and scenario: power (HELPS at 2,000 occasions), median
occasions to the first HELPS, time-uniform coverage (the true effect inside the
interval at every checkpoint from 100 occasions on), any-time false positives
under no effect, and how often a helpful memory was withheld.

    python -m benchmarks.adaptive_allocation_bench --reps 400 --out adaptive.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
import sys

from commontrace import allocation, experiment

HORIZON = 2000
CHECK_EVERY = 50
FIRST_CHECK = 100
ALPHA = 0.05
STRATA = {
    "flat": (0.5,),
    "moderate": (0.2, 0.5, 0.8),
    "strong": (0.05, 0.5, 0.95),
}
EFFECTS = (0.0, 0.05, 0.10)
DESIGNS = ("fixed10-mixture", "fixed50-mixture", "fixed50-aipw", "fixed50-aipw-strata", "adaptive-aipw-strata")


def _true_effect(bases: tuple[float, ...], tau: float) -> float:
    return statistics.fmean(min(1.0, max(0.0, p + tau)) - p for p in bases)


def _cs_prefixes(scores: list[float], checkpoints: list[int]) -> dict[int, tuple[float, float, float]]:
    """allocation.confidence_sequence at each prefix length, in one pass."""
    out, total, square, k = {}, 0.0, 0.0, 0
    rho2 = allocation._mixture_rho2(ALPHA, HORIZON)
    wanted = set(checkpoints)
    for k, s in enumerate(scores, start=1):
        total += s
        square += s * s
        if k in wanted and k >= 2:
            mean = total / k
            variance = max(0.0, (square - k * mean * mean) / (k - 1))
            v = k * variance * rho2 + 1.0
            radius = math.sqrt(2.0 * v / rho2 * math.log(math.sqrt(v) / ALPHA)) / k
            out[k] = (mean, mean - radius, mean + radius)
    return out


def _simulate(design: str, bases: tuple[float, ...], tau: float, rng: random.Random) -> dict:
    checkpoints = list(range(FIRST_CHECK, HORIZON + 1, CHECK_EVERY))
    stratified = design.endswith("strata")
    adaptive = design.startswith("adaptive")
    rate = 0.1 if design.startswith("fixed10") else 0.5
    policy = allocation.Policy()
    observations: list[allocation.Observation] = []
    intervals: dict[int, tuple[float, float]] = {}
    withheld_helpful = 0
    s = {True: 0, False: 0}
    n = {True: 0, False: 0}
    for t in range(1, HORIZON + 1):
        if adaptive and t > 1 and (t - 1) % policy.era_occasions == 0:
            est = allocation.estimate(observations, alpha=ALPHA, horizon=HORIZON)
            verdict = (experiment.VERDICT_HELPS if est.ci_low > 0 else experiment.VERDICT_HURTS
                       if est.ci_high < 0 else experiment.VERDICT_UNDERPOWERED)
            rate = allocation.next_rate(verdict, policy)
        p = rng.choice(bases)
        stratum = str(bases.index(p)) if stratified else ""
        injected = rng.random() >= rate
        y = rng.random() < (min(1.0, max(0.0, p + tau)) if injected else p)
        observations.append(allocation.Observation(injected, y, rate, stratum))
        s[injected] += y
        n[injected] += 1
        withheld_helpful += (not injected) and tau > 0
        if t in checkpoints and design.endswith("mixture") and n[True] and n[False]:
            baseline = (s[True] + s[False]) / t
            target = experiment.required_n_per_arm(2 * 0.10, baseline) if 0 < baseline < 1 else 0
            intervals[t] = experiment.anytime_confidence_interval(s[True], n[True], s[False], n[False],
                                                                  alpha=ALPHA, target_n_per_arm=target)
    if not design.endswith("mixture"):
        scores = allocation.aipw_scores(observations)
        # The running intersection, as allocation.estimate reports it (from 50 scores on).
        path = _cs_prefixes(scores, list(range(allocation.BURN_IN, HORIZON + 1)))
        low, high = -math.inf, math.inf
        for k in sorted(path):
            low, high = max(low, path[k][1]), min(high, path[k][2])
            if k in checkpoints:
                intervals[k] = (low, high)
    truth = _true_effect(bases, tau)
    first = next((k for k in checkpoints if k in intervals and intervals[k][0] > 0), None)
    # Anytime-valid: a design may stop and declare at its first crossing.
    return {"helps_final": first is not None, "first_helps": first,
            "covered": all(lo <= truth <= hi for lo, hi in intervals.values()),
            "excluded_zero_ever": any(lo > 0 or hi < 0 for lo, hi in intervals.values()),
            "withheld_share": withheld_helpful / HORIZON}


def _wilson(k: int, n: int) -> list[float]:
    if not n:
        return [0.0, 1.0]
    z, p = 1.959963984540054, k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def run(*, reps: int = 400, seed: int = 0, designs=DESIGNS, strata=tuple(STRATA), effects=EFFECTS) -> dict:
    cells = []
    for name in strata:
        bases = STRATA[name]
        for tau in effects:
            for design in designs:
                rng = random.Random(f"{seed}:{name}:{tau}:{design}")
                runs = [_simulate(design, bases, tau, rng) for _ in range(reps)]
                helps = sum(r["helps_final"] for r in runs)
                covered = sum(r["covered"] for r in runs)
                ever = sum(r["excluded_zero_ever"] for r in runs)
                firsts = [r["first_helps"] for r in runs if r["first_helps"] is not None]
                cells.append({
                    "strata": name, "bases": bases, "effect": tau, "design": design, "reps": reps,
                    "power_at_2000": round(helps / reps, 4), "power_ci95": _wilson(helps, reps),
                    "median_occasions_to_helps": statistics.median(firsts) if firsts else None,
                    "time_uniform_coverage": round(covered / reps, 4), "coverage_ci95": _wilson(covered, reps),
                    "false_positive_any_time": round(ever / reps, 4) if tau == 0 else None,
                    "withheld_share_of_helpful": round(statistics.fmean(r["withheld_share"] for r in runs), 4)
                    if tau > 0 else None,
                })
    with open(__file__, "rb") as fh:
        source = hashlib.sha256(fh.read()).hexdigest()
    return {"benchmark": "adaptive-allocation", "version": 1, "seed": seed, "reps": reps, "horizon": HORIZON,
            "alpha": ALPHA, "checkpoints": f"every {CHECK_EVERY} from {FIRST_CHECK}", "source_sha256": source,
            "cells": cells,
            "note": "Seeded simulation of the estimators and allocation policy; not a measurement of any deployment."}


def render(report: dict) -> str:
    lines = [f"Adaptive allocation, {report['reps']} runs per cell, {report['horizon']} occasions, "
             f"alpha {report['alpha']}", "",
             f"{'strata':<9}{'effect':>7}  {'design':<22}{'power':>7}{'median to HELPS':>17}"
             f"{'coverage':>10}{'FP any-time':>12}{'withheld':>10}"]
    def cell(value, spec):
        return "-" if value is None else format(value, spec)

    for c in report["cells"]:
        lines.append(
            f"{c['strata']:<9}{c['effect']:>+7.2f}  {c['design']:<22}{c['power_at_2000']:>7.2f}"
            f"{cell(c['median_occasions_to_helps'], 'g'):>17}{c['time_uniform_coverage']:>10.3f}"
            f"{cell(c['false_positive_any_time'], '.3f'):>12}{cell(c['withheld_share_of_helpful'], '.2f'):>10}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--reps", type=int, default=400)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--designs", default=",".join(DESIGNS))
    p.add_argument("--strata", default=",".join(STRATA))
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)
    report = run(reps=args.reps, seed=args.seed, designs=tuple(args.designs.split(",")),
                 strata=tuple(args.strata.split(",")))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
