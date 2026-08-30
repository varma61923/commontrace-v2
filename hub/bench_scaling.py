"""Does serving one customer get more expensive as their corpus grows?

STRATEGY.md §13.2 calls this link 3 and marks it "unmeasured, and the
weakest link nobody has looked at", with a falsifier stated precisely:

    If serving cost grows with corpus size faster than value does, this is
    a services business wearing infrastructure clothes.

That sentence is the difference between an infrastructure multiple and a
services multiple, and it is answerable with a Postgres instance and an
afternoon. This module answers the cost half of it.

WHAT IT MEASURES
----------------
Every read path a customer actually hits, timed against one org's corpus
at geometrically increasing sizes, then fitted on a log-log scale to
recover the scaling exponent:

    latency ~ corpus_size ** alpha

    alpha ~= 0.0   flat. Cost per call does not care how much history the
                   customer has accumulated. This is the infrastructure
                   answer, and it is what an index-backed query gives you.
    alpha ~= 0.5   sublinear. Fine, and typical of an index scan whose
                   selectivity degrades slowly.
    alpha ~= 1.0   linear. Every doubling of a customer's corpus doubles
                   the cost of serving them. Gross margin per customer
                   falls as they succeed, which is the services shape
                   §13.2 warns about.

A single timing at one corpus size cannot distinguish these, which is why
the existing note in hub/DEPLOYMENT.md ("measured at 50k traces: ~113ms
sequential -> ~9ms index scan") is evidence the index works and not
evidence about the exponent. The exponent needs at least three points.

WHAT IT DOES NOT MEASURE
------------------------
The value half. §13.2's falsifier compares cost growth against value
growth, and this measures only cost. Value per query is what
`fleet_outcomes` and `commons_hits` are for, and it needs real customers
rather than synthetic rows. So a good result here does not prove link 3 --
it removes the cost-side objection to it, which is the half that can be
settled without a customer.

Nor is it a benchmark of Postgres, a claim about any particular deployment's
hardware, or a latency SLO. The absolute milliseconds depend entirely on
the machine; only the SHAPE transfers.

Usage:
    python -m hub.bench_scaling                     # default sizes
    python -m hub.bench_scaling --sizes 1000,4000,16000
    python -m hub.bench_scaling --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
import uuid

from sqlalchemy import text

from hub import crud
from hub.config import HubConfig
from hub.db import make_engine, make_session_factory, session_scope
from hub.models import Base, Organization

DEFAULT_SIZES = (1_000, 4_000, 16_000, 64_000)

# Repetitions per measurement. Enough that a single scheduling hiccup does
# not become the reported number, few enough that the whole sweep stays
# minutes rather than hours. The median is reported rather than the mean
# for the same reason.
REPS = 9

# An exponent at or below this counts as "does not grow with the corpus"
# for reporting purposes. Not a magic number: measurement noise on a loaded
# developer machine is comfortably a few percent per point, and over a 64x
# corpus range that noise alone can manufacture an apparent exponent of
# roughly this size out of a genuinely flat curve.
FLAT_EXPONENT = 0.15

# Above this, cost is growing about as fast as the corpus does, which is
# the shape §13.2 names as fatal.
LINEAR_EXPONENT = 0.85


def _fit_exponent(sizes: list[int], latencies: list[float]) -> float | None:
    """Least-squares slope of log(latency) against log(size).

    A power law is a straight line in log-log, and its slope IS the
    exponent -- so this is an ordinary linear regression on transformed
    axes, not a curve fit that could go wrong quietly. Returns None when
    any latency is non-positive (a measurement too fast for the clock),
    since log(0) would otherwise silently produce an infinite slope.
    """
    points = [(math.log(s), math.log(v)) for s, v in zip(sizes, latencies) if v > 0]
    if len(points) < 2:
        return None
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    denom = sum((x - mean_x) ** 2 for x, _ in points)
    if denom == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / denom


def verdict(alpha: float | None) -> str:
    if alpha is None:
        return "unmeasurable"
    if alpha <= FLAT_EXPONENT:
        return "flat"
    if alpha < LINEAR_EXPONENT:
        return "sublinear"
    return "LINEAR-OR-WORSE"


async def _seed(session, org_id: str, n: int, start: int) -> None:
    """Bulk-insert n traces. Raw INSERT ... SELECT over generate_series
    rather than the ORM: building 64,000 Trace objects in Python to measure
    how fast Postgres reads them would spend most of the runtime on the
    part that is not under test.
    """
    await session.execute(
        text(
            """
            INSERT INTO traces (
                id, org_id, title, context_text, solution_text, tags, agent_type,
                agent_id, profile, extensions, watch_condition, review_after,
                contributor, created_at, outcome, trust, retrievals, depth,
                quarantined, quarantine_reason, shared_with_commons,
                shared_rationale, commons_hits, commons_source, commons_votes,
                commons_retraction_reason
            )
            SELECT
                gen_random_uuid(), :org_id,
                (ARRAY[
                    'connection pool exhausted under retry storm',
                    'webhook delivered twice after a gateway timeout',
                    'hydration mismatch on server-rendered timestamps',
                    'advisory lock survived the transaction and blocked a worker',
                    'token refresh raced two concurrent requests',
                    'pagination cursor skipped rows after a concurrent insert',
                    'timezone conversion dropped an hour across a DST boundary',
                    'retry amplified a partial outage into a full one',
                    'cache stampede after a coordinated expiry',
                    'unicode normalization broke an equality check',
                    'floating point sum drifted in a running total',
                    'file descriptor leak under slow client reads',
                    'clock skew between replicas reordered events',
                    'idempotency key reused across two logical writes',
                    'batch job double counted on a mid-run restart',
                    'regex backtracked catastrophically on adversarial input',
                    'signal handler ran during an interrupted syscall',
                    'json number lost precision crossing a language boundary',
                    'index bloat degraded a previously fast lookup',
                    'connection reset mid-stream truncated a response'
                ])[1 + (i % 20)] || ' (case ' || i || ')',
                'observed in production run ' || i || ' after a dependency degraded',
                'bound the retry, set an explicit timeout, and record the outcome',
                ARRAY['postgres', 'retries', 'run' || (i % 50)],
                (ARRAY['support','code','sales'])[1 + (i % 3)],
                'agent-' || (i % 25), '', '{}'::jsonb, '', '', '',
                now() - (i || ' minutes')::interval,
                jsonb_build_object(
                    'resolved', (i % 3) <> 0,
                    'escalated', (i % 7) = 0,
                    'repeated_error', (i % 5) = 0,
                    'frustration_signal', (i % 11) = 0,
                    'tokens_used', 800 + (i % 900),
                    'baseline', i < CAST(:baseline_cut AS bigint)
                ),
                0.5, 0, 0, false, '', false, '', 0, 'org', 0, ''
            FROM generate_series(CAST(:start AS bigint), CAST(:end AS bigint)) AS i
            """
        ),
        {"org_id": org_id, "start": start, "end": start + n - 1, "baseline_cut": start + n // 2},
    )


async def _time(fn, reps: int = REPS) -> float:
    """Median wall-clock milliseconds over `reps` runs, after one warm-up.

    The warm-up is discarded deliberately: the first call of a sweep pays
    for cold shared buffers and a first-time query plan, which is a real
    cost but not the one being compared across corpus sizes.
    """
    await fn()
    samples = []
    for _ in range(reps):
        t0 = time.perf_counter()
        await fn()
        samples.append((time.perf_counter() - t0) * 1000)
    return statistics.median(samples)


async def run(sizes: list[int], as_json: bool) -> int:
    config = HubConfig.from_env()
    engine = make_engine(config)
    session_factory = make_session_factory(engine)

    bench_db_marker = f"scaling-bench-{uuid.uuid4().hex[:8]}"
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "pgcrypto"'))

    async with session_scope(session_factory) as session:
        org = Organization(name=bench_db_marker)
        session.add(org)
        await session.flush()
        org_id = org.id

    # Every path a customer's own traffic exercises. commons_overlap and
    # commons_search are deliberately absent: they scan the OPERATOR's
    # curated corpus, which does not grow when this customer succeeds, so
    # they are not part of this question. They have their own explicit
    # bound (commons.max_corpus_scan()).
    async def probes(session):
        return {
            # Matches ~1/20 of the corpus: what an engineer looking up a
            # specific failure actually issues.
            "search_traces (selective)": lambda: crud.search_traces(
                session, org_id, query="hydration mismatch timestamps", limit=20
            ),
            # Matches every row. Not a realistic query -- included because
            # it bounds the worst case, and because reporting only the
            # selective number would hide that ORDER BY ts_rank has to
            # score every match and cannot be served by an index.
            "search_traces (matches all)": lambda: crud.search_traces(
                session, org_id, query="production run", limit=20
            ),
            "search_traces (by tag)": lambda: crud.search_traces(
                session, org_id, tags=["postgres"], limit=20
            ),
            "list_tags": lambda: crud.list_tags(session, org_id),
            "entitlements": lambda: crud.entitlements(session, org_id),
            "agents_under_management": lambda: crud.agents_under_management(session, org_id),
            "fleet_outcomes": lambda: crud.fleet_outcomes(session, org_id),
        }

    results: dict[str, dict[int, float]] = {}
    seeded = 0
    for target in sizes:
        async with session_scope(session_factory) as session:
            await _seed(session, org_id, target - seeded, seeded + 1)
        seeded = target
        # ANALYZE so the planner's row estimates match reality at this size.
        # Without it the planner keeps stale statistics and may choose a
        # plan appropriate to a much smaller table, which would make the
        # measured curve an artifact of stale stats rather than of size.
        async with engine.begin() as conn:
            await conn.execute(text("ANALYZE traces"))

        async with session_scope(session_factory) as session:
            for name, fn in (await probes(session)).items():
                ms = await _time(fn)
                results.setdefault(name, {})[target] = ms
        if not as_json:
            print(f"  measured at {target:,} traces", file=sys.stderr)

    report = []
    for name, by_size in results.items():
        xs = sorted(by_size)
        ys = [by_size[x] for x in xs]
        alpha = _fit_exponent(xs, ys)
        report.append(
            {
                "path": name,
                "latency_ms": {str(x): round(by_size[x], 3) for x in xs},
                "growth_factor": round(ys[-1] / ys[0], 2) if ys[0] > 0 else None,
                "corpus_factor": round(xs[-1] / xs[0], 2),
                "exponent": round(alpha, 3) if alpha is not None else None,
                "verdict": verdict(alpha),
            }
        )

    async with session_scope(session_factory) as session:
        org = await session.get(Organization, org_id)
        if org is not None:
            await session.delete(org)
    await engine.dispose()

    if as_json:
        print(json.dumps({"sizes": sizes, "reps": REPS, "paths": report}, indent=2))
        return 0

    print(f"\ncorpus sizes: {', '.join(f'{s:,}' for s in sizes)}   "
          f"({sizes[-1] // sizes[0]}x range, median of {REPS} runs)\n")
    header = f"{'read path':<26} " + " ".join(f"{s:>9,}" for s in sizes) + f" {'x':>7} {'alpha':>7}  verdict"
    print(header)
    print("-" * len(header))
    for row in report:
        cells = " ".join(f"{row['latency_ms'][str(s)]:>9.2f}" for s in sizes)
        alpha = f"{row['exponent']:>7.2f}" if row["exponent"] is not None else f"{'-':>7}"
        print(f"{row['path']:<26} {cells} {row['growth_factor']:>6.1f}x {alpha}  {row['verdict']}")
    print()
    print(f"alpha is the fitted exponent in latency ~ size**alpha over a "
          f"{sizes[-1] // sizes[0]}x corpus range.")
    print(f"  <= {FLAT_EXPONENT}   flat        cost per call ignores how much history the customer has")
    print(f"  <  {LINEAR_EXPONENT}   sublinear   grows, but slower than the corpus")
    print(f"  >= {LINEAR_EXPONENT}   LINEAR      every doubling of their corpus doubles the cost of")
    print( "                            serving them -- STRATEGY.md §13.2's failure mode")
    bad = [r["path"] for r in report if r["verdict"] == "LINEAR-OR-WORSE"]
    if bad:
        print(f"\nLinear-or-worse: {', '.join(bad)}")
        print("Each is a path whose cost tracks the customer's own success. Bound it, or")
        print("accept that gross margin per customer falls as they grow.")
    else:
        print("\nNo path grows linearly with the customer's own corpus.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hub.bench_scaling", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument(
        "--sizes",
        default=",".join(str(s) for s in DEFAULT_SIZES),
        help="comma-separated corpus sizes, ascending (default: %(default)s)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    try:
        sizes = sorted({int(s) for s in args.sizes.split(",") if s.strip()})
    except ValueError:
        print(f"error: --sizes must be integers, got {args.sizes!r}", file=sys.stderr)
        return 2
    if len(sizes) < 3:
        print("error: need at least 3 sizes to fit an exponent", file=sys.stderr)
        return 2
    return asyncio.run(run(sizes, args.json))


if __name__ == "__main__":
    raise SystemExit(main())
