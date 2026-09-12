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
Five rules. The first three govern each memory's own figure; the last two
govern what may be done with them together, and were added because the
arithmetic that combined them was the weakest thing in this module.

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

4. **Memories that shared occasions are not added together.** Summing
   `effect x n_injected` is a count of occasions only if no occasion was
   counted twice. One occasion matching three memories, all injected,
   resolving once, was three improved occasions in the total -- and then
   three times the money, because this is the quantity an invoice is
   computed from. `OccasionOverlap` answers whether the sum is a count at
   all, and when it is not there is no total, no money and no ledger. The
   per-memory effects are untouched: it is the addition that was unsound,
   not the estimates.

   Where the sum is refused there is still a valid aggregate, and it is the
   one a customer asks for anyway -- `policy_effect`, occasions that got any
   memory against occasions that got none, one row per occasion by
   construction. It attributes nothing to an individual memory, which is the
   trade: a number you can add up, about the policy rather than its parts.

5. **The interval is combined in quadrature, and the selection is priced.**
   Adding per-memory interval ENDPOINTS produced something that was not a
   95% interval for the sum under any assumption -- too wide for independent
   estimates (their errors partly cancel), undefined for dependent ones.
   And counting only the memories that cleared significance selects on the
   same data it reports, biasing the total's magnitude away from zero. So
   `occasions_improved_unselected` is reported beside the billable figure:
   the same total without that selection, which makes the size of the
   winner's curse a number rather than a caveat nobody reads.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from dataclasses import dataclass, field

from commontrace import decay as decay_mod
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

# The two-sided normal quantile the estimator's own 95% intervals are built
# from (commontrace/experiment.py). Used to read an SE back out of a
# reported interval and to put the combined one back together, so the
# aggregate interval is expressed in the same units on the same convention
# as the per-memory ones rather than a second, slightly different 95%.
_Z_95 = 1.959963984540054


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


# --- Can these memories be added together at all? ---------------------------
#
# THE DEFECT THIS EXISTS TO FIX. Each memory's contribution is
# `effect x n_injected` -- how many more occasions went well because that
# memory existed. Summing those across memories was the whole aggregate, and
# it is only a count of occasions if no occasion is counted twice.
#
# Nothing guaranteed that. One occasion can receive several independently
# randomized memories: a support contact matches three different traces, all
# three are injected, the contact resolves. Each memory's marginal effect
# legitimately includes that occasion, so adding the three contributions
# attributes one improved outcome up to three times -- and then prices it
# three times, because this is the quantity an invoice is computed from.
#
# The fix is not a better estimator, it is knowing whether the question is
# answerable: if the counted memories were injected on disjoint sets of
# occasions, the sum is a count of distinct occasions and the arithmetic
# holds. If they overlap, it is not, and this module's own rule -- a
# compromised measurement produces no number, not a hedged one -- applies to
# the aggregate exactly as it already applies to the per-memory effects.
@dataclass(frozen=True)
class OccasionOverlap:
    """Which memories were injected on the same occasions as which others.

    `shared_pairs` holds an unordered pair per co-injected memory pair;
    `unique_injected_occasions` is the size of the union across every memory,
    i.e. how many DISTINCT occasions received anything at all -- the honest
    denominator, and the ceiling no sum of contributions may exceed.
    """

    shared_pairs: frozenset[frozenset[str]] = field(default_factory=frozenset)
    unique_injected_occasions: int = 0

    def conflicts_among(self, slugs) -> list[tuple[str, str]]:
        """The co-injected pairs that both fall inside `slugs`, sorted for a
        stable message. Pairs involving a memory nobody is counting cannot
        double-attribute anything, so they are not conflicts."""
        wanted = set(slugs)
        found = [
            tuple(sorted(pair)) for pair in self.shared_pairs
            if len(pair & wanted) == 2
        ]
        return sorted(found)


def overlap_from_assignments(assignments) -> OccasionOverlap:
    """Build the overlap record from raw (lesson, occasion, injected) rows --
    the same `integrity.Assignment` list the validity audit already consumes.

    Only INJECTED rows matter: a memory that was withheld on an occasion
    contributed nothing to it, so it cannot double-attribute its outcome.
    """
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


# --- The aggregate that IS answerable when the sum is not ------------------
#
# Refusing a total is correct and, on its own, unhelpful -- particularly on
# the Hub, where `holdout_assign` takes a LIST of traces for one occasion, so
# co-injection is the normal case rather than the exceptional one. "You may
# not add these up" would then be the answer to almost every real fleet.
#
# There is a valid aggregate available, and it is the one the customer
# actually asks for: not "what was each memory worth" summed, but "what was
# having the memory system worth". Each trace is randomized independently per
# occasion, so the occasions where NOTHING was injected are a genuine control
# arm for the whole policy -- randomly formed, concurrent, same fleet. One row
# per occasion, so an occasion is counted exactly once by construction, and
# the double-attribution problem cannot arise at all.
#
# What it does NOT do is attribute credit to individual memories; that is the
# question the per-memory effects answer, and the one whose SUM is unsound.
# The two are reported side by side rather than one being made to stand in
# for the other.
@dataclass(frozen=True)
class PolicyEffect:
    """Occasions that went well with any memory injected, against occasions
    that got none."""

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
    """Estimate the whole memory policy's effect, on unique occasions.

    An occasion is TREATED if any memory was injected on it and CONTROL if
    every memory eligible for it was withheld. Occasions with no reported
    outcome are excluded from both arms -- the same rule `experiment.analyze`
    applies, and the reason `integrity.audit` exists to check whether that
    exclusion is even-handed.

    Returns a `readable=False` result rather than raising when an arm is too
    small to support a comparison: at a 10% holdout rate, the all-withheld
    arm is rare by construction (every eligible memory has to land tails at
    once), and reporting a difference computed from three occasions would be
    worse than saying the design cannot answer yet.
    """
    treated_success: dict[str, bool] = {}
    treated_any: dict[str, bool] = {}
    for row in assignments:
        occasion = str(getattr(row, "occasion_id", "") or "")
        if not occasion:
            continue
        succeeded = getattr(row, "succeeded", None)
        if succeeded is None:
            # Unresolved: no outcome to put in either arm. Recorded as seen so
            # an occasion that is partly resolved is not silently half-counted.
            treated_any.setdefault(occasion, False)
            treated_any[occasion] = treated_any[occasion] or bool(
                getattr(row, "injected", False)
            )
            continue
        treated_any[occasion] = treated_any.get(occasion, False) or bool(
            getattr(row, "injected", False)
        )
        # An occasion has ONE outcome; rows disagreeing about it is a
        # recording fault, and the conservative reading of a disagreement is
        # the failure, so `and` rather than `or`.
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
    # Whether the memories above may be ADDED TOGETHER, which is a separate
    # question from whether each one's effect is readable. False when two
    # counted memories were injected on the same occasions (the sum would
    # attribute one improved outcome more than once) or when nothing told
    # this module either way. `readable` still governs the per-memory
    # figures; this governs the total, the money, and the ledger.
    aggregate_readable: bool = True
    aggregate_reason: str = ""
    # How many DISTINCT occasions received any memory at all -- the ceiling
    # the total cannot exceed, and the denominator a reader needs to judge
    # whether a number is large. None when no assignment record was supplied.
    unique_occasions: int | None = None
    # The same total computed over EVERY measured memory rather than only the
    # ones whose effect cleared significance. `occasions_improved` selects on
    # the data it then reports, which biases its magnitude away from zero
    # (the winner's curse); this does not select, so it is the unbiased
    # estimate of the same quantity, and the gap between them is what that
    # selection is worth. Not billable -- it includes effects the experiment
    # could not establish -- which is exactly why both are reported.
    occasions_improved_unselected: float = 0.0
    n_examined: int = 0
    # The whole-policy comparison on UNIQUE occasions: occasions that got any
    # memory against occasions that got none. Valid exactly where the sum
    # above is not, because an occasion appears in it once by construction --
    # so this is what a fleet whose memories share occasions can still be
    # told, instead of only being told "no". None when no assignment record
    # was supplied to compute it from.
    policy: PolicyEffect | None = None

    # What the evidence horizon did, per memory (commontrace/decay.py). None
    # when no horizon was supplied, which is the historical behaviour and is
    # distinguishable from "a horizon ran and found nothing stale" -- those
    # are different facts and only one of them means decay is switched on.
    decay: decay_mod.DecayReport | None = None

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
    def billable(self) -> bool:
        """Both gates. `readable` says the effects can be trusted at all;
        `aggregate_readable` says they can be added up. A figure needs both,
        and every path that produces a number or an invoice goes through
        here rather than re-deciding it."""
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
        """A hash-chained line per counted memory, or nothing at all.

        Each entry carries the SHA-256 of its own fields prefixed by the
        previous entry's hash, so the chain is only reproducible if every line
        is present, unmodified and in order. Editing one figure, deleting an
        inconvenient HURTS line, or reordering to bury one changes that
        entry's hash and every hash after it -- which is the property an
        invoice needs and a spreadsheet does not have.

        Returns [] when the run is not readable, when the memories may not be
        added together (`aggregate_readable`), or when no rate was agreed.
        That is the same refusal `money` makes, for the same reason: a ledger
        is a stronger claim than a number, so it must not exist in any case
        where the number itself would be withheld. A chain of verifiable lines
        computed off a compromised experiment -- or off memories that
        double-attribute the same occasions -- would be worse than no ledger:
        it would make an unsupportable figure look audited.
        """
        rate = self.rate
        if rate is None or not self.billable:
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


def sign_ledger(
    entries: list[LedgerEntry], key: bytes, *, org_id: str, issued_at: str,
    evidence_digest: str = "", prereg_fingerprint: str = "",
) -> str:
    """HMAC-SHA256 over (domain, org_id, issued_at, root, evidence, prereg),
    hex-encoded.

    `evidence_digest` and `prereg_fingerprint` are what make the invoice
    ANCHORED rather than merely tamper-evident. The chain proves the printed
    lines were not edited; these bind the invoice to the raw assignment rows
    it was computed from (commontrace/raw_export.py) and to the design that
    was registered before the run (commontrace/prereg.py). Without them, an
    issuer could hand over a perfectly signed invoice and a data export that
    has nothing to do with it, and both would check out on their own. Empty
    strings keep a signature over a ledger with no such artifacts distinct
    from one that had them -- they are part of the signed payload either way.

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
        (org_id, issued_at, ledger_root(entries), evidence_digest, prereg_fingerprint)
    ).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_ledger_signature(
    entries: list[LedgerEntry], signature: str, key: bytes, *, org_id: str,
    issued_at: str, evidence_digest: str = "", prereg_fingerprint: str = "",
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
    expected = sign_ledger(
        entries, key, org_id=org_id, issued_at=issued_at,
        evidence_digest=evidence_digest, prereg_fingerprint=prereg_fingerprint,
    )
    return hmac.compare_digest(expected, signature)


def _check_aggregate(
    counted_slugs: list[str], overlap: OccasionOverlap | None
) -> tuple[bool, str]:
    """Whether these memories' contributions may be added into one count.

    Three cases, and the middle one is the whole point:

      * Fewer than two counted memories -- nothing to double-count, so the
        question does not arise and no assignment record is needed to
        answer it.
      * Two or more, and no overlap record -- UNKNOWN, which is not the
        same as fine. Summing anyway is what produced an invoice that could
        silently bill the same improved occasion several times.
      * Two or more with a record -- answerable exactly: the sum holds if
        and only if no two counted memories were injected on a shared
        occasion.
    """
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
    """Causal value delivered, or a refusal to state one.

    `report` is the validity audit over the same run. Passing None means "not
    audited", which is treated as not-readable rather than as clean: a value
    figure computed from an unexamined experiment is the exact artifact this
    module exists to not produce.

    `evidence_horizon_days` turns on evidence decay (commontrace/decay.py):
    an effect nobody has re-measured inside the horizon stops being billed.
    None keeps the historical behaviour, where an estimate is counted forever
    regardless of when it was taken -- opt-in, because switching it on
    changes an invoice and that is a decision rather than an upgrade.

    `last_measured` maps slug -> the date behind that effect. A slug missing
    from it is UNDATED, which is treated exactly as expired: "we cannot tell
    when this was measured" and "this was measured too long ago" have the
    same standing in an argument about whether a number is current.
    """
    # One input, two derived facts: whether these memories may be added
    # (overlap) and what the policy as a whole was worth (policy). A caller
    # with the assignment log should not have to know it needs both.
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
            # An unreadable run has no aggregate to qualify separately: the
            # refusal above already covers every figure that would come out
            # of it. Left True so nothing reads a second, unrelated reason.
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
            # An unreadable run has no aggregate to qualify separately: the
            # refusal above already covers every figure that would come out
            # of it. Left True so nothing reads a second, unrelated reason.
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

        # HELPS and HURTS both count. Dropping the second would make this a
        # brochure -- see the module docstring.
        include = effect.verdict in (experiment.VERDICT_HELPS, experiment.VERDICT_HURTS)
        why_not = ""

        # And an estimate is a statement about the world WHEN IT WAS TAKEN.
        # Past the horizon a HELPS stops being billed and a HURTS keeps
        # counting -- see commontrace/decay.py for why those are different.
        # Both rules move the figure down, which is the point: when evidence
        # decays, it resolves against the party that benefits from the doubt.
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
        # The unbiased half of the post-selection story: every memory the
        # experiment MEASURED contributes its point estimate, including the
        # ones whose effect did not clear significance. Their estimates are
        # noisy, not biased; excluding them on the strength of their own data
        # is what introduces bias, so this total is the one that does not.
        # UNDERPOWERED memories are excluded from both: their design could
        # not detect an effect worth acting on, so their point estimate is
        # not an estimate of anything useful.
        if effect.verdict != experiment.VERDICT_UNDERPOWERED:
            unselected_total += improved

        if include:
            counted += 1
            total += improved
            # Variance of this contribution, recovered from the interval the
            # estimator already reported rather than recomputed from counts:
            # a 95% normal interval is estimate +/- 1.96*SE, so its half-width
            # over 1.96 is that SE, scaled by n_injected exactly as the point
            # estimate is. Combined in quadrature below.
            #
            # THIS IS NOT WHAT THIS USED TO DO. It summed the interval
            # ENDPOINTS, which is not a 95% interval for a sum under any
            # assumption: for independent estimates it is far too wide (the
            # errors partly cancel, which is what the square root captures),
            # and for dependent ones it is simply undefined without the
            # covariance. The independence this does assume is what the
            # occasion-overlap gate below establishes -- disjoint occasion
            # sets under independent randomization -- which is why the two
            # belong together.
            item_se = abs(item_high - item_low) / (2.0 * _Z_95)
            variance += item_se * item_se

    ci_half_width = _Z_95 * math.sqrt(variance)
    low = total - ci_half_width
    high = total + ci_half_width

    counted_slugs = [m.slug for m in memories if m.counted]
    aggregate_readable, aggregate_reason = _check_aggregate(counted_slugs, overlap)

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
        # The headline total is exactly the figure that gets quoted, so it
        # must not appear at all when the memories may not be added. Stated
        # where the number would have been, not in a footnote under it.
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
