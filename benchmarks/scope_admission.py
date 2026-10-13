"""Paired scope admission microbenchmark; excludes storage, ranking and model time.

Run ``python -m benchmarks.scope_admission``. Dense orthogonal contexts represent
bulk memory views with many project/repository labels. Ordinary contexts usually
have far fewer labels; all sizes are reported to show that boundary. This is
exact admission with fresh principal checks, without authorization caching.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import time
from collections.abc import Callable

from commontrace import memory_control


def original_matches(labels: list[str], context: list[str], *, governed: bool = True) -> bool:
    """Frozen pre-optimization admission oracle, including principal checks."""
    if governed and memory_control.REQUIRED_PRINCIPAL_SCOPE.get() \
            and memory_control.REQUIRED_PRINCIPAL_SCOPE.get() not in labels:
        return False
    orthogonal = [s for s in labels if s.split(":", 1)[0] in memory_control.DIMENSIONS and ":" in s]
    legacy = [s for s in labels if s not in orthogonal]
    return set(orthogonal) <= set(context) and (not legacy or bool(set(legacy) & set(context)))


def measure(*, labels: int, trials: int = 5, repetitions: int = 100) -> dict:
    for name, value, upper in (("labels", labels, 10000), ("trials", trials, 100),
                               ("repetitions", repetitions, 10000)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
            raise ValueError(f"{name} must be an integer in 1..{upper}")
    scopes = [f"project:project-{i}" for i in range(labels)]
    cases = [(scopes, scopes, True), (scopes, scopes[:-1], True),
             ([*scopes, "legacy"], [*scopes, "legacy"], True),
             ([*scopes, "legacy"], scopes, True), (scopes, scopes, False)]
    samples: dict[str, list[float]] = {"baseline": [], "candidate": []}
    digests = {}
    functions: dict[str, Callable] = {"baseline": original_matches, "candidate": memory_control.matches}
    token = memory_control.REQUIRED_PRINCIPAL_SCOPE.set("")
    try:
        for trial in range(trials + 1):
            order = ("baseline", "candidate") if trial % 2 == 0 else ("candidate", "baseline")
            for variant in order:
                function = functions[variant]
                start = time.perf_counter_ns()
                results = [function(row, context, governed=governed) for _ in range(repetitions)
                           for row, context, governed in cases]
                elapsed = (time.perf_counter_ns() - start) / 1_000_000
                digests[variant] = hashlib.sha256(bytes(results)).hexdigest()
                if trial:
                    samples[variant].append(elapsed)
        if digests["baseline"] != digests["candidate"]:
            raise RuntimeError("scope admission results changed")
        medians = {variant: statistics.median(values) for variant, values in samples.items()}
        return {"benchmark": "scope-admission-v1", "labels": labels, "trials": trials,
                "calls_per_trial": len(cases) * repetitions, "python": platform.python_version(),
                "baseline_median_ms": medians["baseline"], "candidate_median_ms": medians["candidate"],
                "speedup": medians["baseline"] / medians["candidate"], "samples_ms": samples,
                "admission_sha256": digests["candidate"], "excludes": "storage, ranking, network, model inference"}
    finally:
        memory_control.REQUIRED_PRINCIPAL_SCOPE.reset(token)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=int, nargs="+", default=[1, 10, 100, 1000])
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--repetitions", type=int, default=100)
    args = parser.parse_args()
    try:
        for count in args.labels:
            print(json.dumps(measure(labels=count, trials=args.trials, repetitions=args.repetitions), sort_keys=True))
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
