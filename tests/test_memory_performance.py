"""Tests for memory performance, chunked deduplication,
inverted-index lexical pruning, and incremental vector caching.
"""
import hashlib
import os

import numpy as np
import pytest

from commontrace.reference.build_index import (
    EMBEDDING_DIM,
    build_or_update_index,
)
from commontrace.reference.measure_performance import (
    SemanticDuplicatesResult,
    _chunked_pairwise_duplicates,
    _lexical_tokens,
    compute_lexical_duplicates,
    compute_semantic_duplicates,
)


def _write_test_lesson(lessons_dir, name, description="A test lesson", rule="Apply this rule."):
    path = os.path.join(str(lessons_dir), f"{name}.md")
    content = f"""---
name: {name}
description: {description}
tags: [test]
agent_type: code
domain: testing
importance: 3
importance_rationale: Test rationale.
importance_history: []
applies_when: When testing memory performance.
do_not_apply_when: Never.
uses: 0
last_hit: NEVER
source_traces: []
status: active
---

## Rule
{rule}

## Why
Testing only.
"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path

# ============================================================================
# 1. compute_semantic_duplicates & Chunking Tests
# ============================================================================

def _naive_pairwise_duplicates(embeddings: np.ndarray, slugs: list[str], threshold: float = 0.85):
    """Naive, non-chunked all-pairs ground truth dot product."""
    n = len(slugs)
    embs = np.asarray(embeddings, dtype=np.float32)
    sim = embs @ embs.T
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            score = float(sim[i, j])
            if score > threshold:
                pairs.append((slugs[i], slugs[j], score))
    pairs.sort(key=lambda t: t[2], reverse=True)
    return pairs


class TestSemanticDuplicatesPerformance:
    def test_chunked_vs_naive_equivalence(self):
        """Verify chunked float32 block multiplication produces identical pairs to naive dot product."""
        rng = np.random.default_rng(42)
        n = 50
        dim = 64
        # Generate random normalized vectors
        raw = rng.standard_normal((n, dim), dtype=np.float32)
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        embeddings = raw / norms
        slugs = [f"lesson_{i:03d}" for i in range(n)]

        threshold = 0.25
        naive_result = _naive_pairwise_duplicates(embeddings, slugs, threshold=threshold)

        # Test varying chunk sizes: 1, 7, 25, 50, 100
        for chunk_size in [1, 7, 25, 50, 100]:
            chunked_pairs = _chunked_pairwise_duplicates(
                embeddings, slugs, threshold=threshold, chunk_size=chunk_size
            )
            assert len(chunked_pairs) == len(naive_result)
            for (c_a, c_b, c_score), (n_a, n_b, n_score) in zip(chunked_pairs, naive_result):
                assert c_a == n_a
                assert c_b == n_b
                assert pytest.approx(c_score, rel=1e-5, abs=1e-5) == n_score

    def test_float32_vs_float64_input_compatibility(self):
        """Verify both float32 and float64 arrays are handled cleanly by compute_semantic_duplicates."""
        rng = np.random.default_rng(123)
        n = 10
        dim = 32
        raw_f64 = rng.standard_normal((n, dim), dtype=np.float64)
        embeddings_f64 = raw_f64 / np.linalg.norm(raw_f64, axis=1, keepdims=True)
        slugs = [f"slug_{i}" for i in range(n)]

        # Call with float64
        res_f64 = compute_semantic_duplicates(embeddings_f64, slugs, threshold=0.1, chunk_size=3)
        assert isinstance(res_f64, SemanticDuplicatesResult)

        # Call with float32
        embeddings_f32 = embeddings_f64.astype(np.float32)
        res_f32 = compute_semantic_duplicates(embeddings_f32, slugs, threshold=0.1, chunk_size=3)

        assert res_f64.count == res_f32.count
        for (a1, b1, s1), (a2, b2, s2) in zip(res_f64.pairs, res_f32.pairs):
            assert a1 == a2
            assert b1 == b2
            assert pytest.approx(s1, abs=1e-5) == s2

    def test_direct_interface_semantic_duplicates_result(self):
        """Verify SemanticDuplicatesResult behaves as 2-tuple and dict-like object."""
        embs = np.array([[1.0, 0.0], [0.99, 0.1], [0.0, 1.0]], dtype=np.float32)
        norms = np.linalg.norm(embs, axis=1, keepdims=True)
        embs = embs / norms
        slugs = ["a", "b", "c"]

        res = compute_semantic_duplicates(embs, slugs, threshold=0.8)

        # Tuple unpacking
        count, pairs = res
        assert count == 1
        assert len(pairs) == 1
        assert pairs[0][0] == "a"
        assert pairs[0][1] == "b"

        # Dict-like access
        assert res["count"] == 1
        assert len(res["pairs"]) == 1
        assert res["available"] is True
        assert res["n_lessons"] == 3
        assert res.get("pairs") == pairs
        assert "pairs" in res
        assert "unknown_key" not in res

        # Attribute access
        assert res.count == 1
        assert res.n_lessons == 3
        assert res.available is True

    def test_edge_cases_empty_and_single_lesson(self):
        empty_embs = np.zeros((0, 16), dtype=np.float32)
        res = compute_semantic_duplicates(empty_embs, [], threshold=0.85)
        assert res.count == 0
        assert res.pairs == []

        single_emb = np.ones((1, 16), dtype=np.float32)
        res_single = compute_semantic_duplicates(single_emb, ["only_one"], threshold=0.85)
        assert res_single.count == 0
        assert res_single.pairs == []

    def test_file_path_invocation(self, tmp_path):
        index_file = tmp_path / "index.npz"
        embs = np.array([[1.0, 0.0], [0.95, 0.05]], dtype=np.float32)
        slugs = np.array(["lesson_1", "lesson_2"])
        np.savez(str(index_file), embeddings=embs, slugs=slugs)

        res = compute_semantic_duplicates(str(index_file), threshold=0.8)
        assert res["available"] is True
        assert len(res["pairs"]) == 1
        assert res["pairs"][0][0] == "lesson_1"
        assert res["pairs"][0][1] == "lesson_2"

        # Nonexistent path
        missing_res = compute_semantic_duplicates(str(tmp_path / "missing.npz"))
        assert missing_res["available"] is False
        assert "No attention index found" in missing_res["message"]


# ============================================================================
# 2. compute_lexical_duplicates & Inverted Index Pruning Tests
# ============================================================================

def _naive_lexical_duplicates(lessons: dict, threshold: float):
    """Naive all-pairs O(N^2) Jaccard duplicate calculation."""
    items = []
    for name, fm in sorted(lessons.items()):
        toks = _lexical_tokens(fm.get("description")) | _lexical_tokens(fm.get("applies_when"))
        if toks:
            items.append((name, toks))

    n = len(items)
    if n < 2:
        return {"pairs": [], "n_lessons": n, "threshold": threshold}

    pairs = []
    for i in range(n):
        name_a, toks_a = items[i]
        for j in range(i + 1, n):
            name_b, toks_b = items[j]
            union = toks_a | toks_b
            if not union:
                continue
            score = len(toks_a & toks_b) / len(union)
            if threshold <= 0 or score >= threshold:
                pairs.append({"a": name_a, "b": name_b, "score": round(score, 3)})

    pairs.sort(key=lambda pair: (-pair["score"], pair["a"], pair["b"]))
    return {"pairs": pairs, "n_lessons": n, "threshold": threshold}


class TestLexicalDuplicatesPerformance:
    def test_inverted_index_vs_naive_equivalence(self):
        """Verify inverted index with length pruning produces identical results to naive loop."""
        lessons = {
            "lesson_01": {
                "description": "Handle git checkout and branch switching safely",
                "applies_when": "When running git commands in multi-branch workflows",
            },
            "lesson_02": {
                "description": "Safe git branch switching and workspace checkout",
                "applies_when": "When running git branch commands",
            },
            "lesson_03": {
                "description": "CUDA memory allocation and GPU tensor caching",
                "applies_when": "When allocating PyTorch GPU memory on CUDA devices",
            },
            "lesson_04": {
                "description": "PyTorch GPU memory and CUDA tensor allocation guide",
                "applies_when": "When using CUDA GPU devices in training",
            },
            "lesson_05": {
                "description": "Completely unrelated text about baking sourdough bread",
                "applies_when": "When making flour water salt yeast fermentation",
            },
            "lesson_06": {
                "description": "Sourdough bread baking temperature and yeast timing",
                "applies_when": "When fermenting dough with water flour salt",
            },
        }

        for thresh in [0.1, 0.3, 0.5, 0.7, 0.0, -1.0]:
            fast_res = compute_lexical_duplicates(lessons, threshold=thresh)
            naive_res = _naive_lexical_duplicates(lessons, threshold=thresh)

            assert fast_res["n_lessons"] == naive_res["n_lessons"]
            assert len(fast_res["pairs"]) == len(naive_res["pairs"])
            for p_fast, p_naive in zip(fast_res["pairs"], naive_res["pairs"]):
                assert p_fast["a"] == p_naive["a"]
                assert p_fast["b"] == p_naive["b"]
                assert p_fast["score"] == p_naive["score"]

    def test_length_pruning_boundary(self):
        """Verify candidate pairs with length ratio < threshold are pruned without computing Jaccard."""
        # Lesson A has 2 tokens; Lesson B has 20 tokens.
        # Jaccard(A, B) <= 2 / 20 = 0.1.
        # If threshold is 0.5, length pruning must immediately bypass pair (A, B).
        lessons = {
            "short": {
                "description": "alpha beta",
                "applies_when": "alpha beta",
            },
            "long": {
                "description": (
                    "alpha beta gamma delta epsilon zeta eta theta iota kappa "
                    "lambda mu nu xi omicron pi rho sigma tau"
                ),
                "applies_when": (
                    "alpha beta gamma delta epsilon zeta eta theta iota kappa "
                    "lambda mu nu xi omicron pi rho sigma tau"
                ),
            },
        }
        res = compute_lexical_duplicates(lessons, threshold=0.5)
        assert res["pairs"] == []

    def test_unicode_tokens_supported(self):
        lessons = {
            "de_1": {
                "description": "Überprüfen der Schlüsselwörter und Schnittstellen",
                "applies_when": "Wenn Wörterbücher überprüft werden",
            },
            "de_2": {
                "description": "Überprüfen der Schlüsselwörter und Schnittstellen genau",
                "applies_when": "Wenn Wörterbücher überprüft werden müssen",
            },
        }
        res = compute_lexical_duplicates(lessons, threshold=0.5)
        assert len(res["pairs"]) == 1
        assert res["pairs"][0]["score"] >= 0.5


# ============================================================================
# 3. build_or_update_index Incremental Caching Tests
# ============================================================================

class MockSentenceTransformer:
    """Mock encoder that tracks encode calls and returns deterministic embeddings."""
    total_calls = 0
    encoded_texts_history: list[list[str]] = []

    def __init__(self, model_name: str):
        self.model_name = model_name

    def encode(self, texts, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False):
        MockSentenceTransformer.total_calls += 1
        MockSentenceTransformer.encoded_texts_history.append(list(texts))
        n = len(texts)
        # Deterministic pseudo-embeddings derived from text hashes
        embs = np.zeros((n, EMBEDDING_DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            h = hashlib.sha256(t.encode("utf-8")).digest()
            for j in range(min(len(h), EMBEDDING_DIM)):
                embs[i, j] = (h[j] / 255.0) * 2.0 - 1.0
        norms = np.linalg.norm(embs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return embs / norms


class TestIncrementalVectorCaching:
    @pytest.fixture(autouse=True)
    def patch_sentence_transformer(self, monkeypatch):
        MockSentenceTransformer.total_calls = 0
        MockSentenceTransformer.encoded_texts_history = []
        monkeypatch.setattr(
            "commontrace.reference.build_index.SentenceTransformer",
            MockSentenceTransformer,
        )

    def test_incremental_workflow(self, tmp_path):
        mem = tmp_path / "memory"
        lessons_dir = mem / "lessons"
        lessons_dir.mkdir(parents=True)
        output_npz = str(tmp_path / "index.npz")

        # Step 1: Create 3 lessons
        _write_test_lesson(lessons_dir, "lesson_alpha", description="Alpha rule", rule="Do alpha")
        _write_test_lesson(lessons_dir, "lesson_beta", description="Beta rule", rule="Do beta")
        _write_test_lesson(lessons_dir, "lesson_gamma", description="Gamma rule", rule="Do gamma")

        # Run 1: Clean build
        res1 = build_or_update_index(str(lessons_dir), output_npz)
        assert res1["n_lessons"] == 3
        assert res1["encoded_count"] == 3
        assert res1["reused_count"] == 0
        assert MockSentenceTransformer.total_calls == 1
        assert len(MockSentenceTransformer.encoded_texts_history[0]) == 3

        # Verify output .npz contents
        assert os.path.exists(output_npz)
        with np.load(output_npz, allow_pickle=False) as data:
            assert len(data["slugs"]) == 3
            assert data["embeddings"].shape == (3, EMBEDDING_DIM)
            assert "hashes" in data.files
            assert "importances" in data.files
            assert "statuses" in data.files

        # Run 2: Unchanged lessons -> fully reused from cache, 0 encoding calls
        res2 = build_or_update_index(str(lessons_dir), output_npz)
        assert res2["n_lessons"] == 3
        assert res2["encoded_count"] == 0
        assert res2["reused_count"] == 3
        # Model.encode should not have been called again
        assert MockSentenceTransformer.total_calls == 1

        # Run 3: Modify 1 lesson -> only 1 lesson encoded, 2 reused
        _write_test_lesson(lessons_dir, "lesson_beta", description="Beta rule MODIFIED", rule="Do beta modified")
        res3 = build_or_update_index(str(lessons_dir), output_npz)
        assert res3["n_lessons"] == 3
        assert res3["encoded_count"] == 1
        assert res3["reused_count"] == 2
        assert MockSentenceTransformer.total_calls == 2
        assert len(MockSentenceTransformer.encoded_texts_history[-1]) == 1

        # Run 4: Add 1 new lesson -> 1 encoded, 3 reused
        _write_test_lesson(lessons_dir, "lesson_delta", description="Delta rule", rule="Do delta")
        res4 = build_or_update_index(str(lessons_dir), output_npz)
        assert res4["n_lessons"] == 4
        assert res4["encoded_count"] == 1
        assert res4["reused_count"] == 3
        assert MockSentenceTransformer.total_calls == 3

        # Run 5: force_rebuild=True -> all 4 lessons re-encoded
        res5 = build_or_update_index(str(lessons_dir), output_npz, force_rebuild=True)
        assert res5["n_lessons"] == 4
        assert res5["encoded_count"] == 4
        assert res5["reused_count"] == 0
        assert MockSentenceTransformer.total_calls == 4
        assert len(MockSentenceTransformer.encoded_texts_history[-1]) == 4

    def test_empty_lessons_directory(self, tmp_path):
        empty_dir = tmp_path / "empty_lessons"
        empty_dir.mkdir()
        out_npz = str(tmp_path / "empty_index.npz")

        res = build_or_update_index(str(empty_dir), out_npz)
        assert res["n_lessons"] == 0
        assert res["encoded_count"] == 0
        assert res["reused_count"] == 0

        with np.load(out_npz, allow_pickle=False) as data:
            assert len(data["slugs"]) == 0
            assert data["embeddings"].shape == (0, EMBEDDING_DIM)
