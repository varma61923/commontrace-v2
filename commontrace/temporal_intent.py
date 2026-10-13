"""Conservative query-time hints; ambiguous dates never silently pick a past state."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from commontrace.lesson_cache import parse_moment

# Preserve complete timestamps and their offsets instead of truncating to a date.
_EXPLICIT = re.compile(
    r"\b(?:as of|on|at)\s+(\d{4}-\d{2}-\d{2}"
    r"(?:[t ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:z|[+-]\d{2}:?\d{2})?)?)(?![\w:+-]|\.\d)",
    re.IGNORECASE,
)
_RELATIVE = (("yesterday", -1), ("last week", -7), ("tomorrow", 1), ("next week", 7), ("today", 0), ("now", 0))


def resolve(query: str, *, now: datetime | None = None) -> dict:
    reference = parse_moment(now or datetime.now(timezone.utc))
    text = query.casefold()
    explicit = {parse_moment(match.group(1).upper()) for match in _EXPLICIT.finditer(text)}
    if len(explicit) > 1:
        return {"mode": "unspecified", "as_of": None}
    if explicit:
        moment = next(iter(explicit))
        return {"mode": "historical" if moment < reference else "future", "as_of": moment.isoformat()}
    offsets = {offset for phrase, offset in _RELATIVE if re.search(r"\b" + phrase + r"\b", text)}
    if len(offsets) > 1:
        return {"mode": "unspecified", "as_of": None}
    if offsets:
        offset = next(iter(offsets))
        if offset == 0:
            return {"mode": "current", "as_of": None}
        return {"mode": "historical" if offset < 0 else "future",
                "as_of": (reference + timedelta(days=offset)).isoformat()}
    if re.search(r"\b(current|currently|now|today|latest)\b", text):
        return {"mode": "current", "as_of": None}
    if re.search(r"\b(upcoming|future|planned)\b", text):
        return {"mode": "future", "as_of": None}
    return {"mode": "unspecified", "as_of": None}
