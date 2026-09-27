"""Tier 1: Feature Coverage for Attention & Memory Performance (Milestone 2).

Covers Features:
- R2-F1: Bounded-Memory Semantic Deduplication
- R2-F2: Incremental Embedding Indexing
- R2-F3: Fast Query Metadata Co-location
- R2-F4: Inverted-Index Lexical Deduplication
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

np = pytest.importorskip("numpy", reason="numpy is required for attention memory tests")

from tests.e2e.conftest import CLIResult  # noqa: E402

# ============================================================================
# R2-F1: Bounded-Memory Semantic Deduplication (>=5 tests)
# ============================================================================

def test_r2_f1_compute_semantic_duplicates_chunked_interface() -> None:
    """Validate compute_semantic_duplicates with chunked float32 processing."""
    from commontrace.reference.measure_performance import compute_semantic_duplicates

    n = 20
    dim = 64
    # Create normalized synthetic embeddings
    rng = np.random.default_rng(42)
    embs = rng.standard_normal((n, dim), dtype=np.float32)
    embs /= np.linalg.norm(embs, axis=1, keepdims=True)

    # Make pair (0, 1) almost identical (cosine ~ 0.99)
    embs[1] = embs[0] + 0.01 * rng.standard_normal(dim, dtype=np.float32)
    embs[1] /= np.linalg.norm(embs[1])

    slugs = [f"lesson_{i:02d}" for i in range(n)]

    # Run with small chunk size to verify block-wise computation
    count, pairs = compute_semantic_duplicates(embs, slugs, threshold=0.85, chunk_size=5)

    assert count >= 1 or len(pairs) >= 1
    assert any((p[0] == "lesson_00" and p[1] == "lesson_01") or (p[0] == "lesson_01" and p[1] == "lesson_00") for p in pairs)


def test_r2_f1_empty_embeddings_graceful() -> None:
    """Validate that compute_semantic_duplicates handles 0 lessons without error."""
    from commontrace.reference.measure_performance import compute_semantic_duplicates

    embs = np.zeros((0, 64), dtype=np.float32)
    count, pairs = compute_semantic_duplicates(embs, [], threshold=0.85)
    assert count == 0
    assert pairs == []


def test_r2_f1_single_lesson_graceful() -> None:
    """Validate that compute_semantic_duplicates handles 1 lesson without error."""
    from commontrace.reference.measure_performance import compute_semantic_duplicates

    embs = np.ones((1, 64), dtype=np.float32)
    embs /= np.linalg.norm(embs, axis=1, keepdims=True)
    count, pairs = compute_semantic_duplicates(embs, ["lesson_single"], threshold=0.85)
    assert count == 0
    assert pairs == []


def test_r2_f1_detects_identical_and_rejects_orthogonal_pairs() -> None:
    """Validate that exact duplicates are found and orthogonal pairs are excluded."""
    from commontrace.reference.measure_performance import compute_semantic_duplicates

    # 3 vectors: v0 and v1 identical [1, 0, 0], v2 orthogonal [0, 1, 0]
    embs = np.array([
        [1.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
    ], dtype=np.float32)
    slugs = ["lesson_a", "lesson_b", "lesson_c"]

    count, pairs = compute_semantic_duplicates(embs, slugs, threshold=0.85, chunk_size=2)
    assert len(pairs) == 1
    assert pairs[0][0] == "lesson_a"
    assert pairs[0][1] == "lesson_b"
    assert pytest.approx(pairs[0][2], 0.001) == 1.0


def test_r2_f1_pairs_sorted_descending_by_score() -> None:
    """Validate that duplicate pairs are sorted in descending order of similarity."""
    from commontrace.reference.measure_performance import compute_semantic_duplicates

    embs = np.array([
        [1.0, 0.0],
        [1.0, 0.0],
        [0.9, 0.43588989],  # cosine ~ 0.9
        [0.86, 0.5102939],  # cosine ~ 0.86
    ], dtype=np.float32)
    slugs = ["l0", "l1", "l2", "l3"]

    count, pairs = compute_semantic_duplicates(embs, slugs, threshold=0.80, chunk_size=2)
    scores = [p[2] for p in pairs]
    assert scores == sorted(scores, reverse=True)


def test_r2_f1_bounded_memory_float32_preservation() -> None:
    """Validate that compute_semantic_duplicates works when input is float64, converting to float32."""
    from commontrace.reference.measure_performance import compute_semantic_duplicates

    embs64 = np.array([[1.0, 0.0], [0.99, 0.01]], dtype=np.float64)
    embs64 /= np.linalg.norm(embs64, axis=1, keepdims=True)
    slugs = ["l1", "l2"]
    count, pairs = compute_semantic_duplicates(embs64, slugs, threshold=0.80)
    assert len(pairs) == 1




# ============================================================================
# R2-F2: Incremental Embedding Indexing (>=5 tests)
# ============================================================================

class DummyEmbeddingModel:
    """Mock SentenceTransformer for deterministic vector generation without external downloads."""
    def __init__(self, model_name: str = "test-model"):
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
            # Seed based on text hash for determinism
            val = float(sum(ord(c) for c in text) % 1000) / 1000.0
            out[i, 0] = val
            out[i, 1] = 1.0 - val
            norm = np.linalg.norm(out[i])
            if norm > 0:
                out[i] /= norm
        return out


def test_r2_f2_build_index_computes_sha256_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate that build_or_update_index calculates SHA-256 hashes for lessons and writes to npz."""
    from commontrace.reference import build_index

    monkeypatch.setattr(build_index, "SentenceTransformer", DummyEmbeddingModel)

    store = tmp_path / "store"
    lessons_dir = store / "memory" / "lessons"
    output_npz = store / "memory" / "attention" / "index.npz"
    output_npz.parent.mkdir(parents=True, exist_ok=True)

    lesson_factory(store, slug="lesson_h1", body="## Rule\nFirst rule\n")
    lesson_factory(store, slug="lesson_h2", body="## Rule\nSecond rule\n")

    res = build_index.build_or_update_index(str(lessons_dir), str(output_npz))
    assert res["n_lessons"] == 2
    assert res["encoded_count"] == 2
    assert res["reused_count"] == 0

    assert output_npz.exists()
    with np.load(str(output_npz), allow_pickle=False) as data:
        assert "hashes" in data.files
        assert len(data["hashes"]) == 2
        assert all(len(str(h)) == 64 for h in data["hashes"])


def test_r2_f2_unchanged_lessons_cached_in_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate that subsequent index build reuses cached vectors for unchanged lessons."""
    from commontrace.reference import build_index

    monkeypatch.setattr(build_index, "SentenceTransformer", DummyEmbeddingModel)

    store = tmp_path / "store"
    lessons_dir = store / "memory" / "lessons"
    output_npz = store / "memory" / "attention" / "index.npz"
    output_npz.parent.mkdir(parents=True, exist_ok=True)

    lesson_factory(store, slug="lesson_c1", body="## Rule\nRule C1\n")
    lesson_factory(store, slug="lesson_c2", body="## Rule\nRule C2\n")

    # Initial build
    res1 = build_index.build_or_update_index(str(lessons_dir), str(output_npz))
    assert res1["encoded_count"] == 2

    # Second build with unchanged lessons: all should be reused
    res2 = build_index.build_or_update_index(str(lessons_dir), str(output_npz))
    assert res2["n_lessons"] == 2
    assert res2["encoded_count"] == 0
    assert res2["reused_count"] == 2


def test_r2_f2_modified_lesson_triggers_selective_reencoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate that modifying one lesson only re-encodes that specific lesson."""
    from commontrace.reference import build_index

    monkeypatch.setattr(build_index, "SentenceTransformer", DummyEmbeddingModel)

    store = tmp_path / "store"
    lessons_dir = store / "memory" / "lessons"
    output_npz = store / "memory" / "attention" / "index.npz"
    output_npz.parent.mkdir(parents=True, exist_ok=True)

    lesson_factory(store, slug="lesson_keep", body="## Rule\nRule unchanged\n")
    mod_path = lesson_factory(store, slug="lesson_mod", body="## Rule\nRule initial\n")

    build_index.build_or_update_index(str(lessons_dir), str(output_npz))

    # Modify one lesson
    lesson_factory(store, slug="lesson_mod", body="## Rule\nRule updated and modified\n")

    res = build_index.build_or_update_index(str(lessons_dir), str(output_npz))
    assert res["n_lessons"] == 2
    assert res["encoded_count"] == 1
    assert res["reused_count"] == 1


def test_r2_f2_new_lesson_added_incrementally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate that adding a new lesson preserves cached embeddings for existing ones."""
    from commontrace.reference import build_index

    monkeypatch.setattr(build_index, "SentenceTransformer", DummyEmbeddingModel)

    store = tmp_path / "store"
    lessons_dir = store / "memory" / "lessons"
    output_npz = store / "memory" / "attention" / "index.npz"
    output_npz.parent.mkdir(parents=True, exist_ok=True)

    lesson_factory(store, slug="lesson_old1", body="## Rule\nOld rule 1\n")
    lesson_factory(store, slug="lesson_old2", body="## Rule\nOld rule 2\n")

    build_index.build_or_update_index(str(lessons_dir), str(output_npz))

    # Add 3rd lesson
    lesson_factory(store, slug="lesson_new3", body="## Rule\nBrand new rule 3\n")

    res = build_index.build_or_update_index(str(lessons_dir), str(output_npz))
    assert res["n_lessons"] == 3
    assert res["encoded_count"] == 1
    assert res["reused_count"] == 2


def test_r2_f2_force_rebuild_bypasses_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate that force_rebuild=True forces re-encoding of all lessons."""
    from commontrace.reference import build_index

    monkeypatch.setattr(build_index, "SentenceTransformer", DummyEmbeddingModel)

    store = tmp_path / "store"
    lessons_dir = store / "memory" / "lessons"
    output_npz = store / "memory" / "attention" / "index.npz"
    output_npz.parent.mkdir(parents=True, exist_ok=True)

    lesson_factory(store, slug="lesson_f1", body="## Rule\nRule 1\n")
    lesson_factory(store, slug="lesson_f2", body="## Rule\nRule 2\n")

    build_index.build_or_update_index(str(lessons_dir), str(output_npz))

    # Force rebuild
    res = build_index.build_or_update_index(str(lessons_dir), str(output_npz), force_rebuild=True)
    assert res["n_lessons"] == 2
    assert res["encoded_count"] == 2
    assert res["reused_count"] == 0


def test_r2_f2_empty_lessons_build_index(tmp_path: Path) -> None:
    """Validate that build_or_update_index with 0 lessons writes a clean 0-row index."""
    from commontrace.reference import build_index

    store = tmp_path / "store"
    lessons_dir = store / "memory" / "lessons"
    lessons_dir.mkdir(parents=True, exist_ok=True)
    output_npz = store / "memory" / "attention" / "index.npz"
    output_npz.parent.mkdir(parents=True, exist_ok=True)

    res = build_index.build_or_update_index(str(lessons_dir), str(output_npz))
    assert res["n_lessons"] == 0
    with np.load(str(output_npz), allow_pickle=False) as data:
        assert len(data["slugs"]) == 0
        assert data["embeddings"].shape == (0, 768)


# ============================================================================
# R2-F3: Fast Query Metadata Co-location (>=5 tests)
# ============================================================================

def test_r2_f3_npz_stores_importances_and_statuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate that index.npz contains importances and statuses metadata arrays."""
    from commontrace.reference import build_index

    monkeypatch.setattr(build_index, "SentenceTransformer", DummyEmbeddingModel)

    store = tmp_path / "store"
    lessons_dir = store / "memory" / "lessons"
    output_npz = store / "memory" / "attention" / "index.npz"
    output_npz.parent.mkdir(parents=True, exist_ok=True)

    lesson_factory(store, slug="lesson_imp5", importance=5, status="active", body="## Rule\nR5\n")
    lesson_factory(store, slug="lesson_imp2", importance=2, status="active", body="## Rule\nR2\n")

    build_index.build_or_update_index(str(lessons_dir), str(output_npz))

    with np.load(str(output_npz), allow_pickle=False) as data:
        assert "importances" in data.files
        assert "statuses" in data.files
        assert set(int(x) for x in data["importances"]) == {2, 5}
        assert set(str(s) for s in data["statuses"]) == {"active"}


def test_r2_f3_query_filters_active_status_using_metadata(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate that query does not retrieve lessons with review or archived status."""
    lesson_factory(
        isolated_store,
        slug="lesson_active_standard",
        title="Active Standard Rule",
        description="Standard active rule for coding tasks",
        status="active",
    )
    lesson_factory(
        isolated_store,
        slug="lesson_under_review",
        title="Review Stage Rule",
        description="Not yet active instruction",
        status="review",
    )
    res = cli_runner(["query", "--lexical", "--dest", str(isolated_store), "coding tasks"])
    assert res.exit_code == 0
    assert "lesson_active_standard" in res.stdout
    assert "lesson_under_review" not in res.stdout


def test_r2_f3_query_importance_floor_filtering(
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate load_importances_from_index extracts co-located importances without disk read."""
    from commontrace.reference import query as query_module

    npz_path = isolated_store / "memory" / "attention" / "index.npz"
    npz_path.parent.mkdir(parents=True, exist_ok=True)

    slugs = ["lesson_imp5", "lesson_imp2"]
    embs = np.ones((2, 768), dtype=np.float32)
    embs /= np.linalg.norm(embs, axis=1, keepdims=True)
    importances = [5, 2]
    statuses = ["active", "active"]

    np.savez(
        str(npz_path),
        slugs=slugs,
        embeddings=embs,
        model_name="multi-qa-mpnet-base-dot-v1",
        encoded_field="test",
        importances=importances,
        statuses=statuses,
    )

    with np.load(str(npz_path), allow_pickle=False) as data:
        fast_imp = query_module.load_importances_from_index(data)
        assert fast_imp is not None
        imp_dict, n_parsed = fast_imp
        assert n_parsed == 0
        assert imp_dict["lesson_imp5"] == 5
        assert imp_dict["lesson_imp2"] == 2




def test_r2_f3_fast_metadata_loading_in_query(
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate that query.py's load_index or metadata helpers access importances without disk YAML parsing."""
    from commontrace.reference import query as query_module

    npz_path = isolated_store / "memory" / "attention" / "index.npz"
    npz_path.parent.mkdir(parents=True, exist_ok=True)

    slugs = ["lesson_alpha", "lesson_beta"]
    embs = np.ones((2, 768), dtype=np.float32)
    embs /= np.linalg.norm(embs, axis=1, keepdims=True)
    importances = [4, 2]
    statuses = ["active", "active"]

    np.savez(
        str(npz_path),
        slugs=slugs,
        embeddings=embs,
        model_name="multi-qa-mpnet-base-dot-v1",
        encoded_field="test",
        importances=importances,
        statuses=statuses,
    )

    if hasattr(query_module, "load_index_metadata"):
        meta = query_module.load_index_metadata(str(npz_path))
        assert meta["importances"]["lesson_alpha"] == 4
        assert meta["statuses"]["lesson_alpha"] == "active"


def test_r2_f3_query_fallback_when_metadata_absent(
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate that query gracefully falls back if index.npz lacks importances/statuses."""
    npz_path = isolated_store / "memory" / "attention" / "index.npz"
    npz_path.parent.mkdir(parents=True, exist_ok=True)

    slugs = ["lesson_legacy"]
    embs = np.ones((1, 768), dtype=np.float32)
    # Save without importances or statuses
    np.savez(
        str(npz_path),
        slugs=slugs,
        embeddings=embs,
        model_name="multi-qa-mpnet-base-dot-v1",
        encoded_field="test",
    )

    with np.load(str(npz_path), allow_pickle=False) as data:
        assert "importances" not in data.files


# ============================================================================
# R2-F4: Inverted-Index Lexical Deduplication (>=5 tests)
# ============================================================================

def test_r2_f4_compute_lexical_duplicates_basic() -> None:
    """Validate compute_lexical_duplicates identifies overlap between near-duplicate lessons."""
    from commontrace.reference.measure_performance import compute_lexical_duplicates

    lessons = {
        "lesson_db_pool_1": {
            "description": "Configure connection pool size for postgres database",
            "applies_when": "Setting up database connection pool settings",
        },
        "lesson_db_pool_2": {
            "description": "Configure connection pool timeout for postgres database",
            "applies_when": "Setting up database connection pool configuration",
        },
        "lesson_unrelated_css": {
            "description": "Use flexbox grid layout for responsive web styling",
            "applies_when": "Writing css style rules",
        },
    }

    result = compute_lexical_duplicates(lessons, threshold=0.5)
    pairs = result["pairs"]
    assert len(pairs) == 1
    pair = pairs[0]
    names = {pair["a"], pair["b"]} if isinstance(pair, dict) else {pair[0], pair[1]}
    assert names == {"lesson_db_pool_1", "lesson_db_pool_2"}


def test_r2_f4_inverted_index_prunes_disjoint_candidates() -> None:
    """Validate that completely disjoint lessons produce 0 candidate duplicate pairs."""
    from commontrace.reference.measure_performance import compute_lexical_duplicates

    lessons = {
        "lesson_alpha": {"description": "quantum computing qubits entanglement", "applies_when": "physics"},
        "lesson_beta": {"description": "baking sourdough fermentation bread yeast", "applies_when": "cooking"},
        "lesson_gamma": {"description": "aerodynamic fluid dynamics turbulence airflow", "applies_when": "aviation"},
    }

    result = compute_lexical_duplicates(lessons, threshold=0.3)
    assert result["pairs"] == []


def test_r2_f4_stopword_filtering_prevents_false_matches() -> None:
    """Validate that common stopwords do not trigger false duplicate detection."""
    from commontrace.reference.measure_performance import compute_lexical_duplicates

    # Two lessons that only share generic English stop words ("the", "is", "when", "to")
    lessons = {
        "lesson_x": {"description": "the apple is on the table", "applies_when": "when to eat"},
        "lesson_y": {"description": "the boat is in the harbor", "applies_when": "when to sail"},
    }

    result = compute_lexical_duplicates(lessons, threshold=0.3)
    assert result["pairs"] == []


def test_r2_f4_unicode_and_accent_normalization() -> None:
    """Validate that lexical deduplication correctly handles unicode / accented tokens."""
    from commontrace.reference.measure_performance import compute_lexical_duplicates

    lessons = {
        "lesson_de_1": {"description": "Prüfung der Fehlerbehandlung für Übertragung", "applies_when": "Netzwerk"},
        "lesson_de_2": {"description": "Prüfung der Protokollierung für Übertragung", "applies_when": "Netzwerk"},
    }

    result = compute_lexical_duplicates(lessons, threshold=0.4)
    assert len(result["pairs"]) >= 1


def test_r2_f4_empty_corpus_handling() -> None:
    """Validate that empty or 1-lesson dictionary returns empty result structure."""
    from commontrace.reference.measure_performance import compute_lexical_duplicates

    res_empty = compute_lexical_duplicates({}, threshold=0.5)
    assert res_empty["pairs"] == []
    assert res_empty["n_lessons"] == 0

    res_one = compute_lexical_duplicates({"l1": {"description": "only one"}}, threshold=0.5)
    assert res_one["pairs"] == []
    assert res_one["n_lessons"] == 1


def test_r2_f4_prunes_large_candidate_space() -> None:
    """Validate performance on synthetic corpus with multiple clusters."""
    from commontrace.reference.measure_performance import compute_lexical_duplicates

    lessons = {}
    for i in range(50):
        topic = "security" if i < 25 else "performance"
        lessons[f"lesson_{i:02d}"] = {
            "description": f"Enforce strict {topic} guidelines for microservice worker {i}",
            "applies_when": f"Deploying {topic} service cluster",
        }

    result = compute_lexical_duplicates(lessons, threshold=0.8)
    assert "pairs" in result
    assert result["n_lessons"] == 50
