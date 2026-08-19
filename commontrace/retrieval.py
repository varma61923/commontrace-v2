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

_WORD_RE = re.compile(r"[a-z0-9]+")

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
    return [
        (str(fm.get("description", "")), 1.0),
        (str(fm.get("applies_when", "")), 1.5),
        (" ".join(fm.get("tags") or []), 2.0),
        (str(fm.get("domain", "")), 1.0),
    ]


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

    ranked: list[RankedLesson] = []
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
            ranked.append(
                RankedLesson(
                    path=path,
                    slug=str(fm.get("name", "")),
                    description=str(fm.get("description", "")),
                    score=score,
                    matched_terms=sorted(matched),
                )
            )

    by_slug = {str(fm.get("name", "")): fm for _, fm in lessons}
    ranked.sort(
        key=lambda r: (
            r.score,
            by_slug.get(r.slug, {}).get("importance", 0) or 0,
            by_slug.get(r.slug, {}).get("uses", 0) or 0,
        ),
        reverse=True,
    )
    return ranked[:top_k]
