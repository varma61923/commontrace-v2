"""Evidence gets old. What was true in March is a claim, not a measurement."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

DEFAULT_HORIZON_DAYS = 180

# Explicit type-conditioned half-lives; identity does not decay by default.
HALF_LIVES_DAYS = {"identity": None, "preference": 365.0, "environment": 30.0,
                   "temporary": 1.0, "procedure": 180.0, "general": 90.0}


def perishability(memory_type: str, age_days: float, *, half_life_days: float | None = None) -> float:
    """Ranking freshness in [0,1], independent of evidence or forgetting."""
    import math

    if not math.isfinite(age_days) or age_days < 0:
        raise ValueError("age must be finite and nonnegative")
    half_life = half_life_days if half_life_days is not None else HALF_LIVES_DAYS.get(memory_type, 90.0)
    if half_life is None:
        return 1.0
    if not math.isfinite(half_life) or half_life <= 0:
        raise ValueError("half-life must be positive and finite")
    return math.exp(-math.log(2) * age_days / half_life)

FRESH = "fresh"
STALE = "stale"
UNDATED = "undated"


@dataclass(frozen=True)
class Freshness:
    """How current one effect estimate's evidence is."""

    state: str
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
    age = max(0.0, (reference - moment).total_seconds() / 86400.0)
    return Freshness(FRESH if age <= horizon else STALE, age, horizon)


def still_counts(verdict: str, fresh: Freshness, *, helps: str, hurts: str) -> tuple[bool, str]:
    """Whether an effect with this verdict and this freshness still counts."""
    if fresh.is_current:
        return True, ""
    if verdict == hurts:
        return True, ""
    if verdict == helps:
        return False, (
            f"established, but the evidence {fresh.describe()}. An effect "
            "measured that long ago is a claim about the past, not a "
            "measurement of the present, so it is not billed. Re-run the "
            "holdout for this memory to count it again."
        )
    return False, ""


@dataclass(frozen=True)
class DecayItem:
    """One measured lesson and whether stale benefit was withheld from billing."""
    slug: str
    verdict: str
    freshness: Freshness
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
        """Slugs an operator should re-run the holdout for, worst first."""
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
