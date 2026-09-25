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

import heapq
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from commontrace._lexical import STOPWORDS as _STOPWORDS
from commontrace._lexical import WORD_RE as _WORD_RE
from commontrace._stem import stem as _stem

# The relevance floor a lesson must clear to be retrieved at all, when a store
# has not configured its own (commontrace/retrieval_io.py). See `rank_lessons`
# below for what the number means.
#
# This was originally tuned against a single corpus -- the six-field fixture
# below -- where 0.10 looked free: it removed 84% of collateral retrievals at
# no cost to recall or top-1. It is not free everywhere. commons/eval/ is a
# second, independently-authored corpus (46 lessons, longer and more
# naturalistic query text, no shared authorship with the fixture below) that
# hub/tests/test_commons.py's TestRetrievalTiersDiffer pins specific
# thresholds against: at floor=0.10, recall_anywhere on that corpus fell to
# ~83% (below the pinned >90% bar) and results for negative-control probes
# nearly vanished. A floor tuned on one corpus alone was silently overfit to
# it.
#
# 0.04 is the largest value that keeps BOTH corpora's existing thresholds
# intact, measured with `commontrace/reference/measure_retrieval.py` (the
# six-field fixture) and `commons/eval/retrieval_tiers.py` (the 46-lesson
# corpus) together:
#
#   floor   commons/eval           six-field fixture (worst field, pollution)
#           recall_anywhere        legal   sales   marketing  coding  hr   robotics
#   0.00    ~93%                   2.50x   2.39x   2.00x      2.00x   2.33x  1.89x
#   0.04    91.3%          <- here 2.33x   2.11x   2.00x      1.94x   1.83x  1.72x
#   0.10    ~83%                   1.22x   1.22x   1.28x      1.22x   1.17x  1.00x
#
# So this is a real tension, not a free lunch: 0.04 buys noticeably less
# pollution reduction on the six-field fixture than 0.10 did (worst field
# 2.50x -> 2.33x, versus 0.10's 2.50x -> 1.22x), in exchange for not
# regressing commons/eval's recall. Recall was prioritized over pollution
# here because a lesson that never reaches the agent cannot help it, while
# residual pollution at this floor is still caught by a second layer of
# defense -- commontrace/integrity.py's check_marginal_eligibility and
# check_assignment_concentration -- which flags a lesson whose assignments
# are disproportionately marginal-relevance or concentrated, rather than
# silently pooling them into the causal estimate. See
# tests/test_cross_field_retrieval.py for the CI gate this corresponds to.
IDF_V2_FLOOR = 0.04

# Scorer identities, recorded on every holdout assignment so an experiment can
# never silently pool occasions scored two different ways.
#
# idf-v3 is idf-v2 with every term Porter-stemmed (commontrace/_stem.py), so
# "reset", "resets" and "resetting" are one term. OPT-IN, not the default:
# see IDF_V3_FLOOR below for what it buys and the one place it costs. A
# scorer is recorded on every holdout assignment, so a store can switch
# only by starting a new randomization, never mid-experiment.
SCORER_IDF_V3 = "idf-v3"
SCORER_IDF_V2 = "idf-v2"
SCORER_IDF = SCORER_IDF_V2
SCORER_COUNT = "count-v1"
LEXICAL_SCORERS = (SCORER_IDF_V3, SCORER_IDF_V2, SCORER_COUNT)

# idf-v3's floor, chosen the way IDF_V2_FLOOR was: jointly against every
# corpus with a gate on it, never one alone. Stemming lets more lessons
# match, so relevance sits higher on the same scale and the floor has to
# rise with it -- at 0.04, legal-field pollution on the six-field fixture
# went to 2.50x, over its 2.4x ceiling. Measured together
# (commontrace/reference/measure_retrieval.py, commons/eval/
# retrieval_tiers.py, and the public LoCoMo dialogue benchmark):
#
#   scorer  floor   fixture worst   fixture   commons   commons   LoCoMo
#                   pollution       min P@1   R@5       neg@1     R@10    MRR
#   idf-v2  0.04    2.33x           0.89      0.913     1.00      0.540   0.387
#   idf-v3  0.04    2.50x           1.00      0.978     1.00      0.580   0.421
#   idf-v3  0.064   2.17x   <- here 1.00      0.957     0.86      0.558   0.414
#   idf-v3  0.08    1.78x           1.00        --        --      0.472   0.390
#
# Why it is not the default: at 0.064 it is better than idf-v2 on the worst
# field and in seven of the eight fields, but NOT in clinical (1.72x ->
# 2.06x) -- Porter conflates derivations ("medically"/"medication" ->
# "medic", "authorization" -> "author"), and a small curated store feels
# every extra match. Raising the floor until every field is cleaner (0.08)
# gives back the recall that was the point. tests/test_cross_field_
# retrieval.py requires the DEFAULT scorer to beat the historical one in
# every single field, and that bar is not lowered to ship this. Stores of
# conversational or free-text memory, where recall is the constraint, are
# the case for opting in.
#
# Lighter stemmers were measured as a default candidate, on the same corpora
# plus LongMemEval (session R@5 / R@10):
#
#   stemmer                  floor  worst   every   commons  LoCoMo  LME
#                                   field   field   R@5      R@10    R@5 / R@10
#   none (idf-v2)            0.04   2.33x   yes     0.913    0.540   0.852 / 0.940
#   Porter step 1 only       0.064  2.06x   NO      0.978    0.550   0.916 / 0.950
#   (plurals, -ed, -ing)     0.068  2.06x   yes     0.957    0.533   0.908 / 0.922
#   Harman S-stemmer         0.064  2.06x   yes     0.913    0.522   0.888 / 0.913
#   Porter (idf-v3)          0.072  2.00x   yes     0.957    0.523   0.901 / 0.907
#
# Every floor high enough to pass the clinical field gives back top-10
# recall on both public datasets, so none replaced idf-v2 as the default:
# the gain in precision is real, but a default must not find fewer of the
# lessons that answer the task.
IDF_V3_FLOOR = 0.064

# The default for the default scorer.
DEFAULT_FLOOR = IDF_V2_FLOOR


def default_floor(scorer: str) -> float:
    """The floor a store gets for `scorer` when it has not set its own."""
    if scorer == SCORER_COUNT:
        return 0.0
    if scorer == SCORER_IDF_V2:
        return IDF_V2_FLOOR
    return IDF_V3_FLOOR

# BM25's length-normalization strength. 0 disables it; 1 normalizes fully.
_LENGTH_B = 0.5
_LENGTH_CLAMP = (0.5, 1.5)

# \d is excluded from the strip below only via the stopword/length filters,
# same as before. See commontrace/_lexical.py for why \w/re.UNICODE and this
# exact stopword list: non-English text and the historical drift-between-
# copies bug this replaced.


def _tokenize(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1]


def _terms_for(scorer: str, terms) -> set[str]:
    """The terms a scorer compares: stemmed for idf-v3, as tokenized for
    the others. Applied to already-tokenized terms, so a `term_cache` built
    for one scorer serves every scorer."""
    if scorer == SCORER_IDF_V3:
        return {_stem(t) for t in terms}
    return set(terms)


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
    #
    # ALWAYS the pure topical relevance, unaffected by `reliability_weight`/
    # `recency_weight` below -- those adjust ORDER among lessons that already
    # cleared this floor, never the floor itself or the number recorded here.
    # A caller wanting the value that actually decided this lesson's
    # position reads `relevance` alongside `reliability_adjustment` and
    # `recency_adjustment`, not a blended replacement for any of the three.
    relevance: float = 0.0
    # Which scorer produced `relevance`. An experiment that pooled occasions
    # from two scorers would be comparing two different treatments.
    scorer: str = SCORER_IDF
    # In [-1, 1]; 0.0 when the caller passed no `reliability_lookup` (or the
    # slug had none) -- see `rank_lessons`'s `reliability_weight`.
    reliability_adjustment: float = 0.0
    # In [-1, 1]; 0.0 when the caller passed no `recency_lookup` -- see
    # `rank_lessons`'s `recency_weight`.
    recency_adjustment: float = 0.0


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
        (str(fm.get("description") or ""), _FIELD_WEIGHTS[0]),
        (str(fm.get("applies_when") or ""), _FIELD_WEIGHTS[1]),
        (" ".join(tags_list), _FIELD_WEIGHTS[2]),
        (str(fm.get("domain") or ""), _FIELD_WEIGHTS[3]),
    ]


# The field weights, in `_lesson_text_weighted`'s fixed order (description,
# applies_when, tags, domain). Pulled out as its own constant so a caller
# holding pre-tokenized terms -- commontrace/lesson_cache.py's `field_terms`,
# which tokenizes each field in this exact order -- can pair them with their
# weight without re-deriving field text it does not have.
_FIELD_WEIGHTS: tuple[float, float, float, float] = (1.0, 1.5, 2.0, 1.0)


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


@dataclass(frozen=True)
class _CorpusIndex:
    """Everything `rank_lessons` derives from the corpus rather than the query.

    n_terms[i]    lesson i's distinct terms across all fields (length factor)
    postings      term -> (lesson indices containing it, ascending;
                           per lesson, the summed weight of the fields it is in;
                           per lesson, the strongest such field's weight)
    doc_freq      term -> number of lessons containing it
    """

    n_terms: list[int]
    postings: dict[str, tuple[tuple[int, ...], tuple[float, ...], tuple[float, ...]]]
    doc_freq: dict[str, int]
    n_docs: int
    avg_field_len: float
    max_idf: float


def _build_index(lessons, term_cache, scorer: str) -> _CorpusIndex:
    n_terms: list[int] = []
    postings: dict[str, tuple[list[int], list[float], list[float]]] = {}
    for i, (path, fm) in enumerate(lessons):
        cached = term_cache.get(path) if term_cache else None
        if cached is not None and len(cached) == len(_FIELD_WEIGHTS):
            pairs = zip(cached, _FIELD_WEIGHTS)
        else:
            pairs = ((_tokenize(text), weight) for text, weight in _lesson_text_weighted(fm))
        # term -> [summed weight of the fields containing it, strongest one]
        in_doc: dict[str, list[float]] = {}
        for raw_terms, weight in pairs:
            for term in _terms_for(scorer, raw_terms):
                entry = in_doc.get(term)
                if entry is None:
                    in_doc[term] = [weight, weight]
                else:
                    entry[0] += weight
                    if weight > entry[1]:
                        entry[1] = weight
        for term, (weight_sum, best) in in_doc.items():
            post = postings.get(term)
            if post is None:
                post = postings[term] = ([], [], [])
            post[0].append(i)
            post[1].append(weight_sum)
            post[2].append(best)
        n_terms.append(len(in_doc))
    doc_freq = {term: len(post[0]) for term, post in postings.items()}
    n_docs = len(lessons)
    return _CorpusIndex(
        n_terms=n_terms,
        postings={term: (tuple(a), tuple(b), tuple(c)) for term, (a, b, c) in postings.items()},
        doc_freq=doc_freq,
        n_docs=n_docs,
        avg_field_len=(sum(n_terms) / len(n_terms)) if n_terms else 0.0,
        max_idf=max((_idf(n_docs, df) for df in doc_freq.values()), default=0.0),
    )


# Recently built indexes, keyed by exactly what they were built from. A
# long-lived MCP server answers every `retrieve` against the same store, and
# rebuilding every lesson's term sets and the document-frequency table on
# each call made ranking linear in the store's size: 153ms per query at
# 6,400 lessons, for statistics that had not changed. Small, because a
# process ranks against one store (occasionally a filtered view of it, e.g.
# `exclude_shown`), and each entry holds a whole store's term sets.
_INDEX_CACHE: dict[tuple, tuple[tuple, _CorpusIndex]] = {}
_INDEX_CACHE_MAX = 4


def _corpus_index(lessons, term_cache, scorer: str) -> _CorpusIndex:
    """The corpus index for `lessons`, reused while none of them changed.

    Reuse needs proof the corpus is the same one: `term_cache` must carry
    each lesson file's (mtime_ns, size) -- commontrace/lesson_cache.py's
    TermCache does -- and the key is every lesson's path AND stamp, in
    order, so a changed, added, removed or re-ordered lesson is a different
    key. Anything without stamps (a caller's own list, a test) builds a
    fresh index every call, which is the behaviour before this existed.
    Either way the index, and so every relevance, is identical: this only
    decides whether it is rebuilt.
    """
    stamps = getattr(term_cache, "stamps", None)
    if not stamps:
        return _build_index(lessons, term_cache, scorer)
    if lessons is getattr(term_cache, "lessons", None) and term_cache.fingerprint is not None:
        # The unfiltered snapshot this term cache was built with: its
        # fingerprint, and that fingerprint's hash, were computed once.
        fingerprint, fp_hash = term_cache.fingerprint, term_cache.fingerprint_hash
    else:
        try:
            fingerprint = tuple((path, stamps[path]) for path, _fm in lessons)
        except KeyError:
            return _build_index(lessons, term_cache, scorer)
        fp_hash = hash(fingerprint)
    # Keyed by the hash (a large tuple re-hashes on every lookup), and a hit
    # is only a hit if the full fingerprint matches too -- by identity for
    # the same snapshot, which is the common case and costs nothing.
    key = (scorer, fp_hash)
    hit = _INDEX_CACHE.get(key)
    if hit is not None and (hit[0] is fingerprint or hit[0] == fingerprint):
        return hit[1]
    index = _build_index(lessons, term_cache, scorer)
    if key not in _INDEX_CACHE and len(_INDEX_CACHE) >= _INDEX_CACHE_MAX:
        _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
    _INDEX_CACHE[key] = (fingerprint, index)
    return index


def rank_lessons(
    task: str,
    lessons: list[tuple[str, dict]],
    top_k: int = 10,
    floor: float | None = None,
    scorer: str = SCORER_IDF,
    term_cache: dict[str, list[list[str]]] | None = None,
    reliability_lookup: dict[str, float] | None = None,
    reliability_weight: float = 0.0,
    recency_lookup: dict[str, float] | None = None,
    recency_weight: float = 0.0,
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

    Only lessons at or above `floor` are returned. `floor=None` uses the
    scorer's own default (`default_floor`); pass 0.0 to disable the gate.

    Ties break toward higher `importance` (a lesson explicitly marked more
    consequential wins), then toward more prior `uses`.

    `scorer=SCORER_COUNT` restores the historical raw-additive behaviour
    exactly, for a store mid-experiment that must not have its lesson
    eligibility re-randomized under it (commontrace/retrieval_io.py).

    `term_cache`, if given, maps a lesson's path to its four fields already
    tokenized -- in `_lesson_text_weighted`'s fixed order (description,
    applies_when, tags, domain) -- so a caller that already paid for
    tokenization on a prior call (commontrace/lesson_cache.py, keyed by file
    mtime) does not pay for it again. A path absent from the cache, or an
    absent/`None` cache entirely, tokenizes fresh: this parameter can only
    make a call faster, never change what it returns, because
    `set(cached_terms) == set(_tokenize(field_text))` by construction --
    `lesson_cache.field_terms` derives the cache from nothing but this same
    `_tokenize`.

    `reliability_lookup`/`recency_lookup` (slug -> adjustment in [-1, 1];
    see `commontrace/reliability.py`'s `ranking_adjustments` and
    `commontrace/recency.py`'s `recency_lookup`) and their weights let a
    store that has opted in (`commontrace/retrieval_io.py`'s
    `reliability_weight`/`recency_weight`, both default 0.0) break ties
    among already-eligible lessons using their track record and freshness,
    not just topical match. Both default to inert: with weight 0.0 (or no
    lookup at all) every RankedLesson's `reliability_adjustment`/
    `recency_adjustment` is 0.0 and ordering is byte-for-byte identical to
    calling this function with neither argument.

    THIS NEVER CHANGES ELIGIBILITY. `rel >= floor` below is computed and
    gated on the pure topical `relevance` alone, before either adjustment is
    applied -- a HARMFUL-verdict lesson that would have cleared the floor
    still clears it and is still returned, just later in the list than an
    otherwise-equal lesson with a better track record. Silently hiding a
    lesson based on a statistical verdict computed from a possibly-small
    sample would be a stronger, riskier claim than "rank it lower," and this
    module does not make it. `adjusted` (the sort key) is therefore a
    SEPARATE number from the `relevance` stored on `RankedLesson` -- the
    floor, the holdout log, and every existing caller keep reading the exact
    number they always did.
    """
    query_terms = _terms_for(scorer, _tokenize(task))
    if not query_terms:
        return []
    if floor is None:
        floor = default_floor(scorer)

    # Corpus statistics over the candidate set: document frequency per term,
    # and mean term count per weighted field -- built once per store snapshot
    # and reused while the store is unchanged (`_corpus_index`).
    index = _corpus_index(lessons, term_cache, scorer)
    doc_freq = index.doc_freq
    n_docs = index.n_docs

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
    # The reference scale is the most informative term the corpus contains,
    # not the query's own total. Dividing by the query's own IDF sum makes the
    # measure self-normalizing, which hides exactly what IDF is for: a query
    # made entirely of a field's boilerplate ("node" in a robotics store) has
    # 100% of its own information covered by every lesson, and scored ~1.0.
    # Against an absolute reference it scores what it is worth -- near zero --
    # while a discriminating match still reaches the top of the scale.
    max_idf = index.max_idf
    total_query_idf = len(query_terms) * max_idf

    # (RankedLesson, importance, uses) rather than a separate by_slug dict
    # keyed on fm.get("name"): two lessons with the same (or both missing/
    # empty) `name` collided in that dict, so the tie-break silently used
    # the WRONG lesson's importance/uses for one of them. Carrying each
    # lesson's own sort fields alongside it here instead ties them to the
    # specific (path, fm) pair that produced them, not to a name that may
    # not be unique.
    scored: list[tuple[RankedLesson, int, int]] = []
    # Only lessons sharing a term with the query can score above zero, and a
    # zero score is never returned -- so only those are visited, in corpus
    # order, which keeps the stable sort's tie order exactly what a full pass
    # produced.
    # One pass over the query terms' postings. Each lesson's `score` is the
    # sum, over the fields a matched term appears in, of that field's weight;
    # its `covered` credits each matched term once, at its strongest field --
    # a term repeated across four fields is not counted four times, the
    # other half of the breadth-beats-precision problem. Terms are visited in
    # sorted order, so each lesson's `covered` is summed in sorted-term
    # order: str hashing (hence set iteration order) is PYTHONHASHSEED-
    # randomized per process, floating-point addition is not associative,
    # and summing in an unspecified order made `rel` -- tested against
    # `floor` before it is rounded -- vary at the ULP level between
    # processes (measured: 99.7% of realistic (idf, weight) draws are
    # order-dependent). A lesson within a few ULP of the floor could clear it
    # in one process and not another, and eligibility is the denominator of
    # a causal estimate (commontrace/integrity.py). `score` is a sum of
    # multiples of 0.5 and exact in any order.
    #
    # Only lessons sharing a term with the query can score above zero, and a
    # zero score is never returned, so only those are visited -- in corpus
    # order, which keeps the stable selection's tie order exactly what a full
    # pass produced.
    acc: dict[int, list] = {}
    for term in sorted(query_terms):
        post = index.postings.get(term)
        if post is None:
            continue
        term_idf = query_idf.get(term, 0.0)
        for i, weight_sum, best in zip(*post):
            a = acc.get(i)
            if a is None:
                a = acc[i] = [0.0, 0.0, []]
            a[0] += weight_sum
            a[1] += term_idf * (best / _MAX_FIELD_WEIGHT)
            a[2].append(term)
    for i in sorted(acc):
        (path, fm) = lessons[i]
        score, covered, matched = acc[i]

        if scorer == SCORER_COUNT:
            rel = score
        elif total_query_idf > 0 and matched:
            lam = _length_factor(index.n_terms[i], index.avg_field_len)
            # Clamped: `lam` may exceed 1.0 for a lesson shorter than average
            # (a deliberate small boost), and the bound this measure documents
            # -- and that the shared floor depends on -- must hold regardless.
            rel = min(1.0, (covered * lam) / total_query_idf)
        else:
            rel = 0.0

        if score > 0 and rel >= floor:
            slug = str(fm.get("name", ""))
            reliability_adj = reliability_lookup.get(slug, 0.0) if reliability_lookup else 0.0
            recency_adj = recency_lookup.get(slug, 0.0) if recency_lookup else 0.0
            # Clamped to [0, 1]: an adjustment is bounded to [-1, 1] and a
            # weight is expected small, but neither is validated here (the
            # config surface that sets them does that) -- a misconfigured
            # weight must degrade to "ranking is a little off," never to a
            # relevance value outside the range every other caller of this
            # function already assumes.
            adjusted = min(1.0, max(0.0,
                rel + reliability_weight * reliability_adj + recency_weight * recency_adj,
            ))
            # The sort key plus what is needed to build the result -- the
            # RankedLesson itself is only built for the lessons returned.
            scored.append((
                adjusted, score,
                _rank_int(fm.get("importance", 0)), _rank_int(fm.get("uses", 0)),
                path, fm, slug, matched, rel, reliability_adj, recency_adj,
            ))

    # max(0, ...): a plain `scored[:top_k]` on a negative top_k is a Python
    # slice, not a bounds check -- `scored[:-1]` means "all but the last
    # item", not "nothing", so a negative top_k silently returned nearly
    # the whole ranked list instead of failing. query_cmd.py's CLI already
    # rejects a negative --top-k before it reaches here; this clamp is the
    # same guarantee for any other caller of this function directly.
    #
    # heapq.nlargest is documented as equivalent to
    # sorted(..., reverse=True)[:n] -- the same stable order, ties included
    # -- without sorting every lesson that cleared the floor to return three.
    top = heapq.nlargest(max(0, top_k), scored, key=lambda item: item[:4])
    return [
        RankedLesson(
            path=path,
            slug=slug,
            description=str(fm.get("description", "")),
            score=score,
            matched_terms=list(matched),
            relevance=round(rel, 6),
            scorer=scorer,
            reliability_adjustment=round(reliability_adj, 6) if reliability_lookup else 0.0,
            recency_adjustment=round(recency_adj, 6) if recency_lookup else 0.0,
        )
        for (_adj, score, _imp, _uses, path, fm, slug, matched, rel,
             reliability_adj, recency_adj) in top
    ]


# --- Combining two rankings that do not share a scale -----------------------
#
# The lexical scorer above returns a relevance in [0, 1]; the optional
# semantic layer (memory/attention) returns a cosine similarity. Neither is
# convertible into the other, and the obvious fixes are both wrong: adding
# them compares quantities with different meanings, and normalizing each to
# its own maximum makes a ranking's top result score 1.0 whether it was an
# excellent match or the least bad of a bad set.
#
# Reciprocal Rank Fusion sidesteps the question by using only POSITION. A
# document's contribution from each arm is 1/(k + rank), so an arm can only
# say "I put this first, this second" -- which is the one thing every
# retrieval arm can say comparably. Adding an arm never requires rescaling
# any other.
DEFAULT_RRF_K = 60


def reciprocal_rank_fusion(
    arms: dict[str, list[str]],
    k: int = DEFAULT_RRF_K,
    top_k: int | None = None,
    weights: dict[str, float] | None = None,
) -> list[tuple[str, float]]:
    """Fuse several ranked id lists into one, by position alone.

    `arms` maps an arm's name to its ranked ids, best first. Returns
    (id, fused_score) pairs, best first, with ties broken by id so the
    result is deterministic -- a retrieval order that varied between two
    identical calls would put the same occasion in different arms of an
    experiment on a retry.

    `weights` scales an arm's contribution, defaulting to 1.0 for any arm it
    does not name. It exists because the arms are not always equally good on
    a given corpus and the benchmark in commons/eval/hybrid_fusion.py sweeps
    for the mix that is -- the weights belong to a tuning result, not to the
    formula, so they are a parameter here rather than a constant.

    `k` damps the advantage of a top position: at k=60 (the value the
    original RRF paper uses, and the one every implementation since has
    copied) the difference between rank 1 and rank 2 is small enough that
    agreement across arms outweighs a single arm's confidence, which is the
    entire point of fusing.

    An id that appears in one arm and not another is not penalised beyond
    simply not scoring from the arm that missed it. That asymmetry is
    deliberate: a lexical arm cannot be expected to find a paraphrase, and
    treating its silence as a vote against would make adding an arm reduce
    recall.
    """
    if k <= 0:
        raise ValueError(f"RRF k must be positive, got {k}")
    fused: dict[str, float] = {}
    for arm, ranked in arms.items():
        weight = 1.0 if weights is None else float(weights.get(arm, 1.0))
        for position, item in enumerate(ranked, start=1):
            fused[item] = fused.get(item, 0.0) + weight / (k + position)
    ordered = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))
    return ordered if top_k is None else ordered[: max(0, top_k)]


# --- Second-stage reranking --------------------------------------------------
#
# `rank_lessons` (and `reciprocal_rank_fusion` for the hybrid path) is a
# first-stage scorer: fast, dependency-free, and comparable across stores.
# Nothing in this module ever runs a heavier second pass over the winners of
# that first stage -- a cross-encoder, an LLM judge, or a bespoke scorer a
# deployment already has. This module had no seam for one at all.
#
# The seam here is a plain callable, not a class hierarchy -- the same shape
# `redundancy.find_near_duplicates`'s own `similarity` parameter already
# uses for the identical reason: the core install (PyYAML only) must not
# gain a hard dependency just to define an extension point nobody has to
# use. A concrete embeddings-based reranker, when a caller wants one, is
# exactly the kind of thing that belongs behind the optional `attention`
# extra (see commontrace/reference/), not in this module.
Reranker = Callable[[str, list[RankedLesson]], list[RankedLesson]]


def apply_reranker(
    task: str,
    ranked: list[RankedLesson],
    reranker: Reranker | None,
) -> list[RankedLesson]:
    """Apply an optional second-stage `reranker` to an already-ranked list.

    `reranker=None` (the default) is a no-op: returns `ranked` unchanged,
    so a caller that has not opted in pays no extra cost and sees no
    behavior change at all -- calling this with `reranker=None` is
    identical to not calling it.

    A reranker receives `task` and the FULL ranked list (already past the
    relevance floor -- eligibility is decided upstream, exactly as
    `rank_lessons`'s own `reliability_weight`/`recency_weight` never touch
    it either) and returns a re-ordered list over the SAME
    `RankedLesson` objects. It is a caller error for a reranker to add,
    drop, or duplicate an item -- this function does not defend against
    that (validating a reranker's output on every call would be a real
    cost every retrieval pays for a mistake only the reranker's author can
    make), so a reranker's own tests are where that guarantee is checked,
    the same trust boundary `redundancy.find_near_duplicates` places on a
    caller-supplied `similarity` function.
    """
    if reranker is None:
        return ranked
    return reranker(task, ranked)
