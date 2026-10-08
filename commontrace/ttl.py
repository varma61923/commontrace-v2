"""Per-lesson TTL expiry and fact forget/expire accounting."""
from __future__ import annotations

import datetime
from typing import Any

EXPIRES_FIELD = "expires"


def expiry_for_type(memory_type: str, *, valid_from: str | None = None,
                    ttl_hours: float | None = None) -> str | None:
    """An explicit temporary assertion expires automatically; stable facts do not.

    This is a write-time default, not a retrospective rewrite of old records.
    Unknown/general types require an explicit TTL rather than guessing retention.
    """
    import math

    hours = ttl_hours if ttl_hours is not None else {"temporary": 24.0, "environment": 720.0}.get(memory_type)
    if hours is None:
        return None
    if not math.isfinite(hours) or hours <= 0:
        raise ValueError("TTL hours must be finite and positive")
    start = _moment(valid_from)
    return (start + datetime.timedelta(hours=hours)).isoformat()


def parse_expiry(value: str | datetime.date | datetime.datetime) -> datetime.datetime:
    """Parse an ``expires``/``expires_at`` value into an aware UTC datetime."""
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
    """True when a lesson's ``expires`` instant has passed."""
    raw = fm.get(EXPIRES_FIELD)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return False
    try:
        return _moment(as_of) >= parse_expiry(raw)
    except ValueError:
        return True


def trace_is_live(trace: dict[str, Any], as_of=None) -> bool:
    """Trace evidence shares expiry aliases and inclusive/exclusive validity gates."""
    moment = _moment(as_of)
    try:
        for field in ("expires", "expires_at", "valid_until"):
            value = trace.get(field)
            if value is not None and value != "" and moment >= parse_expiry(value):
                return False
        start = trace.get("valid_from")
        if start is not None and start != "" and moment < parse_expiry(start):
            return False
    except (ValueError, TypeError):
        return False
    return True


def count_expired(
    lessons: list[tuple[str, dict[str, Any]]],
    as_of: str | datetime.date | datetime.datetime | None = None,
) -> int:
    """How many of these lessons are expired at ``as_of`` (default: now)."""
    moment = _moment(as_of)
    return sum(1 for _path, fm in lessons if lesson_is_expired(fm, moment))


def fact_is_expired(
    fact: Any, as_of: str | datetime.date | datetime.datetime | None = None,
) -> bool:
    """True when a fact's ``expires_at`` instant has passed."""
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
    """Small count bundle for ``doctor`` and query notices."""
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
