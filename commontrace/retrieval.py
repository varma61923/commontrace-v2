from __future__ import annotations

import heapq
import math
import os
import sys
import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from commontrace import corpus_bin
from commontrace._lexical import STOPWORDS as _STOPWORDS
from commontrace._lexical import WORD_RE as _WORD_RE
from commontrace._lexical import has_cjk as _has_cjk
from commontrace._lexical import segment_cjk as _segment_cjk
from commontrace._stem import stem as _stem
from commontrace.runtime_cache import RuntimeCache

IDF_V2_FLOOR = 0.04

SCORER_ADAPTIVE = "adaptive-v1"
SCORER_IDF_V3 = "idf-v3"
SCORER_IDF_V2 = "idf-v2"
SCORER_BM25 = "bm25-v1"
SCORER_IDF = SCORER_ADAPTIVE
SCORER_COUNT = "count-v1"
LEXICAL_SCORERS = (SCORER_ADAPTIVE, SCORER_IDF_V3, SCORER_IDF_V2, SCORER_BM25, SCORER_COUNT)

BM25_K1 = 1.2
BM25_B = 0.75

IDF_V3_FLOOR = 0.064
ADAPTIVE_FLOOR = IDF_V2_FLOOR
ADAPTIVE_MIN_TAIL_RATIO = 0.30
ADAPTIVE_MAX_TAIL_RATIO = 0.60
ADAPTIVE_TAIL_RATIO_STEP = 0.04

DEFAULT_FLOOR = ADAPTIVE_FLOOR


def default_floor(scorer: str) -> float:
    """The floor a store gets for `scorer` when it has not set its own."""
    if scorer == SCORER_COUNT:
        return 0.0
    if scorer in (SCORER_ADAPTIVE, SCORER_IDF_V2, SCORER_BM25):
        return IDF_V2_FLOOR
    return IDF_V3_FLOOR

_LENGTH_B = 0.5
_LENGTH_CLAMP = (0.5, 1.5)


def _tokenize(text: str) -> list[str]:
    toks = [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1]
    if not any(_has_cjk(t) for t in toks):
        return toks
    out: list[str] = []
    for tok in toks:
        if _has_cjk(tok):
            out.extend(_segment_cjk(tok))
        else:
            out.append(tok)
    return out


def _terms_for(scorer: str, terms) -> set[str]:
    if scorer in (SCORER_ADAPTIVE, SCORER_IDF_V3, SCORER_BM25):
        return {_stem(t) for t in terms}
    return set(terms)


def _adaptive_tail_ratio(n_terms: int) -> float:
    return min(
        ADAPTIVE_MAX_TAIL_RATIO,
        ADAPTIVE_MIN_TAIL_RATIO + ADAPTIVE_TAIL_RATIO_STEP * max(0, n_terms),
    )


@dataclass(frozen=True)
class RankedLesson:
    path: str
    slug: str
    description: str
    score: float
    matched_terms: list[str]
    relevance: float = 0.0
    scorer: str = SCORER_IDF
    reliability_adjustment: float = 0.0
    recency_adjustment: float = 0.0
    graph_adjustment: float = 0.0


def _lesson_text_weighted(fm: dict) -> list[tuple[str, float]]:
    tags = fm.get("tags")
    tags_list = [str(t) for t in tags if t is not None] if isinstance(tags, (list, tuple)) else []
    return [
        (str(fm.get("description") or ""), _FIELD_WEIGHTS[0]),
        (str(fm.get("applies_when") or ""), _FIELD_WEIGHTS[1]),
        (" ".join(tags_list), _FIELD_WEIGHTS[2]),
        (str(fm.get("domain") or ""), _FIELD_WEIGHTS[3]),
    ]


_FIELD_WEIGHTS: tuple[float, float, float, float] = (1.0, 1.5, 2.0, 1.0)


_MAX_FIELD_WEIGHT = 2.0


def _idf(n_docs: int, doc_freq: int) -> float:
    return math.log(1.0 + (n_docs - doc_freq + 0.5) / (doc_freq + 0.5))


def _length_factor(n_terms: int, avg_terms: float) -> float:
    if avg_terms <= 0:
        return 1.0
    raw = 1.0 / (1.0 + _LENGTH_B * ((n_terms / avg_terms) - 1.0))
    low, high = _LENGTH_CLAMP
    return max(low, min(high, raw))


def _bm25_term(tf: float, idf: float, doc_len: int, avg_len: float) -> float:
    norm = (1.0 - BM25_B + BM25_B * (doc_len / avg_len)) if avg_len > 0 else 1.0
    return idf * (tf * (BM25_K1 + 1.0)) / (tf + BM25_K1 * norm)


def _rank_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class _CorpusIndex:
    n_terms: list[int]
    postings: dict[str, tuple[tuple[int, ...], tuple[float, ...], tuple[float, ...]]]
    doc_freq: dict[str, int]
    n_docs: int
    avg_field_len: float
    max_idf: float
    length_factors: tuple[float, ...] = ()
    tie_breaks: tuple[tuple[int, int], ...] = ()


def _build_index(lessons, term_cache, scorer: str) -> _CorpusIndex:
    n_terms: list[int] = []
    postings: dict[str, tuple[list[int], list[float], list[float]]] = {}
    for i, (path, fm) in enumerate(lessons):
        cached = term_cache.get(path) if term_cache else None
        if cached is not None and len(cached) == len(_FIELD_WEIGHTS):
            pairs = zip(cached, _FIELD_WEIGHTS)
        else:
            pairs = ((_tokenize(text), weight) for text, weight in _lesson_text_weighted(fm))
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
    avg = (sum(n_terms) / len(n_terms)) if n_terms else 0.0
    return _CorpusIndex(
        n_terms=n_terms,
        postings={term: (tuple(a), tuple(b), tuple(c)) for term, (a, b, c) in postings.items()},
        doc_freq=doc_freq,
        n_docs=n_docs,
        avg_field_len=avg,
        max_idf=max((_idf(n_docs, df) for df in doc_freq.values()), default=0.0),
        length_factors=tuple(_length_factor(n, avg) for n in n_terms),
        tie_breaks=tuple((_rank_int(fm.get("importance", 0)), _rank_int(fm.get("uses", 0))) for _p, fm in lessons),
    )


_INDEX_CACHE_MAX = 4
_INDEX_CACHE_MAX_BYTES = 64 * 1024 * 1024


def _index_bytes(fingerprint: tuple, index: _CorpusIndex) -> int:
    """Conservative retained size without a posting-sized traversal or ID set.

    Shared references are counted again deliberately. Numeric posting arrays
    use fixed float sizes and an upper bound for document-ID integer sizes.
    The budget bounds cached indexes, not a caller's active query or build.
    """
    def sequence_bytes(value) -> int:
        size = 0
        pending = [value]
        while pending:
            item = pending.pop()
            size += sys.getsizeof(item)
            if isinstance(item, (tuple, list)):
                pending.extend(item)
        return size

    values = vars(index)
    size = (512 + sys.getsizeof(index) + sys.getsizeof(values)
            + sum(sys.getsizeof(name) for name in values)
            + sequence_bytes(fingerprint) + sequence_bytes(index.n_terms)
            + sequence_bytes(index.tie_breaks)
            + sys.getsizeof(index.n_docs) + sys.getsizeof(index.avg_field_len)
            + sys.getsizeof(index.max_idf) + sys.getsizeof(index.length_factors)
            + len(index.length_factors) * sys.getsizeof(0.0)
            + sys.getsizeof(index.postings) + sys.getsizeof(index.doc_freq))
    id_bytes, float_bytes = sys.getsizeof(index.n_docs), sys.getsizeof(0.0)
    for term, posting in index.postings.items():
        ids, weights, best = posting
        size += (2 * sys.getsizeof(term) + sys.getsizeof(posting)
                 + sys.getsizeof(ids) + len(ids) * id_bytes
                 + sys.getsizeof(weights) + len(weights) * float_bytes
                 + sys.getsizeof(best) + len(best) * float_bytes
                 + sys.getsizeof(index.doc_freq[term]))
    return size


@dataclass
class _IndexFlight:
    fingerprint: tuple
    ready: threading.Event = field(default_factory=threading.Event)
    index: _CorpusIndex | None = None
    error: BaseException | None = None


class _CorpusIndexCache:
    """Bounded LRU with one cold build per source snapshot, not one global build.

    Hashes narrow the lookup only; equality remains mandatory before reuse.
    Disk I/O, tokenization and size accounting never hold the cache lock.
    """

    def __init__(self) -> None:
        self._after_fork()

    def _after_fork(self) -> None:
        # A fork can inherit locks and unfinished builds owned by vanished
        # threads. Do not acquire any inherited lock when resetting the child.
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple, tuple[tuple, _CorpusIndex, int]] = OrderedDict()
        self._flights: dict[tuple, _IndexFlight] = {}
        self.bytes_used = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            # Existing readers can finish their build, but cannot republish it.
            self._flights.clear()
            self.bytes_used = 0

    def get_or_build(self, key: tuple, fingerprint: tuple,
                     build: Callable[[], _CorpusIndex]) -> _CorpusIndex:
        while True:
            with self._lock:
                hit = self._entries.get(key)
                if hit is not None and (hit[0] is fingerprint or hit[0] == fingerprint):
                    self._entries.move_to_end(key)
                    return hit[1]
                flight = self._flights.get(key)
                if flight is None:
                    flight = _IndexFlight(fingerprint)
                    self._flights[key] = flight
                    owner = True
                else:
                    owner = False
            if owner:
                break
            flight.ready.wait()
            if flight.fingerprint is fingerprint or flight.fingerprint == fingerprint:
                if flight.error is not None:
                    raise flight.error
                assert flight.index is not None
                return flight.index
            # A hash collision may have just built a different snapshot.

        try:
            index = build()
            size = _index_bytes(fingerprint, index)
            with self._lock:
                if self._flights.get(key) is flight:
                    if _INDEX_CACHE_MAX > 0 and size <= _INDEX_CACHE_MAX_BYTES:
                        replaced = self._entries.pop(key, None)
                        if replaced is not None:
                            self.bytes_used -= replaced[2]
                        while self._entries and (
                            len(self._entries) >= _INDEX_CACHE_MAX
                            or self.bytes_used + size > _INDEX_CACHE_MAX_BYTES
                        ):
                            _old_key, old = self._entries.popitem(last=False)
                            self.bytes_used -= old[2]
                        self._entries[key] = (fingerprint, index, size)
                        self.bytes_used += size
                    del self._flights[key]
                flight.index = index
                flight.ready.set()
            return index
        except BaseException as error:
            # Wake every waiter even on a cancelled or failed build, without
            # caching a partial result or preventing a subsequent retry.
            with self._lock:
                if self._flights.get(key) is flight:
                    del self._flights[key]
                flight.error = error
                flight.ready.set()
            raise


_INDEX_CACHE = _CorpusIndexCache()
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_INDEX_CACHE._after_fork)


def _corpus_index(lessons, term_cache, scorer: str) -> _CorpusIndex:
    stamps = getattr(term_cache, "stamps", None)
    if not stamps:
        return _build_index(lessons, term_cache, scorer)
    if lessons is getattr(term_cache, "lessons", None) and term_cache.fingerprint is not None:
        fingerprint, fp_hash = term_cache.fingerprint, term_cache.fingerprint_hash
    else:
        try:
            fingerprint = tuple((path, stamps[path]) for path, _fm in lessons)
        except KeyError:
            return _build_index(lessons, term_cache, scorer)
        fp_hash = hash(fingerprint)
    key = (scorer, fp_hash)
    bin_dir = getattr(term_cache, "bin_dir", None)

    def build() -> _CorpusIndex:
        if bin_dir:
            try:
                persisted = corpus_bin.load(bin_dir, scorer, lessons, fingerprint)
            except Exception:
                persisted = None
            if persisted is not None:
                return persisted
        index = _build_index(lessons, term_cache, scorer)
        if bin_dir and fingerprint == getattr(term_cache, "fingerprint", None):
            try:
                corpus_bin.save(bin_dir, scorer, fingerprint, index)
            except Exception:
                pass
        return index

    return _INDEX_CACHE.get_or_build(key, fingerprint, build)


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
    adaptive_tail: bool = True,
    graph_boost_lookup: dict[str, float] | None = None,
    graph_weight: float = 0.0,
    cache_results: bool = True,
) -> list[RankedLesson]:
    query_terms = _terms_for(scorer, _tokenize(task))
    if not query_terms or top_k <= 0:
        return []
    if floor is None:
        floor = default_floor(scorer)
    index = _corpus_index(lessons, term_cache, scorer)

    def rank():
        return _rank_numeric(query_terms, index, lessons, top_k, floor, scorer,
                             reliability_lookup, reliability_weight, recency_lookup,
                             recency_weight, adaptive_tail, graph_boost_lookup, graph_weight)

    # Dynamic signals are never frozen in a result cache. Weak identity keeps
    # this cache from retaining a corpus, and cannot alias a recycled object ID.
    cache_enabled = cache_results and os.environ.get("COMMONTRACE_QUERY_CACHE", "1").strip().lower() \
        not in ("0", "false", "off", "no")
    if cache_enabled and not (reliability_lookup or recency_lookup or graph_boost_lookup):
        key = (_IndexIdentity(index), scorer, tuple(sorted(query_terms)), top_k, floor, adaptive_tail)
        rows = _QUERY_CACHE.get_or_load(key, rank)
    else:
        rows = rank()
    return [RankedLesson(
        path=lessons[i][0], slug=str(lessons[i][1].get("name", "")),
        description=str(lessons[i][1].get("description", "")),
        score=score, matched_terms=list(matches), relevance=round(rel, 6), scorer=scorer,
        reliability_adjustment=round(reliability_adj, 6) if reliability_lookup else 0.0,
        recency_adjustment=round(recency_adj, 6) if recency_lookup else 0.0,
        graph_adjustment=round(graph_adj, 6) if graph_boost_lookup else 0.0,
    ) for i, score, matches, rel, reliability_adj, recency_adj, graph_adj in rows]


class _IndexIdentity:
    """Hashable weak identity, including for an unhashable frozen dataclass."""

    __slots__ = ("ref", "ident")

    def __init__(self, index: _CorpusIndex) -> None:
        self.ref = weakref.ref(index)
        self.ident = id(index)

    def __hash__(self) -> int:
        return self.ident

    def __eq__(self, other) -> bool:
        return (isinstance(other, _IndexIdentity) and self.ref() is not None
                and self.ref() is other.ref())


def _query_bytes(key, rows) -> int:
    # Conservative per-entry overhead plus keys, tuple records, numeric values,
    # matched-term strings and their references. No corpus is retained here.
    size = 1024 + sys.getsizeof(key) + sys.getsizeof(key[0]) + sys.getsizeof(key[0].ref)
    size += sum(sys.getsizeof(value) for value in key[1:])
    size += sum(sys.getsizeof(term) for term in key[2]) + sys.getsizeof(rows)
    for row in rows:
        # Unboosted templates have one bounded document ID and five floats.
        # Their numeric size is fixed; traversing them individually penalizes
        # every unique query without making the estimate more conservative.
        size += _QUERY_ROW_BYTES + sys.getsizeof(row[2])
        size += sum(sys.getsizeof(term) for term in row[2])
    return size


_QUERY_ROW_BYTES = sys.getsizeof((None,) * 7) + sys.getsizeof(sys.maxsize) + 5 * sys.getsizeof(0.0)
_QUERY_CACHE = RuntimeCache(max_entries=256, max_bytes=8 * 1024 * 1024, ttl=30.0, weigh=_query_bytes)


def _rank_numeric(query_terms, index, lessons, top_k, floor, scorer,
                  reliability_lookup, reliability_weight, recency_lookup,
                  recency_weight, adaptive_tail, graph_boost_lookup, graph_weight):
    doc_freq = index.doc_freq
    n_docs = index.n_docs

    query_idf = {
        t: _idf(n_docs, doc_freq[t]) for t in query_terms if doc_freq.get(t, 0) > 0
    }
    max_idf = index.max_idf
    total_query_idf = len(query_terms) * max_idf

    # Keep candidate state numeric; matched-term lists and result records are
    # needed only for returned rows, not every posting in a broad query.
    # Absent query terms still affect normalization above, but never need a bit
    # in candidate state. This bounds masks by matched terms rather than a
    # potentially huge out-of-vocabulary query.
    terms = sorted(term for term in query_terms if term in index.postings)
    sparse = sum(len(index.postings[t][0]) for t in terms) <= n_docs // 4
    scores = {} if sparse else [0.0] * n_docs
    coverage = {} if sparse else [0.0] * n_docs
    masks = {} if sparse else [0] * n_docs
    compact_matches = len(terms) <= 64
    candidates = []
    is_bm25 = scorer == SCORER_BM25
    for position, term in enumerate(terms):
        post = index.postings.get(term)
        if post is None:
            continue
        term_idf = query_idf.get(term, 0.0)
        bit = 1 << position if compact_matches else 0
        for i, weight_sum, best in zip(*post):
            previous = masks.get(i, 0) if sparse else masks[i]
            contribution = _bm25_term(weight_sum, term_idf, index.n_terms[i], index.avg_field_len) if is_bm25 \
                else term_idf * (best / _MAX_FIELD_WEIGHT)
            if previous:
                scores[i] += weight_sum
                coverage[i] += contribution
            else:
                candidates.append(i)
                scores[i] = weight_sum
                coverage[i] = contribution
            if compact_matches:
                masks[i] = previous | bit
            elif previous:
                previous.append(term)
            else:
                # Very large queries use storage proportional to actual hits,
                # avoiding a corpus-sized array of arbitrarily wide integers.
                masks[i] = [term]

    adaptive = scorer == SCORER_ADAPTIVE and adaptive_tail
    relevance = {} if sparse else [0.0] * n_docs
    peak = 0.0
    length_factors = index.length_factors
    for i in candidates:
        if scorer == SCORER_COUNT:
            rel = scores[i]
        elif total_query_idf <= 0:
            rel = 0.0
        elif is_bm25:
            rel = min(1.0, coverage[i] / total_query_idf)
        else:
            lam = length_factors[i] if length_factors else _length_factor(index.n_terms[i], index.avg_field_len)
            rel = min(1.0, (coverage[i] * lam) / total_query_idf)
        relevance[i] = rel
        if adaptive and scores[i] > 0 and rel > peak:
            peak = rel
    if adaptive:
        floor = max(floor, peak * _adaptive_tail_ratio(len(query_terms)))

    scored = []
    tie_breaks = index.tie_breaks
    for i in candidates:
        score, rel = scores[i], relevance[i]
        if not (score > 0 and rel >= floor):
            continue
        path, fm = lessons[i]
        slug = str(fm.get("name", ""))
        reliability_adj = reliability_lookup.get(slug, 0.0) if reliability_lookup else 0.0
        recency_adj = recency_lookup.get(slug, 0.0) if recency_lookup else 0.0
        graph_adj = graph_boost_lookup.get(slug, 0.0) if graph_boost_lookup else 0.0
        adjusted = min(1.0, max(0.0,
            rel + reliability_weight * reliability_adj
            + recency_weight * recency_adj + graph_weight * graph_adj,
        ))
        importance, uses = tie_breaks[i] if tie_breaks else (
            _rank_int(fm.get("importance", 0)), _rank_int(fm.get("uses", 0)))
        # Accumulation visits posting lists in term order. Explicitly preserve
        # the former stable corpus-order tie break without sorting candidates.
        scored.append((adjusted, score, importance, uses, -i, i, path, slug, rel,
                       reliability_adj, recency_adj, graph_adj))

    top = heapq.nlargest(max(0, top_k), scored, key=lambda item: item[:5])
    return tuple(
        (i, score,
         tuple(term for position, term in enumerate(terms) if masks[i] & (1 << position))
         if compact_matches else tuple(masks[i]),
         rel, reliability_adj, recency_adj, graph_adj)
        for (_adj, score, _imp, _uses, _order, i, path, slug, rel,
             reliability_adj, recency_adj, graph_adj) in top
    )


DEFAULT_RRF_K = 60


def reciprocal_rank_fusion(
    arms: dict[str, list[str]],
    k: int = DEFAULT_RRF_K,
    top_k: int | None = None,
    weights: dict[str, float] | None = None,
) -> list[tuple[str, float]]:
    """Fuse several ranked id lists into one, by position alone."""
    if k <= 0:
        raise ValueError(f"RRF k must be positive, got {k}")
    fused: dict[str, float] = {}
    for arm, ranked in arms.items():
        weight = 1.0 if weights is None else float(weights.get(arm, 1.0))
        for position, item in enumerate(ranked, start=1):
            fused[item] = fused.get(item, 0.0) + weight / (k + position)
    ordered = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))
    return ordered if top_k is None else ordered[: max(0, top_k)]


Reranker = Callable[[str, list[RankedLesson]], list[RankedLesson]]


def apply_reranker(
    task: str,
    ranked: list[RankedLesson],
    reranker: Reranker | None,
) -> list[RankedLesson]:
    """Apply an optional second-stage `reranker` to an already-ranked list."""
    if reranker is None:
        return ranked
    return reranker(task, ranked)
