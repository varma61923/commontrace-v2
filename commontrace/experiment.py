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
) -> list[CausalEffect]:
    """Estimate each lesson's causal effect from its holdout arms."""
    by_lesson: dict[str, list[HoldoutObservation]] = {}
    for obs in observations:
        by_lesson.setdefault(obs.lesson_slug, []).append(obs)

    staged: list[tuple[str, int, int, int, int]] = []  # slug, s_inj, n_inj, s_wit, n_wit
    for slug, rows in sorted(by_lesson.items()):
        inj = [r for r in rows if r.injected]
        wit = [r for r in rows if not r.injected]
        staged.append((slug, sum(r.succeeded for r in inj), len(inj),
                       sum(r.succeeded for r in wit), len(wit)))

    # p-values only from adequately-powered comparisons; underpowered ones
    # must not enter the FDR correction, where they would inflate m and
    # weaken every genuine result.
    testable = [s for s in staged if s[2] >= min_arm and s[4] >= min_arm]
    p_values = [two_proportion_test(s[1], s[2], s[3], s[4])[1] for s in testable]
    significance = benjamini_hochberg(p_values, alpha=alpha)
    sig_by_slug = {s[0]: sig for s, sig in zip(testable, significance)}

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
            if significant and effect > 0:
                verdict, note = VERDICT_HELPS, "Injecting this lesson causes better outcomes."
            elif significant and effect < 0:
                verdict, note = (
                    VERDICT_HURTS,
                    "Outcomes are WORSE when this is injected. Correlational scoring "
                    "cannot see this -- only the holdout can.",
                )
            else:
                verdict = VERDICT_NO_EFFECT
                note = "No effect distinguishable from noise"
                if mde is not None:
                    note += f"; this sample could only have detected an effect of ~{mde:.0%} or larger"
                note += "."

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
