"""Seventeen offline memory-task contracts; synthetic evidence, not leaderboard accuracy.

Gold source labels are consumed only after public recall. This suite measures
source delivery and lexical abstention; it does not generate or grade answers.
"""
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time

from commontrace.conversation import Options, Store, recall
from commontrace.conversation.search import forget_store

TASKS = (
    ("single-hop", "Which city did Mira visit in March?", ("visit",)),
    ("multi-hop", "Who founded the company that Mira's mentor works for?", ("mentor", "founder")),
    ("open-domain-temporal", "When was Atlas incorporated?", ("founder",)),
    ("temporal-single-session-user", "When did I visit Rome in March?", ("visit",)),
    ("single-session-assistant", "What did you recommend for authentication?", ("assistant",)),
    ("single-session-preference", "What food do I prefer?", ("preference",)),
    ("knowledge-update", "What job do I have now?", ("new-job",)),
    ("temporal-reasoning", "How many months passed between the Orion prototype and pilot?", ("prototype", "pilot")),
    ("multi-session", "Which cities did I live in before and after moving?", ("old-home", "new-home")),
    ("preference-following", "Suggest a dinner that fits my food preferences.", ("preference",)),
    ("instruction-following", "Draft a project status update.", ("instruction",)),
    ("information-extraction", "Extract the Orion production budget and deployment city.", ("production",)),
    ("multi-session-reasoning", "Compare Orion prototype, pilot and production across our sessions.",
     ("prototype", "pilot", "production")),
    ("summarization", "Summarize Orion launch progress across our sessions.", ("prototype", "pilot", "production")),
    ("event-ordering", "In what order did Orion reach prototype, pilot and production?",
     ("prototype", "pilot", "production")),
    ("abstention", "What is Mira's passport serial identifier?", ()),
    ("contradiction-resolution", "Which conflicting Orion production budgets were reported?", ("production", "conflict")),
)


def history():
    """Fictional source history with role, time, preference and correction evidence."""
    return [
        ("January", "2024-01-10", [
            ("old-job", "user", "I work as a nurse."),
            ("old-home", "user", "I live in Paris."),
            ("preference", "user", "I prefer vegetarian food and dislike seafood."),
            ("instruction", "user", "Always format your answers as bullet points."),
            ("prototype", "user", "Orion launch reached prototype on January 10 with a budget of 8000."),
            ("mentor", "user", "Mira's mentor Arun works for Atlas."),
        ]),
        ("March", "2024-03-12", [
            ("founder", "user", "Nora founded Atlas, which was incorporated in 1998."),
            ("visit", "user", "I am Mira. I visited Rome on March 12."),
            ("pilot", "user", "Orion launch reached pilot on March 12 with a budget of 10000."),
            ("assistant", "assistant", "For authentication, I recommend OAuth with short-lived tokens."),
            ("new-job", "user", "I work as a teacher now."),
        ]),
        ("May", "2024-05-15", [
            ("new-home", "user", "I moved to Lyon."),
            ("production", "user", "Orion launch reached production on May 15 in Berlin with a budget of 12000."),
            ("conflict", "user", "Orion production budget was reported as 15000 in a separate unsigned report."),
        ]),
    ]


def run(budget=300):
    rows = []
    with tempfile.TemporaryDirectory(prefix="commontrace-task-suite-") as root, Store(root, "user") as store:
        for session, date, messages in history():
            store.add(session, [{"id": identity, "role": role, "speaker": "Mira" if role == "user" else "Assistant",
                                 "text": text} for identity, role, text in messages], session_at=date)
        for task, question, gold in TASKS:
            for strategy in ("legacy", "coverage-v2"):
                forget_store(store)
                start = time.perf_counter()
                result = recall(store, question, options=Options(
                    budget=budget, embedder="none", rerank=None, context_strategy=strategy))
                elapsed = 1000 * (time.perf_counter() - start)
                refs = {turn.ref for turn in store.turns(result.turns).values()}
                rows.append({"task": task, "strategy": strategy, "tokens": result.tokens, "latency_ms": elapsed,
                             "source_recall": len(set(gold) & refs) / len(gold) if gold else None,
                             "complete_sources": set(gold) <= refs if gold else None,
                             "lexical_abstain": bool(result.explain.get("abstain")),
                             "budget_respected": result.tokens <= budget})
    summaries = {}
    for strategy in ("legacy", "coverage-v2"):
        selected = [r for r in rows if r["strategy"] == strategy]
        summaries[strategy] = {
            "mean_source_recall": statistics.mean(r["source_recall"] for r in selected if r["source_recall"] is not None),
            "complete_source_rate": statistics.mean(r["complete_sources"] for r in selected if r["complete_sources"] is not None),
            "budget_respected": all(r["budget_respected"] for r in selected),
            "abstention_contract": next(r["lexical_abstain"] for r in selected if r["task"] == "abstention")}
    return {"benchmark": "synthetic-17-task-evidence-contracts", "budget": budget,
            "scope": "source delivery and lexical uncertainty; not answer accuracy or official dataset results",
            "summary": summaries, "rows": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget", type=int, default=300)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if not 100 <= args.budget <= 12000:
        parser.error("budget must be in 100..12000")
    report = run(args.budget)
    print(json.dumps(report, sort_keys=True))
    return int(args.check and any(not r["budget_respected"] or not r["abstention_contract"]
                                  for r in report["summary"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
