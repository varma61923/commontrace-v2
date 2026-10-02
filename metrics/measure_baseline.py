"""Measure the baseline every later phase reports against.

    python metrics/measure_baseline.py            # writes metrics/baseline.json
    python metrics/measure_baseline.py --check    # prints, writes nothing

Everything is the real CLI in a subprocess (what an agent hook pays), on a
SIMULATED support fleet whose outcomes this script draws itself. Absolute
times are this machine's; the file records the machine so a later run is
compared like with like. Nothing here is a customer measurement.

What is measured, and what is deliberately not:

* activation -- seconds from `init` to the first trace, the first active
  lesson, the first injected lesson, and the compute time to a causal verdict.
  The verdict's compute time is not the time a real fleet waits for one: that
  is set by occasion volume, which `occasions_needed` reports.
* query latency -- `commontrace query` cold-process p50/p95 on a 1,000-lesson store.
* hub -- the per-request floor under concurrent load (`hub/bench_concurrency.py`),
  not search latency, which needs a populated corpus. Skipped, with the reason,
  when HUB_DATABASE_URL is not set.
* install -- exit status of `commontrace install` for each target.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "metrics", "baseline.json")
sys.path.insert(0, REPO)

from commontrace import experiment, holdout_io  # noqa: E402
from commontrace.commands import install_cmd  # noqa: E402

LESSON = "lesson_check_suppression"
BODY = """## Rule
Check the email suppression list before re-sending a password-reset email.

## Why
A bounced address is auto-suppressed, so every re-send silently does nothing.

## How to apply
Look the address up in the suppression list; if present, remove it and trigger the reset again.

## Counter-examples
Not when the customer received the email but the link expired.
"""


def cli(*args: str, cwd: str | None = None) -> tuple[float, subprocess.CompletedProcess]:
    start = time.perf_counter()
    done = subprocess.run(
        [sys.executable, "-m", "commontrace.cli", *args],
        capture_output=True, text=True, cwd=cwd or REPO, check=False,
        env={**os.environ, "PYTHONPATH": REPO},
    )
    return time.perf_counter() - start, done


def must(*args: str, cwd: str | None = None) -> float:
    took, done = cli(*args, cwd=cwd)
    if done.returncode != 0:
        raise SystemExit(f"commontrace {' '.join(args)} failed ({done.returncode}):\n{done.stderr}")
    return took


def activation(seed: int = 0) -> dict:
    root = tempfile.mkdtemp(prefix="ct-baseline-")
    try:
        t0 = time.perf_counter()
        must("init", "--function", "support", "--dest", root)
        must(
            "capture", "--title", "Password reset email never arrived",
            "--context", "Customer requested a reset; no email arrived.",
            "--solution", "The address was on the suppression list after a bounce; removed it.",
            "--tags", "email,reset", "--occasion-id", "T-1", "--resolved", "--dest", root,
        )
        first_trace = time.perf_counter() - t0

        must(
            "lesson", "new", "--slug", LESSON,
            "--description", "Check the suppression list before re-sending a reset email",
            "--agent-type", "support", "--domain", "troubleshooting", "--tags", "email,reset",
            "--applies-when", "A customer reports a missing password-reset email",
            "--do-not-apply-when", "The customer received the email but the link failed",
            "--importance", "4", "--importance-rationale", "Blocks login entirely",
            "--dest", root,
        )
        path = os.path.join(root, "memory", "lessons", f"{LESSON}.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        head = text[: text.index("\n## Rule")]
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(head + "\n" + BODY)
        must("lesson", "approve", LESSON, "--rationale", "baseline measurement", "--dest", root)
        first_active = time.perf_counter() - t0

        _, done = cli("query", "customer password reset email never arrived", "--dest", root)
        injected = LESSON in done.stdout
        first_injected = time.perf_counter() - t0

        rate, per_arm, true_effect, baseline = 0.5, 400, 0.15, 0.60
        must("experiment", "--configure", "--rate", str(rate), "--dest", root)
        config = holdout_io.load_config(root)
        rng = random.Random(seed)
        for i in range(per_arm * 2):
            occasion = f"occ-{i}"
            withheld = holdout_io.assign_and_log(
                root, [LESSON], occasion_id=occasion, rate=rate, salt=config.salt,
            )
            p = baseline if LESSON in withheld else baseline + true_effect
            holdout_io.record_outcome(root, occasion, rng.random() < p)
        took, done = cli("experiment", "--json", "--dest", root)
        verdict = None
        if done.returncode == 0:
            try:
                data = json.loads(done.stdout)
                rows = data.get("effects") or data.get("lessons") or []
                verdict = rows[0].get("verdict") if rows else None
            except ValueError:
                verdict = None
        design = experiment.plan(effect=0.05, baseline=0.70, rate=0.5)
        return {
            "fleet": "simulated support fleet, outcomes drawn by this script",
            "seconds_to_first_trace": round(first_trace, 2),
            "seconds_to_first_active_lesson": round(first_active, 2),
            "seconds_to_first_injected_lesson": round(first_injected, 2),
            "lesson_injected": injected,
            "seconds_to_compute_first_causal_verdict": round(took, 2),
            "simulated_occasions": per_arm * 2,
            "seeded_effect": true_effect,
            "verdict": verdict,
            "occasions_needed_for_5pp_at_70pct_baseline_50pct_holdout": design.occasions_needed,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def query_latency(n_lessons: int = 1000, runs: int = 40) -> dict:
    from commontrace.reference.measure_local_latency import _queries, build_store

    root = tempfile.mkdtemp(prefix="ct-baseline-q-")
    try:
        must("init", "--agent-type", "general", "--dest", root)
        build_store(root, n_lessons)
        queries = _queries(random.Random(1), runs + 3)
        for q in queries[:3]:
            cli("query", q, "--dest", root)
        times = sorted(cli("query", q, "--dest", root)[0] * 1000 for q in queries[3:])
        return {
            "lessons": n_lessons, "runs": runs, "process": "cold subprocess per query",
            "p50_ms": round(statistics.median(times), 1),
            "p95_ms": round(times[int(0.95 * (len(times) - 1))], 1),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def hub_floor() -> dict:
    if not os.environ.get("HUB_DATABASE_URL"):
        return {"skipped": "HUB_DATABASE_URL not set (needs a migrated Postgres)"}
    done = subprocess.run(
        [sys.executable, "-m", "hub.bench_concurrency", "--clients", "1,8,32",
         "--backend", "both", "--json"],
        capture_output=True, text=True, cwd=REPO, check=False,
    )
    if done.returncode != 0:
        return {"skipped": f"bench_concurrency exited {done.returncode}", "stderr_tail": done.stderr[-300:]}
    start = done.stdout.find("{")
    try:
        raw = json.loads(done.stdout[start:]) if start >= 0 else {}
    except ValueError:
        return {"skipped": "could not parse bench_concurrency output"}
    out: dict = {"what": "per-request floor (auth + rate limit) under concurrency, not search latency"}
    for backend, rows in (raw.get("backends") or {}).items():
        if isinstance(rows, list):
            out[backend] = {
                str(r["clients"]): {"rps": round(r["rps"]), "p95_ms": round(r["p95_ms"], 1)}
                for r in rows
            }
    return out


def install_matrix() -> dict:
    results = {}
    for target in install_cmd.TARGETS:
        root = tempfile.mkdtemp(prefix=f"ct-baseline-i-{target}-")
        try:
            must("init", "--agent-type", "general", "--dest", root)
            _, done = cli("install", "--target", target, "--dest", root, cwd=root)
            results[target] = done.returncode == 0
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return {"targets": results, "success": sum(results.values()), "of": len(results)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="print the result without writing it")
    args = ap.parse_args()
    from commontrace import __version__

    result = {
        "schema": 1,
        "commontrace": __version__,
        "machine": {
            "python": platform.python_version(), "platform": platform.platform(),
            "cpus": os.cpu_count(),
        },
        "activation": activation(),
        "query_latency": query_latency(),
        "hub": hub_floor(),
        "install": install_matrix(),
    }
    text = json.dumps(result, indent=2) + "\n"
    if args.check:
        print(text, end="")
    else:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
