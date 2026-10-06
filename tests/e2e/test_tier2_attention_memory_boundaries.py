from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

np = pytest.importorskip("numpy", reason="numpy is required for attention memory boundary tests")


class DummyEmbeddingModel:
    def __init__(self, model_name: str = "multi-qa-mpnet-base-dot-v1"):
        self.model_name = model_name

    def encode(
        self,
        texts: list[str],
        normalize_embeddings: bool = True,
        convert_to_numpy: bool = True,
        show_progress_bar: bool = False,
    ) -> np.ndarray:
        dim = 768
        out = np.zeros((len(texts), dim), dtype=np.float32)
        for i, text in enumerate(texts):
            val = float(sum(ord(c) for c in text) % 1000) / 1000.0
            out[i, 0] = val
            out[i, 1] = 1.0 - val
            norm = np.linalg.norm(out[i])
            if norm > 0:
                out[i] /= norm
        return out


def test_boundary_chunk_size_one() -> None:
    from commontrace.reference.measure_performance import compute_semantic_duplicates

    n = 6
    dim = 16
    rng = np.random.default_rng(101)
    embs = rng.standard_normal((n, dim), dtype=np.float32)
    embs /= np.linalg.norm(embs, axis=1, keepdims=True)

    embs[3] = embs[0].copy()

    slugs = [f"lesson_{i}" for i in range(n)]
    count, pairs = compute_semantic_duplicates(embs, slugs, threshold=0.99, chunk_size=1)

    assert count == 1
    assert len(pairs) == 1
    found_slugs = {pairs[0][0], pairs[0][1]}
    assert found_slugs == {"lesson_0", "lesson_3"}


def test_boundary_chunk_size_exceeds_total_items() -> None:
    from commontrace.reference.measure_performance import compute_semantic_duplicates

    n = 5
    dim = 8
    rng = np.random.default_rng(202)
    embs = rng.standard_normal((n, dim), dtype=np.float32)
    embs /= np.linalg.norm(embs, axis=1, keepdims=True)

    slugs = [f"l_{i}" for i in range(n)]
    count, pairs = compute_semantic_duplicates(embs, slugs, threshold=0.85, chunk_size=10000)

    assert isinstance(count, int)
    assert isinstance(pairs, list)
    assert count >= 0


def test_boundary_identical_all_elements() -> None:
    from commontrace.reference.measure_performance import compute_semantic_duplicates

    n = 5
    dim = 10
    v = np.ones((1, dim), dtype=np.float32)
    v /= np.linalg.norm(v)
    embs = np.repeat(v, n, axis=0)

    slugs = [f"slug_{i}" for i in range(n)]
    count, pairs = compute_semantic_duplicates(embs, slugs, threshold=0.999, chunk_size=2)

    expected_pairs = n * (n - 1) // 2
    assert count == expected_pairs
    assert len(pairs) == expected_pairs


def test_boundary_strictly_orthogonal_embeddings() -> None:
    from commontrace.reference.measure_performance import compute_semantic_duplicates

    embs = np.eye(4, dtype=np.float32)
    slugs = ["b0", "b1", "b2", "b3"]

    count, pairs = compute_semantic_duplicates(embs, slugs, threshold=0.01, chunk_size=2)
    assert count == 0
    assert pairs == []


def test_boundary_threshold_epsilon_above_one() -> None:
    from commontrace.reference.measure_performance import compute_semantic_duplicates

    rng = np.random.default_rng(303)
    embs = rng.standard_normal((4, 8), dtype=np.float32)
    embs /= np.linalg.norm(embs, axis=1, keepdims=True)
    slugs = ["s1", "s2", "s3", "s4"]

    count, pairs = compute_semantic_duplicates(embs, slugs, threshold=1.0001, chunk_size=2)
    assert count == 0
    assert pairs == []


def test_boundary_corrupt_index_npz_recovery(
    isolated_store: Path,
    monkeypatch: pytest.MonkeyPatch,
    lesson_factory: Callable[..., Path],
) -> None:
    from commontrace.reference import build_index

    monkeypatch.setattr(build_index, "SentenceTransformer", DummyEmbeddingModel)

    lesson_factory(isolated_store, slug="lesson_corrupt_recov_1", title="Recov 1")
    lesson_factory(isolated_store, slug="lesson_corrupt_recov_2", title="Recov 2")

    attention_dir = isolated_store / "memory" / "attention"
    attention_dir.mkdir(parents=True, exist_ok=True)
    index_npz = attention_dir / "index.npz"

    index_npz.write_bytes(b"\x00\xff\xfeCORRUPT_NOT_A_ZIP_ARCHIVE\x00")

    res = build_index.build_or_update_index(
        str(isolated_store / "memory" / "lessons"),
        str(index_npz),
    )

    assert res["n_lessons"] == 2
    assert index_npz.exists()
    with np.load(str(index_npz), allow_pickle=False) as data:
        assert "slugs" in data
        assert len(data["slugs"]) == 2


def test_boundary_non_markdown_and_templates_ignored(
    isolated_store: Path,
    monkeypatch: pytest.MonkeyPatch,
    lesson_factory: Callable[..., Path],
) -> None:
    from commontrace.reference import build_index

    monkeypatch.setattr(build_index, "SentenceTransformer", DummyEmbeddingModel)

    lessons_dir = isolated_store / "memory" / "lessons"
    lesson_factory(isolated_store, slug="lesson_valid_md", title="Valid Markdown")

    (lessons_dir / "lesson_template.md").write_text("# Template\nNot a real lesson.")
    (lessons_dir / "notes.txt").write_text("Random notes")
    (lessons_dir / "data.json").write_text('{"key": "value"}')
    (lessons_dir / ".hidden_lesson.md").write_text("# Hidden")

    output_file = isolated_store / "memory" / "attention" / "index.npz"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    res = build_index.build_or_update_index(
        str(lessons_dir),
        str(output_file),
    )

    assert res["n_lessons"] == 1
    with np.load(str(output_file), allow_pickle=False) as data:
        slugs = [str(s) for s in data["slugs"]]
        assert "lesson_template" not in slugs
        assert "lesson_valid_md" in slugs


def test_boundary_zero_lessons_creates_valid_empty_index(
    isolated_store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from commontrace.reference import build_index

    monkeypatch.setattr(build_index, "SentenceTransformer", DummyEmbeddingModel)

    lessons_dir = isolated_store / "memory" / "lessons"
    output_file = isolated_store / "memory" / "attention" / "index.npz"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    res = build_index.build_or_update_index(
        str(lessons_dir),
        str(output_file),
    )

    assert res["n_lessons"] == 0
    assert res["encoded_count"] == 0
    assert output_file.exists()


def test_boundary_partial_deletion_of_indexed_files(
    isolated_store: Path,
    monkeypatch: pytest.MonkeyPatch,
    lesson_factory: Callable[..., Path],
) -> None:
    from commontrace.reference import build_index

    monkeypatch.setattr(build_index, "SentenceTransformer", DummyEmbeddingModel)

    l1 = lesson_factory(isolated_store, slug="lesson_keep_me", title="Keep Me")
    l2 = lesson_factory(isolated_store, slug="lesson_delete_me", title="Delete Me")

    output_file = isolated_store / "memory" / "attention" / "index.npz"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    res1 = build_index.build_or_update_index(str(isolated_store / "memory" / "lessons"), str(output_file))
    assert res1["n_lessons"] == 2

    l2.unlink()

    res2 = build_index.build_or_update_index(str(isolated_store / "memory" / "lessons"), str(output_file))
    assert res2["n_lessons"] == 1

    with np.load(str(output_file), allow_pickle=False) as data2:
        slugs_after = [str(s) for s in data2["slugs"]]
        assert "lesson_keep_me" in slugs_after
        assert "lesson_delete_me" not in slugs_after


def test_boundary_mtime_unchanged_skips_encoding(
    isolated_store: Path,
    monkeypatch: pytest.MonkeyPatch,
    lesson_factory: Callable[..., Path],
) -> None:
    from commontrace.reference import build_index

    monkeypatch.setattr(build_index, "SentenceTransformer", DummyEmbeddingModel)

    lesson_factory(isolated_store, slug="lesson_static_test", title="Static Lesson")
    output_file = isolated_store / "memory" / "attention" / "index.npz"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    res1 = build_index.build_or_update_index(str(isolated_store / "memory" / "lessons"), str(output_file))
    assert res1["encoded_count"] == 1
    assert res1["reused_count"] == 0

    res2 = build_index.build_or_update_index(str(isolated_store / "memory" / "lessons"), str(output_file))
    assert res2["encoded_count"] == 0
    assert res2["reused_count"] == 1


def test_boundary_missing_importances_key_fallback(
    isolated_store: Path,
) -> None:
    from commontrace.reference.query import load_importances_from_index

    attention_dir = isolated_store / "memory" / "attention"
    attention_dir.mkdir(parents=True, exist_ok=True)
    npz_path = attention_dir / "index.npz"

    np.savez(
        str(npz_path),
        embeddings=np.ones((2, 768), dtype=np.float32),
        slugs=np.array(["s1", "s2"]),
    )

    with np.load(str(npz_path), allow_pickle=False) as data:
        res = load_importances_from_index(data)
    assert res is None


def test_boundary_corrupt_importance_values(
    isolated_store: Path,
) -> None:
    from commontrace.reference.query import load_importances_from_index

    attention_dir = isolated_store / "memory" / "attention"
    attention_dir.mkdir(parents=True, exist_ok=True)
    npz_path = attention_dir / "index.npz"

    np.savez(
        str(npz_path),
        embeddings=np.ones((2, 768), dtype=np.float32),
        slugs=np.array(["s1", "s2"]),
        importances=np.array([99999, -50], dtype=np.int64),
        statuses=np.array(["active", "active"]),
    )

    with np.load(str(npz_path), allow_pickle=False) as data:
        res = load_importances_from_index(data)

    assert res is not None
    imp_dict = res[0]
    assert imp_dict["s1"] == 99999
    assert imp_dict["s2"] == -50


def test_boundary_status_filtering_in_fast_loader(
    isolated_store: Path,
) -> None:
    from commontrace.reference.query import load_importances_from_index

    attention_dir = isolated_store / "memory" / "attention"
    attention_dir.mkdir(parents=True, exist_ok=True)
    npz_path = attention_dir / "index.npz"

    np.savez(
        str(npz_path),
        embeddings=np.ones((3, 768), dtype=np.float32),
        slugs=np.array(["active_one", "inactive_two", "active_three"]),
        importances=np.array([5, 8, 2], dtype=np.int64),
        statuses=np.array(["active", "review", "active"]),
    )

    with np.load(str(npz_path), allow_pickle=False) as data:
        res = load_importances_from_index(data)

    assert res is not None
    imp_dict = res[0]
    assert "active_one" in imp_dict
    assert "active_three" in imp_dict
    assert "inactive_two" not in imp_dict


def test_boundary_corrupt_index_file_raises_cleanly(
    isolated_store: Path,
) -> None:
    attention_dir = isolated_store / "memory" / "attention"
    attention_dir.mkdir(parents=True, exist_ok=True)
    npz_path = attention_dir / "index.npz"
    npz_path.write_bytes(b"NOT_A_ZIP_FILE")

    with pytest.raises(Exception):
        with np.load(str(npz_path), allow_pickle=False) as data:
            pass


def test_boundary_cosine_similarity_computation() -> None:
    q_emb = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    doc_embs = np.array([
        [-1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [1.0, 0.0, 0.0],
    ], dtype=np.float32)

    scores = doc_embs @ q_emb
    assert scores[0] == -1.0
    assert scores[1] == 0.0
    assert scores[2] == 1.0


def test_boundary_zero_tokens_and_whitespace_only() -> None:
    from commontrace.reference.measure_performance import compute_lexical_duplicates

    lessons = {
        "l1": {"description": "   \n\t   ", "applies_when": "   "},
        "l2": {"description": "??? !!! ***", "applies_when": "---"},
    }

    res = compute_lexical_duplicates(lessons, threshold=0.7)
    assert res["pairs"] == []
    assert res["n_lessons"] == 0


def test_boundary_single_shared_token_below_threshold() -> None:
    from commontrace.reference.measure_performance import compute_lexical_duplicates

    lessons = {
        "l1": {
            "description": "alpha beta gamma delta epsilon zeta eta sharedtoken",
            "applies_when": "always",
        },
        "l2": {
            "description": "theta iota kappa lambda mu nu xi sharedtoken",
            "applies_when": "sometimes",
        },
    }

    res = compute_lexical_duplicates(lessons, threshold=0.8)
    assert len(res["pairs"]) == 0


def test_boundary_identical_wording_detected() -> None:
    from commontrace.reference.measure_performance import compute_lexical_duplicates

    text = "thorough system integration testing and boundary verification protocol"
    lessons = {
        "doc_a": {"description": text, "applies_when": "always on verification"},
        "doc_b": {"description": text, "applies_when": "always on verification"},
    }

    res = compute_lexical_duplicates(lessons, threshold=0.9)
    assert len(res["pairs"]) == 1
    pair = res["pairs"][0]
    assert pair["a"] == "doc_a" and pair["b"] == "doc_b"
    assert pair["score"] >= 0.9


def test_boundary_degenerate_threshold_zero() -> None:
    from commontrace.reference.measure_performance import compute_lexical_duplicates

    lessons = {
        "doc_1": {"description": "first unique document testing", "applies_when": "when ready"},
        "doc_2": {"description": "second unique document testing", "applies_when": "when ready"},
    }

    res = compute_lexical_duplicates(lessons, threshold=0.0)
    assert "pairs" in res
    assert res["threshold"] == 0.0


def test_boundary_disjoint_vocabularies_zero_pairs() -> None:
    from commontrace.reference.measure_performance import compute_lexical_duplicates

    lessons = {
        "science": {"description": "quantum mechanics astrophysics particle physics", "applies_when": "laboratory"},
        "cooking": {"description": "baking pastry culinary gourmet gastronomy", "applies_when": "kitchen"},
    }

    res = compute_lexical_duplicates(lessons, threshold=0.1)
    assert res["pairs"] == []
    assert res["n_lessons"] == 2
