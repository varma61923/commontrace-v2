"""CodingLessonBench v1: isolated real test commands, randomized lesson exposure.

The runner is an operator-supplied executable receiving JSON on stdin and
returning {"patch": "unified diff"}. No model or outcome is fabricated. Tasks
are explicit SWE-style checkouts with a test argv; each occasion starts fresh.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict

from commontrace import experiment


def run(tasks: list[dict], runner: list[str], *, occasions=100, seed=61923, timeout=120) -> dict:
    if not tasks or not runner or type(occasions) is not int or occasions < 2:
        raise ValueError("tasks, an executable runner and at least two occasions are required")
    rng = random.Random(seed)
    observations, rows = [], []
    for index in range(occasions):
        task = tasks[index % len(tasks)]
        injected = rng.random() >= 0.5
        if not all(key in task for key in ("id", "repo", "prompt", "test_command", "lesson")):
            raise ValueError("coding tasks require id, repo, prompt, test_command and lesson")
        if not isinstance(task["test_command"], list) or any(
            not isinstance(value, str) for value in task["test_command"]
        ):
            raise ValueError("test command must be an argument vector")
        with tempfile.TemporaryDirectory(prefix="codinglessonbench-") as directory:
            working = os.path.join(directory, "repo")
            shutil.copytree(
                task["repo"], working, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv")
            )
            request = {
                "task_id": task["id"],
                "prompt": task["prompt"],
                "repo": working,
                "lesson": task["lesson"] if injected else "",
                "occasion_id": str(index),
            }
            succeeded, error, usage, cost = False, None, {}, None
            started = time.perf_counter()
            try:
                generated = subprocess.run(
                    runner,
                    input=json.dumps(request),
                    text=True,
                    capture_output=True,
                    cwd=working,
                    timeout=timeout,
                    check=False,
                )
                if generated.returncode:
                    raise ValueError("runner exited unsuccessfully")
                reply = json.loads(generated.stdout)
                offered = reply.get("usage", {})
                if isinstance(offered, dict):
                    usage = {
                        key: offered[key]
                        for key in ("input_tokens", "output_tokens")
                        if type(offered.get(key)) is int and offered[key] >= 0
                    }
                offered_cost = reply.get("cost_usd")
                if type(offered_cost) in (int, float) and math.isfinite(offered_cost) and offered_cost >= 0:
                    cost = offered_cost
                patch = reply["patch"]
                if not isinstance(patch, str) or len(patch.encode()) > 1000000:
                    raise ValueError("runner patch must be bounded text")
                applied = subprocess.run(
                    ["git", "apply", "-"],
                    input=patch,
                    text=True,
                    capture_output=True,
                    cwd=working,
                    timeout=10,
                    check=False,
                )
                if applied.returncode:
                    raise ValueError("patch did not apply")
                checked = subprocess.run(
                    task["test_command"], cwd=working, capture_output=True, timeout=timeout, check=False
                )
                succeeded = checked.returncode == 0
            except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as exc:
                error = type(exc).__name__
        observation = experiment.HoldoutObservation("coding-lesson", str(index), injected, succeeded, rate=0.5)
        observations.append(observation)
        rows.append(
            {
                "occasion": index,
                "task": task["id"],
                "injected": injected,
                "succeeded": succeeded,
                "execution_error": error,
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "usage": usage,
                "cost_usd": cost,
                "label_relevant": task.get("lesson_relevant") if type(task.get("lesson_relevant")) is bool else None,
                "label_harmful": task.get("lesson_harmful") if type(task.get("lesson_harmful")) is bool else None,
            }
        )
    effects = experiment.analyze(observations, sequential=True)
    effect = asdict(effects[0]) if effects else None
    labelled = [row for row in rows if row["injected"] and row["label_relevant"] is not None]
    precision = sum(row["label_relevant"] for row in labelled) / len(labelled) if labelled else None
    priced = [row for row in rows if row["cost_usd"] is not None]
    return {
        "benchmark": "CodingLessonBench-v1",
        "seed": seed,
        "occasions": occasions,
        "rows": rows,
        "task_manifest_sha256": hashlib.sha256(json.dumps(tasks, sort_keys=True).encode()).hexdigest(),
        "causal_effect": effect,
        "injection_precision": precision,
        "labelled_injections": len(labelled),
        "cost": {
            "known_usd": sum(row["cost_usd"] for row in priced),
            "unpriced_occasions": len(rows) - len(priced),
            "tokens": {key: sum(row["usage"].get(key, 0) for row in rows) for key in ("input_tokens", "output_tokens")},
        },
        "harmful_retirement_N": None,
        "precision_gate_met": precision is not None and precision >= 0.8,
        "uplift_gate_met": bool(effect and effect["effect"] >= 0.05 and effect["ci_low"] > 0),
        "limitations": [
            "Results apply only to the supplied task distribution and runner.",
            "No inference that an unlabelled fixture is SWE-bench.",
            "Precision requires explicit task labels; unpriced calls are never counted as free.",
            "Harmful-retirement N requires an actual lifecycle intervention, not only a negative effect.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--runner", required=True)
    parser.add_argument(
        "--model", required=True, help="Actual runner/model identity, including deterministic fixture labels."
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--occasions", type=int, default=100)
    parser.add_argument("--seed", type=int, default=61923)
    args = parser.parse_args()
    with open(args.tasks, encoding="utf-8") as stream:
        tasks = json.load(stream)
    result = run(tasks, shlex.split(args.runner), occasions=args.occasions, seed=args.seed)
    result["runner_model"] = args.model
    with open(args.output, "w", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
