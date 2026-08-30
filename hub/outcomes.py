"""Fleet outcome measurement: is this customer's agent fleet actually
getting better, and can the number survive being quoted?

WHY THIS EXISTS
---------------
`Trace.outcome` has carried the five business-outcome fields since the
schema was written -- `resolved`, `escalated`, `repeated_error`,
`frustration_signal`, plus token/call cost -- and, critically, a
`baseline` flag marking traces captured BEFORE this product was injecting
lessons. Every `contribute_trace` writes all of it.

Until this module, the Hub read exactly two things from that column: it
copied it onto the wire projection, and it carried it forward on amend.
It computed nothing. So a deployment holding a year of a fleet's outcome
history could not answer the one question the whole product exists to
answer -- "is it working?" -- and neither could the operator running it.

That is not a missing report. STRATEGY.md §11.3 names measured effect on
the customer's own data as the entire moat ("nobody rips out the thing
with a measured effect size on their own data"), and §11.5 names measured
resolution-rate improvement as the only pricing denominator this product
can defend. Both were claims about a number nothing in the Hub could
compute. The client CLI could compute a local version of it, for a
customer who thought to run it, against files on their own disk. The
service could not.

WHAT THIS IS NOT, STATED BEFORE ANYTHING ELSE
---------------------------------------------
**This is a before/after comparison. It is not a causal estimate, and it
must never be described as one.** `outcome.baseline` marks a time window.
Anything else that changed between the windows -- a model upgrade, a shift
in task mix, a team simply getting better at its job, a seasonal change in
what customers ask -- is confounded with this product's contribution and
cannot be separated from it by any amount of statistics applied to these
two buckets.

commontrace/experiment.py is the design that CAN support a causal claim:
it withholds lessons at random, so the arms differ only by the treatment.
This module deliberately borrows that module's statistics and deliberately
does not borrow its language. Everything here reports an *observed change*
with honest uncertainty, and every rendering path carries
`OBSERVATIONAL_CAVEAT`.

The reason for that care is narrow and practical: this is the number that
will end up in a renewal conversation. A number that gets quoted and later
demolished is worse than no number, and "our customers improved 23%" is
demolished by the first person who asks what else changed that quarter.

WHY THE STATISTICS ARE IMPORTED AND NOT REIMPLEMENTED
-----------------------------------------------------
Same reasoning hub/commons.py gives for importing the client's MinHash:
a near-copy that drifted would not fail loudly, it would return a
confident, wrong number. Here the specific failure is worse than wrong --
it is *disagreement*. The customer runs `commontrace impact` on their own
store and the operator runs `hub.manage outcomes` on the same fleet; if
those two produced different deltas for the same data, the number is
finished as evidence no matter which one was right.

commontrace/experiment.py is pure stdlib (hashlib, math, dataclasses), so
importing it costs the Hub no new dependency -- the same property that
made importing commontrace.overlap free.
"""

from __future__ import annotations

from commontrace import experiment

# The four proportion metrics, with the direction that counts as better.
# `repeated_error` is the one this product most directly claims to move:
# a fleet that keeps rediscovering the same failure is the problem
# CommonTrace exists to solve, so a fall here is the closest thing to a
# mechanism-level result rather than a general "things improved".
PROPORTION_METRICS: tuple[tuple[str, str, str], ...] = (
    ("resolution_rate", "resolved", "up"),
    ("repeated_error_rate", "repeated_error", "down"),
    ("escalation_rate", "escalated", "down"),
    ("frustration_rate", "frustration_signal", "down"),
)

# Cost metrics. Reported as means with no significance test attached, on
# purpose: these are unbounded continuous values, often heavily skewed by a
# handful of very long tasks, and a two-proportion z-test does not apply to
# them. Quietly running one anyway to produce a p-value for every row would
# be the single easiest way to make this report look more rigorous than it
# is.
MEAN_METRICS: tuple[tuple[str, str], ...] = (
    ("avg_tokens_used", "tokens_used"),
    ("avg_llm_calls", "llm_calls"),
)

# The textbook condition for the normal approximation behind
# `two_proportion_test`: at least this many successes AND this many
# failures in EACH arm. Preferred to a flat minimum on n because it adapts
# to the rate -- 40 traces is plenty at a 50% resolution rate and not
# nearly enough at 2% -- and because it is the actual assumption being
# relied on rather than a round number chosen to feel careful.
MIN_PER_CELL = 5

# False-discovery rate for the Benjamini-Hochberg correction applied across
# the proportion metrics in one report.
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
    """Strictly a bool, not merely truthy.

    JSONB round-trips whatever was written, and `contribute_trace` accepts
    the outcome object largely as given -- so a client that sent the STRING
    "false" would land a truthy value in a field this module counts as a
    success, silently inverting the rate. The reference implementation
    (commontrace/reference/pilot_metrics.py) makes the same check for the
    same reason against hand-edited frontmatter.
    """
    return isinstance(value, bool)


def _is_number(value: object) -> bool:
    """A real number, excluding bool -- `isinstance(True, int)` is True in
    Python, so a `resolved: true` misfiled under `tokens_used` would
    otherwise be averaged in as the number 1."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def split_arms(outcomes: list[dict]) -> tuple[list[dict], list[dict]]:
    """(baseline, current). `baseline` is opt-in and defaults false, so a
    fleet that never ran a baseline window has an empty first arm and every
    metric correctly reports insufficient_data rather than comparing the
    fleet against nothing."""
    baseline = [o for o in outcomes if _is_bool(o.get("baseline")) and o["baseline"]]
    current = [o for o in outcomes if not (_is_bool(o.get("baseline")) and o["baseline"])]
    return baseline, current


def proportion(outcomes: list[dict], field: str) -> tuple[int, int]:
    """(successes, n) over the outcomes that recorded a real boolean for
    this field. A trace that left the field null is excluded from this
    metric's denominator entirely rather than counted as false -- the
    schema is explicit that absent means "not recorded", and treating it as
    a negative would make every fleet's resolution rate fall as it captured
    more traces without filling the field in."""
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
    """The whole before/after report for one fleet.

    Three things here are the difference between a report and a sales
    slide, and all three make the conclusion weaker rather than stronger:

    1. **Benjamini-Hochberg across the four proportion metrics.** Testing
       four things at alpha=0.05 and quoting whichever came back
       significant is how a null result gets published as a win. The
       correction is applied to exactly the tests in THIS report -- see
       `hub/manage.py outcomes` on why scanning many orgs for the
       significant ones is a further multiple-comparisons problem this
       cannot fix for you.

    2. **A minimum detectable effect on every inconclusive row.** "No
       significant improvement" from 60 traces and from 60,000 are the same
       string and opposite facts. The MDE says which one you are reading,
       so a small sample reports "this cannot answer the question yet"
       rather than "the product does nothing".

    3. **`worsened` is a real verdict.** A significant move in the wrong
       direction is reported as such, with the same prominence as a win. A
       measurement instrument that can only return good news is not a
       measurement instrument, and the moat argument in STRATEGY.md §11.3
       depends on this number being one a customer can trust against their
       own interest.
    """
    rows: list[dict] = []
    testable_idx: list[int] = []
    p_values: list[float] = []

    for name, field, direction in PROPORTION_METRICS:
        b_s, b_n = proportion(baseline, field)
        c_s, c_n = proportion(current, field)
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

        # Current first, baseline second, so a positive delta always means
        # "the rate went up since baseline" regardless of whether up is the
        # good direction for this metric. `direction` carries the good/bad
        # reading; the sign stays a plain statement about the data.
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
        # Computed against the baseline rate and the smaller arm: the
        # question an MDE answers is "what could this experiment have
        # detected", and the weaker arm is what bounds that.
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
                # The interval excludes zero but the verdict says no change,
                # and a reader who spots that without an explanation will
                # reasonably conclude one of the two numbers is broken.
                # Neither is: the interval is a plain uncorrected 95% CI on
                # this metric alone, while significance is judged after
                # correcting across all four. Deliberately not "fixed" by
                # widening the intervals to match -- the CI is the right
                # thing to read when asking how big the change on THIS
                # metric plausibly is, and silently inflating it would make
                # every effect size in the report less informative to hide
                # one apparent inconsistency.
                note += (
                    " The interval on this row excludes zero while the row reads as no"
                    " change: the interval is uncorrected and describes this metric"
                    " alone, whereas significance is judged after correcting across"
                    " all four metrics tested here."
                )
            row["note"] = note

    cost: list[dict] = []
    for name, field in MEAN_METRICS:
        b_v, b_n = mean(baseline, field)
        c_v, c_n = mean(current, field)
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
        "n_baseline_traces": len(baseline),
        "n_current_traces": len(current),
        "metrics": rows,
        "cost": cost,
        "alpha": alpha,
        "headline": headline(rows, len(baseline)),
        "caveat": OBSERVATIONAL_CAVEAT,
    }


def headline(rows: list[dict], n_baseline: int) -> str:
    """One sentence an operator or a customer can read without decoding the
    table -- and the sentence has to stay honest when the news is bad or
    absent, which is most of the time early on."""
    if not n_baseline:
        return (
            "No baseline window recorded, so there is nothing to compare against. "
            "Capture traces with `outcome.baseline: true` during a period before "
            "lessons are being injected (`commontrace capture --baseline`), or run "
            "a randomized holdout instead, which needs no baseline window at all."
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
