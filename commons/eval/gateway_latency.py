"""How long does one recall take on the path a control loop would call, and does it grow
with history?

    python -m commons.eval.gateway_latency                      # relaxed durability, 10,000 occasions
    python -m commons.eval.gateway_latency --strict --occasions 4000
    python -m commons.eval.gateway_latency --items 20 --json

Drives `Gateway.handle` in-process (no socket, no JSON-over-HTTP framing: add your
transport's cost to these). Each occasion is one `recall` of `--items` memories and
one `outcome`. Latency is reported per bucket of 2,000 occasions: a path whose cost
grows with the log shows up as a rising column, which is the failure this exists to
catch (an earlier version of `record_outcome` was O(n^2) and would have).

The numbers are THIS MACHINE'S. A robot's flash storage can make `--strict` (an
fsync per log line) far slower than here, which is what `--relaxed-durability` is for.
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import tempfile
import time

from commontrace import gateway, holdout_io

BUCKET = 2000


def measure(occasions: int, items: int, durable: bool) -> dict:
    root = tempfile.mkdtemp(prefix="commontrace-lat-")
    try:
        holdout_io.configure(root, rate=0.5, salt="latency")
        token = "t" * 32
        gw = gateway.Gateway(root, token=token, durable=durable)
        headers = {"Authorization": f"Bearer {token}", "Host": "localhost"}
        candidates = [{"id": f"m{i}", "text": "a memory about the task " * 20} for i in range(items)]
        buckets, current = [], []
        for i in range(occasions):
            body = json.dumps({"occasion_id": f"o{i}", "items": candidates, "agent_id": "bench"}).encode()
            t0 = time.perf_counter()
            r = gw.handle("POST", "/v1/recall", headers, body)
            t1 = time.perf_counter()
            gw.handle("POST", "/v1/outcome", headers,
                      json.dumps({"occasion_id": f"o{i}", "succeeded": i % 2 == 0}).encode())
            t2 = time.perf_counter()
            if r.status != 200:
                raise RuntimeError(f"recall returned {r.status}")
            current.append(((t1 - t0) * 1000, (t2 - t1) * 1000))
            if len(current) == BUCKET or i == occasions - 1:
                rec = sorted(c[0] for c in current)
                out = sorted(c[1] for c in current)
                buckets.append({
                    "through_occasion": i + 1,
                    "recall_p50_ms": round(statistics.median(rec), 3),
                    "recall_p99_ms": round(rec[int(0.99 * (len(rec) - 1))], 3),
                    "outcome_p50_ms": round(statistics.median(out), 3),
                    "outcome_p99_ms": round(out[int(0.99 * (len(out) - 1))], 3),
                })
                current = []
        return {"occasions": occasions, "items_per_recall": items,
                "durability": "strict (fsync per line)" if durable else "relaxed (no fsync)",
                "buckets": buckets}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--occasions", type=int, default=10_000)
    ap.add_argument("--items", type=int, default=8)
    ap.add_argument("--strict", action="store_true", help="fsync every log line (the default store behaviour)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    result = measure(args.occasions, args.items, durable=args.strict)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"{result['occasions']:,} occasions x {result['items_per_recall']} items, {result['durability']}")
    print("through   recall p50   recall p99   outcome p50   outcome p99   (ms)")
    for b in result["buckets"]:
        print(f"{b['through_occasion']:7d}   {b['recall_p50_ms']:10.3f}   {b['recall_p99_ms']:10.3f}   "
              f"{b['outcome_p50_ms']:11.3f}   {b['outcome_p99_ms']:11.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
