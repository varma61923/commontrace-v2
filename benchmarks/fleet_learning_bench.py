"""A robot fleet learning as one: embedding-only skills, graded rewards, embodiment-specific effects.

A seeded simulation of the full loop a physical-AI fleet runs through UniMem:

- Twelve robot contexts (four embodiments at three sites, ``embodiment/site/pick``).
- Skills are memories with no text, only an embedding from the fleet's encoder.
  They come in families: variants of one skill share a direction in embedding
  space and the same embodiment-specific effect.
- Every era of 250 occasions, each family contributes new variants, as a fleet
  that keeps distilling skills would.
- Each occasion is one robot task. Four live skills are offered, the allocation
  policy withholds each at its rate for that skill in that robot's context, and
  the outcome is graded task progress in [0, 1], recorded by `unimem.rewards`
  randomized rounding.

Policies are replanned at every era from all resolved rows:

- ``fixed50``: withhold everything half the time;
- ``thompson``: `causal_gate` per skill and context;
- ``thompson-field``: the same, plus the effect field over embeddings (new
  variants are forecast from their family) and the per-skill context field
  (sparse contexts borrow from contexts sharing an embodiment);
- ``oracle``: shows exactly the skills that help in that context.

Reported: mean expected task progress (the true value, not the rounded bits)
and regret per 100 tasks against the oracle, mean +/- standard error over
seeds, and the paired lift of thompson-field over thompson.

    python -m benchmarks.fleet_learning_bench --reps 10 --out fleet.json
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys

from unimem import causal_gate, effect_field, rewards
from unimem.causal_gate import Row

ERAS = 6
ERA_OCCASIONS = 250
SHOWN = 4
FLOOR = 0.05
BASE = 0.45
DIMS = 24
EMBODIMENTS = ("jaw", "suction", "dexterous", "magnet")
SITES = ("depot", "yard", "lab")
FAMILIES = {
    "regrasp": {"jaw": 0.15, "suction": 0.05, "dexterous": 0.10, "magnet": 0.0},
    "vacuum-check": {"jaw": 0.0, "suction": 0.15, "dexterous": -0.05, "magnet": -0.05},
    "skip-collision": {"jaw": -0.15, "suction": -0.15, "dexterous": -0.15, "magnet": -0.15},
    "slow-approach": {"jaw": 0.05, "suction": -0.10, "dexterous": 0.05, "magnet": 0.10},
    "log-telemetry": {"jaw": 0.0, "suction": 0.0, "dexterous": 0.0, "magnet": 0.0},
}
NEW_PER_FAMILY = 2
POLICIES = ("fixed50", "thompson", "thompson-field", "oracle")


def _variant(rng: random.Random, family: str) -> list[float]:
    center = random.Random(f"center:{family}")
    return [center.gauss(0, 1) + rng.gauss(0, 0.35) for _ in range(DIMS)]


def _rate(policy: str, rows: list[Row], vectors: dict[str, list[float]]):
    if policy == "fixed50" or not rows:
        return lambda memory, context: 0.5
    field = policy == "thompson-field"
    measured = {r.lesson for r in rows}
    model = causal_gate.fit(
        rows,
        kernel=effect_field.Kernel.from_embeddings(vectors, "fleet-encoder") if field else None,
        candidates=[m for m in vectors if m not in measured] if field else (),
        context_transfer=field,
    )
    lesson_rates, context_rates = causal_gate.rates(model, FLOOR)

    def rate_for(memory: str, context: str) -> float:
        return context_rates.get(memory, {}).get(context, lesson_rates.get(memory, 0.5))

    return rate_for


def run(seed: int, policy: str) -> dict[str, float]:
    arrivals = random.Random(f"arrivals:{seed}")
    world = random.Random(f"world:{seed}")  # common random numbers across policies
    contexts = [f"{e}/{s}/pick" for e in EMBODIMENTS for s in SITES]
    vectors: dict[str, list[float]] = {}
    family_of: dict[str, str] = {}
    rows: list[Row] = []
    value = 0.0
    tasks = 0
    for era in range(ERAS):
        for family in FAMILIES:
            for i in range(NEW_PER_FAMILY):
                memory = f"{family}-{era}-{i}"
                vectors[memory], family_of[memory] = _variant(arrivals, family), family
        live = sorted(vectors)
        rate_for = _rate(policy, rows, vectors)
        for i in range(ERA_OCCASIONS):
            occasion = f"e{era}t{i}"
            context = world.choice(contexts)
            embodiment = context.split("/")[0]
            offered = world.sample(live, SHOWN)
            draws = {m: world.random() for m in offered}
            effect = {m: FAMILIES[family_of[m]][embodiment] for m in offered}
            if policy == "oracle":
                shown = {m: effect[m] > 0 for m in offered}
            else:
                shown = {m: draws[m] >= rate_for(m, context) for m in offered}
            expected = min(1.0, max(0.0, BASE + sum(effect[m] for m in offered if shown[m])))
            progress = min(1.0, max(0.0, expected + world.uniform(-0.1, 0.1)))
            value += expected
            tasks += 1
            if policy != "oracle":
                bit = rewards.realize(f"fleet:{seed}", occasion, progress)
                rows += [
                    Row(m, occasion, shown[m], bit, rate_for(m, context), context, f"{era:02d}{i:06d}") for m in offered
                ]
    return {"progress": value / tasks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reps", type=int, default=10)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    by_policy = {p: [run(seed, p)["progress"] for seed in range(args.reps)] for p in POLICIES}

    def mean_se(values: list[float]) -> dict[str, float]:
        se = statistics.stdev(values) / len(values) ** 0.5 if len(values) > 1 else 0.0
        return {"mean": round(statistics.fmean(values), 5), "se": round(se, 5)}

    report: dict = {
        "config": {
            "eras": ERAS,
            "era_occasions": ERA_OCCASIONS,
            "contexts": len(EMBODIMENTS) * len(SITES),
            "families": FAMILIES,
            "reps": args.reps,
        },
        "progress": {p: mean_se(v) for p, v in by_policy.items()},
        "regret_per_100": {
            p: mean_se([100 * (o - x) for x, o in zip(v, by_policy["oracle"])]) for p, v in by_policy.items()
        },
        "field_lift_per_100": mean_se(
            [100 * (f - t) for f, t in zip(by_policy["thompson-field"], by_policy["thompson"])]
        ),
    }
    print("policy           progress         regret/100 tasks vs oracle")
    for p in POLICIES:
        prog, reg = report["progress"][p], report["regret_per_100"][p]
        print(f"  {p:15s} {prog['mean']:.3f} +/- {prog['se']:.3f}   {reg['mean']:+.2f} +/- {reg['se']:.2f}")
    lift = report["field_lift_per_100"]
    print(f"  paired thompson-field lift over thompson: {lift['mean']:+.2f} +/- {lift['se']:.2f} per 100 tasks")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
