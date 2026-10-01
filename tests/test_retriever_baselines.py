"""The shipped ranker must stay at least as good as textbook BM25 on the independently written probes."""
import os
import sys

import pytest

EVAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "commons", "eval")


@pytest.fixture(scope="module")
def results():
    sys.path.insert(0, EVAL)
    try:
        import retriever_baselines
        return retriever_baselines.evaluate(dense_models=())
    finally:
        sys.path.remove(EVAL)


def test_shipped_lexical_is_not_worse_than_bm25_or_tfidf_on_held_out_probes(results):
    held_out = results["commons"]
    for metric in ("recall@1", "recall@3", "mrr"):
        assert held_out["shipped lexical"][metric] >= held_out["Okapi BM25"][metric], metric
        assert held_out["shipped lexical"][metric] >= held_out["TF-IDF cosine"][metric], metric


def test_the_tuning_corpus_stays_saturated(results):
    assert results["fields"]["shipped lexical"]["recall@3"] == 1.0
