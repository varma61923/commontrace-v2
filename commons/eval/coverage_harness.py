"""Does the causal answer hold for memory held in Mem0, Letta, Zep, Claude
memory stores and AgentCore, and does automatic withdrawal act in time?

    python -m commons.eval.coverage_harness                     # 200 seeds, every adapter
    python -m commons.eval.coverage_harness --seeds 50 --adapters mem0,zep --jobs 4
    python -m commons.eval.coverage_harness --scenario withdrawal --seeds 100

Slow and opt-in: it is evidence, not a unit test. Each run drives the whole path
a customer's agent drives -- a vendor-shaped client, its adapter, `MeasuredMemory`,
the holdout log, and the same analysis `commontrace experiment` runs -- with
outcomes drawn HERE from a planted truth, never read back from anything
CommonTrace computed. The fakes return each SDK's documented shape (as read from
its published source; see commontrace/memory_adapters.py), not live accounts.

COVERAGE. Three memories per run: one that raises the success rate by 20pp, one
that lowers it by 20pp, one that does nothing. Per adapter, over the seeds:

  * each memory's verdict is the right one (HELPS / HURTS / not claimed);
  * the 95% interval contains the planted effect in at least 90% of runs. It is
    not asked for 95%: the interval is a normal approximation, and a harness
    that demanded exactly nominal coverage would fail honest implementations.

WITHDRAWAL. With the store's harm policy on `withdraw`, how many occasions pass
before the harmful memory stops being delivered, against the fixed-horizon plan
for the same effect; and how often a memory that does not hurt is withdrawn.
The wait is longer than the plan because the verdict acted on is the
anytime-valid one, which stays valid however often it is looked at.

ATTRITION. Outcomes that never arrive. Dropped at random (30%) the estimate must stay
unbiased and its interval must still cover. Dropped only when the occasion failed and
the memory under test was delivered (60%), the estimate IS biased and the validity audit must say
so; the harness reports how often it does, and how often a biased estimate went out
unflagged.

EDITS. A memory rewritten while the experiment runs (its text changes at the midpoint
and the planted effect changes with it). The estimate pools two treatments, so the
audit must flag it in every run.

Seeds are `range(n)`, chosen before any run. No other vendor or product is run.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from types import SimpleNamespace as NS

from commontrace import experiment, holdout_io, integrity, retrieval_io
from commontrace import memory_adapters as ma
from commontrace.commands import experiment_cmd
from commontrace.measure import CausalMemory

GOOD, BAD, NEUTRAL = "good", "bad", "neutral"
EFFECTS = {GOOD: +0.20, BAD: -0.20, NEUTRAL: 0.0}
BASELINE = 0.5
OCCASIONS = 400
RATE = 0.5
COVERAGE_FLOOR = 0.90
VERDICT_FLOOR = 0.90
TEXT = {
    GOOD: "set an idempotency key on webhook handlers",
    BAD: "retry the payment call three times without backoff",
    NEUTRAL: "the office is closed on Fridays",
}


# --- Vendor-shaped fakes: the documented return shapes, nothing more -------------


def _mem0():
    client = NS(search=lambda q, **kw: {"results": [{"id": k, "memory": v} for k, v in TEXT.items()]},
                delete=lambda i: None)
    return ma.Mem0Adapter(client)


def _letta():
    passages = NS(
        search=lambda agent_id, *, query, **kw: NS(
            count=len(TEXT), results=[NS(id=k, content=v, timestamp="t", tags=None) for k, v in TEXT.items()]),
        delete=lambda memory_id, *, agent_id: None,
    )
    return ma.LettaAdapter(NS(agents=NS(passages=passages)), agent_id="agent-1")


def _zep():
    def search(*, query, scope=None, **kw):
        return NS(edges=[NS(uuid_=k, fact=v, name="n") for k, v in TEXT.items()], nodes=None, episodes=None)
    return ma.ZepAdapter(NS(graph=NS(search=search, edge=NS(delete=lambda uuid_: None))), user_id="u1")


def _claude_store():
    def list_(memory_store_id, **params):
        rows = [NS(type="memory_prefix", path="/notes/")]
        rows += [NS(type="memory", id=k, path=f"/notes/{k}.md", content=v, content_sha256=f"sha-{k}")
                 for k, v in TEXT.items()]
        return iter(rows)
    client = NS(beta=NS(memory_stores=NS(memories=NS(list=list_, delete=lambda i, **kw: None))))
    return ma.ClaudeMemoryStoreAdapter(client, memory_store_id="memstore_1")


def _agentcore():
    client = NS(
        retrieve_memories=lambda **kw: [
            {"memoryRecordId": k, "content": {"text": v}, "score": 0.5} for k, v in TEXT.items()],
        delete_memory_record=lambda **kw: None,
    )
    return ma.AgentCoreAdapter(client, memory_id="mem-1", namespace="/actor/a/")


ADAPTERS = {"mem0": _mem0, "letta": _letta, "zep": _zep, "claude-memory-store": _claude_store,
            "agentcore": _agentcore}


# --- Coverage ----------------------------------------------------------------------


def run_seed(adapter: str, seed: int, occasions: int = OCCASIONS) -> dict:
    """One run: returns {memory: {verdict, effect, ci_low, ci_high}} and the integrity verdict."""
    root = tempfile.mkdtemp(prefix=f"commontrace-cov-{adapter}-")
    try:
        holdout_io.configure(root, rate=RATE, salt=f"cov-{adapter}-{seed}")
        memory = ma.MeasuredMemory(ADAPTERS[adapter](), root=root, on_harm="inform")
        rng = random.Random(f"{adapter}:{seed}")
        for i in range(occasions):
            delivered = {item.id for item in memory.recall("webhook fired twice", occasion_id=f"o{i}")}
            p = BASELINE + sum(effect for key, effect in EFFECTS.items() if key in delivered)
            memory.record_outcome(f"o{i}", succeeded=rng.random() < p)
        rows, _rate, _corrupt = experiment_cmd._load(root)
        rows, _salt, _other = experiment_cmd.scope_to_current_salt(root, rows)
        effects = {e.lesson_slug: e for e in experiment.analyze(experiment_cmd._observations(rows))}
        return {
            key: {"verdict": e.verdict, "effect": e.effect, "ci_low": e.ci_low, "ci_high": e.ci_high}
            for key, e in effects.items()
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _run_seed_args(args: tuple) -> tuple[str, int, dict]:
    adapter, seed, occasions = args
    return adapter, seed, run_seed(adapter, seed, occasions)


def summarise(adapter: str, runs: list[dict]) -> dict:
    """Per-memory verdict correctness and interval coverage over `runs`."""
    n = len(runs)
    out: dict = {"adapter": adapter, "seeds": n, "memories": {}}
    for key, truth in EFFECTS.items():
        rows = [r[key] for r in runs if key in r]
        if truth > 0:
            correct = sum(1 for r in rows if r["verdict"] == experiment.VERDICT_HELPS)
        elif truth < 0:
            correct = sum(1 for r in rows if r["verdict"] == experiment.VERDICT_HURTS)
        else:
            correct = sum(1 for r in rows if r["verdict"] not in (experiment.VERDICT_HELPS,
                                                                 experiment.VERDICT_HURTS))
        covered = sum(1 for r in rows if r["ci_low"] <= truth <= r["ci_high"])
        out["memories"][key] = {
            "true_effect": truth,
            "verdict_correct": correct / n,
            "ci_coverage": covered / n,
            "mean_estimate": statistics.fmean(r["effect"] for r in rows) if rows else None,
        }
    out["passed"] = all(
        m["verdict_correct"] >= VERDICT_FLOOR and m["ci_coverage"] >= COVERAGE_FLOOR
        for m in out["memories"].values()
    )
    return out


def coverage(adapters: list[str], seeds: int, jobs: int, occasions: int = OCCASIONS) -> list[dict]:
    work = [(a, s, occasions) for a in adapters for s in range(seeds)]
    results: dict[str, list] = {a: [] for a in adapters}
    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            for adapter, _seed, result in pool.map(_run_seed_args, work, chunksize=4):
                results[adapter].append(result)
    else:
        for item in work:
            adapter, _seed, result = _run_seed_args(item)
            results[adapter].append(result)
    return [summarise(a, results[a]) for a in adapters]


# --- Withdrawal ---------------------------------------------------------------------


def _withdrawal_seed(args: tuple) -> dict:
    seed, max_occasions = args
    root = tempfile.mkdtemp(prefix="commontrace-wd-")
    try:
        holdout_io.configure(root, rate=RATE, salt=f"wd-{seed}")
        path = retrieval_io.config_path(root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"harm_policy": "withdraw"}, fh)
        items = [{"id": k, "memory": TEXT[k]} for k in EFFECTS]
        memory = CausalMemory(lambda q, **kw: items, root=root)
        rng = random.Random(f"wd:{seed}")
        gone: dict[str, int] = {}
        for i in range(max_occasions):
            recall = memory.recall_detailed("q", occasion_id=f"o{i}")
            delivered = {x["id"] for x in recall.items}
            for key in recall.withdrawn:
                gone.setdefault(key, i)
            p = BASELINE + sum(e for k, e in EFFECTS.items() if k in delivered)
            memory.record_outcome(f"o{i}", succeeded=rng.random() < min(max(p, 0.02), 0.98))
        return gone
    finally:
        shutil.rmtree(root, ignore_errors=True)


def withdrawal(seeds: int, jobs: int, max_occasions: int = 1500) -> dict:
    work = [(s, max_occasions) for s in range(seeds)]
    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            runs = list(pool.map(_withdrawal_seed, work, chunksize=2))
    else:
        runs = [_withdrawal_seed(w) for w in work]
    planned = experiment.plan(effect=abs(EFFECTS[BAD]), baseline=BASELINE, rate=RATE).occasions_needed
    when = sorted(r[BAD] for r in runs if BAD in r)
    return {
        "seeds": seeds,
        "planned_occasions_fixed_horizon": planned,
        "harmful_withdrawn": len(when) / seeds,
        "median_occasions_to_withdraw": statistics.median(when) if when else None,
        "p90_occasions_to_withdraw": when[int(0.9 * (len(when) - 1))] if when else None,
        "median_over_plan": round(statistics.median(when) / planned, 2) if when else None,
        "helpful_withdrawn": sum(1 for r in runs if GOOD in r) / seeds,
        "neutral_withdrawn": sum(1 for r in runs if NEUTRAL in r) / seeds,
        "max_occasions": max_occasions,
    }


# --- Attrition and mid-run edits ----------------------------------------------------

ATTRITION_RANDOM = 0.30
ATTRITION_DIFFERENTIAL = 0.60
EDIT_AT = 0.5


def _variant_seed(args: tuple) -> dict:
    """One run of `variant` ("random_attrition", "differential_attrition" or "edit") on the
    Mem0-shaped fake. Returns the per-memory estimates and the audit's verdict."""
    variant, seed, occasions = args
    root = tempfile.mkdtemp(prefix=f"commontrace-{variant}-")
    try:
        holdout_io.configure(root, rate=RATE, salt=f"{variant}-{seed}")
        texts = dict(TEXT)
        client = NS(search=lambda q, **kw: {"results": [{"id": k, "memory": v} for k, v in texts.items()]},
                    delete=lambda i: None)
        memory = ma.MeasuredMemory(ma.Mem0Adapter(client), root=root, on_harm="inform")
        rng = random.Random(f"{variant}:{seed}")
        effects = dict(EFFECTS)
        lost = 0
        for i in range(occasions):
            if variant == "edit" and i == int(occasions * EDIT_AT):
                texts[GOOD] = "set an idempotency key on webhook handlers, then wait for the ack"
                effects[GOOD] = 0.0  # the rewrite does nothing; the pooled arm now averages two treatments
            delivered = {item.id for item in memory.recall("webhook fired twice", occasion_id=f"o{i}")}
            p = BASELINE + sum(e for k, e in effects.items() if k in delivered)
            succeeded = rng.random() < p
            if variant == "random_attrition" and rng.random() < ATTRITION_RANDOM:
                lost += 1
                continue
            if variant == "differential_attrition" and not succeeded and GOOD in delivered \
                    and rng.random() < ATTRITION_DIFFERENTIAL:
                lost += 1
                continue
            memory.record_outcome(f"o{i}", succeeded=succeeded)
        rows, _rate, _corrupt = experiment_cmd._load(root)
        rows, _salt, _other = experiment_cmd.scope_to_current_salt(root, rows)
        report = integrity.audit(rows)
        estimates = {e.lesson_slug: e for e in experiment.analyze(experiment_cmd._observations(rows))}
        return {
            "audit": report.verdict, "lost": lost,
            "memories": {k: {"verdict": e.verdict, "effect": e.effect, "ci_low": e.ci_low, "ci_high": e.ci_high}
                         for k, e in estimates.items()},
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _run_variant(variant: str, seeds: int, jobs: int, occasions: int) -> list[dict]:
    work = [(variant, s, occasions) for s in range(seeds)]
    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            return list(pool.map(_variant_seed, work, chunksize=4))
    return [_variant_seed(w) for w in work]


def attrition(seeds: int, jobs: int, occasions: int = OCCASIONS) -> dict:
    """Random loss keeps the answer honest; loss correlated with outcome and treatment is flagged."""
    # Same number of REPORTED outcomes as a run without loss, so a pass means the loss did
    # not bias or mis-cover, not that power happened to survive it.
    random_runs = _run_variant("random_attrition", seeds, jobs, int(occasions / (1 - ATTRITION_RANDOM)))
    diff_runs = _run_variant("differential_attrition", seeds, jobs, occasions)
    random_summary = summarise("random_attrition", [r["memories"] for r in random_runs])
    # The memory the loss is correlated with in every run: GOOD's injected arm loses its failures.
    biased = [r for r in diff_runs
              if GOOD in r["memories"] and r["memories"][GOOD]["ci_low"] > EFFECTS[GOOD]]
    flagged = [r for r in diff_runs if r["audit"] != integrity.VERDICT_SOUND]
    return {
        "seeds": seeds,
        "random": {"loss": ATTRITION_RANDOM, "memories": random_summary["memories"],
                   "passed": random_summary["passed"],
                   "audit_not_compromised": sum(r["audit"] != integrity.VERDICT_COMPROMISED
                                                for r in random_runs) / seeds},
        "differential": {
            "loss_of_failures_when_delivered": ATTRITION_DIFFERENTIAL,
            "flagged_by_audit": len(flagged) / seeds,
            "estimate_biased_upward": len(biased) / seeds,
            "biased_and_unflagged": sum(1 for r in biased if r["audit"] == integrity.VERDICT_SOUND) / seeds,
        },
    }


def edits(seeds: int, jobs: int, occasions: int = OCCASIONS) -> dict:
    """A memory rewritten mid-run is flagged COMPROMISED, every time."""
    runs = _run_variant("edit", seeds, jobs, occasions)
    return {
        "seeds": seeds,
        "edited_memory_flagged_compromised": sum(r["audit"] == integrity.VERDICT_COMPROMISED for r in runs) / seeds,
        "passed": all(r["audit"] == integrity.VERDICT_COMPROMISED for r in runs),
    }


# --- CLI ----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", choices=("coverage", "withdrawal", "attrition", "edits", "all"), default="all")
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--adapters", default=",".join(ADAPTERS))
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--occasions", type=int, default=OCCASIONS)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    names = [a.strip() for a in args.adapters.split(",") if a.strip()]
    unknown = [a for a in names if a not in ADAPTERS]
    if unknown:
        print(f"unknown adapter(s): {', '.join(unknown)}; known: {', '.join(ADAPTERS)}", file=sys.stderr)
        return 2

    report: dict = {"seeds": args.seeds}
    ok = True
    if args.scenario in ("coverage", "all"):
        report["coverage"] = coverage(names, args.seeds, args.jobs, args.occasions)
        ok = ok and all(c["passed"] for c in report["coverage"])
    if args.scenario in ("withdrawal", "all"):
        report["withdrawal"] = withdrawal(args.seeds, args.jobs)
    if args.scenario in ("attrition", "all"):
        report["attrition"] = attrition(args.seeds, args.jobs, args.occasions)
        ok = ok and report["attrition"]["random"]["passed"]
    if args.scenario in ("edits", "all"):
        report["edits"] = edits(args.seeds, args.jobs, args.occasions)
        ok = ok and report["edits"]["passed"]
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"seeds: {args.seeds} (range(n), fixed before any run)\n")
        for c in report.get("coverage", []):
            print(f"[{'PASS' if c['passed'] else 'FAIL'}] {c['adapter']}")
            for key, m in c["memories"].items():
                print(f"    {key:8s} true {m['true_effect']:+.2f}  verdict right {m['verdict_correct']:.0%}  "
                      f"95% CI covers {m['ci_coverage']:.0%}  mean estimate {m['mean_estimate']:+.3f}")
        if "withdrawal" in report:
            w = report["withdrawal"]
            print("\nautomatic withdrawal (policy `withdraw`, anytime-valid verdict)")
            print(f"    harmful memory withdrawn in {w['harmful_withdrawn']:.0%} of runs, median "
                  f"{w['median_occasions_to_withdraw']} occasions (p90 {w['p90_occasions_to_withdraw']}) "
                  f"vs {w['planned_occasions_fixed_horizon']} planned at a fixed horizon "
                  f"({w['median_over_plan']}x)")
            print(f"    helpful withdrawn in {w['helpful_withdrawn']:.0%}, neutral in {w['neutral_withdrawn']:.0%}")
        if "attrition" in report:
            a = report["attrition"]
            print(f"\nattrition: {a['random']['loss']:.0%} of outcomes lost at random -> "
                  f"{'PASS' if a['random']['passed'] else 'FAIL'} (verdicts and coverage as without loss)")
            d = a["differential"]
            print("    failures lost when the helpful memory was delivered "
                  f"({d['loss_of_failures_when_delivered']:.0%}): audit flagged "
                  f"{d['flagged_by_audit']:.0%} of runs; estimate biased upward in "
                  f"{d['estimate_biased_upward']:.0%}, of which unflagged {d['biased_and_unflagged']:.0%}")
        if "edits" in report:
            e = report["edits"]
            print(f"\nmid-run edit: audit COMPROMISED in {e['edited_memory_flagged_compromised']:.0%} of runs -> "
                  f"{'PASS' if e['passed'] else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
