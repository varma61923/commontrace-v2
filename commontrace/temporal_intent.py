"""Conservative query-time hints; ambiguous dates never silently pick a past state."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


def resolve(query: str, *, now: datetime | None = None) -> dict:
    reference = now or datetime.now(timezone.utc)
    text = query.casefold()
    explicit = re.search(r"\b(?:as of|on|at)\s+(\d{4}-\d{2}-\d{2})(?:\b|$)", text)
    if explicit:
        moment = datetime.fromisoformat(explicit.group(1)).replace(tzinfo=timezone.utc)
        return {"mode": "historical" if moment < reference else "future", "as_of": moment.isoformat()}
    for phrase, offset in (("yesterday", -1), ("last week", -7), ("tomorrow", 1), ("next week", 7)):
        if re.search(r"\b"+phrase+r"\b", text):
            return {"mode": "historical" if offset < 0 else "future",
                    "as_of": (reference+timedelta(days=offset)).isoformat()}
    if re.search(r"\b(current|currently|now|today|latest)\b", text):
        return {"mode": "current", "as_of": None}
    if re.search(r"\b(upcoming|future|planned)\b", text):
        return {"mode": "future", "as_of": None}
    return {"mode": "unspecified", "as_of": None}
