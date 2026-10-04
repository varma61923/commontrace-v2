"""Paired bootstrap confidence intervals and statistical hypothesis testing for benchmarks.

Provides:
- bootstrap_ci: single-series 95% bootstrap confidence interval.
- paired_bootstrap_test: paired bootstrap comparison between two systems/conditions,
  returning point delta, 95% CI, and two-sided p-value.
- compare_runs: compares two conversation_bench JSON output runs across all common metrics.
- bootstrap_rows: adds empirical 95% CIs to a list of benchmark rows.
"""
from __future__ import annotations

import math
import random
from typing import Any, Callable, Sequence


def percentile(data: Sequence[float], p: float) -> float:
    """Compute empirical percentile using linear interpolation (matches numpy default)."""
    if not data:
        raise ValueError("Cannot compute percentile of empty data")
    sorted_data = sorted(data)
    n = len(sorted_data)
    if n == 1:
        return sorted_data[0]
    # Rank in [0, n - 1]
    rank = (n - 1) * (p / 100.0)
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return sorted_data[low]
    weight = rank - low
    return sorted_data[low] * (1.0 - weight) + sorted_data[high] * weight


def bootstrap_ci(
    values: Sequence[float | int | bool],
    statistic: Callable[[Sequence[float]], float] | None = None,
    n_resamples: int = 2000,
    confidence_level: float = 0.95,
    seed: int = 42,
) -> dict[str, float]:
    """Compute empirical bootstrap confidence interval for a single series.

    Returns dict with 'mean', 'ci_lower', 'ci_upper', and 'margin_of_error'.
    """
    clean_vals = [float(v) for v in values if v is not None]
    if not clean_vals:
        return {"mean": 0.0, "ci_lower": 0.0, "ci_upper": 0.0, "margin_of_error": 0.0}

    n = len(clean_vals)
    stat_fn = statistic or (lambda xs: sum(xs) / len(xs))
    point_est = stat_fn(clean_vals)

    if n == 1:
        return {
            "mean": round(point_est, 4),
            "ci_lower": round(point_est, 4),
            "ci_upper": round(point_est, 4),
            "margin_of_error": 0.0,
        }

    rng = random.Random(seed)
    boot_stats = []
    alpha = (1.0 - confidence_level) / 2.0

    for _ in range(n_resamples):
        sample = rng.choices(clean_vals, k=n)
        boot_stats.append(stat_fn(sample))

    ci_low = percentile(boot_stats, alpha * 100.0)
    ci_high = percentile(boot_stats, (1.0 - alpha) * 100.0)

    return {
        "mean": round(point_est, 4),
        "ci_lower": round(ci_low, 4),
        "ci_upper": round(ci_high, 4),
        "margin_of_error": round((ci_high - ci_low) / 2.0, 4),
    }


def paired_bootstrap_test(
    treatment: Sequence[float | int | bool],
    baseline: Sequence[float | int | bool],
    statistic: Callable[[Sequence[float]], float] | None = None,
    n_resamples: int = 2000,
    confidence_level: float = 0.95,
    seed: int = 42,
) -> dict[str, Any]:
    """Compute paired bootstrap difference and two-sided p-value.

    Tests whether treatment is significantly different from baseline on matched items.
    Hypothesis:
      H0: Delta = treatment_stat - baseline_stat == 0
      H1: Delta != 0
    """
    if len(treatment) != len(baseline):
        raise ValueError(f"Lengths must match for paired bootstrap: {len(treatment)} vs {len(baseline)}")

    # Extract paired valid cases
    pairs = [
        (float(t), float(b))
        for t, b in zip(treatment, baseline)
        if t is not None and b is not None
    ]
    if not pairs:
        return {
            "n": 0,
            "treatment_mean": 0.0,
            "baseline_mean": 0.0,
            "delta": 0.0,
            "ci_lower": 0.0,
            "ci_upper": 0.0,
            "p_value": 1.0,
            "significant": False,
        }

    n = len(pairs)
    t_vals = [p[0] for p in pairs]
    b_vals = [p[1] for p in pairs]

    stat_fn = statistic or (lambda xs: sum(xs) / len(xs))
    t_mean = stat_fn(t_vals)
    b_mean = stat_fn(b_vals)
    observed_delta = t_mean - b_mean

    if n == 1 or observed_delta == 0.0:
        return {
            "n": n,
            "treatment_mean": round(t_mean, 4),
            "baseline_mean": round(b_mean, 4),
            "delta": round(observed_delta, 4),
            "ci_lower": round(observed_delta, 4),
            "ci_upper": round(observed_delta, 4),
            "p_value": 1.0,
            "significant": False,
        }

    rng = random.Random(seed)
    deltas = []
    alpha = (1.0 - confidence_level) / 2.0

    # Draw paired bootstrap samples
    for _ in range(n_resamples):
        sample = rng.choices(pairs, k=n)
        s_t = [p[0] for p in sample]
        s_b = [p[1] for p in sample]
        deltas.append(stat_fn(s_t) - stat_fn(s_b))

    ci_low = percentile(deltas, alpha * 100.0)
    ci_high = percentile(deltas, (1.0 - alpha) * 100.0)

    # Shifted bootstrap test under null hypothesis H0 (Delta = 0)
    # Efron & Tibshirani (1993) / Berg-Kirkpatrick et al. (2012)
    shifted_deltas = [d - observed_delta for d in deltas]
    abs_obs = abs(observed_delta)
    count_extreme = sum(1 for sd in shifted_deltas if abs(sd) >= abs_obs)
    p_val = (count_extreme + 1.0) / (n_resamples + 1.0)

    # Significant if 95% CI excludes 0 (equivalent to two-sided alpha=0.05)
    significant = (ci_low > 0.0) or (ci_high < 0.0)

    return {
        "n": n,
        "treatment_mean": round(t_mean, 4),
        "baseline_mean": round(b_mean, 4),
        "delta": round(observed_delta, 4),
        "ci_lower": round(ci_low, 4),
        "ci_upper": round(ci_high, 4),
        "p_value": round(p_val, 4),
        "significant": significant,
    }


def compare_runs(
    treatment_run: dict[str, Any],
    baseline_run: dict[str, Any],
    metrics: Sequence[str] = ("accuracy", "evidence", "complete", "score", "answer_in_context"),
    n_resamples: int = 2000,
    seed: int = 42,
) -> dict[str, Any]:
    """Compare two benchmark runs using paired bootstrap on matching question IDs."""
    t_rows = treatment_run.get("rows", [])
    b_rows = baseline_run.get("rows", [])

    if not t_rows or not b_rows:
        return {"error": "Missing rows in one or both benchmark runs", "comparisons": {}}

    # Map by ID
    t_by_id = {r["id"]: r for r in t_rows if "id" in r}
    b_by_id = {r["id"]: r for r in b_rows if "id" in r}

    common_ids = sorted(set(t_by_id.keys()) & set(b_by_id.keys()))
    if not common_ids:
        return {"error": "No common question IDs between benchmark runs", "comparisons": {}}

    results: dict[str, Any] = {
        "common_questions": len(common_ids),
        "overall": {},
        "by_type": {},
    }

    # Overall metrics
    for m in metrics:
        t_seq = [t_by_id[qid].get(m) for qid in common_ids if m in t_by_id[qid] and m in b_by_id[qid]]
        b_seq = [b_by_id[qid].get(m) for qid in common_ids if m in t_by_id[qid] and m in b_by_id[qid]]
        # Filter to pairs where at least one has non-None value
        valid_pairs = [(tv, bv) for tv, bv in zip(t_seq, b_seq) if tv is not None and bv is not None]
        if valid_pairs:
            test_res = paired_bootstrap_test(
                treatment=[p[0] for p in valid_pairs],
                baseline=[p[1] for p in valid_pairs],
                n_resamples=n_resamples,
                seed=seed,
            )
            results["overall"][m] = test_res

    # Per-type metrics
    types = sorted({t_by_id[qid].get("type") for qid in common_ids if t_by_id[qid].get("type")})
    for t_name in types:
        results["by_type"][t_name] = {}
        type_ids = [qid for qid in common_ids if t_by_id[qid].get("type") == t_name]
        for m in metrics:
            t_seq = [t_by_id[qid].get(m) for qid in type_ids if m in t_by_id[qid] and m in b_by_id[qid]]
            b_seq = [b_by_id[qid].get(m) for qid in type_ids if m in t_by_id[qid] and m in b_by_id[qid]]
            valid_pairs = [(tv, bv) for tv, bv in zip(t_seq, b_seq) if tv is not None and bv is not None]
            if valid_pairs:
                test_res = paired_bootstrap_test(
                    treatment=[p[0] for p in valid_pairs],
                    baseline=[p[1] for p in valid_pairs],
                    n_resamples=n_resamples,
                    seed=seed,
                )
                results["by_type"][t_name][m] = test_res

    return results


def format_comparison_markdown(comparison: dict[str, Any]) -> str:
    """Format comparison results as an informative Markdown report."""
    if "error" in comparison:
        return f"**Comparison Error:** {comparison['error']}"

    lines = [
        f"### Benchmark Comparison (N = {comparison['common_questions']} paired questions)",
        "",
        "| Metric | Baseline | Treatment | Delta (Δ) | 95% Confidence Interval | p-value | Significant? |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]

    overall = comparison.get("overall", {})
    for metric, res in overall.items():
        sig_str = "✅ Yes" if res["significant"] else "❌ No"
        ci_str = f"[{res['ci_lower']:+.4f}, {res['ci_upper']:+.4f}]"
        lines.append(
            f"| **{metric}** | {res['baseline_mean']:.4f} | {res['treatment_mean']:.4f} | "
            f"**{res['delta']:+.4f}** | {ci_str} | {res['p_value']:.4f} | {sig_str} |"
        )

    by_type = comparison.get("by_type", {})
    if by_type:
        lines.extend([
            "",
            "#### Breakdown by Question Type",
            "",
            "| Category | Metric | Baseline | Treatment | Delta (Δ) | 95% CI | Sig? |",
            "| :--- | :--- | :---: | :---: | :---: | :---: | :---: |",
        ])
        for cat, cat_metrics in by_type.items():
            for metric, res in cat_metrics.items():
                sig_str = "✅" if res["significant"] else "—"
                ci_str = f"[{res['ci_lower']:+.4f}, {res['ci_upper']:+.4f}]"
                lines.append(
                    f"| {cat} | {metric} | {res['baseline_mean']:.4f} | {res['treatment_mean']:.4f} | "
                    f"{res['delta']:+.4f} | {ci_str} | {sig_str} |"
                )

    return "\n".join(lines)
