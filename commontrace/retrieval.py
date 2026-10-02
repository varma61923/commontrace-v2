from __future__ import annotations

import heapq
import math
import re as _re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from commontrace._lexical import STOPWORDS as _STOPWORDS
from commontrace._lexical import WORD_RE as _WORD_RE
from commontrace._stem import stem as _stem

IDF_V2_FLOOR = 0.04

SCORER_ADAPTIVE = "adaptive-v1"
SCORER_IDF_V3 = "idf-v3"
SCORER_IDF_V2 = "idf-v2"
SCORER_IDF = SCORER_ADAPTIVE
SCORER_COUNT = "count-v1"
LEXICAL_SCORERS = (SCORER_ADAPTIVE, SCORER_IDF_V3, SCORER_IDF_V2, SCORER_COUNT)

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
    if scorer in (SCORER_ADAPTIVE, SCORER_IDF_V2):
        return IDF_V2_FLOOR
    return IDF_V3_FLOOR

_LENGTH_B = 0.5
_LENGTH_CLAMP = (0.5, 1.5)


def _tokenize(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1]


def _terms_for(scorer: str, terms) -> set[str]:
    if scorer in (SCORER_ADAPTIVE, SCORER_IDF_V3):
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
            from commontrace import corpus_bin
            persisted = corpus_bin.load(bin_dir, scorer, lessons, fingerprint)
        except Exception:
            persisted = None
        if persisted is not None:
            if key not in _INDEX_CACHE and len(_INDEX_CACHE) >= _INDEX_CACHE_MAX:
                _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
            _INDEX_CACHE[key] = (fingerprint, persisted)
            return persisted
    index = _build_index(lessons, term_cache, scorer)
    if bin_dir:
        try:
            from commontrace import corpus_bin
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
    entity_index: dict[str, list[int]] | None = None,
    entity_boost: float = 0.0,
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
    for term in sorted(query_terms):
        post = index.postings.get(term)
        if post is None:
            continue
        term_idf = query_idf.get(term, 0.0)
        for i, weight_sum, best in zip(*post):
            a = acc_get(i)
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
            entity_adj = 0.0
            if entity_boost and entity_index:
                try:
                    entity_adj = float(_entity_overlap(task, i, lessons, entity_index))
                except Exception:
                    entity_adj = 0.0
            adjusted = min(1.0, max(0.0,
                rel + reliability_weight * reliability_adj
                + recency_weight * recency_adj + graph_weight * graph_adj
                + entity_boost * entity_adj,
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


# ---------------------------------------------------------------------------
# Additive v2 extensions: entity index + hierarchical episode->fact expansion.
# All new symbols keep existing signatures default-compatible.
# ---------------------------------------------------------------------------

_ENTITY_RE = _re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,}")
_ENTITY_SPLIT_RE = _re.compile(r"[^A-Za-z0-9]+")


def _entity_tokens_for_lesson(fm: dict) -> set[str]:
    """Stdlib entity-ish tokens: tags + domain + Capitalized words.

    Lowercased for case-insensitive matching. Pure stdlib, no NLP deps.
    """
    out: set[str] = set()
    try:
        tags = fm.get("tags")
        if isinstance(tags, (list, tuple)):
            for t in tags:
                for part in _ENTITY_SPLIT_RE.split(str(t)):
                    if len(part) > 1:
                        out.add(part.lower())
        domain = fm.get("domain")
        if domain:
            for part in _ENTITY_SPLIT_RE.split(str(domain)):
                if len(part) > 1:
                    out.add(part.lower())
        for field in (fm.get("description") or "", fm.get("applies_when") or ""):
            for m in _ENTITY_RE.findall(str(field)):
                if m[0].isupper() and m.lower() not in _STOPWORDS and len(m) > 1:
                    out.add(m.lower())
    except Exception:
        pass
    return out


def _entity_tokens_for_query(task: str) -> set[str]:
    out: set[str] = set()
    try:
        for m in _ENTITY_RE.findall(task or ""):
            if m[0].isupper() and m.lower() not in _STOPWORDS and len(m) > 1:
                out.add(m.lower())
        # Also include distinctive non-stopword tokens so lowercase
        # entity mentions (e.g. tag names) still match.
        for w in _WORD_RE.findall((task or "").lower()):
            if w not in _STOPWORDS and len(w) > 2:
                out.add(w)
    except Exception:
        pass
    return out


def build_entity_index(
    lessons: list[tuple[str, dict]],
) -> dict[str, list[int]]:
    """Build a stdlib token->doc-id index mapping entity token to doc indices.

    Returns {token: sorted [doc_index, ...]}. Doc ids are positional indices
    into `lessons` so the index stays valid for that ranking call.
    """
    index: dict[str, list[int]] = {}
    for i, (_path, fm) in enumerate(lessons):
        try:
            toks = _entity_tokens_for_lesson(fm if isinstance(fm, dict) else {})
        except Exception:
            toks = set()
        for tok in toks:
            index.setdefault(tok, []).append(i)
    for tok in index:
        index[tok] = sorted(index[tok])
    return index


def _entity_overlap(
    task: str,
    doc_pos: int,
    lessons: list[tuple[str, dict]],
    entity_index: dict[str, list[int]],
) -> float:
    """Count of query entity tokens present in doc `doc_pos` (via index)."""
    if not entity_index:
        return 0.0
    qtoks = _entity_tokens_for_query(task)
    if not qtoks:
        return 0.0
    hits = 0
    for tok in qtoks:
        postings = entity_index.get(tok)
        if postings and doc_pos in postings:
            hits += 1
    return float(hits)


def _as_episode_ids(episodes: list) -> list[str]:
    """Normalize ranked episodes to an ordered id list (best first)."""
    ids: list[str] = []
    for ep in episodes or []:
        if isinstance(ep, str):
            ids.append(ep)
        elif isinstance(ep, (tuple, list)) and ep:
            ids.append(str(ep[0]))
        elif isinstance(ep, dict):
            for key in ("id", "trace_id", "episode_id", "path"):
                if ep.get(key):
                    ids.append(str(ep[key]))
                    break
        else:
            ident = getattr(ep, "id", None) or getattr(ep, "trace_id", None)
            ids.append(str(ident if ident is not None else ep))
    return ids


def _fact_id_of(fact: Any) -> str:
    if isinstance(fact, dict):
        for key in ("id", "fact_id"):
            if fact.get(key):
                return str(fact[key])
        return str(fact.get("statement", fact))
    ident = getattr(fact, "id", None)
    if ident:
        return str(ident)
    return str(fact)


def _fact_sources_of(fact: Any) -> list[str]:
    if isinstance(fact, dict):
        srcs = fact.get("source_traces", []) or fact.get("evidence_ids", []) or fact.get("sources", [])
    else:
        srcs = getattr(fact, "source_traces", None)
        if srcs is None:
            srcs = getattr(fact, "evidence_ids", [])
    try:
        return [str(s) for s in (srcs or [])]
    except Exception:
        return []


def _fact_confidence_of(fact: Any) -> float:
    if isinstance(fact, dict):
        try:
            return float(fact.get("confidence", 0.5))
        except (TypeError, ValueError):
            return 0.5
    try:
        return float(getattr(fact, "confidence", 0.5))
    except (TypeError, ValueError):
        return 0.5


def hierarchical_expand(
    episodes: list,
    facts: list,
    top_n: int = 10,
    min_score: float = 0.0,
    rrf_k: int = DEFAULT_RRF_K,
) -> list[tuple[str, float]]:
    """Expand ranked episodes to linked facts with RRF fusion + floor eviction.

    - `episodes`: ranked episode/trace ids (best first); accepts str ids,
      (id, score) pairs, or dicts/objects with an id field.
    - `facts`: AtomicFact-like objects or dicts carrying `source_traces`
      (episode links) and optional `confidence`.
    - Fusion: episode-position RRF summed over linked episodes, fused with a
      confidence-ranked fact arm via :func:`reciprocal_rank_fusion`.
    - Facts with fused score < `min_score` are evicted (score-floor eviction).
    - Returns [(fact_id, fused_score)] sorted best first, at most `top_n`.
    """
    ep_ids = _as_episode_ids(episodes)
    if not ep_ids or not facts:
        return []
    fact_ids = [_fact_id_of(f) for f in facts]
    ep_set = set(ep_ids)

    # Arm 1: per-fact episode linkage arm (ordered by summed episode RRF).
    ep_rrf = {eid: 1.0 / (rrf_k + pos) for pos, eid in enumerate(ep_ids, start=1)}
    # Arm 2: confidence arm (facts ranked by confidence desc).
    order = sorted(range(len(facts)), key=lambda i: (-_fact_confidence_of(facts[i]), fact_ids[i]))
    conf_ranked = [fact_ids[i] for i in order]
    # Arm 3: linkage arm order — facts with stronger episode linkage first so
    # RRF fusion rewards episode fan-out even when confidences tie.
    link_strength: dict[str, float] = {}
    for fid, fact in zip(fact_ids, facts):
        linked = [s for s in _fact_sources_of(fact) if s in ep_set]
        link_strength[fid] = sum(ep_rrf.get(s, 0.0) for s in linked)
    link_ranked = sorted(fact_ids, key=lambda fid: (-link_strength.get(fid, 0.0), fid))

    arms = {"confidence": conf_ranked, "episode_link": link_ranked}
    fused = dict(
        reciprocal_rank_fusion(arms, k=rrf_k, top_k=None)
    )
    # Add raw linkage strength so directly-linked facts outrank unlinked ones
    # with identical RRF positions, while keeping RRF as the fusion backbone.
    scored: list[tuple[str, float]] = []
    for fid in fact_ids:
        score = fused.get(fid, 0.0) + link_strength.get(fid, 0.0)
        if score >= min_score:
            scored.append((fid, round(score, 6)))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored[: max(0, top_n)]
