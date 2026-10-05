"""Reproduce long-message chunking and repeated-weekday grounding CPU work."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import platform
import statistics
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout")
    parser.add_argument("--characters", type=int, default=1_000_000)
    parser.add_argument("--weekdays", type=int, default=2000)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    sys.path.insert(0, args.checkout)
    from commontrace.conversation import store, timeparse

    paragraph = ("alpha beta gamma delta " * (args.characters // 23 + 1))[:args.characters]
    timeline = "We will coordinate " + "on Tuesday and " * args.weekdays
    outputs = {}
    for name, operation in (
        ("split_units", lambda: store.split_units(paragraph)),
        ("ground_weekdays", lambda: timeparse.ground(timeline, dt.date(2026, 10, 5))),
    ):
        times = []
        result = None
        for _ in range(args.runs):
            start = time.perf_counter()
            result = operation()
            times.append((time.perf_counter() - start) * 1000)
        canonical = result if name == "split_units" else [
            [g.start, g.end, g.phrase, g.label, g.lo.isoformat(), g.hi.isoformat()] for g in result]
        outputs[name] = {"milliseconds": times, "median_ms": statistics.median(times),
                         "items": len(result), "output_sha256": hashlib.sha256(
                             json.dumps(canonical, ensure_ascii=False).encode()).hexdigest()}
    print(json.dumps({"python": platform.python_version(), "checkout": args.checkout,
                      "characters": len(paragraph), "weekday_phrases": args.weekdays,
                      "runs": args.runs, "results": outputs}, indent=2))


if __name__ == "__main__":
    main()
