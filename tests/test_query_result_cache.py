from __future__ import annotations

import gc
import random
import weakref

import pytest

from commontrace import lesson_cache, retrieval


def source(label="store", n=50):
    rng = random.Random(12)
    rows = [(f"/{label}/{i}.md", {
        "name": f"lesson-{i}", "description": " ".join(rng.sample(["retry", "backoff", "timeout", "schema"], 2)),
        "importance": i % 5 + 1, "uses": i % 3,
    }) for i in range(n)]
    terms = lesson_cache.TermCache({p: lesson_cache.field_terms(fm) for p, fm in rows})
    terms.stamps = {p: (1, i) for i, (p, _) in enumerate(rows)}
    terms.lessons = rows
    terms.fingerprint = tuple(terms.stamps.items())
    terms.fingerprint_hash = hash(terms.fingerprint)
    return rows, terms


@pytest.fixture(autouse=True)
def clear():
    retrieval._INDEX_CACHE.clear()
    retrieval._QUERY_CACHE.clear()
    yield
    retrieval._INDEX_CACHE.clear()
    retrieval._QUERY_CACHE.clear()


@pytest.mark.parametrize("scorer", retrieval.LEXICAL_SCORERS)
def test_cached_rankings_match_fresh_rankings_and_do_not_share_mutable_results(scorer):
    rows, terms = source()
    for query in ("retry backoff", "timeout", "schema unknown", "unknown"):
        for k in (1, 10, 100):
            for floor in (0.0, .1, None):
                want = retrieval.rank_lessons(query, rows, top_k=k, floor=floor, scorer=scorer)
                first = retrieval.rank_lessons(query, rows, top_k=k, floor=floor, scorer=scorer, term_cache=terms)
                second = retrieval.rank_lessons(query, rows, top_k=k, floor=floor, scorer=scorer, term_cache=terms)
                assert first == second == want
                if first:
                    first[0].matched_terms.clear()
                    assert second == retrieval.rank_lessons(query, rows, top_k=k, floor=floor,
                                                            scorer=scorer, term_cache=terms)


def test_dynamic_signals_and_other_stores_never_reuse_plain_rankings():
    rows, terms = source()
    retrieval.rank_lessons("retry", rows, term_cache=terms)
    before = retrieval._QUERY_CACHE.stats()["hits"]
    lookup = {"lesson-1": 1.0}
    kwargs = {"reliability_lookup": lookup, "reliability_weight": .5}
    first = retrieval.rank_lessons("retry", rows, term_cache=terms, **kwargs)
    lookup["lesson-1"] = -1.0
    assert retrieval.rank_lessons("retry", rows, term_cache=terms, **kwargs) == retrieval.rank_lessons("retry", rows, **kwargs)
    assert any(r.reliability_adjustment == 1 for r in first)
    assert retrieval._QUERY_CACHE.stats()["hits"] == before
    other_rows, other_terms = source("other")
    assert all(r.path.startswith("/other/") for r in retrieval.rank_lessons("retry", other_rows, term_cache=other_terms))
    subset = rows[::2]
    assert retrieval.rank_lessons("retry", subset, term_cache=terms) == retrieval.rank_lessons("retry", subset)


def test_result_cache_does_not_keep_an_evicted_corpus_alive():
    rows, terms = source()
    index = retrieval._corpus_index(rows, terms, retrieval.SCORER_IDF)
    ref = weakref.ref(index)
    retrieval.rank_lessons("retry", rows, term_cache=terms)
    del index
    retrieval._INDEX_CACHE.clear()
    gc.collect()
    assert ref() is None


def test_unique_query_workloads_can_disable_result_retention(monkeypatch):
    rows, terms = source()
    expected = retrieval.rank_lessons("retry", rows, term_cache=terms)
    retrieval._QUERY_CACHE.clear()
    assert retrieval.rank_lessons("retry", rows, term_cache=terms, cache_results=False) == expected
    assert retrieval._QUERY_CACHE.stats()["entries"] == 0
    monkeypatch.setenv("COMMONTRACE_QUERY_CACHE", "0")
    assert retrieval.rank_lessons("retry", rows, term_cache=terms) == expected
    assert retrieval._QUERY_CACHE.stats()["entries"] == 0
