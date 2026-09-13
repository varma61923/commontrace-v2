"""Evidence gets old. What was true in March is a claim, not a measurement.

WHY THIS EXISTS
---------------
This product's whole argument is that a memory earns its place by measured
effect. A randomized holdout establishes that a lesson HELPS, the lesson
graduates into the working set, and the customer is invoiced against the
occasions it improved.

Nothing in that sentence has a date in it, and every part of it should.

An effect estimate is a statement about the world at the time it was
measured. Six months later the API it described has been deprecated, the
policy it encoded has changed, the vendor it named has been replaced -- and
the estimate is unchanged, because nothing re-ran it. The lesson is still
`active`, still injected, still counted, still billed. The number on the
invoice is not wrong about the past; it is silently presented as a claim
about the present.

The Hub already understood half of this: `_working_set_entry` expires
graduation at a 180-day evidence horizon, so a memory nobody has re-measured
leaves the pinned block. The value ledger -- the surface the customer
actually pays on -- had no horizon at all. So the two surfaces disagreed
about whether the same evidence was current, and the one that disagreed was
the one attached to money.

THE ASYMMETRY, WHICH IS THE WHOLE DESIGN
----------------------------------------
The obvious implementation expires every stale verdict and is wrong, in the
direction that flatters the vendor.

`commontrace/value.py` counts HELPS *and* HURTS, deliberately: "Dropping the
second would make this a brochure." A harmful memory contributes a NEGATIVE
occasions_improved and reduces the invoice. So if staleness simply dropped
every expired verdict:

    a stale HELPS  -> stops counting -> invoice goes DOWN  (good, honest)
    a stale HURTS  -> stops counting -> invoice goes UP    (a vendor
                                        quietly deleting its own harms by
                                        waiting long enough)

The second is the brochure failure wearing a timestamp. So staleness is
resolved in ONE direction -- the customer's:

  * A stale HELPS stops being counted. You cannot bill for value you can no
    longer show is current.
  * A stale HURTS keeps being counted until it is re-measured. A harm you
    stopped looking at is not a harm that went away, and the conservative
    assumption about your own product's damage is that it persists.

Both rules move the invoice the same way: down. That is not a coincidence,
it is the rule -- when evidence decays, resolve against the party that
benefits from the ambiguity, which here is always the vendor.

UNDATED IS NOT FRESH
--------------------
An effect whose evidence carries no date at all is treated exactly as an
expired one. "We cannot tell when this was measured" and "this was measured
too long ago" have the same standing in an argument about whether a number
is current, and assuming in favour of the vendor because a timestamp is
missing is how missing timestamps become convenient.

THIS MODULE DECIDES NOTHING ON ITS OWN
--------------------------------------
Every function here is a pure judgement about dates. Retiring a lesson,
withholding a figure, or telling an operator what is due is the caller's
business -- so the same rule can be applied to an invoice, to a pinned
block, and to a `commontrace lesson` listing without three implementations
drifting apart.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

#: The default horizon, matching the Hub's working-set graduation expiry
#: (hub/crud.py:DEFAULT_EVIDENCE_HORIZON_DAYS) so a memory does not fall out
#: of the pinned block while still being billed on, or the reverse. One
#: number, two surfaces.
DEFAULT_HORIZON_DAYS = 180

FRESH = "fresh"
STALE = "stale"
UNDATED = "undated"


@dataclass(frozen=True)
class Freshness:
    """How current one effect estimate's evidence is."""

    state: str
    #: None when the evidence carries no date.
    age_days: float | None
    horizon_days: int

    @property
    def is_current(self) -> bool:
        return self.state == FRESH

    def describe(self) -> str:
        if self.state == FRESH:
            return f"measured {self.age_days:.0f} days ago"
        if self.state == UNDATED:
            return "carries no measurement date"
        return (
            f"last measured {self.age_days:.0f} days ago, past the "
            f"{self.horizon_days}-day evidence horizon"
        )


def _parse(value: object) -> datetime.datetime | None:
    """A timestamp from whatever shape the caller has.

    Tolerant on purpose: this is fed by a JSONL log line, a Postgres column
    and a hand-edited YAML field, and a date that fails to parse must read as
    *undated* -- which is handled conservatively -- rather than raise inside
    an invoice calculation.
    """
    if isinstance(value, datetime.datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        try:
            moment = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment


def freshness(
    last_measured_at: object,
    *,
    now: datetime.datetime | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> Freshness:
    """How current this evidence is, as of `now`."""
    horizon = max(1, int(horizon_days))
    moment = _parse(last_measured_at)
    if moment is None:
        return Freshness(UNDATED, None, horizon)
    reference = now or datetime.datetime.now(datetime.timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=datetime.timezone.utc)
    # Clamped at zero: the reference clock and the recorded timestamp can come
    # from different machines, and a few seconds of skew must not surface as a
    # negative age.
    age = max(0.0, (reference - moment).total_seconds() / 86400.0)
    return Freshness(FRESH if age <= horizon else STALE, age, horizon)


def still_counts(verdict: str, fresh: Freshness, *, helps: str, hurts: str) -> tuple[bool, str]:
    """Whether an effect with this verdict and this freshness still counts.

    Returns (counts, why_not). `helps`/`hurts` are the caller's verdict
    constants, passed in rather than imported so this module stays free of
    the experiment layer -- and so the Hub and the local tier cannot drift
    into two different spellings of the same rule.

    See the module docstring for why the two verdicts are treated
    differently. The short version: both rules move the invoice down.
    """
    if fresh.is_current:
        return True, ""
    if verdict == hurts:
        # Kept. A harm you stopped measuring is not a harm that went away,
        # and expiring it would let a vendor delete its own damage by
        # waiting.
        return True, ""
    if verdict == helps:
        return False, (
            f"established, but the evidence {fresh.describe()}. An effect "
            "measured that long ago is a claim about the past, not a "
            "measurement of the present, so it is not billed. Re-run the "
            "holdout for this memory to count it again."
        )
    # Any other verdict was not being counted anyway; staleness changes
    # nothing about it and must not invent a reason that reads as one.
    return False, ""


@dataclass(frozen=True)
class DecayItem:
    slug: str
    verdict: str
    freshness: Freshness
    #: True when this item's staleness actually changed the outcome --
    #: a stale HURTS is still counted, and saying it "decayed" without that
    #: distinction would send an operator to re-measure the wrong thing.
    withheld: bool


@dataclass(frozen=True)
class DecayReport:
    """What has gone stale, and what that did."""

    items: tuple[DecayItem, ...] = ()
    horizon_days: int = DEFAULT_HORIZON_DAYS

    @property
    def stale(self) -> tuple[DecayItem, ...]:
        return tuple(i for i in self.items if not i.freshness.is_current)

    @property
    def withheld(self) -> tuple[DecayItem, ...]:
        return tuple(i for i in self.items if i.withheld)

    @property
    def due_for_remeasurement(self) -> tuple[str, ...]:
        """Slugs an operator should re-run the holdout for, worst first.

        Ordered by age rather than by verdict: the oldest evidence is the
        least defensible, whichever way it pointed.
        """
        return tuple(
            item.slug for item in sorted(
                self.stale,
                key=lambda i: (i.freshness.age_days is None, -(i.freshness.age_days or 0.0)),
            )
        )

    def render(self) -> str:
        if not self.items:
            return "No measured effects to check."
        lines = [
            f"Evidence horizon: {self.horizon_days} days",
            "",
        ]
        for item in sorted(self.items, key=lambda i: i.slug):
            mark = "STALE" if not item.freshness.is_current else "fresh"
            note = " (withheld from the invoice)" if item.withheld else ""
            lines.append(
                f"  [{mark:5s}] {item.slug}  {item.verdict}  "
                f"{item.freshness.describe()}{note}"
            )
        lines.append("")
        if self.withheld:
            lines.append(
                f"{len(self.withheld)} memory/memories are no longer billed because "
                "their evidence is older than the horizon. Re-run the holdout to "
                "count them again."
            )
        kept = [i for i in self.stale if not i.withheld]
        if kept:
            lines.append(
                f"{len(kept)} stale memory/memories are STILL counted: a harm that "
                "stopped being measured is not a harm that went away, so it keeps "
                "reducing the figure until it is re-measured."
            )
        if not self.stale:
            lines.append("All evidence is inside the horizon.")
        return "\n".join(lines)
