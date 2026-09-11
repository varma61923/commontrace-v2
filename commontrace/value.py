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

import hashlib
import hmac
from dataclasses import dataclass, field

from commontrace import experiment, integrity

# Domain separator for the audit chain below. Hashing the empty string as a
# genesis would let a ledger built here be spliced into any other SHA-256
# chain that also started from nothing; naming the chain in its own first link
# makes that impossible.
_LEDGER_GENESIS = hashlib.sha256(b"commontrace-value-ledger-v1").hexdigest()

# The separator between fields of a hashed row. 0x1F (ASCII unit separator) is
# the same byte `experiment.is_held_out` puts between the parts of its own
# hash preimage, and for the same reason: it cannot occur in any of the fields,
# so no combination of values can be re-split into a different row that hashes
# the same.
_FIELD_SEP = "\x1f"


@dataclass(frozen=True)
class Tier:
    """One line of a contractual rate card.

    `share` is the fraction of occasions this tier is agreed to represent, and
    `cost_per_occasion` is what one of them is worth. BOTH are inputs supplied
    by the customer, not quantities this package measures -- see RateCard.
    """

    name: str
    share: float
    cost_per_occasion: float


@dataclass(frozen=True)
class RateCard:
    """What the customer has agreed an improved occasion is worth.

    A flat per-occasion rate is the wrong shape for the work it prices.
    Resolving a password reset and averting an SLA breach are both "one
    occasion", and a finance team asked to accept one number for both will
    reject the number rather than the premise. A rate card states the mix
    explicitly: the tiers, what share of occasions each is agreed to be, and
    what one occasion in that tier is worth.

    THE IMPORTANT PART, and the reason this is a separate type rather than a
    dict of numbers: none of this is measured. This package measures occasions
    improved -- causally, against a randomized control, and it refuses to state
    even that when the audit says the sample cannot support it. The tiers, the
    mix and the rates are all contractual, and `blended_rate` is arithmetic
    performed on the customer's own assumptions. Keeping them in a named type
    that travels with the report is what stops a negotiated input from being
    read back later as a finding.
    """

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
        # Tolerance, not equality: a mix written as thirds in a contract cannot
        # sum to exactly one in binary floating point, and rejecting it would
        # be pedantry aimed at the customer's own paperwork.
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

    @property
    def rate(self) -> float | None:
        """What one improved occasion is worth, however it was supplied.

        A rate card wins over a flat rate when both are given: it is the more
        specific statement of the same contractual fact, and silently
        preferring the vaguer one would price the work against a number the
        customer has already superseded.
        """
        if self.rate_card is not None:
            return self.rate_card.blended_rate
        return self.value_per_occasion

    @property
    def money(self) -> float | None:
        rate = self.rate
        if rate is None or not self.readable:
            return None
        return self.occasions_improved * rate

    @property
    def money_range(self) -> tuple[float, float] | None:
        rate = self.rate
        if rate is None or not self.readable:
            return None
        return (self.ci_low * rate, self.ci_high * rate)

    def ledger(self) -> list[LedgerEntry]:
        """A hash-chained line per counted memory, or nothing at all.

        Each entry carries the SHA-256 of its own fields prefixed by the
        previous entry's hash, so the chain is only reproducible if every line
        is present, unmodified and in order. Editing one figure, deleting an
        inconvenient HURTS line, or reordering to bury one changes that
        entry's hash and every hash after it -- which is the property an
        invoice needs and a spreadsheet does not have.

        Returns [] when the run is not readable or no rate was agreed. That is
        the same refusal `money` makes, for the same reason: a ledger is a
        stronger claim than a number, so it must not exist in any case where
        the number itself would be withheld. A chain of verifiable lines
        computed off a compromised experiment would be worse than no ledger --
        it would make an unsupportable figure look audited.
        """
        rate = self.rate
        if rate is None or not self.readable:
            return []
        entries: list[LedgerEntry] = []
        previous = _LEDGER_GENESIS
        for index, memory in enumerate(m for m in self.memories if m.counted):
            money = memory.occasions_improved * rate
            # Fixed field order and fixed precision: the hash has to be
            # reproducible by the customer from the printed numbers, so it
            # cannot depend on repr() drift between platforms or on how a
            # serializer happened to order a dict.
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
    """Recompute the chain. Returns the index of the first bad entry, or None.

    The half of the audit trail that belongs to the reader. A ledger nobody
    can check is a decoration, so this is deliberately written to be portable:
    it reads only the printed fields of each entry, so a customer can
    reimplement it in whatever language their finance team audits in and get
    the same answer from the same invoice.

    An empty ledger verifies -- there is nothing to contradict. Callers that
    care about the difference between "verified" and "absent" check the length.
    """
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


# --- Issuer authentication --------------------------------------------------
#
# WHY THIS EXISTS. verify_ledger() above proves the chain is INTERNALLY
# CONSISTENT: every entry follows from the one before it, back to a fixed
# public genesis, over a fixed public algorithm. That proves nothing about
# WHO produced the chain. Both the genesis and the hashing algorithm are
# public by design (verify_ledger's whole point is that a customer can
# reimplement it), which means anyone with write access to wherever a ledger
# is stored -- a compromised account, a malicious insider, an issuer
# fabricating a smaller invoice after the fact -- can regenerate an entire
# replacement chain from different figures, and it will verify exactly as
# cleanly as the original. A hash chain alone catches EDITING one entry of
# an existing ledger. It does not catch REPLACING the whole thing, and
# "is this actually the invoice CommonTrace issued" is the second question,
# not the first.
#
# The fix is an HMAC over the chain's root, keyed by a secret only the
# issuer holds and that never appears anywhere in the printed ledger. A
# party without that key can still run verify_ledger and confirm internal
# consistency, but cannot produce a signature verify_ledger_signature
# accepts -- so a wholesale fabrication is now distinguishable from a
# genuine invoice too, without asking the customer to simply trust that
# nobody with storage access tampered with the file.
_SIGNATURE_DOMAIN = b"commontrace-value-ledger-signature-v1"


def ledger_root(entries: list[LedgerEntry]) -> str:
    """The single hash a signature covers.

    The last entry's hash if the ledger is non-empty, or the chain genesis
    if it is empty (an empty ledger is itself a fact worth being able to
    sign -- "CommonTrace issued zero counted lines this period" is exactly
    the kind of claim someone might want fabricated evidence against).
    Signing only the root is sufficient, not a shortcut: verify_ledger
    already proves every earlier entry is reachable ONLY by walking the
    chain from genesis to that exact root, so a signature over the root
    transitively covers every entry beneath it.
    """
    return entries[-1].entry_hash if entries else _LEDGER_GENESIS


def sign_ledger(entries: list[LedgerEntry], key: bytes, *, org_id: str, issued_at: str) -> str:
    """HMAC-SHA256 over (domain, org_id, issued_at, root), hex-encoded.

    `key` is the whole point: it must be a secret the issuer holds
    independently of anything printed on the invoice, kept outside this
    module (see hub/config.py HUB_LEDGER_SIGNING_KEY), and never derived
    from the ledger's own contents -- otherwise "signing" would just be
    another public function of the same public data, exactly as
    unauthenticated as entry_hash itself.

    `org_id` and `issued_at` are bound into the payload alongside the root
    so a valid signature minted for one org's ledger cannot be replayed as
    if it were a fresh signature over a different org's chain, or over the
    same chain re-dated to look more current. `_FIELD_SEP` (the same
    separator `ledger()` uses) keeps the three fields from being re-split
    into a different triple that happens to hash the same.
    """
    payload = _SIGNATURE_DOMAIN + _FIELD_SEP.encode("utf-8") + _FIELD_SEP.join(
        (org_id, issued_at, ledger_root(entries))
    ).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_ledger_signature(
    entries: list[LedgerEntry], signature: str, key: bytes, *, org_id: str, issued_at: str
) -> bool:
    """Whether `signature` is what `sign_ledger` produces for this exact
    (org, timestamp, chain) -- i.e. whether whoever holds `key` actually
    issued this invoice, not merely whether the chain is self-consistent
    (verify_ledger covers that separately, with no key required).

    `hmac.compare_digest` rather than `==`: a signature check is exactly the
    kind of comparison a timing side-channel can turn into a byte-at-a-time
    oracle, the same reasoning hub/auth.py's key comparison already applies
    to API keys.
    """
    expected = sign_ledger(entries, key, org_id=org_id, issued_at=issued_at)
    return hmac.compare_digest(expected, signature)


def compute(
    effects: list[experiment.CausalEffect],
    report: integrity.IntegrityReport | None = None,
    value_per_occasion: float | None = None,
    rate_card: RateCard | None = None,
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
            value_per_occasion=value_per_occasion, rate_card=rate_card,
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

    reason = ""
    if counted == 0 and memories:
        # $0/zero occasions here is a correct measurement, not "nothing is
        # working" -- but nothing at the top level said so, and $0 reads as
        # a verdict to anyone who has not also read every memory's
        # `why_not`. Name the strongest trend directly: the memory whose
        # (unestablished) effect times its current injection count is
        # largest, which is also the one closest to clearing the power bar.
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
    ]
    if report.reason:
        lines += [report.reason, ""]
    lines += [
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
