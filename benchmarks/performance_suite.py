"""Reproducible, seeded workload timings against a specified source checkout.

No model/network calls, disabled checks, or weaker durability. Setup and imports
are outside each timed operation. JSON contains raw samples and output digests;
run baseline/candidate serially on an otherwise idle host, alternating order.
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import dataclasses
import hashlib
import json
import os
import platform
import random
import statistics
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

CASES = (
    "effect-fit",
    "context-fit",
    "vector-random",
    "vector-duplicates",
    "vector-sqlite-random",
    "vector-sqlite-duplicates",
    "fact-selective",
    "fact-broad",
    "fact-write",
    "conversation-read",
    "conversation-write",
    "signed-write",
    "gateway-cycle",
)


def _canonical(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _canonical(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(v) for v in value]
    if isinstance(value, float):
        return round(value, 10)
    return value


def workload(name: str, root: str) -> Callable[[], Any]:
    rng = random.Random(20261106)
    if name == "effect-fit":
        from unimem import effect_field

        texts = {f"m{i}": f"group{i // 12} refund ledger invoice policy token{i % 12}" for i in range(192)}
        kernel = effect_field.Kernel.lexical(texts)
        estimates = {m: ((i // 12 % 3 - 1) * 0.2 + rng.uniform(-0.01, 0.01), 0.001) for i, m in enumerate(texts)}
        bases = {m: (0.0, 0.04) for m in texts}

        def fit():
            field = effect_field.fit(estimates, bases, 0.04, kernel)
            return field.summary(), [field.prior(m, *bases[m]) for m in texts]

        return fit
    if name == "context-fit":
        from unimem import causal_gate

        rows = [
            causal_gate.Row(
                f"m{m}", f"o{m}-{c}-{i}", bool(i % 2), rng.random() < 0.6, 0.2 + (i % 7) * 0.1, f"robot{c}/site/pick"
            )
            for m in range(8)
            for c in range(48)
            for i in range(96)
        ]
        return lambda: dataclasses.asdict(causal_gate.fit(rows))
    if name.startswith("vector-"):
        from unimem import vector_scoring

        dimension = 192
        pack = struct.Struct(f"<{dimension}f")
        query = pack.unpack(pack.pack(*(rng.uniform(-1, 1) for _ in range(dimension))))
        rows = [
            (
                f"{i:08}",
                pack.pack(*(query if name.endswith("duplicates") else [rng.uniform(-1, 1) for _ in range(dimension)])),
            )
            for i in range(6000)
        ]
        if "sqlite" in name:
            from unimem.vector_store import SQLiteVectorIndex, VectorRecord

            loop = asyncio.new_event_loop()

            async def prepare():
                index = await SQLiteVectorIndex.open(
                    str(Path(root) / "vectors.db"),
                    tenant="benchmark",
                    namespace="performance",
                    model="seeded",
                    dimension=dimension,
                )
                try:
                    for start in range(0, len(rows), 500):
                        await index.upsert(
                            [VectorRecord(key, pack.unpack(data)) for key, data in rows[start : start + 500]]
                        )
                except BaseException:
                    await index.close()
                    raise
                return index

            try:
                index = loop.run_until_complete(prepare())
            except BaseException:
                loop.close()
                raise

            def retrieve():
                return [(hit.key, hit.score) for hit in loop.run_until_complete(index.search(query, top_k=10))]

            def close():
                try:
                    loop.run_until_complete(index.close())
                finally:
                    loop.close()

            retrieve.close = close  # type: ignore[attr-defined]
            return retrieve
        return lambda: vector_scoring.cosine_topk(iter(rows), query, dimension=dimension, top_k=10)
    if name.startswith("fact-"):
        from benchmarks.fact_search import seed
        from unimem import hierarchical

        seed(root, 5000)
        query = "rarecalibration" if name == "fact-selective" else "service timeout"
        hierarchical.search_facts(root, query, scope="alpha", as_of="2026-01-01T00:00:00Z")
        if name != "fact-write":
            return lambda: [
                (f.to_dict(), score)
                for f, score in hierarchical.search_facts(root, query, scope="alpha", as_of="2026-01-01T00:00:00Z")
            ]
        index = 0

        def write_fact():
            nonlocal index
            index += 1
            fact, _status = hierarchical.add_fact(root, statement=f"Unique perf observation {index}", confidence=0.8)
            return fact.statement

        return write_fact
    if name.startswith("conversation-"):
        from unimem.conversation import Options, Store, recall

        store = Store(root, "performance")
        for session in range(10):
            store.add(
                f"s{session}",
                [
                    {
                        "speaker": "user",
                        "text": f"Component {i} uses timeout {i % 30 + 1}. Support team tracks component {i} safely.",
                    }
                    for i in range(100)
                ],
                extract_profile=False,
            )
        index = 0

        def conversation():
            nonlocal index
            index += 1
            if name == "conversation-read":
                result = recall(
                    store,
                    f"What timeout does component {index % 100} use?",
                    options=Options(budget=800, embedder="none", rerank="none"),
                )
                return result.context
            result = store.add(
                f"new{index}",
                [
                    {
                        "speaker": "user",
                        "text": f"Component {i} has a new timeout of {index}. Operator checked rollout.",
                    }
                    for i in range(100)
                ],
                extract_profile=False,
            )
            return result["added"]

        conversation.close = store.close  # type: ignore[attr-defined]
        return conversation
    if name == "signed-write":
        from unimem import frontier_protocol as fp

        index = 0

        def append():
            nonlocal index
            index += 1
            row = fp.append(root, "performance.jsonl", {"index": index, "values": list(range(100))})
            return row["record"]["index"]

        return append
    if name == "gateway-cycle":
        from unimem import gateway, holdout_io

        holdout_io.configure(root, rate=0.5, salt="performance")
        server = gateway.Gateway(root, token="t" * 32, durable=True)
        headers = {"Authorization": "Bearer " + "t" * 32, "Host": "localhost"}
        items = [{"id": f"m{i}", "text": "Retry safely with bounded backoff"} for i in range(8)]
        index = 0

        def cycle():
            nonlocal index
            index += 1
            first = server.handle(
                "POST", "/v1/recall", headers, json.dumps({"occasion_id": f"o{index}", "items": items}).encode()
            )
            second = server.handle(
                "POST",
                "/v1/outcome",
                headers,
                json.dumps({"occasion_id": f"o{index}", "succeeded": bool(index % 2)}).encode(),
            )
            if first.status != 200 or second.status != 200:
                raise AssertionError((first.body, second.body))
            return [m["id"] for m in json.loads(first.body)["deliver"]]

        return cycle
    raise ValueError(name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--case", choices=(*CASES, "all"), default="all")
    parser.add_argument("--trials", type=int, default=7)
    parser.add_argument("--profile")
    parser.add_argument("--details", action="store_true", help="include results for numerical/semantic parity review")
    parser.add_argument("--compare", help="baseline checkout; compare serial fresh processes in alternating order")
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    if not 3 <= args.trials <= 100:
        parser.error("trials must be in 3..100")
    if args.compare:
        if not 2 <= args.rounds <= 10 or args.profile:
            parser.error("comparison needs 2..10 rounds and no profiling")
        reports: dict[str, list[dict]] = {"baseline": [], "candidate": []}
        sources = {"baseline": args.compare, "candidate": args.source}
        for round_index in range(args.rounds):
            order = ("baseline", "candidate") if round_index % 2 == 0 else ("candidate", "baseline")
            for case in CASES if args.case == "all" else (args.case,):
                for arm in order:
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "--source",
                            sources[arm],
                            "--case",
                            case,
                            "--trials",
                            str(args.trials),
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                        env={**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
                    )
                    worker = json.loads(completed.stdout)
                    if len(reports[arm]) <= round_index:
                        reports[arm].append({**worker, "results": []})
                    retained = reports[arm][round_index]
                    reference = reports[arm][0]
                    if any(worker[key] != reference[key] for key in ("implementation_sha256", "harness_sha256")):
                        raise RuntimeError("source changed during the experiment; repeat with a stable checkout")
                    retained["results"].extend(worker["results"])
        cases = []
        for i, first in enumerate(reports["baseline"][0]["results"]):
            base = [r["results"][i] for r in reports["baseline"]]
            new = [r["results"][i] for r in reports["candidate"]]
            ratios = [a["median_ms"] / b["median_ms"] for a, b in zip(base, new)]
            parity = all(a["output_sha256"] == b["output_sha256"] for a, b in zip(base, new))
            cases.append(
                {
                    "case": first["case"],
                    "output_parity": parity,
                    "baseline_median_ms": statistics.median(r["median_ms"] for r in base),
                    "candidate_median_ms": statistics.median(r["median_ms"] for r in new),
                    "speedup_median": statistics.median(ratios),
                    "speedup_min": min(ratios),
                    "speedup_max": max(ratios),
                }
            )
        print(
            json.dumps(
                {
                    "method": "isolated workload per fresh process; alternating AB/BA order; no profiler",
                    "threads": 1,
                    "rounds": args.rounds,
                    "trials_per_round": args.trials,
                    "results": cases,
                    "raw": reports,
                },
                indent=2,
            )
        )
        if not all(c["output_parity"] for c in cases):
            raise SystemExit(1)
        return
    sys.path.insert(0, str(Path(args.source).resolve()))
    profiler = cProfile.Profile() if args.profile else None
    results = []
    for case in CASES if args.case == "all" else (args.case,):
        with tempfile.TemporaryDirectory(prefix="unimem-perf-") as root:
            operation = workload(case, root)
            try:
                cold_started = time.perf_counter_ns()
                operation()  # Imports/caches warm here; mutation IDs are still fresh.
                first_operation_ms = (time.perf_counter_ns() - cold_started) / 1e6
                times, outputs = [], []
                for _ in range(args.trials):
                    if profiler:
                        profiler.enable()
                    started = time.perf_counter_ns()
                    result = operation()
                    elapsed = (time.perf_counter_ns() - started) / 1e6
                    if profiler:
                        profiler.disable()
                    times.append(elapsed)
                    outputs.append(_canonical(result))
                encoded = json.dumps(outputs, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
                results.append(
                    {
                        "case": case,
                        "samples_ms": times,
                        "median_ms": statistics.median(times),
                        "p95_ms": sorted(times)[int(0.95 * (len(times) - 1))],
                        "first_operation_ms": first_operation_ms,
                        "output_sha256": hashlib.sha256(encoded).hexdigest(),
                        **({"outputs": outputs} if args.details else {}),
                    }
                )
            finally:
                close = getattr(operation, "close", None)
                if close:
                    close()
    if profiler:
        profiler.dump_stats(args.profile)
    source = Path(args.source).resolve()
    implementation = hashlib.sha256()
    for path in sorted((source / "unimem").rglob("*.py")):
        implementation.update(str(path.relative_to(source)).encode())
        implementation.update(path.read_bytes())
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "source": str(source),
                "implementation_sha256": implementation.hexdigest(),
                "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "machine": platform.machine(),
                "trials": args.trials,
                "profiled": bool(profiler),
                "results": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
