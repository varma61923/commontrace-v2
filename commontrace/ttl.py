"""Per-lesson TTL expiry and fact forget/expire accounting.

Single home for ``expires`` (lesson frontmatter) and ``expires_at`` /
``forgotten`` (atomic fact JSONL) semantics, so ``lesson_cache``,
``hierarchical``, ``query_cmd`` (hidden-count notice) and ``doctor``
(counts) agree with each other.

Stdlib only: every other commontrace module may import this one without
creating an import cycle.
"""
from __future__ import annotations

import datetime
from typing import Any

#: The optional lesson frontmatter field holding an ISO-8601 instant after
#: which the lesson is expired (exclusive bound: moment >= expires means
#: expired, mirroring ``valid_until`` semantics).
EXPIRES_FIELD = "expires"


def parse_expiry(value: str | datetime.date | datetime.datetime) -> datetime.datetime:
    """Parse an ``expires``/``expires_at`` value into an aware UTC datetime.

    Raises ``ValueError`` on empty or unparseable input.
    """
    if isinstance(value, datetime.datetime):
        parsed = value
    elif isinstance(value, datetime.date):
        parsed = datetime.datetime.combine(value, datetime.time.min)
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("an expiry date/time is required")
        try:
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"could not parse {value!r} as a date/time; use YYYY-MM-DD or ISO 8601"
            ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _moment(as_of: str | datetime.date | datetime.datetime | None) -> datetime.datetime:
    if as_of is None:
        return datetime.datetime.now(datetime.timezone.utc)
    if isinstance(as_of, datetime.datetime):
        moment = as_of
    elif isinstance(as_of, datetime.date):
        moment = datetime.datetime.combine(as_of, datetime.time.min)
    else:
        moment = parse_expiry(as_of)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc)


def lesson_is_expired(
    fm: dict[str, Any], as_of: str | datetime.date | datetime.datetime | None = None,
) -> bool:
    """True when a lesson's ``expires`` instant has passed.

    Lessons without ``expires`` never expire here. An unparseable ``expires``
    value counts as expired (fail-closed), consistent with how
    ``lesson_cache.filter_eligible`` treats unparseable ``valid_from`` /
    ``valid_until`` (the lesson is withheld, and ``frontmatter.validate_expires``
    explains how to fix the value).
    """
    raw = fm.get(EXPIRES_FIELD)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return False
    try:
        return _moment(as_of) >= parse_expiry(raw)
    except ValueError:
        return True


def count_expired(
    lessons: list[tuple[str, dict[str, Any]]],
    as_of: str | datetime.date | datetime.datetime | None = None,
) -> int:
    """How many of these lessons are expired at ``as_of`` (default: now)."""
    moment = _moment(as_of)
    return sum(1 for _path, fm in lessons if lesson_is_expired(fm, moment))


def partition_expired(
    lessons: list[tuple[str, dict[str, Any]]],
    as_of: str | datetime.date | datetime.datetime | None = None,
) -> tuple[list[tuple[str, dict[str, Any]]], list[tuple[str, dict[str, Any]]]]:
    """Split lessons into (live, expired) at ``as_of`` (default: now)."""
    moment = _moment(as_of)
    live, expired = [], []
    for item in lessons:
        (expired if lesson_is_expired(item[1], moment) else live).append(item)
    return live, expired


def fact_is_expired(
    fact: Any, as_of: str | datetime.date | datetime.datetime | None = None,
) -> bool:
    """True when a fact's ``expires_at`` instant has passed.

    Facts without ``expires_at`` never expire here. Unlike lessons, an
    unparseable ``expires_at`` counts as *not* expired (fail-open): expiry
    does not gate fact listing, so a typo must not silently reclassify a
    fact -- ``add_fact``/``update_fact`` validate strictly at write time
    instead.
    """
    raw = getattr(fact, "expires_at", None)
    if isinstance(fact, dict):
        raw = fact.get("expires_at")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return False
    try:
        return _moment(as_of) >= parse_expiry(raw)
    except ValueError:
        return False


def _is_forgotten(fact: Any) -> bool:
    if isinstance(fact, dict):
        return bool(fact.get("forgotten"))
    return bool(getattr(fact, "forgotten", False))


def summarize(
    lessons: list[tuple[str, dict[str, Any]]],
    facts: list[Any],
    as_of: str | datetime.date | datetime.datetime | None = None,
) -> dict[str, int]:
    """Small count bundle for ``doctor`` and query notices.

    Takes already-loaded data (not a store root) so this module stays
    dependency-free; callers load via ``lesson_cache`` / ``hierarchical``.
    """
    moment = _moment(as_of)
    fact_list = list(facts)
    return {
        "total_lessons": len(lessons),
        "expired_lessons": sum(1 for _p, fm in lessons if lesson_is_expired(fm, moment)),
        "total_facts": len(fact_list),
        "forgotten_facts": sum(1 for f in fact_list if _is_forgotten(f)),
        "expired_facts": sum(1 for f in fact_list if fact_is_expired(f, moment)),
    }


def format_notice(hidden: int) -> str:
    """The one-line query notice reporting TTL-hidden lessons."""
    noun = "lesson" if hidden == 1 else "lessons"
    return (
        f"[commontrace] {hidden} expired {noun} hidden by TTL; "
        "re-run with --show-expired to include them."
    )
