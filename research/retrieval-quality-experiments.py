"""Compare generic recall settings on official evidence at a fixed context budget.

Gold evidence is consumed only after recall to score selected source IDs. No
generation, answer-based query rewriting, or official judge modification occurs.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

VARIANTS = {
    "baseline": {},
    "direct": {"neighbours_before": 0, "neighbours_after": 0, "primary_hits": 12},
    "direct-wide": {"neighbours_before": 0, "neighbours_after": 0, "primary_hits": 12, "pool": 1000},
    "more-primary": {"primary_hits": 8},
    "short-evidence": {"excerpt_tokens": 120, "primary_hits": 8},
    "diverse": {"broad": True},
    "adaptive-primary": {},
}


def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--dataset", choices=("beam", "locomo"), required=True)
    parser.add_argument("--data", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--conversation-index", type=int, default=0)
    parser.add_argument("--budget", type=int, default=1500)
    parser.add_argument("--per-category", type=int, default=0, help="0 evaluates every question in the conversation")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--variants", default=",".join(VARIANTS))
    parser.add_argument("--embedder", choices=("none", "arctic-m"), default="none")
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    if args.budget < 1 or args.conversation_index < 0 or args.per_category < 0:
        parser.error("Positive budget and nonnegative conversation-index/per-category required")
    names = args.variants.split(",")
    if any(name not in VARIANTS for name in names):
        parser.error("Unknown variant")
    sys.path.insert(0, str(Path(args.checkout).resolve()))
    from benchmarks.conversation_bench import answer_in, beam_cases, locomo_cases
    from commontrace.conversation import Options, Store, embed, recall
    from commontrace.conversation.search import _ADVICE

    cases = beam_cases(args.data) if args.dataset == "beam" else locomo_cases(args.data)
    selected = None
    for index, case in enumerate(cases):
        if index == args.conversation_index:
            selected = case
            break
    if selected is None:
        parser.error("Conversation index exceeds dataset")
    space, sessions, now, questions = selected
    if args.per_category:
        import random

        rng = random.Random(args.seed)
        grouped = defaultdict(list)
        for question in questions:
            grouped[question["type"]].append(question)
        questions = [question for category in sorted(grouped)
                     for question in rng.sample(grouped[category], min(args.per_category, len(grouped[category])))]
    options = Options(budget=args.budget, embedder=None if args.embedder == "none" else args.embedder,
                      rerank=None, neighbours_before=1, neighbours_after=1)
    result = {"revision": subprocess.check_output(["git", "-C", args.checkout, "rev-parse", "HEAD"],
                                                  text=True).strip(),
              "dataset": args.dataset, "source_sha256": digest(args.data), "space": space,
              "conversation_index": args.conversation_index, "seed": args.seed,
              "question_ids": [q["id"] for q in questions],
              "question_sha256": [hashlib.sha256(q["question"].encode()).hexdigest() for q in questions],
              "budget": args.budget,
              "embedder": args.embedder, "generation_calls": 0, "judge_calls": 0,
              "embedding": {"encode_calls": 0, "query_texts": 0, "passage_texts": 0},
              "rows": [], "variants": {}}
    if args.embedder != "none":
        model = embed._model(args.embedder)
        original = model.encode
        prefix = embed.MODELS[args.embedder][1]

        def measured_encode(texts, *call_args, **kwargs):
            result["embedding"]["encode_calls"] += 1
            result["embedding"]["query_texts"] += sum(text.startswith(prefix) for text in texts)
            result["embedding"]["passage_texts"] += sum(not text.startswith(prefix) for text in texts)
            return original(texts, *call_args, **kwargs)

        model.encode = measured_encode
    with Store(args.root, space, read_only=args.read_only) as store:
        started = time.perf_counter()
        if not args.read_only:
            for session, date, messages in sessions:
                store.add(session, messages, session_at=date)
        result["ingestion_seconds"] = time.perf_counter() - started
        result["store_stats"] = store.stats()
        if args.prepare:
            started = time.perf_counter()
            result["preparation"] = embed.prepare(store, embed.Embedder(store.root, args.embedder))
            result["preparation_seconds"] = time.perf_counter() - started
        for name in names:
            base_variant = dataclasses.replace(options, **VARIANTS[name])
            for question in questions:
                variant = base_variant
                if name == "adaptive-primary" and (
                        _ADVICE.search(question["question"]) or re.search(
                            r"\b(?:have I ever|do I (?:usually|typically|generally|normally))\b",
                            question["question"], re.I)):
                    variant = dataclasses.replace(variant, primary_hits=8)
                started = time.perf_counter()
                recalled = recall(store, question["question"], now=now, options=variant)
                latency = time.perf_counter() - started
                references = {turn.ref for turn in store.turns(recalled.turns).values()}
                gold = question["evidence"]
                result["rows"].append({"variant": name, "id": question["id"], "type": question["type"],
                                       "evidence": len(gold & references) / len(gold) if gold else None,
                                       "complete": gold <= references if gold else None,
                                       "answer_in_context": answer_in(recalled.context, question["answer"]),
                                       "gold_evidence_count": len(gold), "retrieved_turns": len(references),
                                       "effective_primary_hits": variant.primary_hits,
                                       "tokens": recalled.tokens, "latency_seconds": latency})
            rows = [row for row in result["rows"] if row["variant"] == name]

            def aggregate(items):
                output = {"n": len(items)}
                for field in ("evidence", "complete", "answer_in_context", "tokens", "latency_seconds"):
                    values = [row[field] for row in items if row[field] is not None]
                    output[field] = statistics.mean(values) if values else None
                    output[field + "_n"] = len(values)
                return output

            result["variants"][name] = {"options": dataclasses.asdict(base_variant), "overall": aggregate(rows),
                                       "by_type": {category: aggregate([row for row in rows
                                                                       if row["type"] == category])
                                                   for category in sorted({row["type"] for row in rows})}}
            print(json.dumps({"space": space, "variant": name,
                              **result["variants"][name]["overall"]}), flush=True)
            Path(args.out).write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    raise SystemExit(main())
