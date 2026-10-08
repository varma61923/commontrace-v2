"""Paired public-dataset retrieval evaluation; stdout JSON, temporary stores only.

This measures CommonTrace context selection, not Mem0 answer accuracy. No gold
answers, evidence labels or question categories enter ingestion or retrieval.
Full-source retention is deliberately stricter than selecting a source ID:
an excerpt does not establish that every claim in its original turn was emitted.
Budgets use CommonTrace's character estimate, explicitly not provider tokens.

python -m benchmarks.conversation_retrieval --dataset locomo --data /tmp/locomo10.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import random
import sqlite3
import statistics
import tempfile
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from benchmarks.bootstrap import percentile
from benchmarks.conversation_bench import locomo_cases, longmemeval_cases
from commontrace.conversation import Options, Store, recall
from commontrace.conversation.search import forget_store, tokens

Strategy = Literal["legacy", "coverage-v1"]
STRATEGIES: tuple[Strategy, ...] = ("legacy", "coverage-v1")


@dataclass(frozen=True)
class Question:
    identity: str
    text: str
    category: str
    evidence: frozenset[str]


@dataclass(frozen=True)
class Case:
    identity: str
    sessions: tuple[tuple[str, str | None, tuple[dict[str, Any], ...]], ...]
    now: str | None
    questions: tuple[Question, ...]


@dataclass(frozen=True)
class Measurement:
    case: str
    question: str
    category: str
    strategy: Strategy
    budget: int
    estimated_tokens: int
    latency_ms: float
    selected_source_recall: float | None
    full_source_text_recall: float | None
    complete_sources: bool | None
    unresolved_sources: int

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES or any(not isinstance(value, str) or not value
                                                 for value in (self.case, self.question, self.category)):
            raise ValueError("measurement requires a valid strategy and nonempty identifiers")
        for field, value, low, high in (("budget", self.budget, 1, 100_000),
                                       ("estimated_tokens", self.estimated_tokens, 0, self.budget),
                                       ("unresolved_sources", self.unresolved_sources, 0, 1_000_000)):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"invalid measurement {field}")
        if isinstance(self.latency_ms, bool) or not isinstance(self.latency_ms, (int, float)) \
                or not math.isfinite(self.latency_ms) or self.latency_ms < 0:
            raise ValueError("measurement latency must be finite and nonnegative")
        for recall_value in (self.selected_source_recall, self.full_source_text_recall):
            if recall_value is not None and (isinstance(recall_value, bool) or not isinstance(recall_value, (int, float))
                                            or not math.isfinite(recall_value) or not 0 <= recall_value <= 1):
                raise ValueError("source recall must be finite within zero to one")
        if self.selected_source_recall is None or self.full_source_text_recall is None:
            if self.selected_source_recall is not None or self.full_source_text_recall is not None \
                    or self.complete_sources is not None:
                raise ValueError("absent evidence requires all source metrics to be undefined")
        elif self.full_source_text_recall > self.selected_source_recall \
                or not isinstance(self.complete_sources, bool) \
                or self.complete_sources != (self.full_source_text_recall == 1.0):
            raise ValueError("full source retention cannot exceed selected source recall")


def load_cases(dataset: str, data: str, *, limit: int = 0, seed: int = 0) -> list[Case]:
    """Select exactly limit questions uniformly, with their entire history.

    The existing dataset normalizers remove evaluation-only fields from messages.
    LoCoMo categories 1..4 are evaluated; adversarial category 5 is excluded.
    Unresolved LoCoMo annotations remain in metric denominators and are counted
    as missing sources; ambiguous or malformed references are not guessed.
    LongMemEval uses all questions, including abstention (no gold evidence).
    """
    if dataset not in ("locomo", "longmemeval") or limit < 0:
        raise ValueError("expected locomo or longmemeval and a nonnegative limit")
    normalized = locomo_cases(data) if dataset == "locomo" else longmemeval_cases(data, 0, seed)
    raw_evidence: dict[str, frozenset[str]] = {}
    if dataset == "locomo":
        for conversation in json.loads(Path(data).read_text(encoding="utf-8")):
            for index, question in enumerate(conversation["qa"]):
                raw_evidence[f"{conversation['sample_id']}-{index}"] = frozenset(
                    str(reference).strip() for reference in question.get("evidence", []) if str(reference).strip())
    cases = [Case(str(identity), tuple((session, date, tuple(messages)) for session, date, messages in sessions),
                  now, tuple(Question(str(q["id"]), str(q["question"]), str(q["type"]),
                                      raw_evidence.get(str(q["id"]), frozenset(str(e) for e in q["evidence"])))
                             for q in questions))
             for identity, sessions, now, questions in normalized]
    if len(cases) != len({case.identity for case in cases}):
        raise ValueError("dataset case identifiers must be unique")
    identities = [(c.identity, q.identity) for c in cases for q in c.questions]
    if len(identities) != len(set(identities)):
        raise ValueError("dataset question identifiers must be unique within each case")
    if limit and limit < len(identities):
        selected = set(random.Random(seed).sample(identities, limit))
        cases = [Case(c.identity, c.sessions, c.now,
                      tuple(q for q in c.questions if (c.identity, q.identity) in selected)) for c in cases]
    return [c for c in cases if c.questions]


def source_retention(store: Store, selected: Sequence[int], context: str,
                     evidence: frozenset[str]) -> tuple[float | None, float | None, bool | None, int]:
    """Evaluate delivered canonical text, not hidden originals or answer overlap.

    Grounded temporal annotations count as canonical text. Secret/PII redaction
    remains enabled in both arms. This conservative whole-turn metric can miss
    a sufficient answer excerpt; it is not entailment or answer correctness.
    """
    if not evidence:
        return None, None, None, 0
    delivered = store.turns(selected)
    refs = {turn.ref for turn in delivered.values()}
    normalized_context = " ".join(context.split())
    full = set()
    for turn in delivered.values():
        body = " ".join(turn.annotated().split())
        quote = " ".join(f"{turn.speaker}: {turn.annotated()}".split())
        # A label or header repeating a short body is not the body itself.
        # A tiny fallback can omit attribution, but then the whole context must
        # equal the whole source. Other unlabelled partial quotations are misses.
        if body and (quote in normalized_context or body == normalized_context):
            full.add(turn.ref)
    source_refs = {str(row[0]) for row in store.db.execute(
        "SELECT ref FROM turns WHERE ref IN (SELECT value FROM json_each(?))", (json.dumps(sorted(evidence)),))}
    selected_recall = len(evidence & refs) / len(evidence)
    text_recall = len(evidence & full) / len(evidence)
    return selected_recall, text_recall, evidence <= full, len(evidence - source_refs)


def clustered_delta(rows: Sequence[Measurement], metric: str, *, seed: int = 0,
                    resamples: int = 2000) -> dict[str, float | int | None]:
    """Paired mean difference, resampling whole conversations rather than turns.

    Question-weighted means are recomputed in every sampled cluster draw.
    One independent conversation cannot establish a confidence interval.
    """
    if metric not in ("full_source_text_recall", "selected_source_recall", "estimated_tokens", "latency_ms"):
        raise ValueError("unsupported comparison metric")
    if resamples < 1:
        raise ValueError("resamples must be positive")
    if len({row.budget for row in rows}) > 1:
        raise ValueError("comparison requires the same budget for both strategies")
    paired: dict[tuple[str, str], dict[Strategy, Measurement]] = defaultdict(dict)
    for row in rows:
        key = (row.case, row.question)
        if row.strategy in paired[key]:
            raise ValueError("comparison requires one budget and unique paired questions")
        paired[key][row.strategy] = row
    clusters: dict[str, list[float]] = defaultdict(list)
    for (case, _question), pair in paired.items():
        if set(pair) != set(STRATEGIES):
            raise ValueError("comparison requires both strategies for every question")
        if pair["legacy"].category != pair["coverage-v1"].category:
            raise ValueError("paired question categories must match")
        baseline, candidate = getattr(pair["legacy"], metric), getattr(pair["coverage-v1"], metric)
        if baseline is not None and candidate is not None:
            clusters[case].append(float(candidate) - float(baseline))
    differences = [value for cluster in clusters.values() for value in cluster]
    delta = statistics.mean(differences) if differences else None
    output: dict[str, float | int | None] = {
        "paired_questions": len(differences), "independent_cases": len(clusters),
        "candidate_minus_baseline": delta, "ci_lower": None, "ci_upper": None,
    }
    if len(clusters) > 1:
        rng = random.Random(seed)
        groups = list(clusters.values())
        samples = []
        for _ in range(resamples):
            draw = rng.choices(groups, k=len(groups))
            samples.append(sum(sum(group) for group in draw) / sum(len(group) for group in draw))
        output.update(ci_lower=percentile(samples, 2.5), ci_upper=percentile(samples, 97.5))
    return output


def summarize(rows: Sequence[Measurement]) -> dict[str, object]:
    def mean(field: str) -> float | None:
        values = [float(getattr(row, field)) for row in rows if getattr(row, field) is not None]
        return statistics.mean(values) if values else None

    latencies = [row.latency_ms for row in rows]
    return {"questions": len(rows), "evidence_questions": sum(r.full_source_text_recall is not None for r in rows),
            "mean_selected_source_recall": mean("selected_source_recall"),
            "mean_full_source_text_recall": mean("full_source_text_recall"),
            "complete_source_rate": mean("complete_sources"), "mean_estimated_tokens": mean("estimated_tokens"),
            "latency_ms": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95)} if rows else {},
            "unresolved_sources": sum(row.unresolved_sources for row in rows)}


def implementation_digest() -> str:
    """Identify the evaluator and Python implementation without exposing paths."""
    import commontrace

    package = Path(commontrace.__file__).resolve().parent
    files = [(path.relative_to(package).as_posix(), path.read_bytes()) for path in sorted(package.rglob("*.py"))]
    files.append(("benchmarks/conversation_retrieval.py", Path(__file__).read_bytes()))
    for helper in ("conversation_bench.py", "bootstrap.py"):
        files.append((f"benchmarks/{helper}", Path(__file__).with_name(helper).read_bytes()))
    digest = hashlib.sha256()
    for name, content in files:
        digest.update(json.dumps([name, len(content)]).encode())
        digest.update(content)
    return digest.hexdigest()


def evaluate(dataset: str, data: str, *, budgets: Sequence[int] = (1500, 7000),
             limit: int = 0, seed: int = 0) -> dict[str, object]:
    if not budgets or len(set(budgets)) != len(budgets) or any(
            isinstance(b, bool) or not isinstance(b, int) or not 1 <= b <= 100_000 for b in budgets):
        raise ValueError("budgets must be unique integers between 1 and 100000")
    before = hashlib.sha256(Path(data).read_bytes()).hexdigest()
    implementation = implementation_digest()
    cases = load_cases(dataset, data, limit=limit, seed=seed)
    if not cases:
        raise ValueError("dataset contains no evaluable questions")
    if hashlib.sha256(Path(data).read_bytes()).hexdigest() != before:
        raise ValueError("dataset changed while it was parsed")
    rows: list[Measurement] = []
    history_tokens: list[int] = []
    ingestion_ms = 0.0
    with tempfile.TemporaryDirectory(prefix="commontrace-retrieval-") as root:
        for index, case in enumerate(cases):
            with Store(root, f"case-{index}") as store:
                started = time.perf_counter()
                for session, date, messages in case.sessions:
                    store.add(session, list(messages), session_at=date)
                ingestion_ms += 1000 * (time.perf_counter() - started)
                history_tokens.append(sum(tokens(m["text"]) for _s, _d, ms in case.sessions for m in ms))
                for q_index, question in enumerate(case.questions):
                    for budget in budgets:
                        order = STRATEGIES if q_index % 2 == 0 else tuple(reversed(STRATEGIES))
                        for strategy in order:
                            options = Options(budget=budget, embedder=None, rerank=None,
                                              neighbours_before=1, neighbours_after=1, context_strategy=strategy)
                            # Disable final-response cache, preserve both arms' normal SQLite/index caches.
                            forget_store(store)
                            started = time.perf_counter()
                            result = recall(store, question.text, now=case.now, options=options)
                            elapsed = 1000 * (time.perf_counter() - started)
                            if result.tokens > budget:
                                raise RuntimeError("production recall exceeded its estimated token budget")
                            selected, full, complete, missing = source_retention(
                                store, result.turns, result.context, question.evidence)
                            rows.append(Measurement(case.identity, question.identity, question.category, strategy,
                                                    budget, result.tokens, elapsed, selected, full, complete, missing))
    summaries: dict[str, object] = {}
    for budget in budgets:
        at_budget = [r for r in rows if r.budget == budget]
        arms = {strategy: summarize([r for r in at_budget if r.strategy == strategy]) for strategy in STRATEGIES}
        categories = sorted({r.category for r in at_budget})
        summaries[str(budget)] = {"strategies": arms,
                                 "by_category": {category: {strategy: summarize([
                                     r for r in at_budget if r.category == category and r.strategy == strategy])
                                     for strategy in STRATEGIES} for category in categories},
                                 "paired_cluster_bootstrap": {metric: clustered_delta(at_budget, metric, seed=seed)
                                                              for metric in ("full_source_text_recall",
                                                                             "estimated_tokens", "latency_ms")}}
    selection = [(c.identity, q.identity) for c in cases for q in c.questions]
    if implementation_digest() != implementation:
        raise RuntimeError("implementation changed during evaluation; rerun against frozen source")
    return {"dataset": dataset, "dataset_sha256": before, "seed": seed,
            "implementation_sha256": implementation,
            "selected_questions": len(selection), "independent_cases": len(cases),
            "selection_sha256": hashlib.sha256(json.dumps(selection).encode()).hexdigest(),
            "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "ingestion_ms": ingestion_ms,
            "mean_history_estimated_tokens": statistics.mean(history_tokens),
            "conditions": {"embedding_model": None, "reranker": None, "answer_model": None, "judge": None,
                           "unresolved_annotations": "retained as source misses; no guessed repairs",
                           "token_accounting": "ceil(characters/4), not provider tokenization",
                           "final_response_cache": "disabled", "ingestion": "production redaction enabled",
                           "gold_labels": "evaluation only", "timing": "recall only; excludes ingestion/evaluation",
                           "comparison": "CommonTrace legacy vs coverage-v1; not a competitor accuracy benchmark"},
            "budgets": summaries}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--dataset", choices=("locomo", "longmemeval"), required=True)
    parser.add_argument("--data", required=True)
    parser.add_argument("--budgets", default="1500,7000")
    parser.add_argument("--limit", type=int, default=0, help="exact seeded question sample; zero evaluates all")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        budgets = [int(value) for value in args.budgets.split(",")]
        result = evaluate(args.dataset, args.data, budgets=budgets, limit=args.limit, seed=args.seed)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
