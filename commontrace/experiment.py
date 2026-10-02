"""Randomized holdout: does injecting a lesson actually *cause* better outcomes?"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

DEFAULT_HOLDOUT_RATE = 0.10

DEFAULT_MIN_ARM = 10

DEFAULT_PRACTICAL_EFFECT = 0.10

_Z_95 = 1.959963984540054
_Z_80_POWER = 0.8416212335729143


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
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _check_success_count(s: int, n: int, label: str) -> None:
    if s < 0 or n < 0:
        raise ValueError(f"{label} success/total counts must not be negative, got s={s}, n={n}")
    if s > n:
        raise ValueError(f"{label} success count ({s}) cannot exceed its total count ({n})")


def two_proportion_test(s1: int, n1: int, s2: int, n2: int) -> tuple[float, float]:
    """Two-tailed z-test for a difference in proportions."""
    if n1 <= 0 or n2 <= 0:
        return 0.0, 1.0
    _check_success_count(s1, n1, "arm 1")
    _check_success_count(s2, n2, "arm 2")
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
    """Which hypotheses survive at FDR <= alpha."""
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
    low, high = 0.0, 10.0
    for _ in range(200):
        mid = (low + high) / 2
        if _norm_cdf(mid) < power:
            low = mid
        else:
            high = mid
    return (low + high) / 2


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


def anytime_confidence_interval(
    s1: int, n1: int, s2: int, n2: int,
    alpha: float = 0.05,
    target_n_per_arm: int = 0,
) -> tuple[float, float]:
    _check_success_count(s1, n1, "injected arm")
    _check_success_count(s2, n2, "withheld arm")
    if n1 <= 0 or n2 <= 0:
        return (-1.0, 1.0)
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")

    rate1, rate2 = s1 / n1, s2 / n2
    effect = rate1 - rate2
    variance = rate1 * (1 - rate1) / n1 + rate2 * (1 - rate2) / n2
    if variance <= 0.0:
        variance = 0.25 / n1 + 0.25 / n2

    n_effective = min(n1, n2)
    target = target_n_per_arm if target_n_per_arm > 0 else n_effective
    rho = 1.0 / max(target, 1)
    inner = n_effective * rho + 1.0
    radius = math.sqrt(
        (2.0 * inner / (n_effective * n_effective * rho))
        * math.log(math.sqrt(inner) / alpha)
    )
    half_width = radius * math.sqrt(variance * n_effective)
    return (effect - half_width, effect + half_width)


def minimum_detectable_effect(n_per_arm: int, baseline: float, power: float = 0.80) -> float | None:
    """Smallest true effect this sample size could reliably detect."""
    if n_per_arm <= 0 or not (0 < baseline < 1):
        return None
    if not 0.5 <= power < 1.0:
        raise ValueError(f"power must be in [0.5, 1.0), got {power}")
    return (_Z_95 + _z_for_power(power)) * math.sqrt(2 * baseline * (1 - baseline) / n_per_arm)


def required_n_per_arm(effect: float, baseline: float, power: float = 0.80) -> int:
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
) -> list[CausalEffect]:
    """Estimate each lesson's causal effect from its holdout arms."""
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
    p_values = [two_proportion_test(s[1], s[2], s[3], s[4])[1] for s in testable]
    significance = benjamini_hochberg(p_values, alpha=alpha)
    sig_by_slug = {s[0]: sig for s, sig in zip(testable, significance)}

    sequence_by_slug: dict[str, tuple[float, float]] = {}
    if sequential:
        for slug, s_inj, n_inj, s_wit, n_wit in testable:
            baseline = ((s_inj + s_wit) / (n_inj + n_wit)) if (n_inj + n_wit) else 0.0
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
            if significant and effect > 0:
                verdict, note = VERDICT_HELPS, "Injecting this lesson causes better outcomes."
            elif significant and effect < 0:
                verdict, note = (
                    VERDICT_HURTS,
                    "Outcomes are WORSE when this is injected. Correlational scoring "
                    "cannot see this -- only the holdout can.",
                )
            elif sequential and p <= alpha and slug in sequence_by_slug:
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
