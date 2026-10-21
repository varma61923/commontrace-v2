from __future__ import annotations

import datetime

DEFAULT_HALF_LIFE_DAYS = 180.0

NEVER_HIT_ADJUSTMENT = -1.0


def _parse_last_hit(value: object) -> datetime.datetime | None:
    text = str(value or "").strip()
    if not text or text.upper() == "NEVER":
        return None
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def adjustment(
    last_hit: object,
    *,
    now: datetime.datetime | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    hit = _parse_last_hit(last_hit)
    if hit is None:
        return NEVER_HIT_ADJUSTMENT
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    age_days = max(0.0, (now - hit).total_seconds() / 86400.0)
    if half_life_days <= 0:
        return 1.0 if age_days == 0 else NEVER_HIT_ADJUSTMENT
    decayed = 2.0 ** (-age_days / half_life_days)
    scaled = 2.0 * decayed - 1.0
    return max(NEVER_HIT_ADJUSTMENT, scaled)


def recency_lookup(
    lessons: list[tuple[str, dict]],
    *,
    now: datetime.datetime | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> dict[str, float]:
    return {
        str(fm.get("name", "")): adjustment(
            fm.get("last_hit"), now=now, half_life_days=half_life_days,
        )
        for _path, fm in lessons
    }
