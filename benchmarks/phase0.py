"""Reproduce the bounded, unjudged development baseline through one harness.

    python -m benchmarks.phase0 --data-dir DATA --output FRESH_OUTPUT

Download official inputs separately. Models/vendors are optional local installs;
failed attempts stay explicit and are excluded from completed comparisons.
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

from benchmarks.compare import compare_runs
from benchmarks.conversation_bench import locomo_cases, longmemeval_cases, sample_cases
from benchmarks.measurement import dataset_digest, product_digest

INPUTS = {
    "locomo": ("locomo10.json", "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4", 154),
    "longmemeval": ("longmemeval_s.json", "08d8dad4be43ee2049a22ff5674eb86725d0ce5ff434cde2627e5e8e7e117894", 30),
}
CONDITIONS = (
    ("baseline-lexical", "baseline", "none", "none", "commontrace"),
    ("candidate-lexical", "candidate", "none", "none", "commontrace"),
    ("candidate-dense", "candidate", "minilm", "none", "commontrace"),
    ("candidate-dense-rerank", "candidate", "minilm", "cross-encoder", "commontrace"),
    ("baseline-dense-rerank", "baseline", "minilm", "cross-encoder", "commontrace"),
    ("mem0-raw", "candidate", "none", "none", "mem0-raw"),
    ("graphiti-episodic", "candidate", "none", "none", "graphiti-episodic"),
)


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def validate_completed(summary, expected_ids, product, harness, embedder, rerank):
    rows = summary["rows"]
    identities = [row["id"] for row in rows]
    if len(identities) != len(expected_ids) or set(identities) != expected_ids:
        raise RuntimeError("completed run has a different question selection")
    if summary["provenance"]["product_sha256"] != product:
        raise RuntimeError("completed run used a different product source")
    if summary["provenance"]["harness_sha256"] != harness:
        raise RuntimeError("completed run used a different harness")
    for target in summary.get("modes_detail", {"memory": summary}).values():
        if target["mode"] in ("memory", "budgeted-history"):
            for row in target["rows"]:
                count = row["tokens"]
                if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= target["budget"]:
                    raise RuntimeError("estimated packing budget exceeded or invalid")
    if embedder != "none" and embedder not in summary["effective_embedders"]:
        raise RuntimeError("requested dense model silently fell back")
    if rerank != "none" and any(row.get("ranked_turn_ids") and row.get("effective_rerank") != rerank
                                for row in rows):
        raise RuntimeError("requested reranker silently fell back on eligible candidates")


def validate_payload(payload, expected_ids, product, harness, embedder, rerank, dataset_sha256):
    if not isinstance(payload, dict) or set(payload) != {"1000", "2000"}:
        raise RuntimeError("completed run lacks the exact requested budgets")
    modes = {"memory", "full-context", "no-memory"}
    for budget, summary in payload.items():
        if summary["budget"] != int(budget) or summary["mode"] != "memory":
            raise RuntimeError("completed run has a mismatched primary budget or mode")
        details = summary.get("modes_detail")
        if not isinstance(details, dict) or set(details) != modes:
            raise RuntimeError("completed run lacks requested reference modes")
        if summary["provenance"]["dataset_sha256"] != dataset_sha256:
            raise RuntimeError("completed run used different dataset bytes")
        validate_completed(summary, expected_ids, product, harness, embedder, rerank)
        for mode, target in details.items():
            if target["budget"] != int(budget) or target["mode"] != mode:
                raise RuntimeError("completed reference has a mismatched budget or mode")
            if target["provenance"]["dataset_sha256"] != dataset_sha256:
                raise RuntimeError("completed reference used different dataset bytes")
            validate_completed(target, expected_ids, product, harness,
                               embedder if mode == "memory" else "none",
                               rerank if mode == "memory" else "none")
        if summary["rows"] != details["memory"]["rows"]:
            raise RuntimeError("primary output differs from its memory reference")


def run(args):
    repository = Path(__file__).resolve().parent.parent
    data, output = Path(args.data_dir).resolve(), Path(args.output).resolve()
    selections = {}
    for dataset, (filename, expected, limit) in INPUTS.items():
        path = data / filename
        if dataset_digest(str(path)) != expected:
            raise ValueError(f"{dataset} input does not match the pinned official bytes")
        cases = (sample_cases(list(locomo_cases(str(path))), limit, args.seed) if dataset == "locomo"
                 else list(longmemeval_cases(str(path), limit, args.seed)))
        questions = [q for _space, _sessions, _now, qs in cases for q in qs]
        selections[dataset] = {"question_ids": [q["id"] for q in questions],
            "confirmation_excluded_ids": [q["id"] for q in questions],
            "types": dict(Counter(q["type"] for q in questions)), "input_sha256": expected}
    if args.seed < 0:
        raise ValueError("seed must be nonnegative")
    output.mkdir(parents=True, exist_ok=False)
    # This checkout belongs to the fresh output; never overwrite a caller's tree.
    baseline = output / "baseline-checkout"
    subprocess.run(["git", "worktree", "add", "--detach", str(baseline), args.baseline_commit],
                   cwd=repository, check=True, stdout=subprocess.DEVNULL)
    for source in (repository / "benchmarks").rglob("*.py"):
        relative = source.relative_to(repository)
        target = baseline / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    # Matched harness, distinct pinned product. Editable installation does not
    # override each subprocess's cwd-local source package.
    manifest = {"schema": 1, "scope": "unjudged development retrieval evidence",
        "answer_accuracy": None, "seed": args.seed, "budgets_estimated": [1000, 2000],
        "exact_text_counter": "tiktoken:cl100k_base", "selection": selections,
        "baseline_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=baseline, text=True).strip(),
        "candidate_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip(),
        "products": {"baseline": product_digest(str(baseline)), "candidate": product_digest(str(repository))},
        "harness_sha256": dataset_digest(str(repository / "benchmarks" / "conversation_bench.py")),
        "threads": 2, "attempts": []}
    write_json(output / "manifest.json", manifest)
    env = dict(os.environ)
    env.update({"OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2",
                "HF_HUB_OFFLINE": "1", "MEM0_TELEMETRY": "False", "GRAPHITI_TELEMETRY_ENABLED": "false"})
    completed = {}
    for dataset, (filename, _digest, limit) in INPUTS.items():
        for name, revision, embedder, rerank, adapter in CONDITIONS:
            label = f"{dataset}-{name}"
            result = output / (label + ".json")
            root = output / "stores" / label
            cwd = baseline if revision == "baseline" else repository
            command = [sys.executable, "-m", "benchmarks.conversation_bench", "--dataset", dataset,
                "--data", str(data / filename), "--root", str(root), "--limit", str(limit),
                "--seed", str(args.seed), "--budget", "1000,2000", "--embedder", embedder,
                "--rerank", rerank, "--memory-adapter", adapter, "--tokenizer", "tiktoken:cl100k_base",
                "--modes", "memory,full-context,no-memory", "--bootstrap", "--out", str(result)]
            print("START", label, flush=True)
            started = time.monotonic()
            with (output / (label + ".log")).open("w", encoding="utf-8") as log:
                process = subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)
            attempt = {"label": label, "exit_code": process.returncode,
                       "elapsed_seconds": round(time.monotonic() - started, 3), "command": command}
            if result.is_file():
                attempt["result_sha256"] = dataset_digest(str(result))
            if process.returncode == 0:
                try:
                    payload = json.loads(result.read_text(encoding="utf-8"))
                    expected_ids = set(selections[dataset]["question_ids"])
                    validate_payload(payload, expected_ids, manifest["products"][revision],
                                     manifest["harness_sha256"], embedder, rerank, INPUTS[dataset][1])
                except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                    attempt["status"] = "failed-validation"
                    attempt["reason"] = f"{type(exc).__name__}: {exc}"
                else:
                    attempt["status"] = "completed"
                    completed[label] = payload
            else:
                attempt["status"] = "failed-unavailable"
            manifest["attempts"].append(attempt)
            write_json(output / "manifest.json", manifest)
            print(attempt["status"].upper(), label, attempt["elapsed_seconds"], flush=True)
    scorecard = {"scope": manifest["scope"], "answer_accuracy": None, "attempts": manifest["attempts"],
                 "conditions": {}, "comparisons": {}}
    for label, payload in completed.items():
        scorecard["conditions"][label] = {budget: {
            "overall": summary["overall"], "by_type": summary["by_type"],
            "bootstrap_95ci": summary["bootstrap_95ci"], "profile": summary["retrieval_profile"],
            "effective_embedders": summary["effective_embedders"],
            "effective_rerank": dict(Counter(str(r.get("effective_rerank")) for r in summary["rows"])),
            "worst_evidence_questions": [{key: row[key] for key in (
                "id", "type", "evidence", "complete", "tokens", "context_text_tokens", "gold_turn_ids")}
                for row in sorted((r for r in summary["rows"] if r["evidence"] is not None),
                                  key=lambda r: (r["evidence"], r["id"]))[:20]],
        } for budget, summary in payload.items()}
    for dataset in INPUTS:
        for left, right in (("baseline-lexical", "candidate-lexical"),
                            ("baseline-dense-rerank", "candidate-dense-rerank"),
                            ("candidate-lexical", "candidate-dense"),
                            ("candidate-dense", "candidate-dense-rerank"),
                            ("candidate-lexical", "mem0-raw"),
                            ("candidate-lexical", "graphiti-episodic")):
            baseline_run = completed.get(f"{dataset}-{left}")
            candidate_run = completed.get(f"{dataset}-{right}")
            if baseline_run is not None and candidate_run is not None:
                comparisons = {}
                for budget in baseline_run:
                    try:
                        comparisons[budget] = {"status": "completed", **compare_runs(
                            baseline_run[budget], candidate_run[budget], seed=args.seed)}
                    except (ValueError, TypeError, KeyError, RuntimeError) as exc:
                        comparisons[budget] = {"status": "failed-validation", "reason": f"{type(exc).__name__}: {exc}"}
                scorecard["comparisons"][f"{dataset}-{left}-vs-{right}"] = comparisons
    write_json(output / "scorecard.json", scorecard)
    print("REPORT", output / "scorecard.json", flush=True)
    attempts_ok = all(a["status"] == "completed" for a in manifest["attempts"])
    comparisons_ok = all(result["status"] == "completed" for comparison in scorecard["comparisons"].values()
                         for result in comparison.values())
    return 0 if attempts_ok and comparisons_ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--output", required=True, help="Fresh output directory; existing paths are refused")
    parser.add_argument("--baseline-commit", default="f44bde0")
    parser.add_argument("--seed", type=int, default=20261008)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
