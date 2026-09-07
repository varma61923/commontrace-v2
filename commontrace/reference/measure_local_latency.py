#!/usr/bin/env python3
"""Does one agent's `query` get slower as its fleet learns more?

`hub/SCALING.md` asks this of the Hub and answers it: no read path against a
customer's trace corpus grows linearly. That document measures the SERVER.
This one measures the tier the product actually runs on -- the pip-installable
client and the MCP server, which `protocol/PROTOCOL.md` §5 calls the local tier
and which every agent without a Hub deployment uses exclusively.

The question matters for the same reason link 3 in `STRATEGY.md` §13.2 matters:
a lesson store grows monotonically by design (that is the product working), so
if retrieval cost grows with it, the product gets slower precisely as it starts
delivering value.

Method mirrors `hub/bench_scaling.py` deliberately, so the two numbers can be
read side by side: sweep the corpus over a wide range, take the median of
several runs per point, and fit `latency ~ size**alpha` as an ordinary
least-squares slope on log-log axes. `alpha` near 0 is flat, near 1 is linear.

Reproduce with `python -m commontrace.reference.measure_local_latency`.

The absolute milliseconds are this machine's and transfer to nothing. Only the
exponents, and the ratio between stages, transfer.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
import statistics
import sys
import tempfile
import time

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from commontrace import retrieval  # noqa: E402

SCHEMA_VERSION = 1
DEFAULT_SIZES = (100, 400, 1600, 6400)
RUNS_PER_POINT = 5

# Drawn from six fields rather than one, for the same reason
# commontrace/fixtures/fields/ exists: a corpus of near-identical synthetic
# rows measures the generator, not the retriever (the mistake
# hub/SCALING.md caught in its own first run and pinned a test against).
_VOCAB = [
    "pagination", "cursor", "offset", "retry", "backoff", "timeout", "idempotent",
    "migration", "rollback", "schema", "index", "lock", "deadlock", "transaction",
    "onboarding", "payroll", "grievance", "attrition", "escalation", "policy",
    "pipeline", "renewal", "churn", "discount", "quota", "forecast", "objection",
    "campaign", "segment", "attribution", "creative", "landing", "funnel",
    "trajectory", "kinematics", "calibration", "actuator", "waypoint", "collision",
    "indemnity", "liability", "warranty", "jurisdiction", "arbitration", "clause",
]
_DOMAINS = ["coding", "hr", "sales", "marketing", "robotics", "legal"]


def _lesson_markdown(i: int, rng: random.Random) -> str:
    """A lesson file shaped like a real one -- including the fields that make
    parsing expensive. `last_hit` is a bare YAML date on purpose: PyYAML
    materializes it as a datetime.date, which is exactly the kind of value a
    naive JSON cache would silently corrupt."""
    terms = rng.sample(_VOCAB, 10)
    domain = rng.choice(_DOMAINS)
    return f"""---
name: lesson_bench_{i:06d}
description: {' '.join(terms[:6])}
tags: [{', '.join(terms[:4])}]
agent_type: {domain}
domain: {domain}
importance: {rng.randint(1, 5)}
importance_rationale: "Recurring pattern with a clear fix and a measurable outcome."
importance_history: []
applies_when: {' '.join(terms[3:9])}
do_not_apply_when: {' '.join(rng.sample(_VOCAB, 5))}
uses: {rng.randint(0, 40)}
last_hit: 2026-07-01
source_traces: [2026-07-01_bench-{i:06d}]
source_episodes: []
hub_trace_id: null
status: active
---

## Rule
{' '.join(terms)}.

## Why
Observed repeatedly across {rng.randint(3, 30)} occasions in the {domain} fleet.

## How to apply
{' '.join(rng.sample(_VOCAB, 12))}.

## Counter-examples
{' '.join(rng.sample(_VOCAB, 8))}.
"""


def build_store(root: str, n: int, seed: int = 20260907) -> None:
    """Write `n` lesson files into a real store layout."""
    from commontrace import paths

    ldir = paths.lessons_dir(root)
    os.makedirs(ldir, exist_ok=True)
    rng = random.Random(seed)
    for i in range(n):
        with open(os.path.join(ldir, f"lesson_bench_{i:06d}.md"), "w", encoding="utf-8") as fh:
            fh.write(_lesson_markdown(i, rng))


def _queries(rng: random.Random, k: int = 12) -> list[str]:
    return [" ".join(rng.sample(_VOCAB, 6)) for _ in range(k)]


def measure_point(n: int, runs: int = RUNS_PER_POINT, top_k: int = 3,
                  use_cache: bool = True) -> dict:
    """Time the real client retrieval path at corpus size `n`.

    Stages are timed separately because the split is the finding: if loading
    dominates ranking by two orders of magnitude, making the ranker faster is
    optimizing the wrong thing.

    `use_cache=True` calls exactly what `commontrace query` and the MCP
    server call (`lesson_cache.load_active_with_terms` feeding
    `rank_lessons(term_cache=...)`) -- not a parallel path, the same one, so
    this number cannot drift from what a real query pays.
    `use_cache=False` reproduces the original uncached behaviour for
    comparison: a fresh glob + YAML parse + tokenize on every call.
    """
    from commontrace import lesson_cache
    from commontrace.commands.query_cmd import _iter_active_lessons

    root = tempfile.mkdtemp(prefix=f"ctbench{n}_")
    try:
        build_store(root, n)
        rng = random.Random(7)
        qs = _queries(rng)

        # Warm the filesystem cache so this measures parsing, not cold I/O.
        _iter_active_lessons(root, None)

        load_ms, rank_ms, total_ms = [], [], []
        for r in range(runs):
            q = qs[r % len(qs)]
            if use_cache:
                t0 = time.perf_counter()
                lessons, term_cache = lesson_cache.load_active_with_terms(root, None)
                t1 = time.perf_counter()
                retrieval.rank_lessons(q, lessons, top_k=top_k, term_cache=term_cache)
                t2 = time.perf_counter()
            else:
                t0 = time.perf_counter()
                lessons = _uncached_iter(root)
                t1 = time.perf_counter()
                retrieval.rank_lessons(q, lessons, top_k=top_k)
                t2 = time.perf_counter()
            load_ms.append((t1 - t0) * 1000.0)
            rank_ms.append((t2 - t1) * 1000.0)
            total_ms.append((t2 - t0) * 1000.0)

        return {
            "n_lessons": n,
            "load_ms": statistics.median(load_ms),
            "rank_ms": statistics.median(rank_ms),
            "total_ms": statistics.median(total_ms),
            "load_share": (statistics.median(load_ms) / statistics.median(total_ms)
                           if statistics.median(total_ms) else 0.0),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _uncached_iter(root: str) -> list[tuple[str, dict]]:
    """The pre-cache behaviour, reproduced directly: glob + parse every file,
    every call. Kept only so `--no-cache` can show the number this replaced."""
    import glob as _glob

    from commontrace import frontmatter as _fm
    from commontrace import paths as _paths

    ldir = _paths.lessons_dir(root)
    out = []
    for path in sorted(_glob.glob(os.path.join(ldir, "lesson_*.md"))):
        if os.path.basename(path) == "lesson_template.md":
            continue
        try:
            fm, _body = _fm.read(path)
        except Exception:
            continue
        if fm.get("status") != "active":
            continue
        out.append((path, fm))
    return out


def fit_alpha(points: list[dict], key: str) -> float | None:
    """OLS slope on log-log axes -- the same estimator hub/bench_scaling.py
    uses, so the two documents' exponents mean the same thing."""
    pts = [(p["n_lessons"], p[key]) for p in points if p.get(key) and p["n_lessons"] > 0]
    if len(pts) < 2:
        return None
    xs = [math.log(n) for n, _ in pts]
    ys = [math.log(v) for _, v in pts]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def compute(sizes=DEFAULT_SIZES, runs: int = RUNS_PER_POINT, top_k: int = 3,
           use_cache: bool = True) -> dict:
    points = [measure_point(n, runs=runs, top_k=top_k, use_cache=use_cache) for n in sizes]
    return {
        "schema_version": SCHEMA_VERSION,
        "runs_per_point": runs,
        "top_k": top_k,
        "use_cache": use_cache,
        "points": points,
        "alpha_total": fit_alpha(points, "total_ms"),
        "alpha_load": fit_alpha(points, "load_ms"),
        "alpha_rank": fit_alpha(points, "rank_ms"),
    }


def _verdict(alpha: float | None) -> str:
    if alpha is None:
        return "n/a"
    if alpha < 0.25:
        return "flat"
    if alpha < 0.85:
        return "sublinear"
    return "LINEAR-OR-WORSE"


def render_markdown(report: dict) -> str:
    out = [
        "# Local-tier retrieval latency vs corpus size",
        "",
        f"Median of {report['runs_per_point']} runs per point, "
        f"top_k={report['top_k']}, warm filesystem cache, "
        f"lesson_cache {'ON' if report.get('use_cache', True) else 'OFF'}.",
        "",
        "| lessons | load (parse) | rank (score) | total | load share |",
        "|---:|---:|---:|---:|---:|",
    ]
    for p in report["points"]:
        out.append(
            f"| {p['n_lessons']:,} | {p['load_ms']:.1f} ms | {p['rank_ms']:.1f} ms | "
            f"{p['total_ms']:.1f} ms | {p['load_share']*100:.1f}% |"
        )
    out += ["", "| stage | fitted alpha | |", "|---|---:|---|"]
    for label, key in (("load", "alpha_load"), ("rank", "alpha_rank"), ("total", "alpha_total")):
        a = report[key]
        out.append(f"| {label} | {'n/a' if a is None else f'{a:.2f}'} | {_verdict(a)} |")
    out += [
        "",
        "`alpha` is the fitted exponent in `latency ~ lessons**alpha`, an OLS "
        "slope on log-log axes -- the same estimator `hub/bench_scaling.py` uses.",
    ]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sizes", default=",".join(str(s) for s in DEFAULT_SIZES))
    ap.add_argument("--runs", type=int, default=RUNS_PER_POINT)
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--no-cache", action="store_true",
                    help="Measure the pre-lesson_cache behaviour (fresh glob + "
                         "YAML parse + tokenize every call) instead of the real "
                         "code path, for comparison.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-alpha", type=float, default=None,
                    help="Exit 1 if the fitted total exponent exceeds this. "
                         "This is the CI gate: it fails a change that makes "
                         "retrieval scale worse, not merely slower.")
    ap.add_argument("--max-total-ms", type=float, default=None,
                    help="Exit 1 if the largest corpus's median total exceeds this.")
    args = ap.parse_args()

    sizes = tuple(int(s) for s in args.sizes.split(",") if s.strip())
    report = compute(sizes=sizes, runs=args.runs, top_k=args.top_k,
                     use_cache=not args.no_cache)
    print(json.dumps(report, indent=2) if args.json else render_markdown(report))

    failed = False
    if args.max_alpha is not None and (report["alpha_total"] or 0) > args.max_alpha:
        print(f"FAIL: total alpha {report['alpha_total']:.2f} exceeds "
              f"{args.max_alpha}", file=sys.stderr)
        failed = True
    if args.max_total_ms is not None:
        worst = max(p["total_ms"] for p in report["points"])
        if worst > args.max_total_ms:
            print(f"FAIL: worst median total {worst:.1f} ms exceeds "
                  f"{args.max_total_ms} ms", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
