"""What the memory was worth, in the customer's own units, causally."""

from __future__ import annotations

import hashlib
import hmac
import math
from dataclasses import dataclass, field

from commontrace import decay as decay_mod
from commontrace import experiment, integrity

_LEDGER_GENESIS = hashlib.sha256(b"commontrace-value-ledger-v1").hexdigest()

_FIELD_SEP = "\x1f"

_Z_95 = 1.959963984540054


@dataclass(frozen=True)
class Tier:
    """One line of a contractual rate card."""

    name: str
    share: float
    cost_per_occasion: float


@dataclass(frozen=True)
class RateCard:
    """What the customer has agreed an improved occasion is worth."""

    tiers: tuple[Tier, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.tiers:
            raise ValueError("a rate card needs at least one tier")
        for tier in self.tiers:
            if tier.share < 0:
                raise ValueError(f"tier {tier.name!r} has a negative share")
            if tier.cost_per_occasion < 0:
                raise ValueError(
                    f"tier {tier.name!r} has a negative cost per occasion; a tier "
                    "that costs nothing to get wrong should be priced at zero, and "
                    "one that pays you to fail is not a tier"
                )
        total = sum(t.share for t in self.tiers)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"tier shares sum to {total:.6f}, not 1.0 -- every occasion has to "
                "land in exactly one tier or the blended rate prices a volume that "
                "was never measured"
            )

    @property
    def blended_rate(self) -> float:
        """The share-weighted cost of one improved occasion."""
        return sum(t.share * t.cost_per_occasion for t in self.tiers)


@dataclass(frozen=True)
class LedgerEntry:
    """One tamper-evident line of the value ledger."""

    index: int
    slug: str
    verdict: str
    occasions_improved: float
    rate: float
    money: float
    previous_hash: str
    entry_hash: str


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
class OccasionOverlap:
    """Which memories were injected on the same occasions as which others."""

    shared_pairs: frozenset[frozenset[str]] = field(default_factory=frozenset)
    unique_injected_occasions: int = 0

    def conflicts_among(self, slugs) -> list[tuple[str, str]]:
        wanted = set(slugs)
        found = [
            tuple(sorted(pair)) for pair in self.shared_pairs
            if len(pair & wanted) == 2
        ]
        return sorted(found)


def overlap_from_assignments(assignments) -> OccasionOverlap:
    by_occasion: dict[str, set[str]] = {}
    for row in assignments:
        if not getattr(row, "injected", False):
            continue
        occasion = str(getattr(row, "occasion_id", "") or "")
        lesson = str(getattr(row, "lesson", "") or "")
        if not occasion or not lesson:
            continue
        by_occasion.setdefault(occasion, set()).add(lesson)

    pairs: set[frozenset[str]] = set()
    for lessons in by_occasion.values():
        if len(lessons) < 2:
            continue
        ordered = sorted(lessons)
        for i, first in enumerate(ordered):
            for second in ordered[i + 1:]:
                pairs.add(frozenset((first, second)))
    return OccasionOverlap(
        shared_pairs=frozenset(pairs),
        unique_injected_occasions=len(by_occasion),
    )


@dataclass(frozen=True)
class PolicyEffect:
    n_treated: int
    n_control: int
    rate_treated: float
    rate_control: float
    effect: float
    ci_low: float
    ci_high: float
    p_value: float
    significant: bool
    readable: bool
    reason: str = ""

    @property
    def occasions_improved(self) -> float:
        return self.effect * self.n_treated


def policy_effect(
    assignments, min_arm: int = experiment.DEFAULT_MIN_ARM, alpha: float = 0.05
) -> PolicyEffect:
    """Estimate the whole memory policy's effect, on unique occasions."""
    treated_success: dict[str, bool] = {}
    treated_any: dict[str, bool] = {}
    for row in assignments:
        occasion = str(getattr(row, "occasion_id", "") or "")
        if not occasion:
            continue
        succeeded = getattr(row, "succeeded", None)
        if succeeded is None:
            treated_any.setdefault(occasion, False)
            treated_any[occasion] = treated_any[occasion] or bool(
                getattr(row, "injected", False)
            )
            continue
        treated_any[occasion] = treated_any.get(occasion, False) or bool(
            getattr(row, "injected", False)
        )
        if occasion in treated_success:
            treated_success[occasion] = treated_success[occasion] and bool(succeeded)
        else:
            treated_success[occasion] = bool(succeeded)

    n_treated = n_control = s_treated = s_control = 0
    for occasion, succeeded in treated_success.items():
        if treated_any.get(occasion, False):
            n_treated += 1
            s_treated += 1 if succeeded else 0
        else:
            n_control += 1
            s_control += 1 if succeeded else 0

    rate_treated = (s_treated / n_treated) if n_treated else 0.0
    rate_control = (s_control / n_control) if n_control else 0.0

    if n_treated < min_arm or n_control < min_arm:
        return PolicyEffect(
            n_treated=n_treated, n_control=n_control,
            rate_treated=rate_treated, rate_control=rate_control,
            effect=0.0, ci_low=0.0, ci_high=0.0, p_value=1.0, significant=False,
            readable=False,
            reason=(
                f"Not enough resolved occasions to compare policies: {n_treated} with "
                f"a memory injected, {n_control} with none, against a floor of "
                f"{min_arm} per arm. The all-withheld arm is rare by construction at a "
                "low holdout rate -- every memory eligible for an occasion has to be "
                "withheld at once -- so this fills up more slowly than the per-memory "
                "comparisons beside it."
            ),
        )

    effect = rate_treated - rate_control
    _, p_value = experiment.two_proportion_test(
        s_treated, n_treated, s_control, n_control
    )
    ci_low, ci_high = experiment.diff_confidence_interval(
        s_treated, n_treated, s_control, n_control
    )
    return PolicyEffect(
        n_treated=n_treated, n_control=n_control,
        rate_treated=rate_treated, rate_control=rate_control,
        effect=effect, ci_low=ci_low, ci_high=ci_high,
        p_value=p_value, significant=p_value < alpha,
        readable=True,
    )


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
    rate_card: RateCard | None = None
    aggregate_readable: bool = True
    aggregate_reason: str = ""
    unique_occasions: int | None = None
    occasions_improved_unselected: float = 0.0
    n_examined: int = 0
    policy: PolicyEffect | None = None

    decay: decay_mod.DecayReport | None = None

    @property
    def rate(self) -> float | None:
        """What one improved occasion is worth, however it was supplied."""
        if self.rate_card is not None:
            return self.rate_card.blended_rate
        return self.value_per_occasion

    @property
    def billable(self) -> bool:
        return self.readable and self.aggregate_readable

    @property
    def money(self) -> float | None:
        rate = self.rate
        if rate is None or not self.billable:
            return None
        return self.occasions_improved * rate

    @property
    def money_range(self) -> tuple[float, float] | None:
        rate = self.rate
        if rate is None or not self.billable:
            return None
        return (self.ci_low * rate, self.ci_high * rate)

    def ledger(self) -> list[LedgerEntry]:
        """A hash-chained line per counted memory, or nothing at all."""
        rate = self.rate
        if rate is None or not self.billable:
            return []
        entries: list[LedgerEntry] = []
        previous = _LEDGER_GENESIS
        for index, memory in enumerate(m for m in self.memories if m.counted):
            money = memory.occasions_improved * rate
            row = _FIELD_SEP.join((
                str(index),
                memory.slug,
                memory.verdict,
                f"{memory.occasions_improved:.6f}",
                f"{rate:.6f}",
                f"{money:.6f}",
            ))
            digest = hashlib.sha256(
                (previous + _FIELD_SEP + row).encode("utf-8")
            ).hexdigest()
            entries.append(LedgerEntry(
                index=index, slug=memory.slug, verdict=memory.verdict,
                occasions_improved=memory.occasions_improved, rate=rate,
                money=round(money, 2), previous_hash=previous, entry_hash=digest,
            ))
            previous = digest
        return entries


def verify_ledger(entries: list[LedgerEntry]) -> int | None:
    """Recompute the chain. Returns the index of the first bad entry, or None."""
    previous = _LEDGER_GENESIS
    for position, entry in enumerate(entries):
        if entry.previous_hash != previous or entry.index != position:
            return position
        row = _FIELD_SEP.join((
            str(entry.index),
            entry.slug,
            entry.verdict,
            f"{entry.occasions_improved:.6f}",
            f"{entry.rate:.6f}",
            f"{entry.occasions_improved * entry.rate:.6f}",
        ))
        if hashlib.sha256(
            (previous + _FIELD_SEP + row).encode("utf-8")
        ).hexdigest() != entry.entry_hash:
            return position
        previous = entry.entry_hash
    return None


_SIGNATURE_DOMAIN = b"commontrace-value-ledger-signature-v1"


def ledger_root(entries: list[LedgerEntry]) -> str:
    """The single hash a signature covers."""
    return entries[-1].entry_hash if entries else _LEDGER_GENESIS


def sign_ledger(
    entries: list[LedgerEntry], key: bytes, *, org_id: str, issued_at: str,
    evidence_digest: str = "", prereg_fingerprint: str = "",
) -> str:
    payload = _SIGNATURE_DOMAIN + _FIELD_SEP.encode("utf-8") + _FIELD_SEP.join(
        (org_id, issued_at, ledger_root(entries), evidence_digest, prereg_fingerprint)
    ).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_ledger_signature(
    entries: list[LedgerEntry], signature: str, key: bytes, *, org_id: str,
    issued_at: str, evidence_digest: str = "", prereg_fingerprint: str = "",
) -> bool:
    expected = sign_ledger(
        entries, key, org_id=org_id, issued_at=issued_at,
        evidence_digest=evidence_digest, prereg_fingerprint=prereg_fingerprint,
    )
    return hmac.compare_digest(expected, signature)


def _check_aggregate(
    counted_slugs: list[str], overlap: OccasionOverlap | None
) -> tuple[bool, str]:
    if len(counted_slugs) < 2:
        return True, ""
    if overlap is None:
        return False, (
            "No assignment record was supplied, so it is not known whether these "
            f"{len(counted_slugs)} memories were injected on overlapping occasions. "
            "Each memory's own effect still stands; adding them up does not, because "
            "one occasion that received two of them would be counted twice. Pass the "
            "holdout assignments (commontrace.value.overlap_from_assignments) to get "
            "a total."
        )
    conflicts = overlap.conflicts_among(counted_slugs)
    if conflicts:
        listed = "; ".join(f"{a} + {b}" for a, b in conflicts[:5])
        more = "" if len(conflicts) <= 5 else f" (and {len(conflicts) - 5} more)"
        return False, (
            "These memories were injected on overlapping occasions, so adding their "
            "contributions would attribute the same improved occasion more than once: "
            f"{listed}{more}. Each memory's own effect is unaffected -- it is the SUM "
            "that is not a count of distinct occasions. Measuring them on disjoint "
            "eligibility, or at the level of the policy rather than the memory, is "
            "what makes a total answerable."
        )
    return True, ""


def compute(
    effects: list[experiment.CausalEffect],
    report: integrity.IntegrityReport | None = None,
    value_per_occasion: float | None = None,
    rate_card: RateCard | None = None,
    overlap: OccasionOverlap | None = None,
    assignments=None,
    last_measured: dict[str, object] | None = None,
    evidence_horizon_days: int | None = None,
    now=None,
) -> ValueReport:
    """Causal value delivered, or a refusal to state one."""
    policy = policy_effect(assignments) if assignments is not None else None
    if assignments is not None and overlap is None:
        overlap = overlap_from_assignments(assignments)

    if report is None:
        return ValueReport(
            readable=False,
            reason="No validity audit was supplied for this experiment, so the effects "
                   "it produced have not been checked. A value figure computed from an "
                   "unexamined comparison is not a measurement.",
            memories=[], occasions_improved=0.0, ci_low=0.0, ci_high=0.0,
            n_counted=0, n_excluded=len(effects),
            value_per_occasion=value_per_occasion, rate_card=rate_card,
            aggregate_readable=True,
            unique_occasions=(overlap.unique_injected_occasions if overlap else None),
            n_examined=len(effects),
            policy=policy,
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
            value_per_occasion=value_per_occasion, rate_card=rate_card,
            aggregate_readable=True,
            unique_occasions=(overlap.unique_injected_occasions if overlap else None),
            n_examined=len(effects),
            policy=policy,
        )

    memories: list[MemoryValue] = []
    decayed: list[decay_mod.DecayItem] = []
    total = 0.0
    unselected_total = 0.0
    variance = 0.0
    counted = 0
    for effect in effects:
        improved = effect.effect * effect.n_injected
        item_low = effect.ci_low * effect.n_injected
        item_high = effect.ci_high * effect.n_injected

        include = effect.verdict in (experiment.VERDICT_HELPS, experiment.VERDICT_HURTS)
        why_not = ""

        fresh = None
        if evidence_horizon_days is not None:
            fresh = decay_mod.freshness(
                (last_measured or {}).get(effect.lesson_slug),
                now=now, horizon_days=evidence_horizon_days,
            )
            if include:
                still, stale_reason = decay_mod.still_counts(
                    effect.verdict, fresh,
                    helps=experiment.VERDICT_HELPS, hurts=experiment.VERDICT_HURTS,
                )
                if not still:
                    include = False
                    why_not = stale_reason
            decayed.append(decay_mod.DecayItem(
                slug=effect.lesson_slug, verdict=effect.verdict,
                freshness=fresh, withheld=bool(why_not),
            ))

        if why_not:
            pass
        elif effect.verdict == experiment.VERDICT_UNDERPOWERED:
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
        if effect.verdict != experiment.VERDICT_UNDERPOWERED:
            unselected_total += improved

        if include:
            counted += 1
            total += improved
            item_se = abs(item_high - item_low) / (2.0 * _Z_95)
            variance += item_se * item_se

    ci_half_width = _Z_95 * math.sqrt(variance)
    low = total - ci_half_width
    high = total + ci_half_width

    counted_slugs = [m.slug for m in memories if m.counted]
    aggregate_readable, aggregate_reason = _check_aggregate(counted_slugs, overlap)

    reason = ""
    if counted == 0 and memories:
        best = max(memories, key=lambda m: abs(m.effect) * max(m.n_injected, 1))
        reason = (
            f"Every memory here is UNDERPOWERED or measured with no effect, so the "
            f"total below is correctly {'0' if value_per_occasion is None else '$0'} -- "
            "that is 'not enough evidence yet', not 'this does not work'. The "
            f"strongest trend so far is `{best.slug}` at {best.effect:+.1%} on "
            f"{best.n_injected} injection(s); see `memories` for what each one still "
            "needs."
        )
    return ValueReport(
        readable=True,
        reason=reason,
        memories=memories,
        occasions_improved=round(total, 2),
        ci_low=round(low, 2), ci_high=round(high, 2),
        n_counted=counted, n_excluded=len(effects) - counted,
        value_per_occasion=value_per_occasion, rate_card=rate_card,
        aggregate_readable=aggregate_readable,
        aggregate_reason=aggregate_reason,
        unique_occasions=(overlap.unique_injected_occasions if overlap else None),
        occasions_improved_unselected=round(unselected_total, 2),
        n_examined=len(effects),
        policy=policy,
        decay=(
            decay_mod.DecayReport(
                items=tuple(decayed), horizon_days=evidence_horizon_days)
            if evidence_horizon_days is not None else None
        ),
    )


def render(report: ValueReport, unit: str = "occasion") -> str:
    """The value section, written so the refusal is as legible as a figure."""
    if not report.readable:
        return "## Value delivered\n\n**Not stated.** " + report.reason

    lines = ["## Value delivered", ""]
    if report.aggregate_readable:
        lines += [
            f"**{report.occasions_improved:+,.0f} {unit}s** went differently because of "
            f"this memory, over the measured window "
            f"(95% CI {report.ci_low:+,.0f} to {report.ci_high:+,.0f}).",
            "",
        ]
    else:
        lines += [
            "**No total is stated.** " + report.aggregate_reason,
            "",
        ]
    if report.reason:
        lines += [report.reason, ""]
    if report.aggregate_readable:
        lines += [
            f"Computed from {report.n_counted} memory/memories whose causal effect is "
            f"established. {report.n_excluded} contributed nothing, listed below with "
            "why.",
            "",
        ]
        if report.n_counted and report.occasions_improved_unselected != 0.0:
            lines += [
                f"_Counting every measured memory rather than only the ones that "
                f"cleared significance gives {report.occasions_improved_unselected:+,.0f}"
                f" {unit}s. The figure above selects on the same data it reports, which "
                "biases its magnitude away from zero; this one does not, and is not "
                "billable because it includes effects the experiment did not "
                "establish. The gap between them is what that selection is worth._",
                "",
            ]
    if report.policy is not None and report.policy.readable:
        policy = report.policy
        lines += [
            f"**Whole-policy comparison:** {policy.effect:+.1%} "
            f"(95% CI {policy.ci_low:+.1%} to {policy.ci_high:+.1%}) across "
            f"{policy.n_treated:,} {unit}s that received a memory against "
            f"{policy.n_control:,} that received none -- "
            f"{policy.occasions_improved:+,.0f} {unit}s.",
            "",
            f"_Every {unit} counts once here, whatever number of memories it "
            "received, which is what makes this addable when the per-memory "
            "figures are not. It attributes nothing to an individual memory._",
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
