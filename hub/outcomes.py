from __future__ import annotations

import math

from commontrace import experiment

PROPORTION_METRICS: tuple[tuple[str, str, str], ...] = (
    ("resolution_rate", "resolved", "up"),
    ("repeated_error_rate", "repeated_error", "down"),
    ("escalation_rate", "escalated", "down"),
    ("frustration_rate", "frustration_signal", "down"),
)

MEAN_METRICS: tuple[tuple[str, str], ...] = (
    ("avg_tokens_used", "tokens_used"),
    ("avg_llm_calls", "llm_calls"),
)

MIN_PER_CELL = 5

DEFAULT_ALPHA = 0.05

VERDICT_IMPROVED = "improved"
VERDICT_WORSENED = "worsened"
VERDICT_INCONCLUSIVE = "inconclusive"
VERDICT_INSUFFICIENT = "insufficient_data"

OBSERVATIONAL_CAVEAT = (
    "OBSERVED CHANGE, NOT A CAUSAL EFFECT. `outcome.baseline` marks a time "
    "window, so anything else that changed between the windows -- a model "
    "upgrade, a shift in task mix, the team getting better -- is confounded "
    "with this product's contribution and cannot be separated from it here. "
    "For a claim that survives 'what else changed?', use the randomized "
    "holdout (commontrace/experiment.py): it withholds lessons at random, so "
    "the arms differ only by the treatment."
)

_INSUFFICIENT_NOTE = (
    "Too few observations to run the test: the normal approximation needs at "
    f"least {MIN_PER_CELL} successes and {MIN_PER_CELL} failures in each arm. "
    "This is not evidence of no effect."
)


def _is_bool(value: object) -> bool:
    return isinstance(value, bool)


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


KNOWN_OUTCOME_FIELDS: frozenset[str] = frozenset(
    {field for _n, field, _d in PROPORTION_METRICS}
    | {field for _n, field in MEAN_METRICS}
    | {"baseline"}
)


def validate_outcome(outcome: dict | None) -> dict:
    if outcome is None:
        return {}
    if not isinstance(outcome, dict):
        raise ValueError(f"outcome must be an object, got {type(outcome).__name__}")
    unknown = set(outcome) - KNOWN_OUTCOME_FIELDS
    if unknown:
        raise ValueError(
            f"outcome has unknown field(s) {sorted(unknown)}; "
            f"expected any of {sorted(KNOWN_OUTCOME_FIELDS)}"
        )
    for _n, field, _d in PROPORTION_METRICS:
        if field in outcome and outcome[field] is not None and not _is_bool(outcome[field]):
            raise ValueError(f"outcome.{field} must be a boolean, got {outcome[field]!r}")
    if "baseline" in outcome and not _is_bool(outcome["baseline"]):
        raise ValueError(f"outcome.baseline must be a boolean, got {outcome['baseline']!r}")
    for _n, field in MEAN_METRICS:
        if field not in outcome:
            continue
        value = outcome[field]
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise ValueError(f"outcome.{field} must be an integer, got {value!r}")
        if value is not None and value < 0:
            raise ValueError(f"outcome.{field} must not be negative, got {value!r}")
    return dict(outcome)


class Tally:
    """Pre-aggregated counts for one arm: what `compare` actually needs."""

    __slots__ = ("n", "props", "means")

    def __init__(
        self,
        n: int = 0,
        props: dict[str, tuple[int, int]] | None = None,
        means: dict[str, tuple[float | None, int]] | None = None,
    ) -> None:
        self.n = n
        self.props = props or {}
        self.means = means or {}


def tally(outcomes: list[dict]) -> Tally:
    """Build a Tally from raw outcome dicts, in Python."""
    return Tally(
        n=len(outcomes),
        props={field: proportion(outcomes, field) for _n, field, _d in PROPORTION_METRICS},
        means={field: mean(outcomes, field) for _n, field in MEAN_METRICS},
    )


def split_arms(outcomes: list[dict]) -> tuple[list[dict], list[dict]]:
    baseline = [o for o in outcomes if _is_bool(o.get("baseline")) and o["baseline"]]
    current = [o for o in outcomes if not (_is_bool(o.get("baseline")) and o["baseline"])]
    return baseline, current


def proportion(outcomes: list[dict], field: str) -> tuple[int, int]:
    values = [o.get(field) for o in outcomes]
    values = [v for v in values if _is_bool(v)]
    return sum(1 for v in values if v), len(values)


def mean(outcomes: list[dict], field: str) -> tuple[float | None, int]:
    values = [o.get(field) for o in outcomes]
    values = [v for v in values if _is_number(v)]
    if not values:
        return None, 0
    return sum(values) / len(values), len(values)


def _testable(successes: int, n: int) -> bool:
    return successes >= MIN_PER_CELL and (n - successes) >= MIN_PER_CELL


def _verdict(delta: float, direction: str, significant: bool) -> str:
    if not significant:
        return VERDICT_INCONCLUSIVE
    improved = delta > 0 if direction == "up" else delta < 0
    return VERDICT_IMPROVED if improved else VERDICT_WORSENED


def compare(baseline: list[dict], current: list[dict], alpha: float = DEFAULT_ALPHA) -> dict:
    """The whole before/after report for one fleet, from raw outcome dicts."""
    return compare_tallies(tally(baseline), tally(current), alpha=alpha)


def compare_tallies(baseline: Tally, current: Tally, alpha: float = DEFAULT_ALPHA) -> dict:
    """The whole before/after report for one fleet."""
    rows: list[dict] = []
    testable_idx: list[int] = []
    p_values: list[float] = []

    for name, field, direction in PROPORTION_METRICS:
        b_s, b_n = baseline.props.get(field, (0, 0))
        c_s, c_n = current.props.get(field, (0, 0))
        row: dict = {
            "metric": name,
            "field": field,
            "direction": direction,
            "baseline": {"rate": (b_s / b_n) if b_n else None, "n": b_n},
            "current": {"rate": (c_s / c_n) if c_n else None, "n": c_n},
            "delta": None,
            "ci_95": None,
            "p_value": None,
            "significant": False,
            "min_detectable_effect": None,
            "verdict": VERDICT_INSUFFICIENT,
            "note": _INSUFFICIENT_NOTE,
        }
        if not (_testable(b_s, b_n) and _testable(c_s, c_n)):
            rows.append(row)
            continue

        _z, p = experiment.two_proportion_test(c_s, c_n, b_s, b_n)
        lo, hi = experiment.diff_confidence_interval(c_s, c_n, b_s, b_n)
        row.update(
            {
                "delta": (c_s / c_n) - (b_s / b_n),
                "ci_95": [lo, hi],
                "p_value": p,
                "note": "",
            }
        )
        row["min_detectable_effect"] = experiment.minimum_detectable_effect(
            min(b_n, c_n), b_s / b_n
        )
        testable_idx.append(len(rows))
        p_values.append(p)
        rows.append(row)

    for position, keep in enumerate(experiment.benjamini_hochberg(p_values, alpha)):
        row = rows[testable_idx[position]]
        row["significant"] = keep
        row["verdict"] = _verdict(row["delta"], row["direction"], keep)
        if not keep:
            mde = row["min_detectable_effect"]
            note = "No change distinguishable from noise at this sample size."
            if mde is not None:
                note += (
                    f" The smallest effect this many observations could reliably "
                    f"detect is {mde:.1%}, so a real change smaller than that would "
                    "not show up here."
                )
            lo, hi = row["ci_95"]
            if lo > 0 or hi < 0:
                note += (
                    " The interval on this row excludes zero while the row reads as no"
                    " change: the interval is uncorrected and describes this metric"
                    " alone, whereas significance is judged after correcting across"
                    " all four metrics tested here."
                )
            row["note"] = note

    cost: list[dict] = []
    for name, field in MEAN_METRICS:
        b_v, b_n = baseline.means.get(field, (None, 0))
        c_v, c_n = current.means.get(field, (None, 0))
        cost.append(
            {
                "metric": name,
                "field": field,
                "baseline": {"value": b_v, "n": b_n},
                "current": {"value": c_v, "n": c_n},
                "delta": (c_v - b_v) if (b_v is not None and c_v is not None) else None,
            }
        )

    return {
        "n_baseline_traces": baseline.n,
        "n_current_traces": current.n,
        "metrics": rows,
        "cost": cost,
        "alpha": alpha,
        "headline": headline(rows, baseline.n),
        "caveat": OBSERVATIONAL_CAVEAT,
    }


def headline(rows: list[dict], n_baseline: int) -> str:
    if not n_baseline:
        return (
            "No baseline window recorded, so there is nothing to compare against. "
            "Capture traces with `outcome.baseline: true` during a period before "
            "lessons are being injected (`commontrace capture --baseline`) and push "
            "them with `commontrace sync --push-traces` -- captured traces sit in "
            "the local store until that push, and fleet_outcomes only ever reads "
            "what has actually reached the Hub -- or run a randomized holdout "
            "instead, which needs no baseline window at all."
        )
    improved = [r for r in rows if r["verdict"] == VERDICT_IMPROVED]
    worsened = [r for r in rows if r["verdict"] == VERDICT_WORSENED]
    inconclusive = [r for r in rows if r["verdict"] == VERDICT_INCONCLUSIVE]

    if worsened:
        names = ", ".join(r["metric"] for r in worsened)
        lead = f"{len(worsened)} metric(s) moved in the WRONG direction: {names}."
        if improved:
            lead += f" {len(improved)} improved. Both are real; read the table."
        return lead
    if improved:
        best = max(improved, key=lambda r: abs(r["delta"]))
        return (
            f"{len(improved)} of {len(rows)} metric(s) improved measurably since the "
            f"baseline window -- largest: {best['metric']} by "
            f"{abs(best['delta']):.1%} (95% CI {best['ci_95'][0]:+.1%} to "
            f"{best['ci_95'][1]:+.1%}). Observed, not causal."
        )
    if inconclusive:
        return (
            f"No change distinguishable from noise across {len(inconclusive)} tested "
            "metric(s). Read each row's minimum detectable effect before concluding "
            "the product is doing nothing -- at a small sample that number is large."
        )
    return (
        "Not enough recorded outcomes to test anything yet. Fleets that leave "
        "`outcome` fields null are excluded from those metrics entirely, so this is "
        "as often an instrumentation gap as a volume problem."
    )
