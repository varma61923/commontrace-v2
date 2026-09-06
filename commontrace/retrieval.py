"""Pure-Python lexical retrieval: the local-tier "Reinject the right lesson"
mechanism that works with only the core install (PyYAML, no numpy/
sentence-transformers). protocol/PROTOCOL.md §5 says "Local tier remains
file-based for agents that can only read/write files" -- before this module
existed, `commontrace query` *required* the optional `attention` extra and
simply failed (return 1, "go grep the files yourself") without it, which
made that claim aspirational rather than true. This is not a semantic
retriever -- it is a deliberately simple, honest lexical ranker over
description/applies_when/tags/domain, a fallback tier beneath the optional
semantic attention layer, not a replacement for it.

It is, however, a *field-agnostic* one. Scoring is IDF-weighted and
length-normalized (see `rank_lessons`) rather than raw word-overlap, because
raw overlap is not comparable across fields: a legal store's lessons are
wordier and share more boilerplate than a coding store's, so an unnormalized
sum rewards whichever field writes more, and the hardcoded English stopword
list in commontrace/_lexical.py cannot strip a field's own boilerplate. IDF
derived from the store's own corpus can, with nothing hardcoded per field.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from commontrace._lexical import STOPWORDS as _STOPWORDS
from commontrace._lexical import WORD_RE as _WORD_RE

# The relevance floor a lesson must clear to be retrieved at all, when a store
# has not configured its own (commontrace/retrieval_io.py). See `rank_lessons`
# below for what the number means.
#
# Chosen as the largest value that costs NO recall, measured over a six-field
# corpus (coding, HR, sales, marketing, robotics, legal; 36 lessons, 108
# labelled queries -- the fixture `commontrace bench --retrieval` runs):
#
#   floor   top-1     recall@3   collateral retrievals   worst field
#   0.00    106/108   108/108    128                     legal=27
#   0.10    106/108   108/108     20                     marketing=5   <- here
#   0.12    105/108   106/108     14
#   0.16    101/108   101/108      4
#   0.25     91/108    91/108      0
#
# 0.10 removes 84% of the collateral retrievals -- lessons pulled into
# unrelated tasks on an incidental word -- while matching the no-floor
# ranking exactly on every labelled query. Those collateral retrievals are
# not merely noise in the output: under `--experiment` each one is logged as
# an eligible assignment, so unrelated tasks' outcomes are attributed to a
# lesson that had nothing to do with them (commontrace/integrity.py's
# check_assignment_concentration). Raising the floor further does buy fewer
# still, but starts costing real hits, and a lesson that never reaches the
# agent cannot help it.
DEFAULT_FLOOR = 0.10

# Scorer identities, recorded on every holdout assignment so an experiment can
# never silently pool occasions scored two different ways.
SCORER_IDF = "idf-v2"
SCORER_COUNT = "count-v1"

# BM25's length-normalization strength. 0 disables it; 1 normalizes fully.
_LENGTH_B = 0.5
_LENGTH_CLAMP = (0.5, 1.5)

# \d is excluded from the strip below only via the stopword/length filters,
# same as before. See commontrace/_lexical.py for why \w/re.UNICODE and this
# exact stopword list: non-English text and the historical drift-between-
# copies bug this replaced.


def _tokenize(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1]


@dataclass(frozen=True)
class RankedLesson:
    path: str
    slug: str
    description: str
    score: float
    matched_terms: list[str]
    # Normalized to [0, 1] and comparable ACROSS stores and fields, which the
    # raw additive `score` is not (see `rank_lessons`). This is what the
    # retrieval floor gates on and what gets recorded alongside each holdout
    # assignment, so `commontrace experiment` can tell a lesson that was
    # squarely on-topic from one that scraped in on an incidental word.
    relevance: float = 0.0
    # Which scorer produced `relevance`. An experiment that pooled occasions
    # from two scorers would be comparing two different treatments.
    scorer: str = SCORER_IDF


def _lesson_text_weighted(fm: dict) -> list[tuple[str, float]]:
    """(field text, weight) pairs -- tags and applies_when are the strongest
    activation-condition signal, description is a secondary summary."""
    tags = fm.get("tags")
    tags_list = [str(t) for t in tags if t is not None] if isinstance(tags, (list, tuple)) else []
    # `or ""`, not a bare `.get(..., "")`: the default only applies when the
    # KEY is absent, so a hand-edited `description:` (key present, value
    # None) returned None from .get, and str(None) == "None" -- so a query
    # containing the word "none" spuriously matched every lesson with an
    # empty description/applies_when/domain field, via a token that field
    # never actually contained.
    return [
        (str(fm.get("description") or ""), 1.0),
        (str(fm.get("applies_when") or ""), 1.5),
        (" ".join(tags_list), 2.0),
        (str(fm.get("domain") or ""), 1.0),
    ]


# The strongest weight above. `relevance` divides by it so a term matched at
# the best possible position contributes its full IDF and nothing more --
# which is what keeps the ratio inside [0, 1].
_MAX_FIELD_WEIGHT = 2.0


def _idf(n_docs: int, doc_freq: int) -> float:
    """BM25's non-negative IDF.

    THIS is what makes lexical retrieval field-agnostic. The alternative --
    the hardcoded English stopword list in commontrace/_lexical.py -- cannot
    generalize to a taxonomy that is open by design: it strips "the" and
    "with", but not `pursuant`/`hereto`/`whereas` in a legal store, or
    `node`/`frame`/`topic` in a robotics one. Those behave as that field's
    OWN stopwords: near-universal, and therefore worthless for telling two of
    its lessons apart, while contributing as much raw score as the one term
    that actually discriminates.

    Derived per store from the store's own lessons, so each field learns its
    own low-information vocabulary with nothing hardcoded. The +1 inside the
    log keeps this >= 0: the classic Robertson-Sparck-Jones form goes negative
    for a term in more than half the corpus, which in a six-lesson store would
    let a common term actively subtract from an otherwise good match.
    """
    return math.log(1.0 + (n_docs - doc_freq + 0.5) / (doc_freq + 0.5))


def _length_factor(n_terms: int, avg_terms: float) -> float:
    """Discount a field for being longer than that field usually is.

    Without this, breadth beats precision: a lesson with 30 tags gets 30
    independent chances to match at the tag weight, against a 3-tag lesson's
    3, so tag-stuffing outranks being right. That bias is not uniform across
    fields -- legal and robotics lessons are simply wordier than coding ones,
    so an unnormalized score systematically favours whichever field writes
    more, which is exactly the cross-field comparison this product needs to
    make.
    """
    if avg_terms <= 0:
        return 1.0
    raw = 1.0 / (1.0 + _LENGTH_B * ((n_terms / avg_terms) - 1.0))
    low, high = _LENGTH_CLAMP
    return max(low, min(high, raw))


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
    floor: float | None = None,
    scorer: str = SCORER_IDF,
) -> list[RankedLesson]:
    """Rank `lessons` -- (path, frontmatter) pairs the caller has already
    filtered to what it considers eligible (e.g. status == "active") -- by how
    much of `task`'s information each one covers.

    Each result carries a `relevance` in [0, 1]:

        relevance(L) = SUM over matched t of
                           idf(t) * (best_field_weight(t) / 2.0) * length_factor(L)
                       ------------------------------------------------------------
                           SUM over query terms t present in the corpus of idf(t)

    Read it as: *what fraction of the query's information does this lesson
    cover, weighted by where it matched and discounted for verbosity.* Being
    a ratio of information covered to information available, it is
    dimensionless and invariant to both query length and field verbosity --
    which is what makes one shared `floor` meaningful across a terse coding
    store and a wordy legal one. The previous raw additive score was neither,
    so `score > 0` was the only gate that could be written, and every lesson
    sharing one incidental word with the query was retrieved.

    Only lessons at or above `floor` are returned. `floor=None` uses
    DEFAULT_FLOOR; pass 0.0 to disable the gate.

    Ties break toward higher `importance` (a lesson explicitly marked more
    consequential wins), then toward more prior `uses`.

    `scorer=SCORER_COUNT` restores the historical raw-additive behaviour
    exactly, for a store mid-experiment that must not have its lesson
    eligibility re-randomized under it (commontrace/retrieval_io.py).
    """
    query_terms = set(_tokenize(task))
    if not query_terms:
        return []
    if floor is None:
        floor = 0.0 if scorer == SCORER_COUNT else DEFAULT_FLOOR

    # Corpus statistics over the candidate set: document frequency per term,
    # and mean term count per weighted field. Computed here rather than
    # cached because `lessons` is a store's active set (tens to low hundreds)
    # and it must reflect the store as it is right now -- a stale IDF table
    # would rank against a corpus that no longer exists.
    field_terms_by_lesson: list[list[tuple[set[str], float]]] = []
    doc_freq: dict[str, int] = {}
    field_len_totals: list[float] = []
    for _path, fm in lessons:
        per_field: list[tuple[set[str], float]] = []
        seen_in_doc: set[str] = set()
        for field_text, weight in _lesson_text_weighted(fm):
            terms = set(_tokenize(field_text))
            per_field.append((terms, weight))
            seen_in_doc |= terms
        for term in seen_in_doc:
            doc_freq[term] = doc_freq.get(term, 0) + 1
        field_terms_by_lesson.append(per_field)
        field_len_totals.append(float(len(seen_in_doc)))

    n_docs = len(lessons)
    avg_field_len = (sum(field_len_totals) / len(field_len_totals)) if field_len_totals else 0.0

    # The denominator: the query's total information content -- over EVERY
    # query term, not just the ones some lesson happens to contain.
    #
    # Counting only corpus-present terms collapses the denominator onto the
    # numerator: a query sharing one incidental word with a small store
    # ("... instead of ...") had 100% of its *matchable* information covered
    # and scored 0.50, which is precisely the marginal match the floor exists
    # to reject. Worst in a new fleet's small store, which is the store least
    # able to absorb a bad injection.
    #
    # A term no lesson contains is smoothed to the corpus mean rather than to
    # _idf(n, 0): df=0's natural IDF is the largest value in the scale, so a
    # single unfamiliar proper noun would swamp the denominator and reject
    # every lesson. "Averagely informative, and unmatched" is the honest
    # reading of a term the store has never seen.
    query_idf = {
        t: _idf(n_docs, doc_freq[t]) for t in query_terms if doc_freq.get(t, 0) > 0
    }
    corpus_idfs = [_idf(n_docs, df) for df in doc_freq.values()]
    # The reference scale is the most informative term the corpus contains,
    # not the query's own total. Dividing by the query's own IDF sum makes the
    # measure self-normalizing, which hides exactly what IDF is for: a query
    # made entirely of a field's boilerplate ("node" in a robotics store) has
    # 100% of its own information covered by every lesson, and scored ~1.0.
    # Against an absolute reference it scores what it is worth -- near zero --
    # while a discriminating match still reaches the top of the scale.
    max_idf = max(corpus_idfs) if corpus_idfs else 0.0
    total_query_idf = len(query_terms) * max_idf

    # (RankedLesson, importance, uses) rather than a separate by_slug dict
    # keyed on fm.get("name"): two lessons with the same (or both missing/
    # empty) `name` collided in that dict, so the tie-break silently used
    # the WRONG lesson's importance/uses for one of them. Carrying each
    # lesson's own sort fields alongside it here instead ties them to the
    # specific (path, fm) pair that produced them, not to a name that may
    # not be unique.
    scored: list[tuple[RankedLesson, int, int]] = []
    for (path, fm), per_field in zip(lessons, field_terms_by_lesson):
        score = 0.0
        matched: set[str] = set()
        # Best field weight per matched term, so a term repeated across four
        # fields is credited once at its strongest position rather than four
        # times -- the other half of the breadth-beats-precision problem.
        best_weight: dict[str, float] = {}
        for terms, weight in per_field:
            hits = query_terms & terms
            if not hits:
                continue
            score += weight * len(hits)
            matched |= hits
            for term in hits:
                if weight > best_weight.get(term, 0.0):
                    best_weight[term] = weight

        if scorer == SCORER_COUNT:
            rel = score
        elif total_query_idf > 0 and matched:
            lam = _length_factor(len(set().union(*(t for t, _w in per_field))), avg_field_len)
            covered = sum(
                query_idf.get(term, 0.0) * (best_weight[term] / _MAX_FIELD_WEIGHT)
                for term in matched
            )
            # Clamped: `lam` may exceed 1.0 for a lesson shorter than average
            # (a deliberate small boost), and the bound this measure documents
            # -- and that the shared floor depends on -- must hold regardless.
            rel = min(1.0, (covered * lam) / total_query_idf)
        else:
            rel = 0.0

        if score > 0 and rel >= floor:
            scored.append((
                RankedLesson(
                    path=path,
                    slug=str(fm.get("name", "")),
                    description=str(fm.get("description", "")),
                    score=score,
                    matched_terms=sorted(matched),
                    relevance=round(rel, 6),
                    scorer=scorer,
                ),
                _rank_int(fm.get("importance", 0)),
                _rank_int(fm.get("uses", 0)),
            ))

    scored.sort(key=lambda item: (item[0].relevance, item[0].score, item[1], item[2]), reverse=True)
    # max(0, ...): a plain `scored[:top_k]` on a negative top_k is a Python
    # slice, not a bounds check -- `scored[:-1]` means "all but the last
    # item", not "nothing", so a negative top_k silently returned nearly
    # the whole ranked list instead of failing. query_cmd.py's CLI already
    # rejects a negative --top-k before it reaches here; this clamp is the
    # same guarantee for any other caller of this function directly.
    return [lesson for lesson, _, _ in scored[: max(0, top_k)]]
