"""Measure source hydration and exact prompt construction for long-session summaries."""
import argparse
import hashlib
import json
import os
import statistics
import sys
import tempfile
import time
import tracemalloc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout")
    parser.add_argument("--turns", type=int, default=10000)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    if min(args.turns, args.runs) < 1:
        parser.error("turns and runs must be positive")
    sys.path.insert(0, os.path.abspath(args.checkout))
    from commontrace.conversation import Store
    from commontrace.conversation.summary import summarize

    with tempfile.TemporaryDirectory() as root, Store(root, "summary-profile") as store:
        store.add("s", [{"text": f"Review item {i}: " + "scheduled project assessment details " * 25}
                        for i in range(args.turns)], extract_profile=False)
        store.set_summary("s", "The project assessment was scheduled.", "extractive")
        original = store._row_turn
        hydrated = 0

        def counted(row):
            nonlocal hydrated
            hydrated += 1
            return original(row)

        store._row_turn = counted
        prompts = []

        def complete(prompt):
            prompts.append(hashlib.sha256(prompt.encode()).hexdigest())
            return "The project assessment was scheduled.", {}

        measurements = {}
        for mode in ("unchanged", "forced_model_prompt"):
            samples = []
            for _ in range(args.runs):
                hydrated = 0
                start = time.perf_counter()
                result = summarize(store, ["s"], force=mode != "unchanged",
                                   method="model" if mode != "unchanged" else "extractive", complete=complete)
                samples.append({"ms": (time.perf_counter() - start) * 1000,
                                "hydrated_turns": hydrated, "result": result})
            hydrated = 0
            tracemalloc.start()
            summarize(store, ["s"], force=mode != "unchanged",
                      method="model" if mode != "unchanged" else "extractive", complete=complete)
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            measurements[mode] = {"samples": samples, "median_ms": statistics.median(s["ms"] for s in samples),
                                  "separate_peak_python_bytes": peak, "peak_run_hydrated_turns": hydrated}
        print(json.dumps({"turns": args.turns, "runs": args.runs, "measurements": measurements,
                          "model_prompt_sha256": sorted(set(prompts)),
                          "limits": "Measures summary orchestration and prompt construction; no model inference. "
                                    "Python allocation peak is measured in separate runs, not RSS."}))


if __name__ == "__main__":
    main()
