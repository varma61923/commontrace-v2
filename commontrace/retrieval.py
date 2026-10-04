from __future__ import annotations

import heapq
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from commontrace import corpus_bin
from commontrace._lexical import STOPWORDS as _STOPWORDS
from commontrace._lexical import WORD_RE as _WORD_RE
from commontrace._lexical import has_cjk as _has_cjk
from commontrace._lexical import segment_cjk as _segment_cjk
from commontrace._stem import stem as _stem

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


_INDEX_CACHE: dict[tuple, tuple[tuple, _CorpusIndex]] = {}
_INDEX_CACHE_MAX = 4


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
    hit = _INDEX_CACHE.get(key)
    if hit is not None and (hit[0] is fingerprint or hit[0] == fingerprint):
        return hit[1]
    bin_dir = getattr(term_cache, "bin_dir", None)
    if bin_dir:
        try:
            persisted = corpus_bin.load(bin_dir, scorer, lessons, fingerprint)
        except Exception:
            persisted = None
        if persisted is not None:
            if key not in _INDEX_CACHE and len(_INDEX_CACHE) >= _INDEX_CACHE_MAX:
                _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
            _INDEX_CACHE[key] = (fingerprint, persisted)
            return persisted
    index = _build_index(lessons, term_cache, scorer)
    if bin_dir and fingerprint == getattr(term_cache, "fingerprint", None):
        try:
            corpus_bin.save(bin_dir, scorer, fingerprint, index)
        except Exception:
            pass
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
    adaptive_tail: bool = True,
    graph_boost_lookup: dict[str, float] | None = None,
    graph_weight: float = 0.0,
) -> list[RankedLesson]:
    query_terms = _terms_for(scorer, _tokenize(task))
    if not query_terms:
        return []
    if floor is None:
        floor = default_floor(scorer)

    index = _corpus_index(lessons, term_cache, scorer)
    doc_freq = index.doc_freq
    n_docs = index.n_docs

    query_idf = {
        t: _idf(n_docs, doc_freq[t]) for t in query_terms if doc_freq.get(t, 0) > 0
    }
    max_idf = index.max_idf
    total_query_idf = len(query_terms) * max_idf

    scored: list[tuple] = []
    acc: dict[int, list] = {}
    acc_get = acc.get
    is_bm25 = scorer == SCORER_BM25
    avg_len = index.avg_field_len
    for term in sorted(query_terms):
        post = index.postings.get(term)
        if post is None:
            continue
        term_idf = query_idf.get(term, 0.0)
        for i, weight_sum, best in zip(*post):
            a = acc_get(i)
            if is_bm25:
                contrib = _bm25_term(weight_sum, term_idf, index.n_terms[i], avg_len)
                if a is None:
                    acc[i] = [weight_sum, contrib, [term]]
                    continue
                a[0] += weight_sum
                a[1] += contrib
                a[2].append(term)
                continue
            if a is None:
                acc[i] = [weight_sum, term_idf * (best / _MAX_FIELD_WEIGHT), [term]]
                continue
            a[0] += weight_sum
            a[1] += term_idf * (best / _MAX_FIELD_WEIGHT)
            a[2].append(term)
    length_factors = index.length_factors
    tie_breaks = index.tie_breaks
    for i in sorted(acc):
        (path, fm) = lessons[i]
        score, covered, matched = acc[i]

        if scorer == SCORER_COUNT:
            rel = score
        elif scorer == SCORER_BM25:
            if total_query_idf > 0 and matched:
                rel = min(1.0, covered / total_query_idf)
            else:
                rel = 0.0
        elif total_query_idf > 0 and matched:
            lam = length_factors[i] if length_factors else _length_factor(index.n_terms[i], index.avg_field_len)
            rel = min(1.0, (covered * lam) / total_query_idf)
        else:
            rel = 0.0

        if score > 0 and (scorer == SCORER_ADAPTIVE and adaptive_tail or rel >= floor):
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
            scored.append((
                adjusted, score, importance, uses,
                path, fm, slug, matched, rel, reliability_adj, recency_adj, graph_adj,
            ))

    if scorer == SCORER_ADAPTIVE and adaptive_tail and scored:
        adaptive_floor = max(
            floor,
            max(item[8] for item in scored) * _adaptive_tail_ratio(len(query_terms)),
        )
        scored = [item for item in scored if item[8] >= adaptive_floor]

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
            graph_adjustment=round(graph_adj, 6) if graph_boost_lookup else 0.0,
        )
        for (_adj, score, _imp, _uses, path, fm, slug, matched, rel,
             reliability_adj, recency_adj, graph_adj) in top
    ]


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
