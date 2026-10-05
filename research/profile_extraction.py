"""Profile checkpoint extraction overhead without generation latency."""
import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
import tracemalloc


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("checkout")
    p.add_argument("--turns", type=int, default=20000)
    p.add_argument("--pending", type=int, default=5)
    p.add_argument("--runs", type=int, default=5)
    a = p.parse_args()
    if a.runs <= 0 or a.turns <= 0 or a.pending < 0:
        p.error("counts must be positive and pending turns non-negative")
    sys.path.insert(0, a.checkout)
    from commontrace.conversation.extract import extract
    from commontrace.conversation.store import Store

    with tempfile.TemporaryDirectory() as root:
        with Store(root, "memory") as s:
            s.add(
                "s",
                ({"text": f"Observation item {i}. " + ("Ordinary content. " * 15)} for i in range(a.turns + a.pending)),
                extract_profile=False,
            )
            s.set_meta("extracted:s", str(a.turns - 1))
            s.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        timings = []
        decoded = []
        results = []
        checkpoints = []
        for i in range(a.runs + 1):
            trial = os.path.join(root, f"trial{i}")
            os.makedirs(os.path.join(trial, "memory", "conversations"))
            shutil.copyfile(
                os.path.join(root, "memory", "conversations", "memory.db"),
                os.path.join(trial, "memory", "conversations", "memory.db"),
            )
            with Store(trial, "memory") as s:
                count = [0]
                orig = s._row_turn

                def row(r):
                    count[0] += 1
                    return orig(r)

                s._row_turn = row
                if i == a.runs:
                    tracemalloc.start()
                start = time.perf_counter()
                result = extract(s, sessions=["s"], complete=lambda _: ('{"memories": []}', {}))
                elapsed = (time.perf_counter() - start) * 1000
                if i == a.runs:
                    _, peak = tracemalloc.get_traced_memory()
                    tracemalloc.stop()
                else:
                    timings.append(elapsed)
                    decoded.append(count[0])
                    results.append(result)
                    checkpoints.append(s.get_meta("extracted:s"))
        print(
            json.dumps(
                {
                    "prior_turns": a.turns,
                    "pending_turns": a.pending,
                    "runs_ms": timings,
                    "median_ms": statistics.median(timings),
                    "decoded_turns": decoded,
                    "results": results,
                    "checkpoints": checkpoints,
                    "peak_traced_bytes": peak,
                }
            )
        )


if __name__ == "__main__":
    main()
