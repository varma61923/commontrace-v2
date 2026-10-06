"""Differential oracle for the numeric candidate accumulator.

The oracle deliberately uses the previous per-document dictionary/list algorithm
and stable full sorting, independently of candidate masks and partial selection.
"""
from __future__ import annotations

import math
import random

import pytest

from commontrace import retrieval as r


def _reference(task, lessons, *, scorer, top_k=17, floor=0.0,
               adaptive_tail=True, reliability_lookup=None, reliability_weight=0.0,
               recency_lookup=None, recency_weight=0.0,
               graph_boost_lookup=None, graph_weight=0.0):
    terms = r._terms_for(scorer, r._tokenize(task))
    if not terms:
        return []
    index = r._build_index(lessons, None, scorer)
    denom = len(terms) * index.max_idf
    acc = {}
    for term in sorted(terms):
        post = index.postings.get(term)
        if post is None:
            continue
        idf = r._idf(index.n_docs, index.doc_freq[term])
        for i, weight, best in zip(*post):
            item = acc.setdefault(i, [0.0, 0.0, []])
            item[0] += weight
            item[1] += (r._bm25_term(weight, idf, index.n_terms[i], index.avg_field_len)
                        if scorer == r.SCORER_BM25 else idf * (best / 2.0))
            item[2].append(term)
    rows = []
    for i in sorted(acc):
        score, coverage, matched = acc[i]
        rel = (score if scorer == r.SCORER_COUNT else
               0.0 if denom <= 0 else
               min(1.0, coverage / denom) if scorer == r.SCORER_BM25 else
               min(1.0, coverage * index.length_factors[i] / denom))
        if score <= 0:
            continue
        path, fm = lessons[i]
        slug = str(fm.get("name", ""))
        ra = reliability_lookup.get(slug, 0.0) if reliability_lookup else 0.0
        ca = recency_lookup.get(slug, 0.0) if recency_lookup else 0.0
        ga = graph_boost_lookup.get(slug, 0.0) if graph_boost_lookup else 0.0
        adjusted = min(1.0, max(0.0, rel + reliability_weight * ra
                                + recency_weight * ca + graph_weight * ga))
        rows.append((adjusted, score, *index.tie_breaks[i], rel,
                     r.RankedLesson(path, slug, str(fm.get("description", "")),
                                    score, matched, round(rel, 6), scorer,
                                    round(ra, 6), round(ca, 6), round(ga, 6))))
    if scorer == r.SCORER_ADAPTIVE and adaptive_tail:
        floor = max(floor, max((row[4] for row in rows), default=0.0)
                    * r._adaptive_tail_ratio(len(terms)))
    eligible = [row for row in rows if row[4] >= floor]
    eligible.sort(key=lambda row: row[:4], reverse=True)
    return [row[-1].__dict__ for row in eligible[:max(0, top_k)]]


@pytest.mark.parametrize("scorer", r.LEXICAL_SCORERS)
def test_numeric_accumulator_matches_original_full_sort(scorer):
    rng = random.Random(20261006)
    words = "retry retries timeout backoff schema indexing migration cursor uploads 北京 数据库".split()
    lessons = []
    for i in range(128):
        pick = lambda n: " ".join(rng.choices(words, k=n))  # noqa: E731
        lessons.append((f"p{i}", {"name": str(i), "description": pick(7),
                                 "applies_when": pick(3), "tags": rng.sample(words, 3),
                                 "domain": pick(1), "importance": rng.randrange(5),
                                 "uses": rng.randrange(10)}))
    lookup = {str(i): rng.uniform(-1, 1) for i in range(128)}
    for j in range(48):
        query = " ".join(rng.choices(words + ["unknownword"], k=rng.randrange(1, 8)))
        options = {"scorer": scorer, "top_k": j % 21,
                   "floor": [0.0, 0.08, 0.3, math.nan][j % 4],
                   "adaptive_tail": bool(j % 2),
                   "reliability_lookup": lookup, "reliability_weight": -0.1,
                   "recency_lookup": lookup, "recency_weight": 0.2,
                   "graph_boost_lookup": lookup, "graph_weight": 0.3}
        assert [row.__dict__ for row in r.rank_lessons(query, lessons, **options)] == \
            _reference(query, lessons, **options)


def test_sparse_candidate_discovery_keeps_corpus_ties_and_independent_terms():
    lessons = [("first", {"name": "first", "description": "zebra"}),
               ("second", {"name": "second", "description": "alpha"})]
    lessons += [(f"p{i}", {"description": "unrelated"}) for i in range(100)]
    rows = r.rank_lessons("alpha zebra", lessons, floor=0.0, adaptive_tail=False)
    assert [row.path for row in rows] == ["first", "second"]
    rows[0].matched_terms.append("mutated")
    assert rows[1].matched_terms == ["alpha"]
    assert not r.rank_lessons("alpha zebra", lessons, floor=math.nan)


@pytest.mark.parametrize("scorer", [r.SCORER_ADAPTIVE, r.SCORER_COUNT])
def test_large_absent_query_preserves_normalization_and_exact_evidence(scorer):
    # All absent terms sort before the matched word; masks must not represent
    # those absent positions, and IDF normalization must still count them.
    lessons = [(f"p{i}", {"description": "zebra", "name": str(i)}) for i in range(256)]
    task = " ".join(f"absent{i}" for i in range(10_000)) + " zebra"
    options = {"scorer": scorer, "floor": 0.0, "top_k": 3}
    assert [row.__dict__ for row in r.rank_lessons(task, lessons, **options)] == \
        _reference(task, lessons, **options)


@pytest.mark.parametrize("scorer", r.LEXICAL_SCORERS)
@pytest.mark.parametrize("present", [80, 1024])
def test_large_disjoint_present_query_uses_per_hit_matching(scorer, present):
    lessons = [(f"p{i}", {"description": f"token{i}", "name": str(i)}) for i in range(1024)]
    task = " ".join(f"token{i}" for i in range(present))
    options = {"scorer": scorer, "floor": 0.0, "top_k": 17}
    assert [row.__dict__ for row in r.rank_lessons(task, lessons, **options)] == \
        _reference(task, lessons, **options)
