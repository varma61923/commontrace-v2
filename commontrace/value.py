"""What the memory was worth, in the customer's own units, causally.

WHY THIS EXISTS
---------------
STRATEGY.md 11.5 states the pricing hypothesis this product rests on:

    price against measured resolution-rate improvement per fleet, because
    that is the only quantity this product can prove causally and it scales
    with the customer's own benefit rather than with seats or trace volume.

and asserts that "the mechanism ships" while the number stays a business
decision. Half of that was true. The effect size shipped; nothing turned it
into a QUANTITY OF VALUE that a price could attach to, and the Hub -- the
surface customers actually pay on -- computed no value at all.

Meanwhile the one estimator that did exist, `commontrace impact`, is
correlational by its own admission, in five separate places. So the product
had a causal instrument and a commercial number, and they were not connected
to each other. The commercial number was the confounded one.

THE QUANTITY
------------
Per memory under test:

    occasions_improved = effect x n_injected

`effect` is the causal difference between arms; `n_injected` is how many
occasions actually received it. Their product is "how many more occasions
went well BECAUSE this memory existed" -- a count, in the fleet's own units,
carrying the effect's confidence interval straight through (a linear
transform of the estimate, so the interval transforms with it).

That is deliberately a COUNT and not money. Attaching currency here would
encode a number nobody has agreed to, in the one place people treat as
authoritative (11.5). The caller supplies what one resolved occasion is
worth to them; this supplies how many there were. Ship the mechanism, not
the price.

WHAT MAKES IT DEFENSIBLE RATHER THAN MARKETING
----------------------------------------------
Three rules, and the third is the one that matters:

1. **A compromised experiment produces no number.** Not a hedged number --
   none. If `integrity` says a named mechanism is biasing the effects, then
   every value computed from them is biased too, and a value report is
   exactly the artifact where a caveat gets separated from the figure.

2. **An underpowered memory contributes nothing.** Its effect was not
   established, so multiplying it by a volume produces a large number with
   no evidence under it -- which is how a null becomes a sales figure.

3. **Memories that HURT are subtracted, not dropped.** A value report that
   sums only the winners is not a measurement, it is a brochure. This
   product's whole claim is that it will tell a customer when its own memory
   is making things worse; a value number that quietly excludes those is the
   single fastest way to retract that claim.
"""

from __future__ import annotations

from dataclasses import dataclass

from commontrace import experiment, integrity


@dataclass(frozen=True)
class MemoryValue:
    """One memory's causal contribution, in occasions."""

    slug: str
    verdict: str
    n_injected: int
    effect: float
    occasions_improved: float
    ci_low: float
    ci_high: float
    counted: bool
    why_not: str = ""


@dataclass(frozen=True)
class ValueReport:
    readable: bool
    reason: str
    memories: list[MemoryValue]
    occasions_improved: float
    ci_low: float
    ci_high: float
    n_counted: int
    n_excluded: int
    value_per_occasion: float | None = None

    @property
    def money(self) -> float | None:
        if self.value_per_occasion is None or not self.readable:
            return None
        return self.occasions_improved * self.value_per_occasion

    @property
    def money_range(self) -> tuple[float, float] | None:
        if self.value_per_occasion is None or not self.readable:
            return None
        return (self.ci_low * self.value_per_occasion,
                self.ci_high * self.value_per_occasion)


def compute(
    effects: list[experiment.CausalEffect],
    report: integrity.IntegrityReport | None = None,
    value_per_occasion: float | None = None,
) -> ValueReport:
    """Causal value delivered, or a refusal to state one.

    `report` is the validity audit over the same run. Passing None means "not
    audited", which is treated as not-readable rather than as clean: a value
    figure computed from an unexamined experiment is the exact artifact this
    module exists to not produce.
    """
    if report is None:
        return ValueReport(
            readable=False,
            reason="No validity audit was supplied for this experiment, so the effects "
                   "it produced have not been checked. A value figure computed from an "
                   "unexamined comparison is not a measurement.",
            memories=[], occasions_improved=0.0, ci_low=0.0, ci_high=0.0,
            n_counted=0, n_excluded=len(effects),
            value_per_occasion=value_per_occasion,
        )
    if not report.readable:
        blocking = "; ".join(f.headline for f in report.blocking)
        return ValueReport(
            readable=False,
            reason=(
                "The experiment these effects came from is COMPROMISED, so every value "
                f"computed from them is biased by the same mechanism: {blocking} "
                "No figure is given, because a value report is precisely where a caveat "
                "gets separated from the number it qualifies."
            ),
            memories=[], occasions_improved=0.0, ci_low=0.0, ci_high=0.0,
            n_counted=0, n_excluded=len(effects),
            value_per_occasion=value_per_occasion,
        )

    memories: list[MemoryValue] = []
    total = low = high = 0.0
    counted = 0
    for effect in effects:
        improved = effect.effect * effect.n_injected
        item_low = effect.ci_low * effect.n_injected
        item_high = effect.ci_high * effect.n_injected

        # HELPS and HURTS both count. Dropping the second would make this a
        # brochure -- see the module docstring.
        include = effect.verdict in (experiment.VERDICT_HELPS, experiment.VERDICT_HURTS)
        why_not = ""
        if effect.verdict == experiment.VERDICT_UNDERPOWERED:
            why_not = ("not established: this design could not detect an effect worth "
                       "acting on, so multiplying it by a volume would produce a large "
                       "number with no evidence under it")
        elif effect.verdict == experiment.VERDICT_NO_EFFECT:
            why_not = ("measured, and the effect is indistinguishable from zero -- so "
                       "its contribution is zero, which is a result rather than an "
                       "omission")

        memories.append(MemoryValue(
            slug=effect.lesson_slug, verdict=effect.verdict,
            n_injected=effect.n_injected, effect=effect.effect,
            occasions_improved=round(improved, 2),
            ci_low=round(item_low, 2), ci_high=round(item_high, 2),
            counted=include, why_not=why_not,
        ))
        if include:
            counted += 1
            total += improved
            low += item_low
            high += item_high

    return ValueReport(
        readable=True,
        reason="",
        memories=memories,
        occasions_improved=round(total, 2),
        ci_low=round(low, 2), ci_high=round(high, 2),
        n_counted=counted, n_excluded=len(effects) - counted,
        value_per_occasion=value_per_occasion,
    )


def render(report: ValueReport, unit: str = "occasion") -> str:
    """The value section, written so the refusal is as legible as a figure."""
    if not report.readable:
        return "## Value delivered\n\n**Not stated.** " + report.reason

    lines = [
        "## Value delivered",
        "",
        f"**{report.occasions_improved:+,.0f} {unit}s** went differently because of this "
        f"memory, over the measured window "
        f"(95% CI {report.ci_low:+,.0f} to {report.ci_high:+,.0f}).",
        "",
        f"Computed from {report.n_counted} memory/memories whose causal effect is "
        f"established. {report.n_excluded} contributed nothing, listed below with why.",
        "",
    ]
    if report.money is not None:
        low, high = report.money_range
        lines += [
            f"At the {report.value_per_occasion:,.2f} per resolved {unit} you supplied, "
            f"that is **{report.money:+,.0f}** ({low:+,.0f} to {high:+,.0f}).",
            "",
            f"_The {unit} count is measured here; the rate is yours. This repository "
            "attaches no currency to anything -- it ships the quantity and takes the "
            "price from you._",
            "",
        ]
    lines += ["| Memory | Verdict | Injected | Effect | Occasions | Counted |",
              "|---|---|---:|---:|---:|---|"]
    for m in report.memories:
        mark = "yes" if m.counted else "no"
        lines.append(
            f"| `{m.slug}` | {m.verdict} | {m.n_injected:,} | {m.effect:+.1%} | "
            f"{m.occasions_improved:+,.0f} | {mark} |"
        )
    excluded = [m for m in report.memories if not m.counted and m.why_not]
    if excluded:
        lines += ["", "Why the others contributed nothing:", ""]
        lines += [f"- `{m.slug}` — {m.why_not}." for m in excluded]
    hurts = [m for m in report.memories if m.verdict == experiment.VERDICT_HURTS]
    if hurts:
        lines += [
            "",
            "> Memories measured as HURTING are **subtracted above, not dropped**. A "
            "value figure that sums only the winners is a brochure; this product's "
            "claim is that it will tell you when its own memory is making things "
            "worse, and a number that quietly excludes those retracts the claim.",
        ]
    return "\n".join(lines)
