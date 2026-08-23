"""Pure-Python lexical retrieval: the local-tier "Reinject the right lesson"
mechanism that works with only the core install (PyYAML, no numpy/
sentence-transformers). protocol/PROTOCOL.md §5 says "Local tier remains
file-based for agents that can only read/write files" -- before this module
existed, `commontrace query` *required* the optional `attention` extra and
simply failed (return 1, "go grep the files yourself") without it, which
made that claim aspirational rather than true. This is not a semantic
retriever -- it is a deliberately simple, honest lexical ranker (word-overlap
scoring over description/applies_when/tags), a fallback tier beneath the
optional semantic attention layer, not a replacement for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# \w with re.UNICODE, not [a-z0-9]: the ASCII-only class silently mutilates
# any non-English text. "résumé" tokenized to ['sum'] (the accented letters
# split the word and the fragments were dropped by the len>1 filter), and
# CJK/Cyrillic/Arabic text tokenized to nothing at all -- so a fleet working
# in any of those got zero lexical retrieval with no error to explain it.
# \d is excluded from the strip below only via the stopword/length filters,
# same as before.
_WORD_RE = re.compile(r"\w+", re.UNICODE)

# Cheap English stopword list -- filtering these out of both the task query
# and lesson text keeps scores from being dominated by words that carry no
# discriminating signal ("the", "to", "a", ...).
_STOPWORDS = frozenset(
    """
    a an the of to in on for with and or but is are was were be been being
    this that these those it its as at by from into over under again
    further then once here there when where why how all any both each
    few more most other some such no nor not only own same so than too
    very can will just don should now i you he she we they them his her
    """.split()
)


def _tokenize(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1]


@dataclass(frozen=True)
class RankedLesson:
    path: str
    slug: str
    description: str
    score: float
    matched_terms: list[str]


def _lesson_text_weighted(fm: dict) -> list[tuple[str, float]]:
    """(field text, weight) pairs -- tags and applies_when are the strongest
    activation-condition signal, description is a secondary summary."""
    tags = fm.get("tags")
    tags_list = [str(t) for t in tags if t is not None] if isinstance(tags, (list, tuple)) else []
    return [
        (str(fm.get("description", "")), 1.0),
        (str(fm.get("applies_when", "")), 1.5),
        (" ".join(tags_list), 2.0),
        (str(fm.get("domain", "")), 1.0),
    ]


def _rank_int(value: Any) -> int:
    """Coerce a frontmatter field the schema declares as an integer
    (importance, uses), tolerating a hand-edited file where it isn't one.
    rank_lessons's sort key compares this across every ranked lesson in
    the same tuple position, and Python raises TypeError comparing e.g.
    int and str there -- so one lesson with `importance: high` (a string,
    not the schema's 1-5 integer) used to crash ranking for every lesson,
    not just the malformed one. lesson files are explicitly meant to be
    hand-edited (see commontrace/frontmatter.py) and are not schema
    validated before reaching this function."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def rank_lessons(
    task: str,
    lessons: list[tuple[str, dict]],
    top_k: int = 10,
) -> list[RankedLesson]:
    """`lessons` is a list of (path, frontmatter_dict) pairs, already filtered
    to whatever the caller considers eligible (e.g. status == "active"). Pure
    word-overlap scoring: for each lesson, sum the field weight for every
    query token that appears in that field at least once. Ties break toward
    higher `importance` (a lesson explicitly marked more consequential wins
    a tie), then toward more prior `uses` (empirically useful lessons win
    the next tie)."""
    query_terms = set(_tokenize(task))
    if not query_terms:
        return []

    # (RankedLesson, importance, uses) rather than a separate by_slug dict
    # keyed on fm.get("name"): two lessons with the same (or both missing/
    # empty) `name` collided in that dict, so the tie-break silently used
    # the WRONG lesson's importance/uses for one of them. Carrying each
    # lesson's own sort fields alongside it here instead ties them to the
    # specific (path, fm) pair that produced them, not to a name that may
    # not be unique.
    scored: list[tuple[RankedLesson, int, int]] = []
    for path, fm in lessons:
        score = 0.0
        matched: set[str] = set()
        for field_text, weight in _lesson_text_weighted(fm):
            field_terms = set(_tokenize(field_text))
            hits = query_terms & field_terms
            if hits:
                score += weight * len(hits)
                matched |= hits
        if score > 0:
            scored.append((
                RankedLesson(
                    path=path,
                    slug=str(fm.get("name", "")),
                    description=str(fm.get("description", "")),
                    score=score,
                    matched_terms=sorted(matched),
                ),
                _rank_int(fm.get("importance", 0)),
                _rank_int(fm.get("uses", 0)),
            ))

    scored.sort(key=lambda item: (item[0].score, item[1], item[2]), reverse=True)
    # max(0, ...): a plain `scored[:top_k]` on a negative top_k is a Python
    # slice, not a bounds check -- `scored[:-1]` means "all but the last
    # item", not "nothing", so a negative top_k silently returned nearly
    # the whole ranked list instead of failing. query_cmd.py's CLI already
    # rejects a negative --top-k before it reaches here; this clamp is the
    # same guarantee for any other caller of this function directly.
    return [lesson for lesson, _, _ in scored[: max(0, top_k)]]
