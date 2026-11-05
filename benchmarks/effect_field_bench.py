"""Effect field: does sharing randomized evidence between paraphrases pay off for a learning fleet?

A seeded simulation of a fleet that keeps distilling lessons. Lessons come in
families of paraphrases: different words, one shared idea, one true effect (some
families help, some hurt). Each era of 250 occasions, every family contributes new
paraphrases. Each occasion retrieves four live lessons, and the allocation policy
decides, per lesson, whether it is withheld. Success probability is a base rate
plus the effects of the lessons actually shown.

Policies, replanned at every era from all resolved rows so far:

- ``fixed50``: every lesson withheld half the time (the plain holdout experiment);
- ``thompson``: `causal_gate` (unmeasured lessons start at the 50% explore rate);
- ``thompson-field``: `causal_gate` with an `effect_field` over the lesson texts,
  forecasting rates for lessons that have no evidence yet;
- ``oracle``: shows exactly the lessons that help (an upper bound, not a policy).

Two scenarios: ``linked`` (a family's paraphrases share its effect) and ``placebo``
(the same texts with effects shuffled across lessons, so content predicts nothing
and a field that transfers anyway would be caught out).

Reported per scenario and policy: realized success rate, the share of injections
that were harmful lessons, regret against the oracle (success points lost per 100
occasions), and the share of eras in which the field validated.

    python -m benchmarks.effect_field_bench --reps 20 --out effect_field.json
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys

from unimem import causal_gate, effect_field
from unimem.causal_gate import Row

ERAS = 8
ERA_OCCASIONS = 250
SHOWN = 4
FLOOR = 0.05
BASE = 0.5
FAMILIES = {
    "refund-ledger": ("refund ledger invoice reconcile settlement payout", 0.12),
    "verify-owner": ("verify owner identity password reset account", 0.10),
    "cache-warm": ("cache warm prefetch latency index shard", 0.04),
    "retry-quota": ("retry backoff throttle jitter quota limit", 0.0),
    "legacy-soap": ("legacy soap endpoint deprecated fallback adapter", -0.10),
    "force-push": ("force push rewrite history branch rebase main", -0.15),
}
NEW_PER_FAMILY = 2
POLICIES = ("fixed50", "thompson", "thompson-field", "oracle")


def _paraphrase(rng: random.Random, family: str) -> str:
    words = FAMILIES[family][0].split()
    return " ".join(rng.sample(words, 4) + [f"w{rng.randrange(2000)}" for _ in range(5)])


def _arrivals(rng: random.Random, placebo: bool) -> list[list[tuple[str, str, float]]]:
    """Per era, the lessons that arrive: (id, text, true effect)."""
    eras = []
    for era in range(ERAS):
        batch = [
            (f"{family}-{era}-{i}", _paraphrase(rng, family), effect)
            for family, (_words, effect) in FAMILIES.items()
            for i in range(NEW_PER_FAMILY)
        ]
        eras.append(batch)
    if placebo:
        effects = [e for batch in eras for _m, _t, e in batch]
        rng.shuffle(effects)
        it = iter(effects)
        eras = [[(m, t, next(it)) for m, t, _e in batch] for batch in eras]
    return eras


def _plan(policy: str, rows: list[Row], texts: dict[str, str], live: list[str]) -> tuple[dict[str, float], bool]:
    if policy == "fixed50" or not rows:
        return {}, False
    kernel = effect_field.Kernel.lexical(texts) if policy == "thompson-field" else None
    measured = {r.lesson for r in rows}
    model = causal_gate.fit(rows, kernel=kernel, candidates=[m for m in live if m not in measured])
    rates, _context = causal_gate.rates(model, FLOOR)
    return rates, bool(model.transfer is not None and model.transfer.validated)


def run(seed: int, policy: str, placebo: bool) -> dict:
    rng = random.Random(f"{seed}:{placebo}")
    arrivals = _arrivals(rng, placebo)
    world = random.Random(f"world:{seed}:{placebo}")  # common random numbers across policies
    texts: dict[str, str] = {}
    effects: dict[str, float] = {}
    rows: list[Row] = []
    successes = injections = harmful = occasions = validated = 0
    for era, batch in enumerate(arrivals):
        for memory, text, effect in batch:
            texts[memory], effects[memory] = text, effect
        live = sorted(texts)
        rates, ok = _plan(policy, rows, texts, live)
        validated += ok
        for i in range(ERA_OCCASIONS):
            occasion = f"e{era}o{i}"
            offered = world.sample(live, SHOWN)
            draws = {m: world.random() for m in offered}
            if policy == "oracle":
                shown = {m: effects[m] > 0 for m in offered}
            else:
                shown = {m: draws[m] >= rates.get(m, 0.5) for m in offered}
            p = BASE + sum(effects[m] for m in offered if shown[m])
            succeeded = world.random() < min(0.98, max(0.02, p))
            successes += succeeded
            occasions += 1
            injections += sum(shown.values())
            harmful += sum(shown[m] and effects[m] < 0 for m in offered)
            if policy != "oracle":
                rows += [
                    Row(m, occasion, shown[m], succeeded, rates.get(m, 0.5), "", f"{era:02d}{i:06d}") for m in offered
                ]
    return {
        "success": successes / occasions,
        "harmful_share": harmful / injections if injections else 0.0,
        "validated_eras": validated / ERAS,
    }


def summarize(results: list[dict], oracle: list[dict]) -> dict:
    def mean_se(values: list[float]) -> tuple[float, float]:
        m = statistics.fmean(values)
        return m, (statistics.stdev(values) / len(values) ** 0.5 if len(values) > 1 else 0.0)

    regret = [100.0 * (o["success"] - r["success"]) for r, o in zip(results, oracle)]
    out = {}
    for key, values in (
        ("success", [r["success"] for r in results]),
        ("harmful_share", [r["harmful_share"] for r in results]),
        ("validated_eras", [r["validated_eras"] for r in results]),
        ("regret_per_100", regret),
    ):
        m, se = mean_se(values)
        out[key] = {"mean": round(m, 5), "se": round(se, 5)}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reps", type=int, default=20)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    report: dict = {
        "config": {
            "eras": ERAS,
            "era_occasions": ERA_OCCASIONS,
            "shown": SHOWN,
            "floor": FLOOR,
            "families": {f: e for f, (_w, e) in FAMILIES.items()},
            "new_per_family_per_era": NEW_PER_FAMILY,
            "reps": args.reps,
        },
        "scenarios": {},
    }
    for scenario, placebo in (("linked", False), ("placebo", True)):
        by_policy = {p: [run(seed, p, placebo) for seed in range(args.reps)] for p in POLICIES}
        report["scenarios"][scenario] = {p: summarize(by_policy[p], by_policy["oracle"]) for p in POLICIES}
        lift = [
            100.0 * (f["success"] - t["success"]) for f, t in zip(by_policy["thompson-field"], by_policy["thompson"])
        ]
        se = statistics.stdev(lift) / len(lift) ** 0.5 if len(lift) > 1 else 0.0
        report["scenarios"][scenario]["field_lift_vs_thompson_per_100"] = {
            "mean": round(statistics.fmean(lift), 4),
            "se": round(se, 4),
        }
    for scenario, table in report["scenarios"].items():
        paired = table.pop("field_lift_vs_thompson_per_100")
        print(f"{scenario}: success | harmful share of injections | regret per 100 vs oracle | field validated")
        for policy, row in table.items():
            print(
                f"  {policy:15s} {row['success']['mean']:.3f} +/- {row['success']['se']:.3f} | "
                f"{row['harmful_share']['mean']:.3f} | {row['regret_per_100']['mean']:+.2f} | "
                f"{row['validated_eras']['mean']:.0%}"
            )
        print(f"  paired field lift over thompson: {paired['mean']:+.2f} +/- {paired['se']:.2f} per 100 occasions")
        table["field_lift_vs_thompson_per_100"] = paired
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
