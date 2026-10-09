"""Reproduce strict-budget confirmation on questions excluded from development.

    python -m benchmarks.confirmation --data-dir DATA --development-manifest MANIFEST --output FRESH_OUTPUT

Unjudged, lexical controls and local raw vendor profiles only. The public datasets
may have been seen during model training; 'excluded' refers to this change only.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from benchmarks import conversation_bench as bench
from benchmarks.compare import compare_runs
from benchmarks.measurement import dataset_digest, product_digest
from benchmarks.phase0 import INPUTS, validate_payload, write_json
from benchmarks.requests import text_counter_binding

LIMITS = {"locomo": 40, "longmemeval": 12}
CONDITIONS = (("baseline-lexical", "baseline", "commontrace"),
              ("candidate-lexical", "candidate", "commontrace"),
              ("mem0-raw", "candidate", "mem0-raw"),
              ("graphiti-episodic", "candidate", "graphiti-episodic"))


def development_exclusions(manifest, dataset):
    selection = manifest["selection"][dataset]
    if selection["input_sha256"] != INPUTS[dataset][1]:
        raise ValueError("development selection used different dataset bytes")
    ids = selection["question_ids"]
    if (not isinstance(ids, list) or not ids or any(not isinstance(q, str) or not q for q in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError("development question IDs must be nonempty and unique")
    return ids


def validate_strict(payload, expected_contract, exclusion_sha256):
    for summary in payload.values():
        if any(not isinstance(summary.get(key), dict) for key in
               ("overall", "by_type", "bootstrap_95ci", "retrieval_profile")):
            raise ValueError("confirmation lacks required scorecard fields")
        if summary["overall"].get("n") != len(summary["rows"]) or summary["overall"].get("accuracy") is not None:
            raise ValueError("confirmation summary disagrees with its unjudged rows")
        for target in [summary, *summary["modes_detail"].values()]:
            provenance = target["provenance"]
            evaluation = provenance["evaluation"]
            if (evaluation["context_budget"] != expected_contract
                    or evaluation["context_text_tokenizer"] != "tiktoken:cl100k_base"
                    or evaluation["answer_enabled"] is not False
                    or provenance["sampling"].get("question_exclusions_sha256") != exclusion_sha256):
                raise ValueError("confirmation used a foreign counter, budget or exclusion contract")
        for row in summary["rows"]:
            count = row["context_text_tokens"]
            if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= summary["budget"]:
                raise ValueError("confirmation context text exceeded its cap")
            if row["context_budget"]["text_tokens"] != count:
                raise ValueError("confirmation context accounting disagrees")


def run(args):
    repo = Path(__file__).resolve().parent.parent
    data, output = Path(args.data_dir).resolve(), Path(args.output).resolve()
    development_path = Path(args.development_manifest).resolve()
    development_sha = dataset_digest(str(development_path))
    development = json.loads(development_path.read_text())
    if args.seed < 0:
        raise ValueError("seed must be nonnegative")
    exclusions = {dataset: development_exclusions(development, dataset) for dataset in LIMITS}
    for dataset, (filename, expected, _limit) in INPUTS.items():
        if dataset_digest(str(data / filename)) != expected:
            raise ValueError("confirmation inputs do not match pinned official bytes")
    output.mkdir(parents=True, exist_ok=False)
    baseline = output / "baseline-checkout"
    subprocess.run(["git", "worktree", "add", "--detach", str(baseline), args.baseline_commit],
                   cwd=repo, check=True, stdout=subprocess.DEVNULL)
    for source in (repo / "benchmarks").rglob("*.py"):
        target = baseline / source.relative_to(repo)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    manifest = {"schema": 1, "scope": "unjudged excluded-question strict-budget confirmation",
                "answer_accuracy": None, "seed": args.seed, "budgets_text_tokens": [1000, 2000],
                "development_manifest_sha256": development_sha,
                "commits": {"baseline": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=baseline, text=True).strip(),
                            "candidate": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()},
                "runner_sha256": dataset_digest(__file__), "selection": {}, "attempts": [],
                "products": {"baseline": product_digest(str(baseline)), "candidate": product_digest(str(repo))},
                "harness_sha256": dataset_digest(str(repo / "benchmarks" / "conversation_bench.py"))}
    contract = {"unit": "selected-tokenizer-context-text", "counter": text_counter_binding("tiktoken:cl100k_base"),
                "max_attempts": 16, "full_context": "uncapped",
                "helper_sha256": dataset_digest(str(repo / "benchmarks" / "context_budget.py")),
                "counter_source_sha256": dataset_digest(str(repo / "benchmarks" / "requests.py"))}
    manifest["context_budget"] = contract
    completed = {}
    env = dict(os.environ)
    env.update({"OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2",
                "HF_HUB_OFFLINE": "1", "MEM0_TELEMETRY": "False", "GRAPHITI_TELEMETRY_ENABLED": "false"})
    for dataset, limit in LIMITS.items():
        excluded_path = output / (dataset + "-excluded.json")
        write_json(excluded_path, exclusions[dataset])
        exclusion_sha = dataset_digest(str(excluded_path))
        selection_args = argparse.Namespace(dataset=dataset, data=str(data / INPUTS[dataset][0]),
            limit=limit, seed=args.seed, question_exclusions=str(excluded_path))
        cases = bench._load_cases(selection_args)
        questions = [q for _s, _ss, _now, qs in cases for q in qs]
        ids = {q["id"] for q in questions}
        if len(ids) != limit or ids.intersection(exclusions[dataset]):
            raise ValueError("confirmation selection is not exact and disjoint")
        manifest["selection"][dataset] = {"question_ids": sorted(ids), "excluded_ids": exclusions[dataset],
            "input_sha256": INPUTS[dataset][1], "question_exclusions_sha256": exclusion_sha,
            "types": dict(Counter(q["type"] for q in questions))}
        for name, revision, adapter in CONDITIONS:
            label = f"{dataset}-{name}"
            result = output / (label + ".json")
            command = [sys.executable, "-m", "benchmarks.conversation_bench", "--dataset", dataset,
                "--data", selection_args.data, "--root", str(output / "stores" / label),
                "--limit", str(limit), "--seed", str(args.seed), "--question-exclusions", str(excluded_path),
                "--budget", "1000,2000", "--embedder", "none", "--rerank", "none",
                "--memory-adapter", adapter, "--tokenizer", "tiktoken:cl100k_base", "--strict-context-budget",
                "--modes", "memory,full-context,no-memory", "--bootstrap", "--out", str(result)]
            print("START", label, flush=True)
            started = time.monotonic()
            with (output / (label + ".log")).open("w") as log:
                process = subprocess.run(command, cwd=baseline if revision == "baseline" else repo,
                                         env=env, stdout=log, stderr=subprocess.STDOUT)
            attempt = {"label": label, "exit_code": process.returncode, "command": command,
                       "elapsed_seconds": round(time.monotonic() - started, 3), "status": "failed-unavailable"}
            if result.is_file():
                attempt["result_sha256"] = dataset_digest(str(result))
            if process.returncode == 0:
                try:
                    payload = json.loads(result.read_text())
                    validate_payload(payload, ids, manifest["products"][revision], manifest["harness_sha256"],
                                     "none", "none", INPUTS[dataset][1])
                    validate_strict(payload, contract, exclusion_sha)
                except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                    attempt.update(status="failed-validation", reason=f"{type(exc).__name__}: {exc}")
                else:
                    completed[label] = payload
                    attempt["status"] = "completed"
            manifest["attempts"].append(attempt)
            write_json(output / "manifest.json", manifest)
            print(attempt["status"], label, attempt["elapsed_seconds"], flush=True)
    scorecard = {"scope": manifest["scope"], "answer_accuracy": None, "conditions": {}, "comparisons": {}}
    for label, payload in completed.items():
        scorecard["conditions"][label] = {b: {"overall": s["overall"], "by_type": s["by_type"],
            "bootstrap_95ci": s["bootstrap_95ci"], "profile": s["retrieval_profile"]} for b, s in payload.items()}
    for dataset in LIMITS:
        for name, _revision, _adapter in CONDITIONS[1:]:
            left = completed.get(f"{dataset}-baseline-lexical")
            right = completed.get(f"{dataset}-{name}")
            if left is not None and right is not None:
                paired = {}
                for budget in left:
                    try:
                        paired[budget] = {"status": "completed", **compare_runs(left[budget], right[budget], seed=args.seed)}
                    except (ValueError, KeyError, TypeError, RuntimeError) as exc:
                        paired[budget] = {"status": "failed-validation", "reason": f"{type(exc).__name__}: {exc}"}
                scorecard["comparisons"][f"{dataset}-baseline-vs-{name}"] = paired
    if dataset_digest(str(development_path)) != development_sha or dataset_digest(__file__) != manifest["runner_sha256"]:
        raise RuntimeError("confirmation runner or development selection changed during measurement")
    if any(dataset_digest(str(output / (dataset + "-excluded.json"))) != selection["question_exclusions_sha256"]
           for dataset, selection in manifest["selection"].items()):
        raise RuntimeError("confirmation exclusions changed during measurement")
    write_json(output / "scorecard.json", scorecard)
    return 0 if (all(a["status"] == "completed" for a in manifest["attempts"])
                 and all(r["status"] == "completed" for p in scorecard["comparisons"].values() for r in p.values())) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--development-manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--baseline-commit", default="f44bde0")
    parser.add_argument("--seed", type=int, default=20261009)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
