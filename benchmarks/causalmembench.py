"""CausalMemBench: does a memory system stop handing out harmful memories -- without
withdrawing the good ones -- when the only signal is noisy, delayed task outcomes?

Recall benchmarks ask whether the right passage was found. This one asks the
question a production fleet pays for: a memory that makes outcomes worse must
stop being delivered, and a memory that helps must not be thrown away because
it happens to fire on hard tasks. Ground truth is seeded, so every policy is
scored against the true effect of every memory.

The default scenario is deliberately confounded, because that is where a
correlational policy fails in production:

* ``help-hard`` truly helps, but is only eligible on hard occasions, so the
  occasions it is delivered on succeed *less often* than average.
* ``harm-easy`` truly hurts, but is only eligible on easy occasions, so the
  occasions it is delivered on succeed *more often* than average.
* the rest help, hurt or do nothing on randomly chosen occasions.

Outcomes arrive late (a fixed lag) and some never arrive at all.

Policies (``--policy`` takes ``name`` or ``module:factory`` for your own):

* ``deliver-all``   -- no measurement: the floor.
* ``correlational`` -- withdraw a memory once the occasions it was delivered on
  succeed clearly less often than the fleet average (the common heuristic).
* ``commontrace``   -- `commontrace.measure.CausalMemory` with
  ``on_harm="withdraw"``: per-memory randomized holdout, anytime-valid verdicts.
* ``oracle``        -- knows the truth; delivers every non-harmful memory.

A policy is any object with ``deliver(occasion_id, eligible_ids) -> list[str]``,
``observe(occasion_id, succeeded)`` and ``withdrawn() -> set[str]``.

Metrics per policy (mean and SD over seeds): success rate, harmful deliveries,
occasions until each harmful memory is withdrawn, false withdrawals (a helpful
or neutral memory withdrawn), and the regret against the oracle.

    python -m benchmarks.causalmembench --seeds 5 --occasions 6000 --out run.json
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
from collections import deque
from collections.abc import Callable

HARD_SHARE = 0.3
BASE_EASY = 0.65
BASE_HARD = 0.25
DEFAULT_OCCASIONS = 6000
DEFAULT_LAG = 50
DEFAULT_MISSING = 0.1
CHECK_EVERY = 200


@dataclasses.dataclass(frozen=True)
class Memory:
    id: str
    effect: float
    eligibility: float = 0.5
    only: str | None = None  # "hard" / "easy": eligible only on that kind of occasion

    @property
    def kind(self) -> str:
        return "harmful" if self.effect < 0 else "helpful" if self.effect > 0 else "neutral"


def default_memories() -> list[Memory]:
    out = [Memory(f"help-{i}", 0.15) for i in range(3)]
    out.append(Memory("help-hard", 0.20, eligibility=0.9, only="hard"))
    out += [Memory(f"neutral-{i}", 0.0, eligibility=0.4) for i in range(8)]
    out += [Memory(f"harm-{i}", -0.15) for i in range(2)]
    out.append(Memory("harm-easy", -0.15, eligibility=0.9, only="easy"))
    return out


class DeliverAll:
    name = "deliver-all"

    def deliver(self, occasion_id, eligible):
        return list(eligible)

    def observe(self, occasion_id, succeeded):
        pass

    def withdrawn(self):
        return set()


class Oracle(DeliverAll):
    name = "oracle"

    def __init__(self, memories):
        self._harmful = {m.id for m in memories if m.effect < 0}

    def deliver(self, occasion_id, eligible):
        return [m for m in eligible if m not in self._harmful]

    def withdrawn(self):
        return set(self._harmful)


class Correlational:
    """Withdraw once delivered-occasion success trails the fleet average by `margin`."""

    name = "correlational"

    def __init__(self, memories, *, min_deliveries: int = 100, margin: float = 0.05):
        self._min, self._margin = min_deliveries, margin
        self._delivered: dict[str, list[str]] = {}
        self._stats = {m.id: [0, 0] for m in memories}
        self._overall = [0, 0]
        self._out: set[str] = set()

    def deliver(self, occasion_id, eligible):
        chosen = [m for m in eligible if m not in self._out]
        self._delivered[occasion_id] = chosen
        return chosen

    def observe(self, occasion_id, succeeded):
        self._overall[0] += succeeded
        self._overall[1] += 1
        for m in self._delivered.pop(occasion_id, []):
            self._stats[m][0] += succeeded
            self._stats[m][1] += 1
        mean = self._overall[0] / self._overall[1]
        for m, (s, n) in self._stats.items():
            if n >= self._min and s / n < mean - self._margin:
                self._out.add(m)

    def withdrawn(self):
        return set(self._out)


class CommonTrace:
    """The product path: CausalMemory with harm withdrawal over a scratch store."""

    name = "commontrace"

    def __init__(self, memories, *, rate: float = 0.1, check_every: int = CHECK_EVERY, graduate: bool = False,
                 seed: int = 0):
        from commontrace import holdout_io
        from commontrace.measure import CausalMemory

        self._root = tempfile.mkdtemp(prefix="causalmembench-")
        # A seed-derived salt makes the randomization -- and so every number -- reproducible.
        holdout_io.configure(self._root, rate=rate, note="CausalMemBench", salt=f"causalmembench-{seed}-{rate:g}")
        self._eligible: list[str] = []
        self._memory = CausalMemory(lambda _q: [{"id": m, "text": m} for m in self._eligible],
                                    root=self._root, on_harm="withdraw", check_every=check_every,
                                    durable=False, graduate=graduate)
        self._last_withdrawn: set[str] = set()

    def deliver(self, occasion_id, eligible):
        self._eligible = list(eligible)
        result = self._memory.recall_detailed("occasion", occasion_id=occasion_id)
        self._last_withdrawn |= set(result.withdrawn)
        return [item["id"] for item in result.items]

    def observe(self, occasion_id, succeeded):
        self._memory.record_outcome(occasion_id, succeeded=succeeded)

    def withdrawn(self):
        return set(self._last_withdrawn)

    def close(self):
        shutil.rmtree(self._root, ignore_errors=True)


BUILTIN: dict[str, Callable] = {
    "deliver-all": lambda memories: DeliverAll(),
    "correlational": Correlational,
    "commontrace": lambda memories, seed=0: CommonTrace(memories, seed=seed),
    "oracle": Oracle,
}


def load_policy(spec: str) -> Callable:
    if spec in BUILTIN:
        return BUILTIN[spec]
    if spec.startswith("commontrace@") or spec == "commontrace+graduate":
        # commontrace@0.3: the same policy at another holdout rate (speed of detection
        # against the share of occasions a helpful memory is withheld from);
        # "+graduate" stops randomizing a memory once it is proven to help.
        body = spec.removeprefix("commontrace")
        graduate = body.endswith("+graduate")
        body = body.removesuffix("+graduate")
        try:
            rate = float(body[1:]) if body.startswith("@") else 0.1
        except ValueError:
            raise ValueError(f"bad holdout rate in {spec!r}") from None
        if not 0 < rate < 1 or (body and not body.startswith("@")):
            raise ValueError(f"bad policy {spec!r}: use commontrace@RATE or commontrace@RATE+graduate")
        return lambda memories, seed=0: CommonTrace(memories, rate=rate, graduate=graduate, seed=seed)
    module, sep, attr = spec.partition(":")
    if not sep or not module or not attr:
        raise ValueError(f"unknown policy {spec!r}: use a built-in name or module:factory")
    return getattr(importlib.import_module(module), attr)


def _takes_seed(factory: Callable) -> bool:
    import inspect

    try:
        params = inspect.signature(factory).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "seed" or p.kind is p.VAR_KEYWORD for p in params)


def run_one(factory: Callable, memories: list[Memory], *, seed: int, occasions: int, lag: int,
            missing: float) -> dict:
    """One policy, one seed. Occasions, eligibility and outcome noise depend only on the seed,
    so every policy faces the identical world."""
    world = random.Random(seed)
    noise = random.Random(seed * 7919 + 1)
    policy = factory(memories, seed=seed) if _takes_seed(factory) else factory(memories)
    by_id = {m.id: m for m in memories}
    pending: deque = deque()
    successes = harmful_deliveries = 0
    first_out: dict[str, int] = {}
    try:
        for t in range(occasions):
            hard = world.random() < HARD_SHARE
            base = BASE_HARD if hard else BASE_EASY
            eligible = [m.id for m in memories
                        if (m.only is None or m.only == ("hard" if hard else "easy"))
                        and world.random() < m.eligibility]
            draw = noise.random()
            occasion = f"o{seed}-{t}"
            delivered = policy.deliver(occasion, eligible) if eligible else []
            p = min(0.98, max(0.02, base + sum(by_id[m].effect for m in delivered)))
            succeeded = draw < p
            successes += succeeded
            harmful_deliveries += sum(by_id[m].effect < 0 for m in delivered)
            if world.random() >= missing:
                pending.append((t + lag, occasion, succeeded))
            while pending and pending[0][0] <= t:
                _due, occ, ok = pending.popleft()
                policy.observe(occ, ok)
            for m in policy.withdrawn():
                first_out.setdefault(m, t)
    finally:
        close = getattr(policy, "close", None)
        if close is not None:
            close()
    harmful = [m.id for m in memories if m.effect < 0]
    final = set(first_out)
    return {
        "success_rate": successes / occasions,
        "harmful_deliveries": harmful_deliveries,
        "harmful_withdrawn": sum(m in final for m in harmful) / len(harmful) if harmful else None,
        "occasions_to_withdraw": {m: first_out.get(m) for m in harmful},
        "false_withdrawals": sorted(m for m in final if by_id[m].effect >= 0),
        "helpful_withdrawn": sorted(m for m in final if by_id[m].effect > 0),
    }


def _summary(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {"mean": round(statistics.fmean(values), 4),
            "sd": round(statistics.stdev(values), 4) if len(values) > 1 else 0.0, "n": len(values)}


def run(policies: list[str], *, seeds: int, occasions: int, lag: int, missing: float) -> dict:
    if seeds < 1 or occasions < 100 or lag < 0 or not 0 <= missing < 1:
        raise ValueError("need seeds >= 1, occasions >= 100, lag >= 0 and 0 <= missing < 1")
    memories = default_memories()
    per_policy: dict[str, list[dict]] = {}
    for spec in policies:
        factory = load_policy(spec)
        per_policy[spec] = [run_one(factory, memories, seed=s, occasions=occasions, lag=lag, missing=missing)
                            for s in range(seeds)]
    oracle = per_policy.get("oracle") or [run_one(Oracle, memories, seed=s, occasions=occasions, lag=lag,
                                                  missing=missing) for s in range(seeds)]
    results = {}
    for spec, runs in per_policy.items():
        harmful_ids = [m.id for m in memories if m.effect < 0]
        results[spec] = {
            "success_rate": _summary([r["success_rate"] for r in runs]),
            "regret_vs_oracle": _summary([o["success_rate"] - r["success_rate"] for r, o in zip(runs, oracle)]),
            "harmful_deliveries": _summary([r["harmful_deliveries"] for r in runs]),
            "harmful_withdrawn_share": _summary([r["harmful_withdrawn"] for r in runs]),
            "occasions_to_withdraw": {m: _summary([r["occasions_to_withdraw"][m] for r in runs])
                                      for m in harmful_ids},
            "false_withdrawals_per_run": _summary([len(r["false_withdrawals"]) for r in runs]),
            "helpful_withdrawn_runs": sum(bool(r["helpful_withdrawn"]) for r in runs),
            "runs": runs,
        }
    here = os.path.abspath(__file__)
    with open(here, "rb") as fh:
        source = hashlib.sha256(fh.read()).hexdigest()
    return {
        "benchmark": "CausalMemBench", "version": 1,
        "manifest": {"seeds": seeds, "occasions": occasions, "lag": lag, "missing": missing,
                     "hard_share": HARD_SHARE, "base_easy": BASE_EASY, "base_hard": BASE_HARD,
                     "memories": [dataclasses.asdict(m) for m in memories], "source_sha256": source},
        "results": results,
        "note": "Seeded simulation of the measurement-and-withdrawal layer; it says nothing about "
                "any system's recall quality or live business value.",
    }


def render(report: dict) -> str:
    m = report["manifest"]
    lines = [f"CausalMemBench v{report['version']} -- {m['seeds']} seeds x {m['occasions']} occasions, "
             f"outcome lag {m['lag']}, {m['missing']:.0%} never reported", "",
             "| policy | success | regret vs oracle | harmful deliveries | harmful withdrawn | "
             "false withdrawals / run | runs losing a helpful memory |",
             "| --- | --: | --: | --: | --: | --: | --: |"]

    def fmt(s, pct=False):
        if s is None:
            return "-"
        return f"{s['mean']:.1%} ± {s['sd']:.1%}" if pct else f"{s['mean']:.1f} ± {s['sd']:.1f}"
    for name, r in report["results"].items():
        lines.append(f"| {name} | {fmt(r['success_rate'], True)} | {fmt(r['regret_vs_oracle'], True)} | "
                     f"{fmt(r['harmful_deliveries'])} | {fmt(r['harmful_withdrawn_share'], True)} | "
                     f"{fmt(r['false_withdrawals_per_run'])} | {r['helpful_withdrawn_runs']}/{m['seeds']} |")
    lines += ["", report["note"]]
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--policy", action="append", default=None,
                   help="built-in name or module:factory (repeatable; default: all built-ins)")
    p.add_argument("--seeds", type=int, default=5)
    p.add_argument("--occasions", type=int, default=DEFAULT_OCCASIONS)
    p.add_argument("--lag", type=int, default=DEFAULT_LAG)
    p.add_argument("--missing", type=float, default=DEFAULT_MISSING)
    p.add_argument("--out", default=None, help="write the full JSON report here")
    args = p.parse_args(argv)
    try:
        report = run(args.policy or list(BUILTIN), seeds=args.seeds, occasions=args.occasions,
                     lag=args.lag, missing=args.missing)
    except (ValueError, ImportError, AttributeError) as exc:
        print(f"causalmembench: {exc}", file=sys.stderr)
        return 2
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
