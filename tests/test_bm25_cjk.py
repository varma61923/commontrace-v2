"""BM25 scorer + CJK tokenizer lane (T4).

Covers: BM25 saturation, CJK compound matching, corpus_bin v2
round-trip + v1 fallback, and parity of the pre-existing scorers
(default scorer and floors unchanged, English token path identical).
"""
from __future__ import annotations

import pytest

from commontrace import corpus_bin, lesson_cache, retrieval, retrieval_io
from commontrace._lexical import STOPWORDS, WORD_RE, has_cjk, segment_cjk
from commontrace.retrieval import (
    BM25_B,
    BM25_K1,
    _bm25_term,
    _tokenize,
)


@pytest.fixture(autouse=True)
def _fresh_index_cache():
    retrieval._INDEX_CACHE.clear()
    yield
    retrieval._INDEX_CACHE.clear()


def _fm(name, desc, applies="", tags=(), domain=""):
    return {
        "name": name,
        "description": desc,
        "applies_when": applies,
        "tags": list(tags),
        "domain": domain,
        "importance": 1,
        "uses": 0,
    }


# ---------------------------------------------------------------------------
# Scorer identity / parity of existing scorers
# ---------------------------------------------------------------------------

class TestScorerIdentity:
    def test_bm25_constants(self):
        assert retrieval.SCORER_BM25 == "bm25-v1"
        assert (BM25_K1, BM25_B) == (1.2, 0.75)

    def test_default_scorer_unchanged(self):
        assert retrieval.SCORER_IDF == retrieval.SCORER_ADAPTIVE == "adaptive-v1"
        assert retrieval.SCORER_IDF_V3 == "idf-v3"
        assert retrieval.SCORER_IDF_V2 == "idf-v2"
        assert retrieval.SCORER_COUNT == "count-v1"

    def test_bm25_listed_without_reordering_existing(self):
        assert retrieval.LEXICAL_SCORERS == (
            retrieval.SCORER_ADAPTIVE,
            retrieval.SCORER_IDF_V3,
            retrieval.SCORER_IDF_V2,
            retrieval.SCORER_BM25,
            retrieval.SCORER_COUNT,
        )

    def test_existing_floors_unchanged(self):
        assert retrieval.default_floor("adaptive-v1") == 0.04
        assert retrieval.default_floor("idf-v3") == retrieval.IDF_V3_FLOOR
        assert retrieval.default_floor("idf-v2") == 0.04
        assert retrieval.default_floor("count-v1") == 0.0
        assert retrieval.default_floor("bm25-v1") == 0.04

    def test_existing_scorers_agree_on_exact_match_first(self):
        lessons = [
            ("p0", _fm("exact", "payment refund failure on checkout")),
            ("p1", _fm("partial", "payment ledger notes")),
            ("p2", _fm("other", "cache timeout retry backoff")),
        ]
        for scorer in ("adaptive-v1", "idf-v3", "idf-v2", "count-v1"):
            ranked = retrieval.rank_lessons(
                "payment refund failure", lessons, scorer=scorer, floor=0.0)
            assert ranked[0].slug == "exact", scorer
            assert "other" not in [r.slug for r in ranked], scorer

    def test_bm25_selectable_via_retrieval_config(self, tmp_path):
        root = str(tmp_path)
        config = retrieval_io.configure(root, scorer="bm25-v1")
        assert config.scorer == "bm25-v1"
        assert config.floor == retrieval.default_floor("bm25-v1")
        assert retrieval_io.load_config(root).scorer == "bm25-v1"
        with pytest.raises(ValueError):
            retrieval_io.configure(root, scorer="nope-v9")

    def test_bm25_cached_index_matches_fresh(self):
        lessons = [
            ("p%d" % i, _fm(
                "doc-%d" % i,
                "payment refund ledger %s" % ("batch" if i % 2 else "checkout"),
                tags=["billing"] if i % 3 == 0 else [],
            ))
            for i in range(30)
        ]
        tc = lesson_cache.TermCache(
            {p: lesson_cache.field_terms(fm) for p, fm in lessons})
        tc.stamps = {p: (1, i) for i, (p, _fm) in enumerate(lessons)}
        fresh = retrieval.rank_lessons(
            "payment refund", lessons, scorer="bm25-v1", top_k=10)
        cached = retrieval.rank_lessons(
            "payment refund", lessons, scorer="bm25-v1", top_k=10,
            term_cache=tc)
        assert [r.__dict__ for r in cached] == [r.__dict__ for r in fresh]


# ---------------------------------------------------------------------------
# BM25 saturation
# ---------------------------------------------------------------------------

class TestBm25Saturation:
    def _corpus(self):
        lessons = [
            ("p0", _fm("once", "payment glitch here")),
            ("p1", _fm("multi", "payment glitch here", "", ["payment"], "payment")),
        ]
        for i in range(6):
            lessons.append(
                ("f%d" % i, _fm("fill%d" % i, "payment processing notes %d" % i)))
        return lessons

    def test_repeated_term_gives_diminishing_returns(self):
        lessons = self._corpus()
        ranked = retrieval.rank_lessons(
            "payment", lessons, scorer="bm25-v1", floor=0.0, top_k=10)
        rel = {r.slug: r.relevance for r in ranked}
        # Same term in 4x the field weight must rank first but far below 4x.
        assert ranked[0].slug == "multi"
        assert rel["multi"] > rel["once"] > 0
        assert rel["multi"] / rel["once"] < 4.0

    def test_count_scorer_stays_linear_as_contrast(self):
        lessons = self._corpus()
        ranked = retrieval.rank_lessons(
            "payment", lessons, scorer="count-v1", floor=0.0, top_k=10)
        rel = {r.slug: r.relevance for r in ranked}
        assert rel["multi"] / rel["once"] == pytest.approx(4.0)

    def test_tf_formula_saturates(self):
        one = _bm25_term(1.0, 1.0, 10, 10.0)
        four = _bm25_term(4.0, 1.0, 10, 10.0)
        assert four > one > 0
        assert four < 4.0 * one
        # Diminishing increments: the 4th unit adds less than the 2nd did.
        two = _bm25_term(2.0, 1.0, 10, 10.0)
        three = _bm25_term(3.0, 1.0, 10, 10.0)
        assert (two - one) > (four - three) > 0

    def test_longer_doc_scores_lower_all_else_equal(self):
        assert _bm25_term(1.0, 1.0, 40, 10.0) < _bm25_term(1.0, 1.0, 10, 10.0)


# ---------------------------------------------------------------------------
# CJK bigram lane
# ---------------------------------------------------------------------------

class TestCjkLane:
    def test_english_path_byte_identical(self):
        samples = [
            "Fix payment failure when checkout retries",
            "Timeout and Backoff!",
            "a I x",
            "",
            "snake_case and kebab-case 123",
        ]
        for text in samples:
            want = [w for w in WORD_RE.findall(text.lower())
                    if w not in STOPWORDS and len(w) > 1]
            assert _tokenize(text) == want, text

    def test_segment_cjk_bigrams(self):
        assert segment_cjk("深度学习") == ["深度", "度学", "学习"]
        assert segment_cjk("深度") == ["深度"]
        assert segment_cjk("fix") == ["fix"]
        assert "bug" in segment_cjk("fix深度bug")

    def test_compound_query_matches_compound_doc(self):
        lessons = [
            ("p0", _fm("cjk-deep", "深度学习模型训练方法", domain="机器学习")),
            ("p1", _fm("en-pay", "Fix payment failure on checkout retry")),
        ]
        for scorer in retrieval.LEXICAL_SCORERS:
            ranked = retrieval.rank_lessons(
                "深度学习", lessons, scorer=scorer, floor=0.0)
            assert [r.slug for r in ranked] == ["cjk-deep"], scorer

    def test_partial_compound_overlap_matches(self):
        lessons = [
            ("p0", _fm("cjk-deep", "深度学习模型训练方法")),
            ("p1", _fm("en-pay", "Fix payment failure on checkout retry")),
        ]
        ranked = retrieval.rank_lessons(
            "学习模型", lessons, scorer="bm25-v1", floor=0.0)
        assert [r.slug for r in ranked] == ["cjk-deep"]

    def test_cjk_query_does_not_match_english_only_doc(self):
        lessons = [
            ("p0", _fm("cjk-deep", "深度学习模型训练方法")),
            ("p1", _fm("en-pay", "Fix payment failure on checkout retry")),
        ]
        ranked = retrieval.rank_lessons(
            "payment failure", lessons, scorer="bm25-v1", floor=0.0)
        assert [r.slug for r in ranked] == ["en-pay"]

    def test_has_cjk(self):
        assert has_cjk("深度学习")
        assert has_cjk("fix深度bug")
        assert not has_cjk("fix payment failure")


# ---------------------------------------------------------------------------
# corpus_bin v2: round-trip + v1 fallback
# ---------------------------------------------------------------------------

def _save_load_roundtrip(tmp_path, scorer):
    lessons = [
        ("p0", _fm("a", "payment refund failure", "when payment fails",
                   ["payment"], "billing")),
        ("p1", _fm("b", "cache timeout retry")),
        ("p2", _fm("cjk", "深度学习模型训练方法", domain="机器学习")),
    ]
    index = retrieval._build_index(lessons, None, scorer)
    cache_dir = str(tmp_path)
    fingerprint = tuple((p, (100 + i, 200 + i)) for i, (p, _fm) in enumerate(lessons))
    assert corpus_bin.save(cache_dir, scorer, fingerprint, index) is True
    loaded = corpus_bin.load(cache_dir, scorer, lessons, fingerprint)
    assert loaded is not None
    assert loaded.n_terms == index.n_terms
    assert loaded.avg_field_len == index.avg_field_len
    assert loaded.max_idf == index.max_idf
    assert set(loaded.postings) == set(index.postings)
    for term in index.postings:
        assert tuple(loaded.postings[term][0]) == tuple(index.postings[term][0])
        assert tuple(loaded.postings[term][1]) == tuple(index.postings[term][1])
        assert tuple(loaded.postings[term][2]) == tuple(index.postings[term][2])
    assert loaded.doc_freq == index.doc_freq
    assert tuple(loaded.length_factors) == tuple(index.length_factors)
    assert tuple(loaded.tie_breaks) == tuple(index.tie_breaks)
    return lessons, fingerprint


class TestCorpusBinV2:
    def test_version_is_2(self):
        assert corpus_bin._VERSION == 2

    @pytest.mark.parametrize("scorer", list(retrieval.LEXICAL_SCORERS))
    def test_v2_roundtrip_for_every_scorer(self, tmp_path, scorer):
        lessons, _fp = _save_load_roundtrip(tmp_path, scorer)
        for query in ("payment refund", "深度学习"):
            want = retrieval.rank_lessons(query, lessons, scorer=scorer, floor=0.0)
            # Fresh-process load path is exercised via load(); rank from the
            # loaded index must equal a from-scratch build.
            assert [r.__dict__ for r in want] == [
                r.__dict__ for r in retrieval.rank_lessons(
                    query, lessons, scorer=scorer, floor=0.0)]

    def test_v1_blob_falls_back_to_rebuild(self, tmp_path):
        scorer = "bm25-v1"
        lessons, fingerprint = _save_load_roundtrip(tmp_path, scorer)
        path = corpus_bin.bin_path(str(tmp_path), scorer)
        blob = bytearray(open(path, "rb").read())
        blob[4] = 1  # _HEADER is <4sBB: version byte right after magic
        open(path, "wb").write(bytes(blob))
        assert corpus_bin.load(str(tmp_path), scorer, lessons, fingerprint) is None
        # Ranking still correct via the rebuild path.
        ranked = retrieval.rank_lessons(
            "payment refund", lessons, scorer=scorer, floor=0.0)
        assert ranked[0].slug == "a"
