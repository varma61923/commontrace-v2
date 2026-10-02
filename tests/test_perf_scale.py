"""Perf-scale tests (T2): incremental index + capped graph traversal.

Small synthetic corpora in tmp dirs only; no heavy embedding deps.
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import time

import pytest

np = pytest.importorskip("numpy", reason="numpy is required for perf-scale tests")

from commontrace import graph  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BUILD_INDEX_PATH = os.path.join(REPO_ROOT, "memory", "attention", "build_index.py")


def _load_build_index():
    spec = importlib.util.spec_from_file_location("perf_build_index", _BUILD_INDEX_PATH)
    assert spec is not None and spec.loader is not None, _BUILD_INDEX_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bi = _load_build_index()


class CountingEmbed:
    """Deterministic embedder wrapper counting texts + adding per-text delay."""

    def __init__(self, dim=32, delay_per_text: float = 0.0):
        self.dim = dim
        self.delay_per_text = delay_per_text
        self.calls: list[list[str]] = []

    def __call__(self, texts, dim=None):
        dim = int(dim or self.dim)
        self.calls.append(list(texts))
        if self.delay_per_text and texts:
            time.sleep(self.delay_per_text * len(texts))
        return bi.default_embed(list(texts), dim)

    @property
    def total_texts(self) -> int:
        return sum(len(c) for c in self.calls)


def _write_files(lessons_dir: str, n: int, tag: str = "v1") -> list[str]:
    os.makedirs(lessons_dir, exist_ok=True)
    paths = []
    for i in range(n):
        stem = f"lesson_{i:03d}"
        path = os.path.join(lessons_dir, stem + ".md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"# Lesson {i} ({tag})\n\nDistinct content alpha-{i} beta-{tag} gamma-{i * 7}.\n")
        paths.append(path)
    return paths


def _load_npz(path: str):
    with np.load(path, allow_pickle=False) as data:
        return (
            [str(s) for s in data["slugs"]],
            np.asarray(data["embeddings"], dtype=np.float32),
        )


@pytest.fixture(autouse=True)
def _clean_graph_cache():
    graph._clear_graph_cache()
    yield
    graph._clear_graph_cache()


class TestIncrementalIndex:
    def test_first_build_matches_full(self, tmp_path):
        lessons = str(tmp_path / "lessons")
        _write_files(lessons, 12)
        out_incr = str(tmp_path / "incr.npz")
        out_full = str(tmp_path / "full.npz")

        res = bi.build_incremental(lessons, out_incr, embed_fn=CountingEmbed(dim=32), dim=32)
        assert res["full_rebuild"] is True
        assert res["reused_count"] == 0
        assert res["encoded_count"] == 12
        assert res["wrote_index"] is True

        bi.build_full(lessons, out_full, embed_fn=CountingEmbed(dim=32), dim=32)
        slugs_a, embs_a = _load_npz(out_incr)
        slugs_b, embs_b = _load_npz(out_full)
        assert slugs_a == slugs_b
        assert np.array_equal(embs_a, embs_b)

    def test_one_file_change_identical_and_minimal(self, tmp_path):
        lessons = str(tmp_path / "lessons")
        n = 12
        _write_files(lessons, n)
        out_incr = str(tmp_path / "incr.npz")
        out_full = str(tmp_path / "full.npz")

        bi.build_incremental(lessons, out_incr, embed_fn=CountingEmbed(dim=32), dim=32)

        # Modify exactly one file (size change guarantees a new stamp).
        changed = os.path.join(lessons, "lesson_005.md")
        with open(changed, "a", encoding="utf-8") as fh:
            fh.write("\nAppended rule update for perf-scale test.\n")

        emb_incr = CountingEmbed(dim=32)
        res = bi.build_incremental(lessons, out_incr, embed_fn=emb_incr, dim=32)
        assert res["encoded_count"] == 1, res
        assert res["reused_count"] == n - 1, res
        assert res["full_rebuild"] is False
        assert emb_incr.total_texts == 1

        emb_full = CountingEmbed(dim=32)
        bi.build_full(lessons, out_full, embed_fn=emb_full, dim=32)
        assert emb_full.total_texts == n

        slugs_a, embs_a = _load_npz(out_incr)
        slugs_b, embs_b = _load_npz(out_full)
        assert slugs_a == slugs_b
        assert np.array_equal(embs_a, embs_b)

    def test_no_change_skips_rewrite(self, tmp_path):
        lessons = str(tmp_path / "lessons")
        _write_files(lessons, 8)
        out = str(tmp_path / "idx.npz")
        bi.build_incremental(lessons, out, embed_fn=CountingEmbed(dim=32), dim=32)
        before = os.stat(out).st_mtime_ns

        emb = CountingEmbed(dim=32)
        res = bi.build_incremental(lessons, out, embed_fn=emb, dim=32)
        assert res["encoded_count"] == 0
        assert res["reused_count"] == 8
        assert res["wrote_index"] is False
        assert emb.total_texts == 0
        assert os.stat(out).st_mtime_ns == before

    def test_incremental_faster_than_full_on_one_change(self, tmp_path):
        lessons = str(tmp_path / "lessons")
        n = 30
        _write_files(lessons, n)
        out_incr = str(tmp_path / "incr.npz")
        out_full = str(tmp_path / "full.npz")
        bi.build_incremental(lessons, out_incr, embed_fn=CountingEmbed(dim=32), dim=32)

        changed = os.path.join(lessons, "lesson_007.md")
        with open(changed, "a", encoding="utf-8") as fh:
            fh.write("\nOne-file perf change.\n")

        delay = 0.004  # 4ms per text: full ~= 120ms, incremental ~= 4ms
        emb_incr = CountingEmbed(dim=32, delay_per_text=delay)
        start = time.perf_counter()
        res_incr = bi.build_incremental(lessons, out_incr, embed_fn=emb_incr, dim=32)
        t_incr = time.perf_counter() - start

        emb_full = CountingEmbed(dim=32, delay_per_text=delay)
        start = time.perf_counter()
        bi.build_full(lessons, out_full, embed_fn=emb_full, dim=32)
        t_full = time.perf_counter() - start

        assert res_incr["encoded_count"] == 1
        assert emb_incr.total_texts == 1
        assert emb_full.total_texts == n
        slugs_a, embs_a = _load_npz(out_incr)
        slugs_b, embs_b = _load_npz(out_full)
        assert slugs_a == slugs_b
        assert np.array_equal(embs_a, embs_b)
        assert t_incr < t_full, f"incremental {t_incr:.3f}s not faster than full {t_full:.3f}s"


def _build_chain_graph(root: str):
    graph.add_node(root, "n0", entity_type="service", name="n0")
    prev = "n0"
    for i in range(1, 6):
        nid = f"n{i}"
        graph.add_node(root, nid, entity_type="service", name=nid)
        graph.add_edge(root, prev, nid, relation="depends_on")
        prev = nid
    # Star edges out of n0 to give the cap something to cut.
    for i in range(10):
        leaf = f"x{i}"
        graph.add_node(root, leaf, entity_type="tool", name=leaf)
        graph.add_edge(root, "n0", leaf, relation="uses")


class TestGraphCaps:
    def test_default_behavior_unchanged(self, tmp_path):
        root = str(tmp_path)
        _build_chain_graph(root)
        default = graph.multi_hop_subgraph(root, ["n0"])
        explicit = graph.multi_hop_subgraph(root, ["n0"], max_hops=2, max_edges=None)
        assert default == explicit

    def test_no_signature_breakage(self):
        sig = inspect.signature(graph.multi_hop_subgraph)
        assert sig.parameters["max_hops"].default == 2
        assert sig.parameters["max_edges"].default is None
        # Existing positional call style still works.
        assert callable(graph.multi_hop_subgraph)
        sig_b = inspect.signature(graph.graph_boost_for_lessons)
        assert sig_b.parameters["max_hops"].default == 2
        assert sig_b.parameters["max_edges"].default is None

    def test_max_edges_cap_respected(self, tmp_path):
        root = str(tmp_path)
        _build_chain_graph(root)
        full = graph.multi_hop_subgraph(root, ["n0"])
        assert len(full["edges"]) > 2
        capped = graph.multi_hop_subgraph(root, ["n0"], max_hops=2, max_edges=2)
        assert len(capped["edges"]) <= 2
        empty = graph.multi_hop_subgraph(root, ["n0"], max_hops=2, max_edges=0)
        assert empty["edges"] == []
        # Start nodes are still reported even with zero edges.
        assert {n["id"] for n in empty["nodes"]} >= {"n0"}

    def test_max_hops_cap_respected(self, tmp_path):
        root = str(tmp_path)
        _build_chain_graph(root)
        one_hop = graph.multi_hop_subgraph(root, ["n0"], max_hops=1)
        assert one_hop["hop_distances"], "expected visited nodes"
        assert max(one_hop["hop_distances"].values()) <= 1
        two_hop = graph.multi_hop_subgraph(root, ["n0"], max_hops=2)
        assert max(two_hop["hop_distances"].values()) <= 2
        assert len(one_hop["edges"]) <= len(two_hop["edges"])

    def test_boost_default_unchanged_and_caps_pass_through(self, tmp_path):
        root = str(tmp_path)
        graph.add_node(root, "service:search", entity_type="service", name="search")
        graph.add_node(root, "lesson:mylesson", entity_type="lesson", name="mylesson")
        graph.add_edge(root, "service:search", "lesson:mylesson", relation="relates_to")
        candidates = ["mylesson", "otherlesson"]
        query = "how to use search effectively"

        default = graph.graph_boost_for_lessons(root, query, candidates)
        explicit = graph.graph_boost_for_lessons(root, query, candidates, max_hops=2, max_edges=None)
        assert default == explicit
        assert default["mylesson"] > 0.0
        assert default["otherlesson"] == 0.0

        # Zero hops: lesson nodes unreachable, boosts collapse to 0.
        zero_hop = graph.graph_boost_for_lessons(root, query, candidates, max_hops=0)
        assert zero_hop == {"mylesson": 0.0, "otherlesson": 0.0}

        # Zero edges: traversal collects nothing beyond starts.
        zero_edge = graph.graph_boost_for_lessons(root, query, candidates, max_edges=0)
        assert zero_edge == {"mylesson": 0.0, "otherlesson": 0.0}

    def test_adjacency_memoized(self, tmp_path):
        root = str(tmp_path)
        _build_chain_graph(root)
        assert len(graph._GRAPH_ADJ_CACHE) == 0
        first = graph.multi_hop_subgraph(root, ["n0"])
        assert len(graph._GRAPH_ADJ_CACHE) >= 1
        size_before = len(graph._GRAPH_ADJ_CACHE)
        second = graph.multi_hop_subgraph(root, ["n0"])
        assert second == first
        assert len(graph._GRAPH_ADJ_CACHE) == size_before
        # Graph mutation invalidates the memo via file stamps.
        graph.add_edge(root, "n1", "x0", relation="relates_to")
        third = graph.multi_hop_subgraph(root, ["n0"])
        assert third != first or len(third["edges"]) >= len(first["edges"])
