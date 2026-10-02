"""Health notices derived from benchmark summaries (stdlib only)."""
from __future__ import annotations

from typing import Any

DEFAULT_MAX_POLLUTION = 1.5
DEFAULT_MIN_P_AT_1 = 0.5
DEFAULT_MIN_RECALL = 0.5


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_num(summary: dict, *keys: str) -> float | None:
    for key in keys:
        if key in summary:
            val = _num(summary[key])
            if val is not None:
                return val
    return None


def check_health(
    bench_summary: dict | None,
    max_pollution: float = DEFAULT_MAX_POLLUTION,
    min_p_at_1: float = DEFAULT_MIN_P_AT_1,
    min_recall: float = DEFAULT_MIN_RECALL,
) -> list[dict[str, str]]:
    """Return notices when bench thresholds are crossed.

    Recognized summary keys (aliases supported):
      pollution: pollution_ratio | pollution | noise_ratio
      p@1: p_at_1 | p@1 | precision_at_1 | precision
      recall: recall | recall_at_k | r_at_5
    Empty/healthy summaries return []. Never raises on bad input.
    """
    notices: list[dict[str, str]] = []
    if not isinstance(bench_summary, dict) or not bench_summary:
        return notices
    try:
        pollution = _first_num(bench_summary, "pollution_ratio", "pollution", "noise_ratio")
        if pollution is not None and pollution > max_pollution:
            notices.append({
                "level": "warning",
                "code": "high_pollution",
                "message": f"pollution {pollution:.3g} exceeds {max_pollution:.3g}x ceiling",
            })
        p_at_1 = _first_num(bench_summary, "p_at_1", "p@1", "precision_at_1", "precision", "p_at1")
        if p_at_1 is not None and p_at_1 < min_p_at_1:
            notices.append({
                "level": "warning",
                "code": "low_p_at_1",
                "message": f"p@1 {p_at_1:.3g} below floor {min_p_at_1:.3g}",
            })
        recall = _first_num(bench_summary, "recall", "recall_at_k", "r_at_5", "recall_at_5")
        if recall is not None and recall < min_recall:
            notices.append({
                "level": "warning",
                "code": "low_recall",
                "message": f"recall {recall:.3g} below floor {min_recall:.3g}",
            })
    except Exception:
        return notices
    return notices
