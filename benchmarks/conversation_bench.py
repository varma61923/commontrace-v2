"""LoCoMo and LongMemEval through CommonTrace's conversation memory, end to end.

Each conversation is written with `Store.add` exactly as an agent would write it,
and each question is answered from `recall`'s context. Without a model it measures
what a memory layer owns: how much of the gold evidence reaches the context, how
many tokens that context costs, and whether the gold answer appears in it. With
COMMONTRACE_LLM_* configured, `--answer` also has a model answer from the context
and a judge grade the answer, as published results do.

    python benchmarks/conversation_bench.py --dataset locomo --data locomo10.json
    python benchmarks/conversation_bench.py --dataset longmemeval --data longmemeval_s.json \
        --limit 60 --budget 2000
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from commontrace.conversation import Options, Store, recall  # noqa: E402
from commontrace.conversation.search import assemble, tokens  # noqa: E402

LOCOMO_CATEGORIES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop"}


def locomo_cases(path: str):
    for conv in json.load(open(path)):
        c = conv["conversation"]
        sessions = []
        n = 1
        while f"session_{n}" in c:
            messages = []
            for turn in c[f"session_{n}"]:
                text = turn["text"]
                if turn.get("blip_caption"):
                    text += f" [shares a photo: {turn['blip_caption']}]"
                messages.append({"id": turn["dia_id"], "speaker": turn["speaker"], "text": text})
            sessions.append((f"session {n}", c.get(f"session_{n}_date_time"), messages))
            n += 1
        refs = {m["id"] for _s, _d, ms in sessions for m in ms}
        questions = []
        for i, qa in enumerate(conv["qa"]):
            if qa.get("category") not in LOCOMO_CATEGORIES:
                continue
            gold = {e.strip() for e in qa.get("evidence", []) if e.strip() in refs}
            questions.append({"id": f"{conv['sample_id']}-{i}", "question": qa["question"],
                              "answer": str(qa.get("answer", "")), "type": LOCOMO_CATEGORIES[qa["category"]],
                              "evidence": gold, "sessions": set()})
        yield conv["sample_id"], sessions, None, questions


def longmemeval_cases(path: str, limit: int, seed: int):
    data = json.load(open(path))
    if limit:
        by_type = defaultdict(list)
        for q in data:
            by_type[q["question_type"]].append(q)
        rng = random.Random(seed)
        share = max(1, limit // len(by_type))
        data = [q for t in sorted(by_type) for q in rng.sample(by_type[t], min(share, len(by_type[t])))]
    for q in data:
        sessions, evidence = [], set()
        for sid, date, session in zip(q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"]):
            messages = []
            for j, turn in enumerate(session):
                ref = f"{sid}#{j}"
                if turn.get("has_answer"):
                    evidence.add(ref)
                messages.append({"id": ref, "role": turn["role"], "speaker": turn["role"],
                                 "text": turn["content"]})
            sessions.append((sid, date, messages))
        kind = q["question_type"] + ("-abstain" if q["question_id"].endswith("_abs") else "")
        question = {"id": q["question_id"], "question": q["question"], "answer": str(q["answer"]),
                    "type": kind, "evidence": evidence, "sessions": set(q["answer_session_ids"])}
        yield q["question_id"], sessions, q["question_date"], [question]


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def answer_in(context: str, answer: str) -> bool | None:
    answer = _norm(answer)
    if not answer or len(answer.split()) > 6:
        return None
    return f" {answer} " in f" {_norm(context)} "


ANSWER_PROMPT = """You are answering a question from your memory of past conversations.
Use only the memories below. Dates in [brackets] are when relative time words happened.
Answer in a short phrase. If the memories do not contain the answer, say so.

Memories:
{context}

Question (asked {now}): {question}
Answer:"""

JUDGE_PROMPT = """Grade an answer against a gold answer. Be generous: the answer is CORRECT if it
contains the same information as the gold answer, even if phrased differently or longer.
For time questions, the same date or period in another format is CORRECT.

Question: {question}
Gold answer: {gold}
Answer: {answer}

Reply with exactly one word: CORRECT or WRONG."""


def llm_grade(question: dict, context: str, now: str | None) -> dict:
    from commontrace import llm

    answer, usage = llm.complete(ANSWER_PROMPT.format(context=context, question=question["question"],
                                                      now=now or "now"))
    verdict, _ = llm.complete(JUDGE_PROMPT.format(question=question["question"], gold=question["answer"],
                                                  answer=answer.strip()))
    return {"answer": answer.strip(), "correct": verdict.strip().upper().startswith("CORRECT"),
            "answer_tokens_in": usage.get("input_tokens") or usage.get("prompt_tokens")}


def run(args) -> dict:
    os.makedirs(args.root, exist_ok=True)
    budgets = [int(b) for b in str(args.budget).split(",")]
    opts = Options(budget=budgets[0], embedder=None if args.embedder == "none" else args.embedder,
                   rerank=None if args.rerank == "none" else args.rerank,
                   neighbours_before=args.neighbours, neighbours_after=args.neighbours,
                   profile_facts=args.profile_facts)
    cases = locomo_cases(args.data) if args.dataset == "locomo" else \
        longmemeval_cases(args.data, args.limit, args.seed)
    rows, ingest_s, recall_s, full_tokens = {b: [] for b in budgets}, 0.0, 0.0, []
    for space, sessions, now, questions in cases:
        with Store(args.root, re.sub(r"[^A-Za-z0-9._-]", "_", space)) as store:
            t = time.time()
            if store.stats()["turns"] < sum(len(ms) for _s, _d, ms in sessions):
                for session, date, messages in sessions:
                    store.add(session, messages, session_at=date)
            ingest_s += time.time() - t
            full_tokens.append(sum(tokens(m["text"]) for _s, _d, ms in sessions for m in ms))
            for q in questions:
                t = time.time()
                r = recall(store, q["question"], now=now, options=opts)
                recall_s += time.time() - t
                for budget in budgets:
                    if budget != budgets[0]:
                        opts_b = Options(**{**opts.__dict__, "budget": budget})
                        context, used, n_tokens = assemble(store, q["question"], r.ranked, opts_b)
                    else:
                        context, used, n_tokens = r.context, r.turns, r.tokens
                    kept = store.turns(used).values()
                    refs, sess = {tt.ref for tt in kept}, {tt.session for tt in kept}
                    row = {"id": q["id"], "type": q["type"], "tokens": n_tokens,
                           "evidence": (len(q["evidence"] & refs) / len(q["evidence"])) if q["evidence"] else None,
                           "complete": q["evidence"] <= refs if q["evidence"] else None,
                           "session": (len(q["sessions"] & sess) / len(q["sessions"])) if q["sessions"] else None,
                           "answer_in_context": answer_in(context, q["answer"])}
                    if args.answer:
                        row.update(llm_grade(q, context, now))
                    rows[budget].append(row)
        if args.limit and args.dataset == "locomo" and len(rows[budgets[0]]) >= args.limit:
            break
    n = max(1, len(rows[budgets[0]]))
    return {b: summarize(rs, args, b, ingest_s, recall_s / n, full_tokens) for b, rs in rows.items()}


def _mean(values) -> float | None:
    values = [float(v) for v in values if v is not None]
    return round(statistics.mean(values), 4) if values else None


def summarize(rows, args, budget, ingest_s, recall_s, full_tokens) -> dict:
    def block(rs):
        return {"n": len(rs), "evidence": _mean(r["evidence"] for r in rs),
                "complete": _mean(r["complete"] for r in rs), "session": _mean(r["session"] for r in rs),
                "answer_in_context": _mean(r["answer_in_context"] for r in rs),
                "accuracy": _mean(r.get("correct") for r in rs), "tokens": _mean(r["tokens"] for r in rs)}
    by_type = defaultdict(list)
    for r in rows:
        by_type[r["type"]].append(r)
    return {"dataset": args.dataset, "budget": budget, "embedder": args.embedder, "rerank": args.rerank,
            "neighbours": args.neighbours, "overall": block(rows),
            "by_type": {t: block(rs) for t, rs in sorted(by_type.items())},
            "full_history_tokens": _mean(full_tokens), "ingest_seconds": round(ingest_s, 1),
            "recall_ms": round(1000 * recall_s, 1), "rows": rows}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dataset", choices=("locomo", "longmemeval"), required=True)
    p.add_argument("--data", required=True)
    p.add_argument("--root", default=os.path.join("benchmarks", ".work"),
                   help="store root, reused between runs so ingestion and embeddings are cached")
    p.add_argument("--budget", default="1500", help="tokens; a comma list assembles each ranking at every budget")
    p.add_argument("--embedder", default="arctic-m", choices=("arctic-m", "minilm", "none"))
    p.add_argument("--rerank", default="auto", choices=("auto", "none", "cross-encoder", "cross-encoder-fast"))
    p.add_argument("--neighbours", type=int, default=1)
    p.add_argument("--profile-facts", type=int, default=4)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--answer", action="store_true", help="answer and judge with COMMONTRACE_LLM_*")
    p.add_argument("--out")
    args = p.parse_args(argv)
    results = run(args)
    for budget, result in results.items():
        result.pop("rows") if not args.out else None
    print(json.dumps({b: {k: v for k, v in r.items() if k != "rows"} for b, r in results.items()}, indent=2))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(results, fh, indent=1, default=list)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
