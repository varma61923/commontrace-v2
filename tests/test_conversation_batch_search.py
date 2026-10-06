"""Exact batched recall must preserve independent rankings, scopes and bounded scans."""
import pytest

from commontrace.conversation import Options, Store, embed, recall, search


class Encoder:
    tag = "batch-test"

    def __init__(self, np, dimensions=24):
        self.np = np
        self.dimensions = dimensions
        self.batches = []

    def vectors(self, items):
        self.batches.append(len(items))
        rows = []
        for _hash, body in items:
            # Twin passages produce exact score ties at top-k boundaries.
            seed = int(body.split()[-1]) // 2
            v = self.np.random.default_rng(seed).normal(size=self.dimensions).astype(self.np.float32)
            rows.append(v / self.np.linalg.norm(v))
        return self.np.array(rows).astype(self.np.float16).astype(self.np.float32)


@pytest.mark.parametrize("limit", [1, 7, 200])
@pytest.mark.parametrize("streaming", [False, True])
def test_batched_topk_matches_high_precision_reference_with_boundary_ties(tmp_path, monkeypatch, limit, streaming):
    np = pytest.importorskip("numpy")
    if streaming:
        monkeypatch.setattr(embed, "MAX_INDEX_BYTES", 1)
    monkeypatch.setattr(embed, "SCAN_BATCH", 19)
    with Store(str(tmp_path), "exact") as store:
        store.add("s", [{"text": f"passage {i}"} for i in range(122)])
        e = Encoder(np)
        queries = np.random.default_rng(2026).normal(size=(5, e.dimensions)).astype(np.float32)
        units = store.units()
        ids = [u[0] for u in units]
        vectors = e.vectors([(h, body) for _u, _t, body, h in units])
        expected = []
        for q in queries:
            scores = vectors.astype(np.float64) @ q.astype(np.float64)
            order = sorted(range(len(ids)), key=lambda i: (-scores[i], ids[i]))[:limit]
            expected.append([(ids[i], scores[i]) for i in order])
        actual = embed.search_many(store, e, queries, limit)
        for page, reference in zip(actual, expected):
            assert [u for u, _s in page] == [u for u, _s in reference]
            np.testing.assert_allclose([s for _u, s in page], [s for _u, s in reference], atol=1e-6, rtol=1e-6)
        assert all(size <= 19 for size in e.batches[1:])


def test_filters_and_many_query_batches_keep_exact_results_without_reembedding_excluded_sessions(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    monkeypatch.setattr(embed, "QUERY_BATCH", 3)
    monkeypatch.setattr(embed, "SCAN_BATCH", 11)
    monkeypatch.setattr(embed, "MAX_INDEX_BYTES", 1)
    with Store(str(tmp_path), "filters") as store:
        store.add("public", [{"text": f"passage {i}"} for i in range(22)])
        store.add("private", [{"text": f"passage {i}"} for i in range(22, 44)])
        e = Encoder(np)
        queries = np.random.default_rng(42).normal(size=(8, e.dimensions)).astype(np.float32)
        allowed = {t.id for t in store.session_turns("public")}
        actual = embed.search_many(store, e, queries, 5, allowed=allowed)
        assert len(actual) == 8
        assert sum(e.batches) == 22 * 3  # bounded column batches; eligible rows only
        for q, hits in zip(queries, actual):
            assert set(store.unit_turns(u for u, _s in hits).values()) <= allowed
            single = embed.search(store, e, q, 5, allowed=allowed)
            assert [u for u, _s in single] == [u for u, _s in hits]
        assert embed.search_many(store, e, queries, 5, allowed=set()) == [[] for _ in queries]
        assert embed.search_many(store, e, queries, 0) == [[] for _ in queries]
        assert embed.search_many(store, e, np.empty((0, e.dimensions)), 5) == []


def test_stale_reader_snapshot_cannot_replace_newer_batched_index(tmp_path):
    np = pytest.importorskip("numpy")
    root = str(tmp_path)
    with Store(root, "snapshots") as reader, Store(root, "snapshots") as writer:
        writer.add("old", [{"text": "passage 0"}])
        e = Encoder(np)
        queries = np.ones((2, e.dimensions), dtype=np.float32)
        with reader.read_snapshot():
            old_stamp = reader.unit_stamp()
            writer.add("new", [{"text": "passage 2"}])
            latest = embed.search_many(writer, e, queries, 2)
            older = embed.search_many(reader, e, queries, 2)
            assert all(len(page) == 1 for page in older)
            index = embed._INDEX[(writer.path, writer._units_identity, e.tag)]
            assert index.stamp == writer.unit_stamp() and index.stamp != old_stamp
        assert embed.search_many(reader, e, queries, 2) == latest
        assert sum(e.batches) == 2


def test_recall_encodes_multiple_facets_together_and_preserves_each_answer(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    calls = []

    class FacetEncoder:
        tag = "facets"

        def encode(self, texts, query=False):
            calls.append((list(texts), query))
            return np.array([[1, 1] for _ in texts], dtype=np.float32)

        def vectors(self, items):
            return np.array([[1, 1] for _ in items], dtype=np.float32)

    e = FacetEncoder()
    e.np = np
    monkeypatch.setattr(search, "_embedder", lambda *_: e)
    with Store(str(tmp_path), "facets") as store:
        store.add("color", [{"text": "Project Zephyr color is sky blue."}])
        store.add("budget", [{"text": "Project Zephyr budget is 120 units."}])
        store.add("venue", [{"text": "Project Zephyr venue is Oslo."}])
        opts = Options(rerank=None, graph_hops=0, neighbours_before=0, neighbours_after=0,
                       profile_facts=0, instructions=0, summaries=False)
        result = recall(store, "Summarize Project Zephyr, including color, budget and venue.", options=opts)
        assert len(calls) == 1 and calls[0][1] and len(calls[0][0]) > 1
        assert all(text in result.context for text in ("sky blue", "120 units", "Oslo"))
        calls.clear()
        empty = recall(store, "Where is the venue?", options=Options(sessions=("missing",), rerank=None))
        assert not calls and not empty.turns


@pytest.mark.parametrize("invalid", [[[float("nan"), 0]], [[float("inf"), 0]], [1, 0]])
def test_invalid_vectors_fail_before_corpus_encoding(tmp_path, invalid):
    np = pytest.importorskip("numpy")
    with Store(str(tmp_path), "invalid") as store:
        e = Encoder(np)
        with pytest.raises(ValueError, match="finite two-dimensional"):
            embed.search_many(store, e, invalid, 3)
        assert not e.batches
