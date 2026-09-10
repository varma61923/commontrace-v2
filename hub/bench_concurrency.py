"""What does this Hub serve under CONCURRENT load, and where does it bind?

hub/bench_scaling.py answers "does cost grow with a customer's corpus?" and
passes. It says so itself, in the paragraph this file exists to retire:

    "It also says nothing about concurrency. Every number above is a single
    query against an otherwise idle database."

hub/DEPLOYMENT.md and STRATEGY.md §13.2 disclaim the same thing in nearly
the same words. STRATEGY.md calls link 3 -- serving cost -- "the weakest
link nobody has looked at". This is the look.

WHAT IS MEASURED
----------------
N simultaneous authenticated clients driving the REAL request path --
`hub/server.py`'s ApiKeyAuthMiddleware, with real issued API keys, over
httpx's ASGI transport -- against a trivial handler. The handler is
deliberately empty: bench_scaling already measures query cost, and mixing
the two would hide the thing under test behind it. What is left is the
PER-REQUEST FLOOR every authenticated call pays before any work happens:
key verification plus two rate-limiter decisions (auth, then read).

Reported per concurrency level: achieved requests/second, and p50/p95/p99
latency. Percentiles rather than a mean because an enterprise SLO is
stated in percentiles, and because a mean hides exactly the tail that a
serialized event loop produces.

WHAT THE SHAPE MEANS
--------------------
Throughput that RISES with concurrency means requests genuinely overlap.
Throughput that stays FLAT as concurrency rises means they do not: each
request is holding something the others need, so adding clients only adds
queueing, and p99 grows while RPS does not. That flat line is the
signature this benchmark exists to detect.

`--backend both` runs the same load through both rate-limiter backends,
which is the comparison worth having:

  memory    hub/abuse.py:RateLimiter -- process-local dict, pure CPU.
  postgres  hub/abuse.py:PostgresRateLimiter -- shared buckets, correct
            across replicas, and the documented choice for any
            multi-replica deployment (hub/DEPLOYMENT.md §6).

The Postgres backend's own docstring already concedes the mechanism: its
`check()` is a SYNC method called un-awaited from async code, so it hands
the query to a background loop and blocks the calling thread on
`run_coroutine_threadsafe(...).result()`. The ASGI event loop makes no
other progress during that wait, twice per authenticated request. Whether
that costs real throughput is not a thing to reason about -- it is a thing
to measure, and this measures it.

Run:
    python -m hub.bench_concurrency
    python -m hub.bench_concurrency --clients 1,8,32,128 --backend both --json

Needs a reachable Postgres (HUB_DATABASE_URL, as hub/bench_scaling.py
does). Cleans up the orgs and buckets it creates.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
import uuid

import httpx
from sqlalchemy import text
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from hub import auth
from hub.abuse import PostgresRateLimiter, RateLimiter
from hub.config import HubConfig
from hub.db import make_engine, make_session_factory, session_scope
from hub.models import Organization
from hub.server import ApiKeyAuthMiddleware

DEFAULT_CLIENTS = (1, 8, 32, 128)

# Requests per concurrency level. Enough that per-level noise averages out
# and the percentiles have something to stand on (p99 of 50 samples is
# theatre), few enough that a full --backend both sweep stays in minutes.
DEFAULT_REQUESTS = 600

# Generous enough never to bind before the server does: this benchmark is
# meant to report a slow Hub, not to manufacture failures out of its own
# client timeouts.
_LIMIT_PER_MINUTE = 10_000_000

# Throughput at the highest concurrency level, divided by throughput at
# the lowest. At or below this, adding clients bought essentially nothing
# and the path is serialized; the band between here and _SCALES is the
# ambiguous middle where some overlap is happening.
_SERIALIZED_SPEEDUP = 1.5
_SCALES_SPEEDUP = 4.0


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile.

    statistics.quantiles interpolates, which invents a latency no request
    actually experienced -- fine for a distribution summary, wrong for a
    number quoted as "p99" in an SLO, where the honest claim is "99% of
    real requests came in at or under a value that really happened".
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def verdict(speedup: float | None) -> str:
    if speedup is None:
        return "not measured"
    if speedup <= _SERIALIZED_SPEEDUP:
        return "SERIALIZED"
    if speedup < _SCALES_SPEEDUP:
        return "partial"
    return "scales"


async def _drive(app: Starlette, raw_key: str, clients: int, requests: int) -> dict:
    """`requests` calls spread over `clients` coroutines, all in flight."""
    headers = {"Authorization": f"Bearer {raw_key}"}
    per_client = max(1, requests // clients)
    latencies: list[float] = []
    failures = 0

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bench") as client:
        # One warm-up per client: the first request through a fresh pool
        # pays connection setup and first-call imports, which is a real
        # cost exactly once and would otherwise land entirely in p99.
        await asyncio.gather(
            *(client.get("/mcp", headers=headers) for _ in range(clients)),
            return_exceptions=True,
        )

        async def one_client() -> None:
            nonlocal failures
            for _ in range(per_client):
                start = time.perf_counter()
                try:
                    response = await client.get("/mcp", headers=headers)
                    elapsed = time.perf_counter() - start
                    if response.status_code != 200:
                        failures += 1
                        continue
                except Exception:  # noqa: BLE001 - a failed request is data, not a crash
                    failures += 1
                    continue
                latencies.append(elapsed)

        wall_start = time.perf_counter()
        await asyncio.gather(*(one_client() for _ in range(clients)))
        wall = time.perf_counter() - wall_start

    return {
        "clients": clients,
        "completed": len(latencies),
        "failures": failures,
        "seconds": wall,
        "rps": (len(latencies) / wall) if wall > 0 else 0.0,
        "p50_ms": _percentile(latencies, 50) * 1000.0,
        "p95_ms": _percentile(latencies, 95) * 1000.0,
        "p99_ms": _percentile(latencies, 99) * 1000.0,
    }


def _build_app(session_factory, backend: str, database_url: str) -> Starlette:
    """The real middleware, with the requested limiter backend behind it.

    The route handler does nothing on purpose -- see the module docstring:
    what is being measured is the floor every authenticated request pays
    before any handler runs.
    """
    async def _ok(request):  # noqa: ARG001 - Starlette hands it the request
        return PlainTextResponse("ok")

    def limiter(name: str):
        if backend == "postgres":
            return PostgresRateLimiter(
                per_minute=_LIMIT_PER_MINUTE, burst=_LIMIT_PER_MINUTE,
                database_url=database_url,
                limiter_name=f"bench-{name}-{uuid.uuid4().hex[:8]}",
            )
        return RateLimiter(per_minute=_LIMIT_PER_MINUTE, burst=_LIMIT_PER_MINUTE)

    app = Starlette(routes=[Route("/mcp", _ok)])
    app.add_middleware(
        ApiKeyAuthMiddleware,
        session_factory=session_factory,
        protected_path="/mcp",
        auth_rate_limiter=limiter("auth"),
        read_rate_limiter=limiter("read"),
    )
    return app


async def run(clients: list[int], requests: int, backends: list[str], as_json: bool) -> int:
    config = HubConfig.from_env()
    engine = make_engine(config)
    session_factory = make_session_factory(engine)

    async with session_scope(session_factory) as session:
        org = Organization(name=f"bench-concurrency-{uuid.uuid4().hex[:8]}")
        session.add(org)
        await session.flush()
        org_id = str(org.id)
        issued = await auth.issue_api_key(session, org.id)
    raw_key = issued.raw_key

    report: dict[str, list[dict]] = {}
    try:
        for backend in backends:
            app = _build_app(session_factory, backend, config.database_url)
            rows = []
            for n in clients:
                if not as_json:
                    print(f"  {backend}: {n} client(s) ...", file=sys.stderr)
                rows.append(await _drive(app, raw_key, n, requests))
            report[backend] = rows
    finally:
        async with session_scope(session_factory) as session:
            await session.execute(
                text("DELETE FROM hub_rate_limit_buckets WHERE limiter_name LIKE 'bench-%'")
            )
            await session.execute(
                text("DELETE FROM api_keys WHERE org_id = :o"), {"o": org_id}
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id = :o"), {"o": org_id}
            )
        await engine.dispose()

    if as_json:
        print(json.dumps({"requests_per_level": requests, "backends": report}, indent=2))
        return 0

    for backend, rows in report.items():
        first, last = rows[0]["rps"], rows[-1]["rps"]
        speedup = (last / first) if first > 0 else None
        print()
        print(f"backend: {backend}   ({requests} requests per level)")
        header = f"  {'clients':>8} {'rps':>10} {'p50 ms':>9} {'p95 ms':>9} {'p99 ms':>9} {'failed':>7}"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for row in rows:
            print(f"  {row['clients']:>8} {row['rps']:>10.1f} {row['p50_ms']:>9.2f} "
                  f"{row['p95_ms']:>9.2f} {row['p99_ms']:>9.2f} {row['failures']:>7}")
        if speedup is not None:
            print(f"  {clients[0]} -> {clients[-1]} clients: {speedup:>.2f}x throughput"
                  f"   {verdict(speedup)}")

    print()
    print("throughput speedup from the lowest to the highest concurrency level:")
    print(f"  <= {_SERIALIZED_SPEEDUP}x  SERIALIZED  adding clients bought nothing -- each request")
    print( "                          holds something the others need, so load becomes queue")
    print(f"  <  {_SCALES_SPEEDUP}x  partial     some overlap, something still contended")
    print(f"  >= {_SCALES_SPEEDUP}x  scales      requests genuinely overlap")
    print()
    print("The handler is empty by design: this is the per-request floor (key")
    print("verification + two rate-limiter decisions) that every authenticated")
    print("call pays before any work happens. Query cost is hub/bench_scaling.py.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Concurrent-load benchmark for the Hub's authenticated request path."
    )
    parser.add_argument(
        "--clients", default=",".join(str(c) for c in DEFAULT_CLIENTS),
        help="comma-separated concurrency levels (default: %(default)s)",
    )
    parser.add_argument(
        "--requests", type=int, default=DEFAULT_REQUESTS,
        help="requests per concurrency level (default: %(default)s)",
    )
    parser.add_argument(
        "--backend", default="memory", choices=("memory", "postgres", "both"),
        help="rate-limiter backend to drive load through (default: %(default)s)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    try:
        clients = sorted({int(c) for c in args.clients.split(",") if c.strip()})
    except ValueError:
        print(f"--clients must be comma-separated integers, got {args.clients!r}", file=sys.stderr)
        return 2
    if not clients:
        print("--clients must name at least one concurrency level", file=sys.stderr)
        return 2

    backends = ["memory", "postgres"] if args.backend == "both" else [args.backend]
    return asyncio.run(run(clients, args.requests, backends, args.json))


if __name__ == "__main__":
    raise SystemExit(main())
