"""Randomized holdout: does injecting a lesson actually *cause* better outcomes?"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

DEFAULT_HOLDOUT_RATE = 0.10

DEFAULT_MIN_ARM = 10

DEFAULT_PRACTICAL_EFFECT = 0.10

# Precomputed standard-normal quantiles. Literals, so the power/sample-size
# math below never pays for a solver; _norm_ppf must reproduce each of these
# to ~1e-8 (pinned by TestNormalQuantile in tests/test_experiment.py).
_Z_95 = 1.959963984540054  # Phi^{-1}(0.975): two-sided 95% point (alpha = 0.05)
_Z_80_POWER = 0.8416212335729143  # Phi^{-1}(0.80)
_Z_90_POWER = 1.2815515655446004  # Phi^{-1}(0.90)
_Z_95_POWER = 1.6448536269514722  # Phi^{-1}(0.95)

# The anytime (sequential) boundary is tuned to stop early for effects this
# multiple of the `detectable` passed to analyze(): each lesson's horizon is
# sized for SEQUENTIAL_TARGET_EFFECT_MULTIPLE * detectable, i.e. about a
# quarter of the sample a `detectable` effect needs (sample size scales as
# 1/effect^2). A larger multiple stops large effects sooner and judges small
# ones later. 2.0 is the historical behavior and stays the default; pass
# analyze(..., target_effect_multiple=...) to choose differently.
SEQUENTIAL_TARGET_EFFECT_MULTIPLE = 2.0


def _uniform_from(*parts: str) -> float:
    digest = hashlib.blake2b("\x1f".join(parts).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


def is_held_out(
    lesson_slug: str,
    occasion_id: str,
    rate: float = DEFAULT_HOLDOUT_RATE,
    salt: str = "default",
) -> bool:
    """Should `lesson_slug` be withheld from `occasion_id`?"""
    if not math.isfinite(rate):
        raise ValueError(f"holdout rate must be a finite number, got {rate!r}")
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    return _uniform_from(salt, lesson_slug, occasion_id) < rate


def _norm_cdf(z: float) -> float:
    # erfc avoids catastrophic cancellation in the tails where `1 - erf(...)`
    # otherwise rounds tiny p-values to exactly zero.
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def _check_success_count(s: int, n: int, label: str) -> None:
    if s < 0 or n < 0:
        raise ValueError(f"{label} success/total counts must not be negative, got s={s}, n={n}")
    if s > n:
        raise ValueError(f"{label} success count ({s}) cannot exceed its total count ({n})")


def two_proportion_test(s1: int, n1: int, s2: int, n2: int) -> tuple[float, float]:
    """Two-tailed pooled z-test for a difference in independent proportions."""
    _check_success_count(s1, n1, "arm 1")
    _check_success_count(s2, n2, "arm 2")
    if n1 == 0 or n2 == 0:
        return 0.0, 1.0
    p1, p2 = s1 / n1, s2 / n2
    p_pool = (s1 + s2) / (n1 + n2)
    if p_pool in (0.0, 1.0):
        return 0.0, 1.0
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0, 1.0
    z = (p1 - p2) / se
    return z, 2 * (1 - _norm_cdf(abs(z)))


def diff_confidence_interval(s1: int, n1: int, s2: int, n2: int, z: float = _Z_95) -> tuple[float, float]:
    """Pooled-SE interval for (p1 - p2); the interval twin of two_proportion_test.

    Uses the same pooled variance estimator as the z-test, so the two can
    never disagree: with the z matching the test's alpha, p <= alpha
    (two-sided) holds exactly when this interval excludes zero. The previous
    unpooled (Wald) version could contradict the test at small n.
    """
    if n1 <= 0 or n2 <= 0:
        return (0.0, 0.0)
    _check_success_count(s1, n1, "arm 1")
    _check_success_count(s2, n2, "arm 2")
    p1, p2 = s1 / n1, s2 / n2
    p_pool = (s1 + s2) / (n1 + n2)
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    delta = p1 - p2
    return (delta - z * se, delta + z * se)


def benjamini_hochberg(p_values: list[float], alpha: float = 0.05) -> list[bool]:
    """Which hypotheses survive at FDR <= alpha."""
    if not math.isfinite(alpha) or not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    if any(not math.isfinite(p) or not 0.0 <= p <= 1.0 for p in p_values):
        raise ValueError("p-values must be finite and lie in [0, 1]")
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


def _norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF: Acklam's rational approximation.

    Closed form (a few dozen flops), replacing the 200-iteration bisection
    this used to be. Every power/sample-size call — alpha_spent,
    minimum_detectable_effect, required_n_per_arm, and the per-lesson
    sequential horizons — is now O(1). Worst-case error ~4e-9 against a
    high-precision bisection reference (typically ~1e-9 in the central
    region), far below any statistical tolerance here. Stdlib math only.
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"probability must be strictly between 0 and 1, got {p!r}")
    a1 = -3.969683028665376e01
    a2 = 2.209460984245205e02
    a3 = -2.759285104469687e02
    a4 = 1.383577518672690e02
    a5 = -3.066479806614716e01
    a6 = 2.506628277459239e00
    b1 = -5.447609879822406e01
    b2 = 1.615858368580409e02
    b3 = -1.556989798598866e02
    b4 = 6.680131188771972e01
    b5 = -1.328068155288572e01
    c1 = -7.784894002430293e-03
    c2 = -3.223964580411365e-01
    c3 = -2.400758277161838e00
    c4 = -2.549732539343734e00
    c5 = 4.374664141464968e00
    c6 = 2.938163982698783e00
    d1 = 7.784695709041462e-03
    d2 = 3.224671290700398e-01
    d3 = 2.445134137142996e00
    d4 = 3.754408661907416e00
    p_low, p_high = 0.02425, 1.0 - 0.02425
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        num = ((((c1 * q + c2) * q + c3) * q + c4) * q + c5) * q + c6
        den = (((d1 * q + d2) * q + d3) * q + d4) * q + 1.0
        return num / den
    if p > p_high:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        num = ((((c1 * q + c2) * q + c3) * q + c4) * q + c5) * q + c6
        den = (((d1 * q + d2) * q + d3) * q + d4) * q + 1.0
        return -num / den
    q = p - 0.5
    r = q * q
    num = ((((a1 * r + a2) * r + a3) * r + a4) * r + a5) * r + a6
    den = ((((b1 * r + b2) * r + b3) * r + b4) * r + b5) * r + 1.0
    return num * q / den


def _z_for_power(power: float) -> float:
    """Standard-normal quantile at `power`; thin wrapper over _norm_ppf.

    Kept so existing callers (alpha_spent, minimum_detectable_effect,
    required_n_per_arm, and the per-lesson sequential horizons) do not change.
    """
    return _norm_ppf(power)


SPEND_OBRIEN_FLEMING = "obrien-fleming"
SPEND_POCOCK = "pocock"
SPENDING_FUNCTIONS = (SPEND_OBRIEN_FLEMING, SPEND_POCOCK)


def alpha_spent(
    information_fraction: float,
    alpha: float = 0.05,
    shape: str = SPEND_OBRIEN_FLEMING,
) -> float:
    if shape not in SPENDING_FUNCTIONS:
        raise ValueError(
            f"unknown alpha-spending shape {shape!r}; expected one of "
            f"{', '.join(SPENDING_FUNCTIONS)}"
        )
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    t = min(max(information_fraction, 0.0), 1.0)
    if t <= 0.0:
        return 0.0
    if shape == SPEND_POCOCK:
        return alpha * math.log(1.0 + (math.e - 1.0) * t)
    z_half = _z_for_power(1.0 - alpha / 2.0)
    return 2.0 * (1.0 - _norm_cdf(z_half / math.sqrt(t)))


def _log_beta_binomial_bayes_factor(
    p: float, successes: int, trials: int, prior: float,
) -> float:
    """Log Bayes factor of a symmetric beta-mixture against Bernoulli(p)."""
    failures = trials - successes
    if p <= 0.0 and successes:
        return math.inf
    if p >= 1.0 and failures:
        return math.inf
    log_likelihood = 0.0
    if successes:
        log_likelihood += successes * math.log(p)
    if failures:
        log_likelihood += failures * math.log1p(-p)
    log_mixture = (
        math.lgamma(successes + prior)
        + math.lgamma(failures + prior)
        - math.lgamma(trials + 2.0 * prior)
        - 2.0 * math.lgamma(prior)
        + math.lgamma(2.0 * prior)
    )
    return log_mixture - log_likelihood


def _mixture_proportion_interval(
    successes: int, trials: int, alpha: float, prior_strength: float,
) -> tuple[float, float]:
    """A beta-mixture confidence sequence for one Bernoulli proportion.

    Inverting Ville's inequality for the beta-binomial likelihood-ratio
    martingale gives time-uniform coverage. The two arm intervals are combined
    with a Bonferroni split, so optional stopping and adaptive arm counts remain
    valid for their difference.
    """
    if trials <= 0:
        return 0.0, 1.0
    prior = max(0.5, min(128.0, prior_strength / 2.0))
    threshold = math.log(2.0 / alpha)

    def outside(p: float) -> bool:
        return _log_beta_binomial_bayes_factor(p, successes, trials, prior) > threshold

    estimate = successes / trials
    if not outside(0.0):
        lower = 0.0
    else:
        lo, hi = 0.0, estimate
        for _ in range(64):
            mid = (lo + hi) / 2.0
            if outside(mid):
                lo = mid
            else:
                hi = mid
        lower = hi

    if not outside(1.0):
        upper = 1.0
    else:
        lo, hi = estimate, 1.0
        for _ in range(64):
            mid = (lo + hi) / 2.0
            if outside(mid):
                hi = mid
            else:
                lo = mid
        upper = lo
    return lower, upper


def anytime_confidence_interval(
    s1: int, n1: int, s2: int, n2: int,
    alpha: float = 0.05,
    target_n_per_arm: int = 0,
) -> tuple[float, float]:
    """Anytime-valid confidence interval for the difference in proportions.

    Each arm uses a beta-binomial mixture martingale and receives alpha/2;
    Minkowski subtraction of the two confidence sequences gives a valid
    interval for ``p_injected - p_withheld`` at every stopping time. The
    target horizon controls the symmetric mixture's prior concentration, which
    changes efficiency without changing the coverage guarantee.
    """
    _check_success_count(s1, n1, "injected arm")
    _check_success_count(s2, n2, "withheld arm")
    if n1 <= 0 or n2 <= 0:
        return (-1.0, 1.0)
    if not math.isfinite(alpha) or not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    if target_n_per_arm < 0:
        raise ValueError(f"target_n_per_arm must be non-negative, got {target_n_per_arm}")
    target = target_n_per_arm or min(n1, n2)
    prior_strength = math.sqrt(max(1, target))
    low1, high1 = _mixture_proportion_interval(s1, n1, alpha, prior_strength)
    low2, high2 = _mixture_proportion_interval(s2, n2, alpha, prior_strength)
    return (max(-1.0, low1 - high2), min(1.0, high1 - low2))


def minimum_detectable_effect(n_per_arm: int, baseline: float, power: float = 0.80) -> float | None:
    """Smallest true effect this sample size could reliably detect."""
    if n_per_arm <= 0 or not (0 < baseline < 1):
        return None
    if not 0.5 <= power < 1.0:
        raise ValueError(f"power must be in [0.5, 1.0), got {power}")
    return (_Z_95 + _z_for_power(power)) * math.sqrt(2 * baseline * (1 - baseline) / n_per_arm)


def required_n_per_arm(effect: float, baseline: float, power: float = 0.80) -> int:
    if (not math.isfinite(effect) or not math.isfinite(baseline)
            or not (0 < baseline < 1) or effect <= 0):
        raise ValueError("effect must be finite and > 0; baseline must be finite and strictly between 0 and 1")
    if not 0.5 <= power < 1.0:
        raise ValueError(f"power must be in [0.5, 1.0), got {power}")
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
    """Design the experiment before running it."""
    n_per_arm = required_n_per_arm(effect, baseline, power)
    if not 0.0 < rate < 1.0:
        raise ValueError(f"holdout rate must be in (0.0, 1.0), got {rate}")
    smaller_share = min(rate, 1.0 - rate)
    occasions_needed = math.ceil(n_per_arm / smaller_share)

    rate_for_budget = None
    verdict = "ok"
    if occasions_budget:
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


@dataclass
class HoldoutObservation:
    """One occasion on which a lesson was *eligible* to be injected."""

    lesson_slug: str
    occasion_id: str
    injected: bool
    succeeded: bool


@dataclass
class CausalEffect:
    lesson_slug: str
    n_injected: int
    n_withheld: int
    rate_injected: float
    rate_withheld: float
    effect: float
    ci_low: float
    ci_high: float
    p_value: float
    significant: bool
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
    fixed_horizon: bool = False,
    target_effect_multiple: float = SEQUENTIAL_TARGET_EFFECT_MULTIPLE,
) -> list[CausalEffect]:
    """Estimate each lesson's causal effect from its holdout arms.

    Two readings of the same data — pick explicitly, because judging a
    running experiment by a fixed-sample threshold manufactures winners out
    of noise (see commons/eval/sequential_error_rates.py for the measured
    cost of repeated looks).

    Sequential (``sequential=True``) — ONE rule, valid at every look. Each
    testable lesson is judged against its own anytime-valid confidence
    interval at level ``alpha`` (see anytime_confidence_interval), whose
    horizon is sized for ``target_effect_multiple * detectable``:
    HELPS iff the whole interval is above zero, HURTS iff the whole
    interval is below zero. Otherwise the lesson is not established, and a
    power gate decides what that means: UNDERPOWERED when this sample could
    not have detected ``detectable`` (keep accruing — this is "cannot answer
    yet", not "no effect"), NO_MEASURABLE_EFFECT when it could have and saw
    nothing (evidence of absence). No across-lesson FDR correction is applied
    in this mode: each interval is per-lesson anytime-valid, so with many
    lessons under test expect on the order of ``alpha`` false flags per
    lesson under the global null, however often the run is looked at. The
    reported p-value is the fixed-sample two-sided p, for reference only —
    it does not drive the verdict.

    Fixed-horizon (``fixed_horizon=True``; the legacy spelling
    ``sequential=False`` means the same) — ONE look at a finished run.
    Significance comes from fixed-sample pooled z-tests with a
    Benjamini-Hochberg FDR correction at ``alpha`` across the testable
    lessons, and the reported interval is the matching pooled interval, so
    the p-value and the interval can never disagree. Only honest if nobody
    acted on an earlier look — no lesson retired, no gate checked, no report
    read and the run stopped because of it.

    ``spending_shape`` is retained for backwards compatibility and validated,
    but otherwise ignored: the sequential rule uses a mixture anytime
    boundary, not an alpha-spending function.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    if not 0.0 < detectable < 1.0:
        raise ValueError(f"detectable must be strictly between 0 and 1, got {detectable}")
    if spending_shape not in SPENDING_FUNCTIONS:
        raise ValueError(
            f"unknown alpha-spending shape {spending_shape!r}; expected one of "
            f"{', '.join(SPENDING_FUNCTIONS)}"
        )
    if not target_effect_multiple > 0.0:
        raise ValueError(f"target_effect_multiple must be > 0, got {target_effect_multiple}")
    if fixed_horizon and sequential:
        raise ValueError(
            "analyze() got fixed_horizon=True together with sequential=True: "
            "the fixed-horizon reading and the sequential reading contradict each other"
        )
    use_fixed = fixed_horizon or not sequential

    by_lesson: dict[str, list[HoldoutObservation]] = {}
    for obs in observations:
        by_lesson.setdefault(obs.lesson_slug, []).append(obs)

    staged: list[tuple[str, int, int, int, int]] = []
    for slug, rows in sorted(by_lesson.items()):
        inj = [r for r in rows if r.injected]
        wit = [r for r in rows if not r.injected]
        staged.append((slug, sum(r.succeeded for r in inj), len(inj),
                       sum(r.succeeded for r in wit), len(wit)))

    testable = [s for s in staged if s[2] >= min_arm and s[4] >= min_arm]

    # The interval that drives the verdict, per lesson: the anytime interval
    # in sequential mode, the alpha-matched pooled interval in fixed mode.
    interval_by_slug: dict[str, tuple[float, float]] = {}
    sig_by_slug: dict[str, bool] = {}
    if use_fixed:
        z_fixed = _norm_ppf(1.0 - alpha / 2.0)
        p_values = [two_proportion_test(s[1], s[2], s[3], s[4])[1] for s in testable]
        for s, sig in zip(testable, benjamini_hochberg(p_values, alpha=alpha)):
            sig_by_slug[s[0]] = sig
            interval_by_slug[s[0]] = diff_confidence_interval(s[1], s[2], s[3], s[4], z=z_fixed)
    else:
        for slug, s_inj, n_inj, s_wit, n_wit in testable:
            baseline = ((s_inj + s_wit) / (n_inj + n_wit)) if (n_inj + n_wit) else 0.0
            target = (
                required_n_per_arm(target_effect_multiple * detectable, baseline)
                if 0.0 < baseline < 1.0 else 0
            )
            lo, hi = anytime_confidence_interval(
                s_inj, n_inj, s_wit, n_wit, alpha=alpha, target_n_per_arm=target
            )
            interval_by_slug[slug] = (lo, hi)
            sig_by_slug[slug] = lo > 0.0 or hi < 0.0

    out: list[CausalEffect] = []
    for slug, s_inj, n_inj, s_wit, n_wit in staged:
        rate_inj = (s_inj / n_inj) if n_inj else 0.0
        rate_wit = (s_wit / n_wit) if n_wit else 0.0
        effect = rate_inj - rate_wit
        _, p = two_proportion_test(s_inj, n_inj, s_wit, n_wit)
        baseline = ((s_inj + s_wit) / (n_inj + n_wit)) if (n_inj + n_wit) else 0.0
        mde = minimum_detectable_effect(min(n_inj, n_wit), baseline)

        if n_inj < min_arm or n_wit < min_arm:
            verdict = VERDICT_UNDERPOWERED
            note = (
                f"{n_inj} injected / {n_wit} withheld; {min_arm} needed in each arm. "
                "This is 'cannot answer yet', not 'no effect'."
            )
            significant = False
            lo, hi = diff_confidence_interval(s_inj, n_inj, s_wit, n_wit)
        else:
            significant = sig_by_slug.get(slug, False)
            lo, hi = interval_by_slug[slug]
            if significant and effect > 0:
                verdict, note = VERDICT_HELPS, "Injecting this lesson causes better outcomes."
            elif significant and effect < 0:
                verdict, note = (
                    VERDICT_HURTS,
                    "Outcomes are WORSE when this is injected. Correlational scoring "
                    "cannot see this -- only the holdout can.",
                )
            elif mde is None or mde > detectable:
                verdict = VERDICT_UNDERPOWERED
                seen = f"~{mde:.0%}" if mde is not None else "no effect at all"
                if use_fixed:
                    note = (
                        f"{n_inj} injected / {n_wit} withheld clears the {min_arm}-per-arm "
                        f"floor, but this design could only have detected {seen} or larger, "
                        f"against a target of {detectable:.0%}. Finding nothing here is "
                        "'cannot answer yet', not 'no effect' -- widen the holdout rate or "
                        "keep accruing."
                    )
                else:
                    note = (
                        f"p={p:.4g} against a fixed {alpha:.0%} threshold notwithstanding, "
                        "this is one look at a running experiment, and an interval valid "
                        "at every sample size at once still spans zero "
                        f"({lo:+.1%} to {hi:+.1%}). Judging accumulating data by a fixed "
                        "threshold crosses it by luck sooner or later -- measured on "
                        "this estimator, in a third of null runs. This design could "
                        f"only have detected {seen} or larger, against a target of "
                        f"{detectable:.0%}, so finding nothing here is 'cannot answer "
                        "yet', not 'no effect' -- keep accruing; this is reported the "
                        "moment it clears a boundary that survives having been watched."
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
    return f"{p:.3f}" if p >= 0.001 else "<0.001"


def render(summary: ExperimentSummary, alpha: float = 0.05, fixed_horizon: bool = True) -> str:
    """Render the causal report. `fixed_horizon` selects the significance line
    matching the reading that produced the effects: the FDR/Benjamini-Hochberg
    line for a fixed-horizon analysis (the default, preserving past output),
    the anytime-valid line for a sequential one."""
    eff = summary.effects
    counts: dict[str, int] = {}
    for e in eff:
        counts[e.verdict] = counts.get(e.verdict, 0) + 1

    if fixed_horizon:
        significance_line = (
            f"- Significance at FDR ≤ {alpha:.2f} (Benjamini-Hochberg across all tested lessons)"
        )
    else:
        significance_line = (
            f"- Significance per lesson against an anytime-valid interval at α = {alpha:.2f} "
            "(valid however often the run was looked at; no across-lesson FDR correction)"
        )

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
        significance_line,
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
