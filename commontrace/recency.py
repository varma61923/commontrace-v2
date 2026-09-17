"""How stale is this lesson's evidence? -- a ranking signal, not a lifecycle
one.

WHY THIS EXISTS
---------------
A lesson's `importance` is a human's static, one-time judgment and `uses` is
a monotonically increasing counter -- neither says whether the lesson has
actually mattered *lately*. Two lessons that tie on topical relevance
(`commontrace/retrieval.py`'s `rank_lessons`) are not equally trustworthy if
one was last useful yesterday and the other has not been touched in two
years: the world the second one was written for may no longer exist.

This is deliberately narrower than `commontrace/decay.py`, which ages out a
lesson's *billing* evidence on a value-ledger surface
(`commontrace/value.py`) with its own asymmetric HELPS/HURTS handling. That
module answers "is this number still honest to invoice on"; this one answers
a purely ordinal ranking question -- among lessons already past the
relevance floor, which one's evidence is fresher. The two are independent:
a lesson can be recency-stale for ranking purposes while its value-ledger
verdict is untouched, and vice versa.

WHAT SIGNAL THIS READS
-----------------------
`last_hit`, the frontmatter field every lesson schema already requires
(`protocol/schemas/lesson.schema.json`), starting at `"NEVER"`
(`commontrace/templates.py`) and updated when a lesson is retrieved and
credited with helping. Not the revision journal
(`commontrace/lesson_io.py`): a lesson can be re-validated by being useful
again without its TEXT changing at all, which is exactly the case this
module exists to reward, and the journal only records content changes.

NOTHING HERE CHANGES ELIGIBILITY
---------------------------------
`retrieval.py`'s relevance floor is computed before this module is ever
consulted, and continues to gate purely on topical match. This produces an
`adjustment` in [-1, 1] that only ever changes ORDER among lessons already
past that floor, applied only when a store opts in
(`commontrace/retrieval_io.py`'s `recency_weight`, default 0.0 -- see that
module's docstring on why opting in is a decision, not an upgrade side
effect).
"""

from __future__ import annotations

import datetime

#: The age at which `adjustment` crosses zero -- older than this and a
#: lesson's recency adjustment goes negative; fresher, positive. 180 days
#: matches the order of magnitude `commontrace/decay.py` uses for its own
#: "how long is evidence worth trusting" question on the value-ledger
#: surface, which is not the same question but is the same kind of question,
#: and a fleet's intuition for "stale" should not have to differ between the
#: two.
DEFAULT_HALF_LIFE_DAYS = 180.0

#: What a lesson that has never been hit gets. The same magnitude a HARMFUL
#: reliability verdict gets (commontrace/reliability.py's
#: `_VERDICT_ADJUSTMENT`) -- "we have no evidence this still matters" and
#: "this measurably hurts" are both reasons an otherwise-tied lesson should
#: rank behind one with a real, recent signal, and age alone (no matter how
#: old) is never treated as WORSE than never having been validated at all --
#: see `adjustment`'s floor.
NEVER_HIT_ADJUSTMENT = -1.0


def _parse_last_hit(value: object) -> datetime.datetime | None:
    """Parse a `last_hit` frontmatter value, tolerant of a hand-edited file.

    Returns None for `"NEVER"`, empty, or anything unparseable -- lessons
    are explicitly meant to be hand-edited
    (`commontrace/frontmatter.py`) and are not schema-validated before
    reaching this function, so a malformed date must fall back rather than
    raise: `retrieval.rank_lessons`'s own `_rank_int` takes the identical
    posture for a hand-edited `importance`/`uses`.
    """
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
    """One lesson's recency adjustment in [-1, 1], for
    `retrieval.rank_lessons`'s optional `recency_lookup`.

    Exponential decay, not a linear countdown: a lesson hit yesterday and one
    hit last week should be nearly indistinguishable, while a year of
    silence should matter much more than the raw day count alone suggests.

    A lesson hit right now scores +1.0. One exactly `half_life_days` old
    scores 0.0. Older still, negative, floored at `NEVER_HIT_ADJUSTMENT` so
    age alone is never scored worse than never having been validated at all
    -- an old-but-once-proven lesson and a never-tried one are different
    situations, and the first should not be punished into looking like the
    second.
    """
    hit = _parse_last_hit(last_hit)
    if hit is None:
        return NEVER_HIT_ADJUSTMENT
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    age_days = max(0.0, (now - hit).total_seconds() / 86400.0)
    if half_life_days <= 0:
        return 1.0 if age_days == 0 else NEVER_HIT_ADJUSTMENT
    # Halves every half_life_days; rescaled from (0, 1] to [-1, 1] so 0.0
    # lands at exactly one half-life, matching the docstring above.
    decayed = 2.0 ** (-age_days / half_life_days)
    scaled = 2.0 * decayed - 1.0
    return max(NEVER_HIT_ADJUSTMENT, scaled)


def recency_lookup(
    lessons: list[tuple[str, dict]],
    *,
    now: datetime.datetime | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> dict[str, float]:
    """slug -> recency adjustment, for every lesson in `lessons` -- the same
    (path, frontmatter) pairs `retrieval.rank_lessons` is called with, so a
    caller can build this with no extra file I/O beyond what retrieval
    already pays for."""
    return {
        str(fm.get("name", "")): adjustment(
            fm.get("last_hit"), now=now, half_life_days=half_life_days,
        )
        for _path, fm in lessons
    }
