"""Offline, fail-closed paired comparisons of provenance-bearing benchmark runs.

Rank metrics use the pre-assembly ranking, binary relevance and distinct raw-turn
or session identities. No-gold questions are explicitly excluded, never scored as
zero. Question-weighted means are bootstrapped by conversation, preserving all
paired questions in each sampled cluster. A single conversation cannot provide
an empirical confidence interval; it is reported as unavailable.

    python benchmarks/compare.py --baseline before.json --candidate after.json
    python benchmarks/compare.py --baseline before.json --candidate after.json --check

JSON is emitted to stdout. Inputs must be saved with per-question rows; legacy
outputs lacking content provenance and gold identities cannot be compared safely.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

_SCALARS = {
    "evidence": "evidence", "complete": "complete", "session": "session",
    "answer_in_context": "answer_in_context", "tokens": "tokens",
    "completeness_score": "completeness_score", "accuracy": "correct", "score": "score",
}
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValueError(f"{label} must be a JSON object")
    return cast(dict[str, object], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _digest(value: object, label: str) -> str:
    digest = _string(value, label)
    if _HEX.fullmatch(digest) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return digest


def _ids(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    ids = tuple(_string(item, label) for item in value)
    if len(set(ids)) != len(ids):
        raise ValueError(f"{label} contains duplicate identities")
    return ids


def ranking_metrics(ranked: Sequence[str], gold: Sequence[str]) -> dict[str, float | None]:
    """Binary Recall@5/10 and NDCG@10; identities must already be deduplicated.

    The ideal ranking contains min(10, number of gold identities) relevant hits.
    Missing gold identities remain in the denominator even if unresolvable.
    """
    ranking = _ids(list(ranked), "ranked identities")
    relevant = set(_ids(list(gold), "gold identities"))
    if not relevant:
        return {"recall_at_5": None, "recall_at_10": None, "ndcg_at_10": None}
    dcg = math.fsum(1.0 / math.log2(i + 2) for i, ref in enumerate(ranking[:10]) if ref in relevant)
    ideal = math.fsum(1.0 / math.log2(i + 2) for i in range(min(10, len(relevant))))
    return {
        "recall_at_5": len(relevant.intersection(ranking[:5])) / len(relevant),
        "recall_at_10": len(relevant.intersection(ranking[:10])) / len(relevant),
        "ndcg_at_10": dcg / ideal,
    }


@dataclass(frozen=True)
class _Row:
    identity: str
    category: str
    cluster: str
    question_digest: str
    rubric: str
    gold_turns: frozenset[str]
    gold_sessions: frozenset[str]
    values: Mapping[str, float | None]


@dataclass(frozen=True)
class _Run:
    dataset: str
    budget: int
    mode: str
    provenance: str
    evaluation: str
    rows: Mapping[str, _Row]
    metrics: tuple[str, ...]


def _scalar(value: object, label: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number, boolean or null")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _run(raw: Mapping[str, object]) -> _Run:
    if _integer(raw.get("comparison_schema"), "comparison_schema", 1) != 1:
        raise ValueError("unsupported comparison_schema")
    provenance = dict(_mapping(raw.get("provenance"), "provenance"))
    _digest(provenance.get("dataset_sha256"), "provenance.dataset_sha256")
    _digest(provenance.get("adapter_sha256"), "provenance.adapter_sha256")
    sampling = _mapping(provenance.get("sampling"), "provenance.sampling")
    _integer(sampling.get("seed"), "sampling.seed")
    _integer(sampling.get("limit"), "sampling.limit")
    if not isinstance(sampling.get("personas"), str):
        raise ValueError("sampling.personas must be a string")
    # Product revisions are intentionally allowed to differ in an ablation.
    if "product_sha256" in provenance:
        _digest(provenance.pop("product_sha256"), "provenance.product_sha256")
    try:
        canonical = json.dumps(provenance, sort_keys=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("provenance must contain finite JSON values") from exc
    evaluation = json.dumps(
        {key: raw[key] for key in ("judge", "judge_profile", "judge_model", "answer_model") if key in raw},
        sort_keys=True, allow_nan=False, separators=(",", ":"),
    )
    dataset = _string(raw.get("dataset"), "dataset")
    budget = _integer(raw.get("budget"), "budget", 1)
    mode = _string(raw.get("mode"), "mode")
    if mode not in ("memory", "full-context", "budgeted-history", "no-memory"):
        raise ValueError("unsupported benchmark mode")
    raw_rows = raw.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError("rows must be a nonempty list; save the benchmark with --out")
    rows: dict[str, _Row] = {}
    metric_fields: tuple[str, ...] | None = None
    for raw_row in raw_rows:
        row = _mapping(raw_row, "row")
        identity = _string(row.get("id"), "row.id")
        if identity in rows:
            raise ValueError(f"duplicate question identity: {identity}")
        category = _string(row.get("type"), f"{identity}.type")
        cluster = _string(row.get("cluster_id"), f"{identity}.cluster_id")
        digest = _digest(row.get("question_sha256"), f"{identity}.question_sha256")
        if row.get("mode") != mode:
            raise ValueError(f"row mode disagrees with run mode: {identity}")
        turns = _ids(row.get("gold_turn_ids"), f"{identity}.gold_turn_ids")
        sessions = _ids(row.get("gold_session_ids"), f"{identity}.gold_session_ids")
        ranked_turns = _ids(row.get("ranked_turn_ids"), f"{identity}.ranked_turn_ids")
        ranked_sessions = _ids(row.get("ranked_session_ids"), f"{identity}.ranked_session_ids")
        fields = tuple(key for key, field in _SCALARS.items() if field in row)
        if metric_fields is not None and fields != metric_fields:
            raise ValueError(f"inconsistent scalar metric fields: {identity}")
        metric_fields = fields
        values = {key: _scalar(row[_SCALARS[key]], f"{identity}.{key}") for key in fields}
        for key, value in values.items():
            if value is None and key not in ("evidence", "complete", "session", "answer_in_context", "completeness_score"):
                raise ValueError(f"missing measured value: {identity}.{key}")
            if key == "tokens" and (isinstance(row["tokens"], bool) or not isinstance(row["tokens"], int)):
                raise ValueError(f"{identity}.tokens must be a nonnegative integer")
            if value is not None and (value < 0 or (key not in ("tokens", "score") and value > 1)):
                raise ValueError(f"metric out of range: {identity}.{key}")
        for level, ranking, gold in (("turn", ranked_turns, turns), ("session", ranked_sessions, sessions)):
            values.update({f"{level}_{key}": value if mode == "memory" else None
                           for key, value in ranking_metrics(ranking, gold).items()})
        rubric = json.dumps({"rubric": row["rubric"]} if "rubric" in row else {},
                            sort_keys=True, allow_nan=False, separators=(",", ":"))
        rows[identity] = _Row(identity, category, cluster, digest, rubric, frozenset(turns), frozenset(sessions), values)
    assert metric_fields is not None
    return _Run(dataset, budget, mode, canonical, evaluation, rows, tuple(next(iter(rows.values())).values))


def _percentile(ordered: Sequence[float], quantile: float) -> float:
    rank = (len(ordered) - 1) * quantile
    low, high = math.floor(rank), math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _estimate(
    pairs: Sequence[tuple[str, float | None, float | None]],
    *, n_resamples: int, seed: int, confidence_level: float,
) -> dict[str, object]:
    groups: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for cluster, baseline, candidate in pairs:
        if baseline is not None and candidate is not None:
            groups[cluster].append((baseline, candidate))
    n = sum(map(len, groups.values()))
    result: dict[str, object] = {
        "n_questions": n, "n_clusters": len(groups), "excluded_questions": len(pairs) - n,
        "baseline_mean": None, "candidate_mean": None, "delta": None,
        "ci_lower": None, "ci_upper": None, "significant": False,
        "ci_status": "no_scored_questions" if not n else "insufficient_clusters",
    }
    if not n:
        return result
    base = math.fsum(b for group in groups.values() for b, _ in group) / n
    candidate = math.fsum(t for group in groups.values() for _, t in group) / n
    result.update(baseline_mean=base, candidate_mean=candidate, delta=candidate - base)
    if len(groups) < 2:
        return result
    totals = [
        (len(groups[cluster]), math.fsum(t - b for b, t in groups[cluster])) for cluster in sorted(groups)
    ]
    rng = random.Random(seed)
    deltas: list[float] = []
    for _ in range(n_resamples):
        sampled = rng.choices(totals, k=len(totals))
        deltas.append(math.fsum(delta for _, delta in sampled) / sum(count for count, _ in sampled))
    deltas.sort()
    alpha = (1 - confidence_level) / 2
    lower, upper = _percentile(deltas, alpha), _percentile(deltas, 1 - alpha)
    result.update(ci_lower=lower, ci_upper=upper, significant=lower > 0 or upper < 0, ci_status="available")
    return result


def compare_runs(
    baseline: Mapping[str, object], candidate: Mapping[str, object], *, n_resamples: int = 2000,
    seed: int = 42, confidence_level: float = 0.95,
) -> dict[str, object]:
    """Compare exactly matched questions; invalid pairs raise rather than vanish.

    Percentile intervals are marginal per metric/category, not simultaneous or a
    causal claim. Sample units are conversations; means weight questions equally.
    """
    _integer(n_resamples, "n_resamples", 1)
    _integer(seed, "seed")
    if isinstance(confidence_level, bool) or not isinstance(confidence_level, (int, float)):
        raise ValueError("confidence_level must be between zero and one")
    if not 0 < confidence_level < 1:
        raise ValueError("confidence_level must be between zero and one")
    before, after = _run(baseline), _run(candidate)
    for field in ("dataset", "budget", "mode", "provenance", "evaluation", "metrics"):
        if getattr(before, field) != getattr(after, field):
            raise ValueError(f"incompatible runs: {field} differs")
    if before.rows.keys() != after.rows.keys():
        raise ValueError("question sets differ; comparisons never silently intersect")
    ids = sorted(before.rows)
    for identity in ids:
        b, t = before.rows[identity], after.rows[identity]
        for field in ("category", "cluster", "question_digest", "rubric", "gold_turns", "gold_sessions"):
            if getattr(b, field) != getattr(t, field):
                raise ValueError(f"incompatible question {identity}: {field} differs")
        for metric in before.metrics:
            if (b.values[metric] is None) != (t.values[metric] is None):
                raise ValueError(f"asymmetric null metric: {identity}.{metric}")

    def block(question_ids: Sequence[str]) -> dict[str, object]:
        return {
            metric: _estimate(
                [(before.rows[q].cluster, before.rows[q].values[metric], after.rows[q].values[metric])
                 for q in question_ids],
                n_resamples=n_resamples, seed=seed, confidence_level=confidence_level,
            ) for metric in before.metrics
        }

    categories = sorted({row.category for row in before.rows.values()})
    return {
        "comparison_schema": 1, "dataset": before.dataset, "budget": before.budget, "mode": before.mode,
        "n_questions": len(ids), "n_clusters": len({row.cluster for row in before.rows.values()}),
        "confidence_level": confidence_level, "n_resamples": n_resamples, "seed": seed,
        "sample_unit": "conversation", "mean_weighting": "question",
        "interval_method": "paired_cluster_percentile_bootstrap", "interval_scope": "marginal",
        "overall": block(ids),
        "by_type": {category: block([q for q in ids if before.rows[q].category == category]) for category in categories},
    }


def clustered_estimates(
    run: Mapping[str, object], *, n_resamples: int = 2000,
    seed: int = 42, confidence_level: float = 0.95,
) -> dict[str, object]:
    """Absolute question-weighted means with conversation-cluster intervals.

    The full comparison schema is required so sample units and unavailable values
    have the same semantics as paired comparisons. An absolute interval does not
    measure an improvement, so this output deliberately has no significance flag.
    """
    _integer(n_resamples, "n_resamples", 1)
    _integer(seed, "seed")
    if isinstance(confidence_level, bool) or not isinstance(confidence_level, (int, float)):
        raise ValueError("confidence_level must be between zero and one")
    if not 0 < confidence_level < 1:
        raise ValueError("confidence_level must be between zero and one")
    source = _run(run)
    ids = sorted(source.rows)

    def block(question_ids: Sequence[str]) -> dict[str, object]:
        estimates: dict[str, object] = {}
        for metric in source.metrics:
            pairs = [(source.rows[q].cluster, 0.0 if source.rows[q].values[metric] is not None else None,
                      source.rows[q].values[metric]) for q in question_ids]
            estimate = _estimate(pairs, n_resamples=n_resamples, seed=seed, confidence_level=confidence_level)
            estimates[metric] = {
                "mean": estimate["candidate_mean"],
                **{key: estimate[key] for key in ("ci_lower", "ci_upper", "ci_status", "n_questions",
                                                  "n_clusters", "excluded_questions")},
            }
        return estimates

    categories = sorted({row.category for row in source.rows.values()})
    return {
        "sample_unit": "conversation", "mean_weighting": "question",
        "interval_method": "cluster_percentile_bootstrap", "interval_scope": "marginal",
        "confidence_level": confidence_level, "n_resamples": n_resamples, "seed": seed,
        "overall": block(ids),
        "by_type": {category: block([q for q in ids if source.rows[q].category == category])
                    for category in categories},
    }


def check_regressions(comparison: Mapping[str, object]) -> list[str]:
    """Quality-only gate: reject downward CIs and insufficient scored clusters.

    All-null no-gold metrics remain explicit exclusions. Tokens are descriptive;
    spending more tokens is not itself evidence of worse retrieval quality.
    """
    problems: list[str] = []
    blocks = [("overall", _mapping(comparison.get("overall"), "overall"))]
    blocks.extend((str(k), _mapping(v, str(k))) for k, v in _mapping(comparison.get("by_type"), "by_type").items())
    quality_scored = False
    for category, metrics in blocks:
        for name, raw in metrics.items():
            if name == "tokens":
                continue
            metric = _mapping(raw, name)
            count = _integer(metric.get("n_questions"), f"{category}.{name}.n_questions")
            quality_scored = quality_scored or count > 0
            if count and metric.get("ci_status") != "available":
                problems.append(f"{category}.{name}: insufficient independent conversations")
            upper = metric.get("ci_upper")
            if isinstance(upper, (int, float)) and upper < 0:
                problems.append(f"{category}.{name}: negative confidence interval")
    if not quality_scored:
        problems.append("overall: no scored quality metrics")
    return problems


def _pairs_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


def load_runs(path: str, mode: str) -> dict[str, Mapping[str, object]]:
    """Read a budget map (or single summary), rejecting ambiguous JSON and modes."""
    with open(path, encoding="utf-8") as handle:
        raw = _mapping(json.load(handle, object_pairs_hook=_pairs_object, parse_constant=_reject_constant), "run")
    summaries = {str(raw["budget"]): raw} if "budget" in raw else raw
    result: dict[str, Mapping[str, object]] = {}
    for budget, raw_summary in summaries.items():
        summary = _mapping(raw_summary, f"budget {budget}")
        if "modes_detail" in summary:
            details = _mapping(summary["modes_detail"], "modes_detail")
            summary = _mapping(details.get(mode), f"mode {mode}")
        if summary.get("mode") != mode:
            raise ValueError(f"requested mode missing: {mode}")
        if str(_integer(summary.get("budget"), "budget", 1)) != budget:
            raise ValueError("budget map key disagrees with summary")
        result[budget] = summary
    if not result:
        raise ValueError("budget map must not be empty")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--budget", type=int, help="select one exact budget; default compares all budgets")
    parser.add_argument("--mode", default="memory", choices=("memory", "full-context", "budgeted-history", "no-memory"))
    parser.add_argument("--resamples", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--check", action="store_true", help="fail on negative quality CIs or insufficient clusters")
    args = parser.parse_args(argv)
    try:
        baseline, candidate = load_runs(args.baseline, args.mode), load_runs(args.candidate, args.mode)
        if baseline.keys() != candidate.keys():
            raise ValueError("budget sets differ; select matching input runs")
        budgets = [str(args.budget)] if args.budget is not None else sorted(baseline, key=int)
        if any(budget not in baseline for budget in budgets):
            raise ValueError("requested budget missing")
        output = {budget: compare_runs(baseline[budget], candidate[budget],
                                      n_resamples=args.resamples, seed=args.seed) for budget in budgets}
        failures = {budget: check_regressions(run) for budget, run in output.items()} if args.check else {}
        print(json.dumps({"comparisons": output, "gate_failures": failures}, allow_nan=False, sort_keys=True))
        return 1 if any(failures.values()) else 0
    except (OSError, ValueError) as exc:
        print(f"benchmark comparison refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
