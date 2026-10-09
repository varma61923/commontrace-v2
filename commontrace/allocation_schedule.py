"""Allocation schedules: the pure part, shared by the store and the Hub's integrity audit.

Stdlib only, so the Hub server image can audit scheduled assignments without
the file store. `allocation` publishes and reads schedules; this module says
what a schedule is, how it hashes (PROTOCOL 13.5) and which one governs an
assignment.
"""
from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import asdict, dataclass, field

SCHEDULE_DOMAIN = "commontrace-allocation-schedule-v1"


@dataclass
class Schedule:
    version: int
    salt: str
    effective_from: str
    default_rate: float
    rates: dict[str, float]
    policy: dict
    basis: dict
    previous: str
    created_at: str
    enabled: bool = True
    digest: str = field(default="", compare=False)

    def body(self) -> dict:
        out = asdict(self)
        out.pop("digest")
        return out

    def rate_for(self, lesson: str) -> float:
        return self.rates.get(lesson, self.default_rate)

    def effective_at(self) -> datetime.datetime:
        return datetime.datetime.fromisoformat(self.effective_from)


def schedule_digest(body: dict) -> str:
    """PROTOCOL 13.5: SHA-256 over the domain, U+001E, and the compact sorted-key JSON of the schedule."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((SCHEDULE_DOMAIN + "\x1e" + canonical).encode("utf-8")).hexdigest()


def in_force(schedules: list[Schedule], salt: str, at: datetime.datetime) -> Schedule | None:
    """The schedule governing an assignment made at `at` in the experiment `salt`, if any."""
    current = None
    for s in schedules:
        if s.salt != salt:
            continue
        if s.effective_at() <= at:
            current = s
    return current if current is not None and current.enabled else None


def unscheduled(rows: list, schedules: list[Schedule]) -> list:
    """Assignments whose logged rate is not the one their schedule set (empty when every rate is accounted for)."""
    out = []
    for r in rows:
        if r.at is None:
            out.append(r)
            continue
        governing = in_force(schedules, r.salt, r.at)
        if governing is not None and abs(governing.rate_for(r.lesson) - r.rate) > 1e-9:
            out.append(r)
    return out
