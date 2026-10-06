"""Run unmodified official benchmark protocols against a category-balanced local subset.

Only loopback generation/judging endpoints are allowed. Dataset inputs and model
weights remain outside git; results include input hashes and observable model calls.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit


def local_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise argparse.ArgumentTypeError("Use an HTTP endpoint on localhost, 127.0.0.1 or ::1.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("Endpoint credentials, query strings and fragments are unsupported.")
    return value.rstrip("/")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def balanced(items: list[dict], key: str, count: int, seed: int) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        groups[str(item[key])].append(item)
    rng = random.Random(seed)
    return [item for category in sorted(groups)
            for item in rng.sample(groups[category], min(count, len(groups[category])))]


def subset(args, output: Path) -> dict:
    source = Path(args.data).resolve()
    details = {"source_sha256": sha256(source), "seed": args.seed,
               "per_category": args.per_category, "conversation_index": args.conversation_index}
    if args.dataset == "locomo":
        conversations = json.loads(source.read_text())
        conversation = dict(conversations[args.conversation_index])
        eligible = [dict(q, original_index=i) for i, q in enumerate(conversation["qa"])
                    if q.get("category") in (1, 2, 3, 4)]
        selected = balanced(eligible, "category", args.per_category, args.seed)
        details["selected_original_indices"] = [q["original_index"] for q in selected]
        details["conversation_id"] = conversation["sample_id"]
        conversation["qa"] = [{k: v for k, v in q.items() if k != "original_index"} for q in selected]
        output.write_text(json.dumps([conversation]))
        details["categories"] = [q["category"] for q in selected]
    elif args.dataset == "longmemeval":
        items = json.loads(source.read_text())
        for item in items:
            item["selection_category"] = item["question_type"] + (
                "-abstain" if item["question_id"].endswith("_abs") else "")
        selected = balanced(items, "selection_category", args.per_category, args.seed)
        details["selected_question_ids"] = [q["question_id"] for q in selected]
        details["categories"] = [q["selection_category"] for q in selected]
        output.write_text(json.dumps([{k: v for k, v in q.items() if k != "selection_category"}
                                      for q in selected]))
    else:
        import pandas as pd

        frame = pd.read_parquet(source).iloc[[args.conversation_index]].copy()
        row = frame.iloc[0]
        probing = ast.literal_eval(row["probing_questions"]) if isinstance(row["probing_questions"], str) \
            else row["probing_questions"]
        rng = random.Random(args.seed)
        chosen = {category: rng.sample(list(items), min(args.per_category, len(items)))
                  for category, items in sorted(probing.items())}
        frame["probing_questions"] = [repr(chosen)]
        frame.to_parquet(output)
        details["conversation_id"] = int(row["conversation_id"])
        details["categories"] = [category for category, items in chosen.items() for _ in items]
    details["subset_sha256"] = sha256(output)
    details["questions"] = len(details["categories"])
    return details


def execute(checkout: str, call_log: str, argv: list[str], timeout_seconds: int) -> int:
    """Observe provider responses without changing answer prompts, judges or scores."""
    sys.path.insert(0, checkout)
    from benchmarks import conversation_bench
    from commontrace import llm

    llm._TIMEOUT_SECONDS = timeout_seconds
    original = llm._post_json
    with open(call_log, "w", encoding="utf-8") as output:
        def observed(url, headers, payload):
            start = time.perf_counter()
            content = "\n".join(message.get("content", "") for message in payload.get("messages", []))
            entry = {"model": payload.get("model"), "prompt_sha256": hashlib.sha256(
                json.dumps(payload.get("messages"), sort_keys=True).encode()).hexdigest(),
                "kind": "answer" if content.startswith("You answer from your memory") else "judge"}
            try:
                result = original(url, headers, payload)
                entry["choices"] = result.get("choices")
                entry["usage"] = result.get("usage")
                return result
            except Exception as exc:
                entry["error"] = type(exc).__name__ + ": " + str(exc)
                raise
            finally:
                entry["latency_seconds"] = time.perf_counter() - start
                output.write(json.dumps(entry) + "\n")
                output.flush()
        llm._post_json = observed
        return conversation_bench.main(argv)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=("locomo", "longmemeval", "beam"))
    parser.add_argument("--data")
    parser.add_argument("--checkout", action="append", default=[])
    parser.add_argument("--output")
    parser.add_argument("--endpoint", type=local_endpoint, default="http://127.0.0.1:18081/v1")
    parser.add_argument("--model", default="qwen2.5-1.5b-instruct-q4_k_m")
    parser.add_argument("--model-file")
    parser.add_argument("--per-category", type=int, default=1)
    parser.add_argument("--conversation-index", type=int, default=0)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--budget", type=int, default=1500)
    parser.add_argument("--embedder", choices=("none", "arctic-m", "minilm"), default="arctic-m")
    parser.add_argument("--timeout-seconds", type=int, default=300,
                        help="Local CPU inference HTTP timeout; prompts and scoring are unchanged")
    parser.add_argument("--retrieval-only", action="store_true",
                        help="Run official dataset retrieval metrics without generation or judge calls")
    parser.add_argument("--execute", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--call-log", help=argparse.SUPPRESS)
    args, benchmark_argv = parser.parse_known_args(argv)
    if args.execute:
        return execute(args.checkout[0], args.call_log, benchmark_argv[1:], args.timeout_seconds)
    if not all((args.dataset, args.data, args.checkout, args.output)):
        parser.error("--dataset, --data, --checkout and --output are required")
    if args.per_category < 1 or args.budget < 1 or args.conversation_index < 0 or args.timeout_seconds < 1:
        parser.error("Positive per-category/budget and nonnegative conversation-index are required")
    output = Path(args.output).resolve()
    if output.exists() and any(output.iterdir()):
        parser.error("Use a new empty output directory so recorded results and cold stores are preserved")
    for checkout in args.checkout:
        status = subprocess.check_output(["git", "-C", checkout, "status", "--porcelain"], text=True)
        if status.strip():
            parser.error("Evaluation checkouts must be clean so the recorded git revision describes the code")
    output.mkdir(parents=True, exist_ok=True)
    data = output / ("subset.parquet" if args.dataset == "beam" else "subset.json")
    manifest = {"dataset": args.dataset, "selection": subset(args, data),
                "model": args.model, "model_sha256": sha256(Path(args.model_file)) if args.model_file else None,
                "endpoint": args.endpoint, "budget": args.budget, "embedder": args.embedder,
                "timeout_seconds": args.timeout_seconds, "answer_and_judge_enabled": not args.retrieval_only,
                "judge_model_matches_published_defaults": False, "runs": []}
    environment = dict(os.environ)
    environment.update(COMMONTRACE_LLM_PROVIDER="openai-compatible", COMMONTRACE_LLM_API_KEY="local-only",
                       COMMONTRACE_LLM_BASE_URL=args.endpoint, COMMONTRACE_LLM_MODEL=args.model,
                       HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", OMP_NUM_THREADS="1",
                       OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", PYTHONUNBUFFERED="1")
    for index, checkout in enumerate(args.checkout):
        checkout = str(Path(checkout).resolve())
        revision = subprocess.check_output(["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip()
        prefix = output / f"{index}-{revision[:8]}"
        root = str(prefix) + "-store"
        result_path = str(prefix) + ".json"
        calls_path = str(prefix) + "-calls.jsonl"
        command = [sys.executable, str(Path(__file__).resolve()), "--execute", "--checkout", checkout,
                   "--call-log", calls_path, "--timeout-seconds", str(args.timeout_seconds),
                   "--", "--dataset", args.dataset, "--data", str(data),
                   "--root", root, "--out", result_path, "--budget", str(args.budget),
                   "--embedder", args.embedder, "--rerank", "none", "--judge", args.dataset,
                   "--answer-model", args.model, "--judge-model", args.model, "--no-cache", "--seed", str(args.seed)]
        if not args.retrieval_only:
            command.append("--answer")
        start = time.perf_counter()
        with open(str(prefix) + ".log", "w") as log:
            result = subprocess.run(command, env=environment, cwd=checkout, stdout=log, stderr=log)
        calls = [json.loads(line) for line in Path(calls_path).read_text().splitlines()]
        manifest["runs"].append({"git_sha": revision, "checkout": checkout, "command": command,
                                 "exit_code": result.returncode, "wall_seconds": time.perf_counter() - start,
                                 "result": result_path, "calls": calls_path,
                                 "runner_sha256": sha256(Path(checkout) / "benchmarks" / "conversation_bench.py"),
                                 "judge_sha256": sha256(Path(checkout) / "benchmarks" / "judges" /
                                                        (args.dataset + ".py")),
                                 "model_calls": len(calls),
                                 "model_call_errors": sum("error" in call for call in calls),
                                 "model_input_tokens": sum((call.get("usage") or {}).get("prompt_tokens", 0)
                                                           for call in calls),
                                 "model_output_tokens": sum((call.get("usage") or {}).get("completion_tokens", 0)
                                                            for call in calls),
                                 "truncated_completions": sum(choice.get("finish_reason") == "length"
                                                              for call in calls for choice in
                                                              (call.get("choices") or []))})
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
        print(json.dumps(manifest["runs"][-1]), flush=True)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
