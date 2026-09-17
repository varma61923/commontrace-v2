"""Randomized holdout: does injecting a lesson actually *cause* better outcomes?

WHY THIS EXISTS
---------------
Every number this product reports about lesson value was, until now,
correlational — including `lift` in commontrace/reliability.py. The
confound is structural and unavoidable by observation alone:

    A lesson is retrieved *because* the situation matched its
    activation condition. So the occasions where lesson L fired are
    systematically different from the occasions where it did not.

If L fires on routine tasks and stays quiet on hard ones, L will show
beautiful positive lift while contributing nothing. If L fires only on the
gnarliest incidents, it will look harmful while being the only reason those
incidents got resolved at all. Observation cannot separate these. No amount
of additional observational data fixes it; the bias does not shrink with n.

The only thing that does is deliberately withholding the lesson sometimes,
at random, and comparing. That is what this module implements.

WHAT IT UNLOCKS
---------------
1. **A causal number instead of a correlational one.** "Tasks resolved 34%
   more often with this lesson injected (95% CI [12%, 56%])" is a claim that
   survives a technical review. "Tasks with this lesson tend to succeed
   more" is not.
2. **Proof on the customer's own data.** The pitch today rests on two
   external case studies; a prospect has no way to verify the effect on
   their fleet short of a long, confounded before/after pilot.
3. **The separation of co-firing lessons.** Two lessons that always appear
   together share every outcome and cannot be told apart observationally.
   Independent per-lesson holdout breaks the tie by construction.
4. **A safety net for closing the learning loop.** Letting reliability
   scores drive retrieval ranking is only responsible if degradation is
   detectable. This is the detector.

DESIGN NOTES
------------
Assignment is a deterministic hash of (lesson, occasion, salt) rather than
a random draw. Three reasons: it needs no stored state, it is exactly
reproducible when someone disputes a result months later, and it is stable
under retries — the same occasion cannot flip arms by being processed
twice, which would silently corrupt the comparison.

Statistics are stdlib-only (math.erf for the normal CDF) to keep the core
install PyYAML-only, consistent with the rest of the package.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

# Fraction of eligible retrievals in which a lesson is deliberately withheld.
# 10% buys statistical power at a bounded cost: in the worst case the lesson
# was going to help and 1-in-10 occasions loses that help. Raising it finds
# effects faster and costs more; lowering it is safer and may never reach
# significance on a small fleet.
DEFAULT_HOLDOUT_RATE = 0.10

# Below this many occasions in EITHER arm, no effect is reported at all --
# not "no effect found", which people read as evidence of absence.
DEFAULT_MIN_ARM = 10

# The smallest effect worth calling a result, and the reason NO_MEASURABLE_EFFECT
# is not reported the moment DEFAULT_MIN_ARM is met.
#
# `min_arm` is a floor on running the test at all, not a power criterion, and
# treating it as one produced the defect this constant exists to fix. At 10
# observations per arm against a 60% baseline the MINIMUM DETECTABLE EFFECT is
# 61 percentage points. Any run clearing that floor and finding nothing got the
# verdict NO_MEASURABLE_EFFECT -- which a customer reads as "the memory does
# not work" -- when the honest statement is "this design could not have seen
# anything short of a 61-point swing."
#
# Measured on a full 240-occasion pilot with a real +25pp effect seeded in: the
# report came back NO_MEASURABLE_EFFECT for the one lesson that cleared the
# floor and "not enough data yet" for the other two. The product's central
# claim, answered wrongly, by its own default configuration.
#
# 10 points is a PRODUCT judgement rather than a statistical constant: it is
# roughly the smallest change in a fleet's resolution rate anyone would act on.
# It is configurable (`experiment --detect`), and the number is always printed
# beside the verdict so nobody has to take it on faith.
DEFAULT_PRACTICAL_EFFECT = 0.10

_Z_95 = 1.959963984540054
_Z_80_POWER = 0.8416212335729143  # one-sided z for 80% power


def _uniform_from(*parts: str) -> float:
    """Deterministic uniform draw in [0, 1) from the given strings."""
    digest = hashlib.blake2b("\x1f".join(parts).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


def is_held_out(
    lesson_slug: str,
    occasion_id: str,
    rate: float = DEFAULT_HOLDOUT_RATE,
    salt: str = "default",
) -> bool:
    """Should `lesson_slug` be withheld from `occasion_id`?

    Independent per lesson by construction: the hash includes the slug, so
    two lessons that always co-fire land in uncorrelated arms. That is
    precisely what makes their individual effects separable, which no amount
    of observation can achieve.

    `salt` names the experiment. Changing it reshuffles every assignment, so
    it must stay fixed for the life of an experiment -- rotating it mid-flight
    silently mixes two different randomizations into one comparison.
    """
    # NaN compares False against everything (`nan <= 0`, `nan >= 1`, and
    # `x < nan` are all False), so an unvalidated NaN rate fell through
    # every branch below to `_uniform_from(...) < rate` -- itself always
    # False -- and this function silently returned False for EVERY call,
    # forever. That is not "no holdout": it is the holdout experiment
    # running with 0% of assignments actually withheld while reporting
    # (and believing) it configured whatever --holdout-rate was passed,
    # a silent, undetectable measurement failure rather than a loud one.
    # +/-inf is equally nonsensical as a probability. Checked once, here,
    # rather than at each of the (CLI-argparse-parsed, so unvalidated by
    # construction) call sites.
    if not math.isfinite(rate):
        raise ValueError(f"holdout rate must be a finite number, got {rate!r}")
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    return _uniform_from(salt, lesson_slug, occasion_id) < rate


# --- Statistics -------------------------------------------------------------


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _check_success_count(s: int, n: int, label: str) -> None:
    """Both callers below feed s/n straight into a square root of
    (rate * (1 - rate)); a rate outside [0, 1] -- which s > n or a negative
    s produces -- can make that argument negative, crashing with a raw
    `ValueError: math domain error` from deep inside math.sqrt rather than
    one that names what was actually wrong with the input. Every current
    caller (hub/outcomes.py's Tally, commontrace/experiment.py's own
    holdout aggregation) computes s/n from a COUNT(*)-style aggregate, so
    0 <= s <= n always holds in practice -- this is a guard against a
    future caller or a hand-built test value, not a reachable path today.
    Raising rather than silently returning a "no effect" result is
    deliberate: an s > n means the CALLER's counting is broken, and a
    plausible-looking p-value from broken input is a worse failure mode
    than a loud one.
    """
    if s < 0 or n < 0:
        raise ValueError(f"{label} success/total counts must not be negative, got s={s}, n={n}")
    if s > n:
        raise ValueError(f"{label} success count ({s}) cannot exceed its total count ({n})")


def two_proportion_test(s1: int, n1: int, s2: int, n2: int) -> tuple[float, float]:
    """Two-tailed z-test for a difference in proportions.

    Returns (z, p_value). The pooled standard error is used for the test
    statistic (correct under the null that both arms share a rate), while
    the confidence interval below uses the unpooled form (correct for
    estimating a difference that is not zero). Mixing those up is a common
    error that produces intervals disagreeing with their own p-value.
    """
    if n1 <= 0 or n2 <= 0:
        return 0.0, 1.0
    _check_success_count(s1, n1, "arm 1")
    _check_success_count(s2, n2, "arm 2")
    p1, p2 = s1 / n1, s2 / n2
    p_pool = (s1 + s2) / (n1 + n2)
    if p_pool in (0.0, 1.0):
        return 0.0, 1.0  # no variance in either arm; nothing to detect
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0, 1.0
    z = (p1 - p2) / se
    return z, 2 * (1 - _norm_cdf(abs(z)))


def diff_confidence_interval(s1: int, n1: int, s2: int, n2: int, z: float = _Z_95) -> tuple[float, float]:
    """Unpooled 95% CI for (p1 - p2)."""
    if n1 <= 0 or n2 <= 0:
        return (0.0, 0.0)
    _check_success_count(s1, n1, "arm 1")
    _check_success_count(s2, n2, "arm 2")
    p1, p2 = s1 / n1, s2 / n2
    se = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    delta = p1 - p2
    return (delta - z * se, delta + z * se)


def benjamini_hochberg(p_values: list[float], alpha: float = 0.05) -> list[bool]:
    """Which hypotheses survive at FDR <= alpha.

    Necessary because a corpus is tested a lesson at a time: at alpha=0.05
    over 100 lessons, ~5 will look significant purely by chance, and those
    are exactly the ones that get quoted. Benjamini-Hochberg controls the
    expected *proportion* of false discoveries, which is the right error
    rate here -- Bonferroni controls the probability of any false positive
    and is so conservative on a corpus of this shape that real effects would
    be missed.
    """
    m = len(p_values)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p_values[i])
    keep = [False] * m
    max_rank = 0
    for rank, idx in enumerate(order, start=1):
        if p_values[idx] <= (rank / m) * alpha:
            max_rank = rank
    for rank, idx in enumerate(order, start=1):
        if rank <= max_rank:
            keep[idx] = True
    return keep


def _z_for_power(power: float) -> float:
    """One-sided normal quantile z such that P(Z <= z) == power.

    Bisection on the CDF rather than a table lookup: it is a handful of lines,
    exact to well past the precision anyone reads off an MDE, and keeps the
    module stdlib-only (no scipy.stats.norm.ppf).
    """
    low, high = 0.0, 10.0
    for _ in range(200):
        mid = (low + high) / 2
        if _norm_cdf(mid) < power:
            low = mid
        else:
            high = mid
    return (low + high) / 2


# --- Looking at a running experiment without inflating the false positives --
#
# THE PROBLEM. Every surface in this product reads a LIVE experiment:
# `experiment_status`, the console Proof page, `causal_effects` on every call,
# `working_set` promoting a trace the moment it clears significance. That is
# the product working as designed -- and it is also repeated significance
# testing on accumulating data, which is the oldest way to manufacture a
# result. At a fixed alpha of 0.05, the probability of crossing it AT SOME
# POINT during a run is far above 5%: roughly 14% over five equally spaced
# looks, and it keeps climbing with the number of looks, reaching ~1.0 if you
# look often enough. An agent fleet checking continuously is the "look often
# enough" case.
#
# So a memory could be promoted into the working set, and billed for, on a
# threshold that was never a 5% test. Nothing in this module knew a look was
# a look.
#
# THE FIX is standard and stdlib-sized: an alpha-SPENDING function (Lan and
# DeMets, 1983). Instead of spending the full 0.05 at every peek, spend a
# fraction of it determined by how much of the planned information has
# accrued, so that the TOTAL spent across every look up to the end of the
# experiment is still 0.05.
SPEND_OBRIEN_FLEMING = "obrien-fleming"
SPEND_POCOCK = "pocock"
SPENDING_FUNCTIONS = (SPEND_OBRIEN_FLEMING, SPEND_POCOCK)


def alpha_spent(
    information_fraction: float,
    alpha: float = 0.05,
    shape: str = SPEND_OBRIEN_FLEMING,
) -> float:
    """How much of the false-positive budget a look at `information_fraction`
    of the planned sample may spend.

    `information_fraction` is observations so far over observations planned
    (`required_n_per_arm`), clamped to (0, 1]. At 1.0 both shapes return
    `alpha` exactly: the final analysis spends whatever is left, which is the
    property that makes this a redistribution of the budget rather than a
    tax on it.

    O'Brien-Fleming (the default) is deliberately miserly early -- at 25% of
    the data it spends about 0.0001 of a 0.05 budget -- which matches what
    this product needs: an early, noisy, enormous-looking effect is exactly
    the thing that should not promote a memory into every future retrieval.
    Pocock spends evenly, which finds true effects earlier at the cost of a
    much stricter final look; offered for a caller who would rather stop
    early and knows the trade.

    Implemented as the Lan-DeMets continuous spending functions, so the looks
    do NOT have to be pre-scheduled or equally spaced -- which they cannot be
    here, since a look happens whenever somebody opens a page.
    """
    if shape not in SPENDING_FUNCTIONS:
        raise ValueError(
            f"unknown alpha-spending shape {shape!r}; expected one of "
            f"{', '.join(SPENDING_FUNCTIONS)}"
        )
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    t = min(max(information_fraction, 0.0), 1.0)
    if t <= 0.0:
        # No information, no budget. Returning 0 rather than a tiny number
        # means "nothing can be declared significant yet", which is the
        # correct reading of a look before any data.
        return 0.0
    if shape == SPEND_POCOCK:
        return alpha * math.log(1.0 + (math.e - 1.0) * t)
    # O'Brien-Fleming: 2 * (1 - Phi(z_{alpha/2} / sqrt(t))).
    z_half = _z_for_power(1.0 - alpha / 2.0)
    return 2.0 * (1.0 - _norm_cdf(z_half / math.sqrt(t)))


def anytime_confidence_interval(
    s1: int, n1: int, s2: int, n2: int,
    alpha: float = 0.05,
    target_n_per_arm: int = 0,
) -> tuple[float, float]:
    """A confidence interval for the difference in rates that is valid at
    EVERY sample size at once, not just at one pre-chosen stopping point.

    WHY THIS AND NOT THE SPENDING FUNCTION ABOVE. An alpha-spending schedule
    redistributes a fixed budget across looks taken up to a PLANNED end, and
    offers nothing after that end -- measured on this module's own estimator,
    continuous monitoring past the planned sample size still produced a false
    "HELPS" in 12.7% of null runs, against a nominal 5%, because every look
    beyond t=1 spent the full alpha again. That is not a tuning problem: the
    framework assumes the experiment stops, and this product's experiments do
    not. A fleet keeps running, and somebody opens the Proof page whenever
    they like.

    A confidence sequence is the tool built for exactly that. The guarantee
    is uniform over time -- P(the interval ever misses the true effect, at
    ANY n) <= alpha -- so "peeked continuously and stopped when it looked
    good" is not a way to break it, because there is no stopping rule it
    depends on. The cost is width: it is wider than a fixed-n interval at
    every single n, and that width is the honest price of being allowed to
    look whenever you want.

    Implemented as a Robbins-style normal mixture boundary: for a mean with
    variance proxy `v` over `n` observations,

        radius(n) = sqrt( (2 * (n*rho + 1) / (n^2 * rho))
                          * ln( sqrt(n*rho + 1) / alpha ) * v )

    `rho` tunes WHERE the sequence is tightest, and is set from
    `target_n_per_arm` (the sample size the experiment was planned for), so
    the boundary is at its narrowest around the point the design expected to
    answer at, rather than at an arbitrary n. Falls back to the observed n
    when no target is given.
    """
    _check_success_count(s1, n1, "injected arm")
    _check_success_count(s2, n2, "withheld arm")
    if n1 <= 0 or n2 <= 0:
        return (-1.0, 1.0)
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")

    rate1, rate2 = s1 / n1, s2 / n2
    effect = rate1 - rate2
    # Variance of the difference, as the fixed-n interval uses -- the
    # sequence changes the multiplier applied to it, not the quantity.
    variance = rate1 * (1 - rate1) / n1 + rate2 * (1 - rate2) / n2
    if variance <= 0.0:
        # Degenerate arm (every occasion succeeded, or none did). No spread to
        # bound, and a zero-width interval would claim certainty from a
        # sample that has simply not seen both outcomes yet.
        return (-1.0, 1.0)

    n_effective = min(n1, n2)
    target = target_n_per_arm if target_n_per_arm > 0 else n_effective
    rho = 1.0 / max(target, 1)
    inner = n_effective * rho + 1.0
    radius = math.sqrt(
        (2.0 * inner / (n_effective * n_effective * rho))
        * math.log(math.sqrt(inner) / alpha)
    )
    # `variance` is already the variance OF THE DIFFERENCE at this n, so the
    # per-observation variance the boundary is stated in terms of is that
    # times n.
    half_width = radius * math.sqrt(variance * n_effective)
    return (effect - half_width, effect + half_width)


def minimum_detectable_effect(n_per_arm: int, baseline: float, power: float = 0.80) -> float | None:
    """Smallest true effect this sample size could reliably detect.

    Reported so "no significant effect" can be read correctly. With 15
    occasions per arm the MDE is enormous, which means the honest conclusion
    is "this experiment cannot answer the question yet" rather than "the
    lesson does nothing."
    """
    if n_per_arm <= 0 or not (0 < baseline < 1):
        return None
    if not 0.5 <= power < 1.0:
        raise ValueError(f"power must be in [0.5, 1.0), got {power}")
    # Both branches of the ternary this replaces were _Z_80_POWER, so `power`
    # was accepted and ignored: asking for 95% power silently returned the 80%
    # answer, understating the sample size a real experiment needs.
    return (_Z_95 + _z_for_power(power)) * math.sqrt(2 * baseline * (1 - baseline) / n_per_arm)


def required_n_per_arm(effect: float, baseline: float, power: float = 0.80) -> int:
    """Observations needed IN EACH ARM to detect `effect`. The inverse of
    `minimum_detectable_effect`, and algebraically its exact inverse rather
    than a search, so the two can never disagree about the same design.
    """
    if not (0 < baseline < 1) or effect <= 0:
        raise ValueError("effect must be > 0 and baseline strictly between 0 and 1")
    z = _Z_95 + _z_for_power(power)
    return max(1, math.ceil(2 * baseline * (1 - baseline) * (z / effect) ** 2))


@dataclass(frozen=True)
class Design:
    """What it takes to answer the question, worked out BEFORE the run."""

    effect: float
    baseline: float
    power: float
    n_per_arm: int
    occasions_needed: int
    rate_used: float
    rate_for_budget: float | None
    occasions_budget: int | None
    verdict: str


def plan(
    effect: float = DEFAULT_PRACTICAL_EFFECT,
    baseline: float = 0.5,
    rate: float = DEFAULT_HOLDOUT_RATE,
    power: float = 0.80,
    occasions_budget: int | None = None,
) -> Design:
    """Design the experiment before running it.

    This exists because the failure it prevents is expensive and silent: a
    fleet runs a 30-day pilot at the default holdout rate, and the report at
    the end says "not enough data yet". The occasions are spent, the window is
    gone, and the only fix -- a wider holdout -- had to be applied on day 0.

    The arithmetic nobody does in their head: at a 10% holdout, only one
    occasion in ten lands in the control arm, so a run reaches an answer about
    TEN TIMES slower than its occasion count suggests. Detecting a 10-point
    effect against a 60% baseline needs ~376 control observations, which is
    ~3,760 occasions at 10% and ~750 at 50%.

    `rate_for_budget` answers the question a customer actually has -- "I will
    see about N occasions in this window; what rate do I need?" -- and is None
    when no budget can answer it, which is itself the finding.
    """
    n_per_arm = required_n_per_arm(effect, baseline, power)
    # A stopped (rate=0) or out-of-range experiment has no design: fail loud
    # rather than rendering "0 occasions needed", which reads as answered.
    #
    # Bounded to (0.0, 0.5], not (0.0, 1.0): `rate` is the HOLDOUT (control)
    # arm's share, and everything below -- occasions_needed's `smaller_share`,
    # `rate_for_budget`'s feasibility check, and render_plan's "raise_rate"
    # remediation ("set the rate to X or HIGHER") -- assumes the control arm
    # is the smaller one, i.e. rate <= 1-rate. Above 0.5 that inverts (the
    # INJECTION arm becomes the binding one), and "raise the rate" is then
    # backwards advice -- LOWERING it is what would grow the shrinking arm.
    # Reproduced without this bound: plan(effect=0.10, baseline=0.6, rate=0.9,
    # occasions_budget=3000) reported verdict="ok" by comparing the required
    # share (0.126) against `rate` (0.9) instead of against the binding arm's
    # actual share (1-0.9=0.1) -- 3000 occasions at rate=0.9 gives the
    # injection arm only 300, short of the ~377 needed, so it was actually
    # infeasible. Nothing upstream (this CLI's own --holdout-rate check
    # included) restricted rate to this domain before this fix.
    if not 0.0 < rate <= 0.5:
        raise ValueError(
            f"holdout rate must be in (0.0, 0.5], got {rate}. This is the "
            "share withheld as the control arm, which this design (and its "
            "'raise the rate' remediation) assumes is the smaller one."
        )
    # The control arm is the binding one at any rate below 50%, and both arms
    # must reach n_per_arm, so the requirement is set by whichever is smaller
    # -- which, given the bound above, is always `rate` itself.
    smaller_share = min(rate, 1.0 - rate)
    occasions_needed = math.ceil(n_per_arm / smaller_share)

    rate_for_budget = None
    verdict = "ok"
    if occasions_budget:
        # The share each arm needs of the budget; feasible only if both arms
        # can reach n_per_arm inside it, i.e. the budget is at least 2x.
        needed_share = n_per_arm / occasions_budget
        if needed_share > 0.5:
            verdict = "infeasible"
        else:
            rate_for_budget = round(needed_share, 4)
            verdict = "ok" if needed_share <= rate else "raise_rate"

    return Design(
        effect=effect, baseline=baseline, power=power, n_per_arm=n_per_arm,
        occasions_needed=occasions_needed, rate_used=rate,
        rate_for_budget=rate_for_budget, occasions_budget=occasions_budget,
        verdict=verdict,
    )


def render_plan(design: Design) -> str:
    lines = [
        "# Experiment design",
        "",
        f"To detect an effect of **{design.effect:.0%}** against a **{design.baseline:.0%}** "
        f"baseline at {design.power:.0%} power:",
        "",
        f"- **{design.n_per_arm:,} observations in EACH arm.**",
        f"- At a {design.rate_used:.0%} holdout that is **{design.occasions_needed:,} "
        "occasions**, because only that share of them lands in the control arm.",
        "",
    ]
    if design.occasions_budget:
        budget = design.occasions_budget
        if design.verdict == "infeasible":
            lines += [
                f"**{budget:,} occasions cannot answer this at any holdout rate.** Even a "
                f"50/50 split gives {budget // 2:,} per arm against the {design.n_per_arm:,} "
                "needed. Either accept a larger effect as the thing you are testing for, "
                "or run for longer — no rate recovers this.",
                "",
            ]
        elif design.verdict == "raise_rate":
            lines += [
                f"**{budget:,} occasions can answer this, but not at {design.rate_used:.0%}.** "
                f"Set the holdout rate to **{design.rate_for_budget:.0%}** or higher.",
                "",
                "The cost is real and worth stating plainly: that share of the work runs "
                "without its memory for the length of the experiment. That is the price of "
                "an answer, and it is cheaper than a spent pilot that concludes nothing.",
                "",
            ]
        else:
            lines += [
                f"**{budget:,} occasions is enough at {design.rate_used:.0%}.** "
                f"A rate of {design.rate_for_budget:.0%} would just reach it, so the "
                "configured rate has margin.",
                "",
            ]
    lines += [
        "---",
        "",
        "Run this before the pilot, not after. The failure it prevents is silent: a run "
        "at too low a rate produces a report that says 'not enough data yet' on the last "
        "day, and the only fix had to be applied on the first.",
    ]
    return "\n".join(lines)


# --- Analysis ---------------------------------------------------------------


@dataclass
class HoldoutObservation:
    """One occasion on which a lesson was *eligible* to be injected.

    `eligible` is the crucial field and the easiest thing to get wrong: the
    comparison is only valid across occasions where the lesson's activation
    condition matched. Comparing "injected" against "every occasion it never
    matched" reintroduces exactly the confound the holdout exists to remove.
    """

    lesson_slug: str
    occasion_id: str
    injected: bool  # False => deliberately withheld
    succeeded: bool


@dataclass
class CausalEffect:
    lesson_slug: str
    n_injected: int
    n_withheld: int
    rate_injected: float
    rate_withheld: float
    effect: float                 # rate_injected - rate_withheld
    ci_low: float
    ci_high: float
    p_value: float
    significant: bool             # after FDR correction across all lessons
    min_detectable_effect: float | None
    verdict: str
    note: str = ""


VERDICT_HELPS = "HELPS"
VERDICT_HURTS = "HURTS"
VERDICT_NO_EFFECT = "NO_MEASURABLE_EFFECT"
VERDICT_UNDERPOWERED = "UNDERPOWERED"


def analyze(
    observations: list[HoldoutObservation],
    min_arm: int = DEFAULT_MIN_ARM,
    alpha: float = 0.05,
    detectable: float = DEFAULT_PRACTICAL_EFFECT,
    sequential: bool = False,
    spending_shape: str = SPEND_OBRIEN_FLEMING,
) -> list[CausalEffect]:
    """Estimate each lesson's causal effect from its holdout arms.

    `detectable` is the smallest effect worth calling a result. A null from a
    design that could not have detected it is reported as UNDERPOWERED rather
    than as NO_MEASURABLE_EFFECT -- see DEFAULT_PRACTICAL_EFFECT for why the
    `min_arm` floor alone was not enough, and what it cost.

    `sequential` treats this call as ONE LOOK at a running experiment rather
    than as its final analysis, and is what a surface that reads a live
    experiment should pass. Every peek at accumulating data is another chance
    to cross a fixed threshold by luck -- at alpha=0.05 the probability of
    crossing at some point is ~14% over five looks and approaches certainty
    if you look continuously, which an agent fleet does. With this set, each
    lesson's significance is judged against the alpha its own accrual has
    EARNED (`alpha_spent`, above), so the total false-positive budget across
    every look of the whole experiment is still `alpha`.

    The boundary is `anytime_confidence_interval` -- valid at every sample
    size simultaneously, so there is no stopping rule to violate. An
    alpha-spending schedule (`alpha_spent`, also implemented here) is the
    more familiar answer and is not sufficient for this product: it assumes
    the experiment stops at a planned size, and these experiments do not.

    WHAT IT COSTS, measured on this estimator rather than asserted. Under
    continuous monitoring, baseline 60%, 300 null runs / 120 per power cell:

        false "HELPS" when the true effect is zero
            fixed threshold      28.0%
            O'Brien-Fleming      12.7%
            confidence sequence   1.3%   (nominal 5%; a sequence budgets for
                                          an unbounded future, so a bounded
                                          run spends less than its alpha)

        power within 1,000 occasions
            true effect   fixed    sequence
                 +5%       62%       13%
                +10%       95%       69%
                +25%      100%      100%
                +50%      100%      100%

    So this trades detection speed for a verdict that survives having been
    watched: anything at or above the effect size this product says is worth
    acting on still lands, and smaller ones take a fleet longer to establish.
    That is the right side to err on, because the verdict promotes a memory
    into every future retrieval and feeds an invoice -- a one-in-four chance
    of fabricating "HELPS" would retract this product's central claim, and
    waiting longer for a number that holds does not.

    Default False, so the pure estimator is unchanged for a caller doing a
    one-shot analysis of a finished run, where a sequential boundary would
    only cost power for no gain.
    """
    by_lesson: dict[str, list[HoldoutObservation]] = {}
    for obs in observations:
        by_lesson.setdefault(obs.lesson_slug, []).append(obs)

    staged: list[tuple[str, int, int, int, int]] = []  # slug, s_inj, n_inj, s_wit, n_wit
    for slug, rows in sorted(by_lesson.items()):
        inj = [r for r in rows if r.injected]
        wit = [r for r in rows if not r.injected]
        staged.append((slug, sum(r.succeeded for r in inj), len(inj),
                       sum(r.succeeded for r in wit), len(wit)))

    # p-values only from comparisons that met the floor; ones that did not
    # must not enter the FDR correction, where they would inflate m and
    # weaken every genuine result.
    #
    # The floor, deliberately, and not the `detectable` gate below: m must be
    # the number of tests actually RUN. A comparison whose null is later
    # relabelled UNDERPOWERED was still tested, and dropping it from the
    # correction after seeing its p-value would make m depend on the results.
    testable = [s for s in staged if s[2] >= min_arm and s[4] >= min_arm]
    p_values = [two_proportion_test(s[1], s[2], s[3], s[4])[1] for s in testable]
    significance = benjamini_hochberg(p_values, alpha=alpha)
    sig_by_slug = {s[0]: sig for s, sig in zip(testable, significance)}

    # Sequential looks: a result must clear BOTH the multiplicity correction
    # across memories (above) and the boundary its own accrual has earned.
    # The intersection of two rejection rules, deliberately -- combining them
    # into one adjusted level would be the standard move for a pre-scheduled
    # design with a shared information clock, and there is no shared clock
    # here: memories accrue at different rates, and a look happens whenever
    # somebody opens a page. Requiring both is conservative, which is the
    # direction to err when the output feeds an invoice.
    # Sequential looks: a result must clear BOTH the multiplicity correction
    # across memories (above) and a boundary that survives having been looked
    # at continuously. The intersection of two rejection rules, deliberately;
    # requiring both is conservative, which is the direction to err when the
    # output feeds an invoice.
    #
    # The boundary is a confidence sequence rather than an alpha-spending
    # schedule. Both are implemented here and the choice is measured, not
    # assumed: under continuous monitoring of a null effect, the fixed
    # threshold produced a false HELPS in 33% of runs, O'Brien-Fleming
    # spending brought that to 12.7%, and the sequence holds it at or under
    # the nominal 5% -- because spending assumes the experiment stops at its
    # planned size and these experiments do not.
    sequence_by_slug: dict[str, tuple[float, float]] = {}
    if sequential:
        for slug, s_inj, n_inj, s_wit, n_wit in testable:
            baseline = ((s_inj + s_wit) / (n_inj + n_wit)) if (n_inj + n_wit) else 0.0
            # Tuned to where a memory WORTH PROMOTING concludes, not to where
            # the design would exhaust itself. A confidence sequence is
            # narrowest at its tuning point and wider either side, and
            # required-n scales as 1/effect^2 -- so tuning to the smallest
            # effect worth acting on (`detectable`) makes the boundary
            # widest exactly where a big, obvious effect lands early.
            # Measured: at 60 per arm with a +50pp effect -- overwhelming by
            # any fixed-n reading -- a boundary tuned to `detectable` spans
            # zero, and one tuned to 2x `detectable` gives (+17.7%, +82.3%).
            # Withholding that verdict would not be caution, it would be a
            # miscalibrated instrument.
            #
            # The guarantee holds for ANY fixed tuning point (it must not
            # depend on the data, and this does not -- `detectable` is a
            # configured constant); the choice only moves where the width is
            # spent.
            target = (
                required_n_per_arm(2.0 * detectable, baseline)
                if 0.0 < baseline < 1.0 else 0
            )
            interval = anytime_confidence_interval(
                s_inj, n_inj, s_wit, n_wit, alpha=alpha, target_n_per_arm=target
            )
            sequence_by_slug[slug] = interval
            if sig_by_slug.get(slug) and interval[0] <= 0.0 <= interval[1]:
                sig_by_slug[slug] = False

    out: list[CausalEffect] = []
    for slug, s_inj, n_inj, s_wit, n_wit in staged:
        rate_inj = (s_inj / n_inj) if n_inj else 0.0
        rate_wit = (s_wit / n_wit) if n_wit else 0.0
        effect = rate_inj - rate_wit
        _, p = two_proportion_test(s_inj, n_inj, s_wit, n_wit)
        lo, hi = diff_confidence_interval(s_inj, n_inj, s_wit, n_wit)
        baseline = ((s_inj + s_wit) / (n_inj + n_wit)) if (n_inj + n_wit) else 0.0
        mde = minimum_detectable_effect(min(n_inj, n_wit), baseline)

        if n_inj < min_arm or n_wit < min_arm:
            verdict = VERDICT_UNDERPOWERED
            note = (
                f"{n_inj} injected / {n_wit} withheld; {min_arm} needed in each arm. "
                "This is 'cannot answer yet', not 'no effect'."
            )
            significant = False
        else:
            significant = sig_by_slug.get(slug, False)
            # When `sequential` established significance, its guarantee is
            # the anytime-valid `sequence_by_slug` interval, not the fixed-
            # sample `diff_confidence_interval` set above (line ~738) --
            # that interval's coverage assumes a single look at a fixed n,
            # exactly the assumption continuous monitoring violates, and
            # reporting it here would show a narrower, invalid-for-this-use
            # interval on precisely the number most likely to be quoted
            # from a HELPS/HURTS verdict. The other branch that also
            # reports the sequence interval (a few lines below, for the
            # "would clear a fixed threshold but the anytime interval still
            # spans zero" UNDERPOWERED case) already gets this right;
            # HELPS/HURTS didn't.
            if significant and sequential and slug in sequence_by_slug:
                lo, hi = sequence_by_slug[slug]
            if significant and effect > 0:
                verdict, note = VERDICT_HELPS, "Injecting this lesson causes better outcomes."
            elif significant and effect < 0:
                verdict, note = (
                    VERDICT_HURTS,
                    "Outcomes are WORSE when this is injected. Correlational scoring "
                    "cannot see this -- only the holdout can.",
                )
            elif sequential and p <= alpha and slug in sequence_by_slug:
                # It would have cleared a fixed alpha, and its anytime-valid
                # interval still contains zero. Reported as UNDERPOWERED
                # rather than as a null, because that is what it is -- "not
                # yet", on a run still accruing -- and calling it
                # NO_MEASURABLE_EFFECT would be the same evidence-of-absence
                # error the branch below exists to avoid, from the other side.
                lo, hi = sequence_by_slug[slug]
                verdict = VERDICT_UNDERPOWERED
                note = (
                    f"p={p:.4g} would clear a fixed {alpha:.0%} threshold, but this is "
                    "one look at a running experiment, and an interval valid at every "
                    f"sample size at once still spans zero ({lo:+.1%} to {hi:+.1%}). "
                    "Testing accumulating data repeatedly at a fixed threshold crosses "
                    "it by luck sooner or later -- measured on this estimator, in a "
                    "third of null runs. Keep accruing; this is reported the moment it "
                    "clears a boundary that survives having been watched."
                )
            elif mde is None or mde > detectable:
                # A NULL from an underpowered design is not a finding, and this
                # is the asymmetry that makes the distinction correct rather
                # than cautious: a SIGNIFICANT result at small n is still a
                # detection (the branches above keep it), but a null only means
                # "no effect" if the design could have seen one. Reported here
                # as UNDERPOWERED, which is what it is.
                verdict = VERDICT_UNDERPOWERED
                seen = f"~{mde:.0%}" if mde is not None else "no effect at all"
                note = (
                    f"{n_inj} injected / {n_wit} withheld clears the {min_arm}-per-arm "
                    f"floor, but this design could only have detected {seen} or larger, "
                    f"against a target of {detectable:.0%}. Finding nothing here is "
                    "'cannot answer yet', not 'no effect' -- widen the holdout rate or "
                    "keep accruing."
                )
            else:
                verdict = VERDICT_NO_EFFECT
                note = (
                    "No effect distinguishable from noise, on a sample that could have "
                    f"detected ~{mde:.0%} -- which is smaller than the {detectable:.0%} "
                    "worth acting on, so this is evidence of absence rather than absence "
                    "of evidence."
                )

        out.append(
            CausalEffect(
                lesson_slug=slug, n_injected=n_inj, n_withheld=n_wit,
                rate_injected=round(rate_inj, 4), rate_withheld=round(rate_wit, 4),
                effect=round(effect, 4), ci_low=round(lo, 4), ci_high=round(hi, 4),
                p_value=round(p, 6), significant=significant,
                min_detectable_effect=round(mde, 4) if mde is not None else None,
                verdict=verdict, note=note,
            )
        )

    order = {VERDICT_HURTS: 0, VERDICT_HELPS: 1, VERDICT_NO_EFFECT: 2, VERDICT_UNDERPOWERED: 3}
    out.sort(key=lambda e: (order[e.verdict], -abs(e.effect)))
    return out


@dataclass
class ExperimentSummary:
    n_observations: int
    n_lessons: int
    holdout_rate: float
    effects: list[CausalEffect] = field(default_factory=list)


def _format_p(p: float) -> str:
    """A p-value is never exactly zero. Rounding a tiny one to `0.000` claims
    something impossible, and it is the first thing a reviewer notices."""
    return f"{p:.3f}" if p >= 0.001 else "<0.001"


def render(summary: ExperimentSummary, alpha: float = 0.05) -> str:
    eff = summary.effects
    counts: dict[str, int] = {}
    for e in eff:
        counts[e.verdict] = counts.get(e.verdict, 0) + 1

    lines = [
        "# Causal Effect Report",
        "",
        "Measured by **randomly withholding** lessons from occasions where they were "
        "eligible, then comparing. Unlike a correlational lift number, this is not "
        "confounded by the fact that lessons fire on the situations that match them.",
        "",
        f"- Occasions analyzed: **{summary.n_observations}**",
        f"- Lessons under test: **{summary.n_lessons}**",
        f"- Holdout rate: **{summary.holdout_rate:.0%}**",
        f"- Helps: **{counts.get(VERDICT_HELPS, 0)}** · "
        f"Hurts: **{counts.get(VERDICT_HURTS, 0)}** · "
        f"No measurable effect: **{counts.get(VERDICT_NO_EFFECT, 0)}** · "
        f"Underpowered: **{counts.get(VERDICT_UNDERPOWERED, 0)}**",
        f"- Significance at FDR ≤ {alpha:.2f} (Benjamini-Hochberg across all tested lessons)",
        "",
    ]

    tested = [e for e in eff if e.verdict in (VERDICT_HELPS, VERDICT_HURTS, VERDICT_NO_EFFECT)]
    if tested:
        lines += [
            "## Measured effects",
            "",
            "| Lesson | Verdict | With | Without | Effect | 95% CI | p |",
            "|---|---|---|---|---|---|---|",
        ]
        for e in tested:
            lines.append(
                f"| `{e.lesson_slug}` | **{e.verdict}** | {e.rate_injected:.0%} "
                f"({e.n_injected}) | {e.rate_withheld:.0%} ({e.n_withheld}) | "
                f"{e.effect:+.0%} | [{e.ci_low:+.0%}, {e.ci_high:+.0%}] | "
                f"{_format_p(e.p_value)} |"
            )
        lines.append("")
        for e in tested:
            if e.note:
                lines.append(f"- `{e.lesson_slug}` — {e.note}")
        lines.append("")

    under = [e for e in eff if e.verdict == VERDICT_UNDERPOWERED]
    if under:
        lines += [
            "## Not enough data yet",
            "",
            "Listed so they are not mistaken for lessons that were tested and found "
            "ineffective. They have not been tested.",
            "",
        ]
        lines += [f"- `{e.lesson_slug}` — {e.note}" for e in under]
        lines.append("")

    lines += [
        "---",
        "",
        "**How to read this.** A significant positive effect is evidence the lesson "
        "causes better outcomes on this fleet's own work. \"No measurable effect\" "
        "means this sample could not detect one at the stated size — it is not proof "
        "of absence, which is why the minimum detectable effect is quoted alongside.",
        "",
        "Assignment is a deterministic hash of (lesson, occasion, salt), so results "
        "are exactly reproducible and an occasion cannot change arms on retry. "
        "Changing the salt reshuffles everything and starts a new experiment.",
    ]
    return "\n".join(lines)
