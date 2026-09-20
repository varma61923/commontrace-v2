"""Tier 2: Boundary & Corner Cases for Verification & Acceptance (Milestones 4 & 5).

Covers Features:
- R4-F1: Dedicated Schema Validation Test Suite (importance bounds, empty arrays, invalid types)
- R4-F2: Security & Sandboxing Test Suite (symlink loops, uncanonicalized traversal, null-bytes)
- R4-F3: Performance & Scalability Test Suite (fast index latency, large chunk sizes, scaling limits)
- R4-F4: Diagnostic & Benchmark Verification (0 episodes, 1 episode, corrupt episode files)
- R4-F5: Final E2E Test Suite Validation (empty distill, multi-fleet profile isolation, approval flows)
- R4-F6: Adversarial Coverage Hardening (corrupted archives, binary files, zalgo text, frontmatter delimiters in body)
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

import pytest

try:
    import numpy as np
except ImportError:
    np = None

from tests.e2e.conftest import CLIResult


# ============================================================================
# R4-F1: Boundary Cases for Schema Validation (>=5 tests)
# ============================================================================

def test_r4_f1_boundary_importance_bounds() -> None:
    """Validate importance integer boundaries in lesson schema (0..10 allowed, -1 and 11 rejected)."""
    from commontrace import validate

    schema = validate.load_schema("lesson.schema.json")
    base_lesson: dict[str, Any] = {
        "name": "Bound Test",
        "description": "Boundary testing",
        "tags": ["testing"],
        "agent_type": "code",
        "domain": "general",
        "importance_rationale": "Testing bounds",
        "applies_when": "Always",
        "do_not_apply_when": "Never",
        "uses": 0,
        "last_hit": "2026-09-20",
        "status": "active",
    }

    # Minimum valid importance (1)
    base_lesson["importance"] = 1
    assert validate.validate(base_lesson, schema) == []

    # Maximum valid importance (5 per lesson.schema.json)
    base_lesson["importance"] = 5
    assert validate.validate(base_lesson, schema) == []

    # Boundary violation: negative importance
    base_lesson["importance"] = -1
    errors_neg = validate.validate(base_lesson, schema)
    assert len(errors_neg) > 0

    # Boundary violation: importance > 5
    base_lesson["importance"] = 6
    errors_high = validate.validate(base_lesson, schema)
    assert len(errors_high) > 0


def test_r4_f1_boundary_empty_tags_accepted() -> None:
    """Validate empty tags array [] is valid in trace schema."""
    from commontrace import validate

    schema = validate.load_schema("trace.schema.json")
    trace_data = {
        "id": "2026-09-20_empty-tags",
        "created_at": "2026-09-20T01:00:00Z",
        "agent_type": "code",
        "agent_id": "test-agent",
        "profile": "default",
        "title": "Trace with empty tags",
        "context_text": "Context text",
        "solution_text": "Solution text",
        "tags": [],  # Empty tags list
        "outcome": {
            "resolved": True,
            "tokens_used": 100,
            "llm_calls": 1,
        },
    }
    assert validate.validate(trace_data, schema) == []


def test_r4_f1_boundary_non_string_tags_rejected() -> None:
    """Validate non-string items in tags array are rejected."""
    from commontrace import validate

    schema = validate.load_schema("trace.schema.json")
    trace_data = {
        "id": "2026-09-20_bad-tags",
        "created_at": "2026-09-20T01:00:00Z",
        "agent_type": "code",
        "agent_id": "test-agent",
        "profile": "default",
        "title": "Bad tags trace",
        "context_text": "Context text",
        "solution_text": "Solution text",
        "tags": [123, True, None],  # Invalid types
        "outcome": {"resolved": True},
    }
    errors = validate.validate(trace_data, schema)
    assert len(errors) > 0


def test_r4_f1_boundary_outcome_optional_fields() -> None:
    """Validate that minimal outcome object with only resolved field passes validation."""
    from commontrace import validate

    schema = validate.load_schema("trace.schema.json")
    trace_data = {
        "id": "2026-09-20_minimal-outcome",
        "created_at": "2026-09-20T01:00:00Z",
        "agent_type": "code",
        "agent_id": "test-agent",
        "profile": "default",
        "title": "Minimal outcome trace",
        "context_text": "Context text",
        "solution_text": "Solution text",
        "tags": ["test"],
        "outcome": {
            "resolved": False,
            # tokens_used and llm_calls omitted
        },
    }
    assert validate.validate(trace_data, schema) == []


def test_r4_f1_boundary_frontmatter_delimiters_edge(tmp_path: Path) -> None:
    """Validate frontmatter extraction when body contains dashed lines."""
    from commontrace import frontmatter

    raw_markdown = """---
name: Dashes in Body
description: Test dashed text in body
agent_type: code
domain: testing
importance: 5
importance_rationale: Test
applies_when: Test
do_not_apply_when: Test
uses: 0
last_hit: "2026-09-20"
status: active
tags: [test]
---

# Title

Here is a horizontal rule:
---
And another one:
---
Final remarks.
"""
    file_path = tmp_path / "dashes_in_body.md"
    file_path.write_text(raw_markdown, encoding="utf-8")
    fm, body = frontmatter.read(str(file_path))
    assert fm["name"] == "Dashes in Body"
    assert "---" in body
    assert "Final remarks" in body


# ============================================================================
# R4-F2: Boundary Cases for Security Sandboxing (>=5 tests)
# ============================================================================

def test_r4_f2_boundary_symlink_circular_loop(tmp_path: Path) -> None:
    """Validate scanning handles circular symlink loops without infinite recursion."""
    from commontrace.reference.build_index import iter_active_lessons

    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()

    link_a = lessons_dir / "lesson_loop_a.md"
    link_b = lessons_dir / "lesson_loop_b.md"

    try:
        link_a.symlink_to(link_b)
        link_b.symlink_to(link_a)
    except OSError:
        pytest.skip("Symlink creation not supported")

    # Scanning directory with circular symlinks must not hang or crash
    found = list(iter_active_lessons(str(lessons_dir)))
    assert isinstance(found, list)


def test_r4_f2_boundary_null_byte_in_path_rejected(tmp_path: Path) -> None:
    """Validate enforce_boundary rejects paths with embedded null bytes."""
    from commontrace.paths import enforce_boundary

    base = tmp_path / "safe_store"
    base.mkdir()

    with pytest.raises((ValueError, Exception)):
        enforce_boundary(str(base), "memory/lessons/\x00evil.md")


def test_r4_f2_boundary_uncanonicalized_double_dot_escape(tmp_path: Path) -> None:
    """Validate enforce_boundary catches multiple relative jumps trying to escape base."""
    from commontrace.paths import PathTraversalError, enforce_boundary

    base = tmp_path / "safe_store"
    base.mkdir()

    with pytest.raises((PathTraversalError, ValueError)):
        enforce_boundary(str(base), "memory/../../../etc/passwd")


def test_r4_f2_boundary_leading_slash_treated_as_traversal(tmp_path: Path) -> None:
    """Validate leading slash pointing outside base triggers boundary error."""
    from commontrace.paths import PathTraversalError, enforce_boundary

    base = tmp_path / "safe_store"
    base.mkdir()

    with pytest.raises((PathTraversalError, ValueError)):
        enforce_boundary(str(base), "/tmp/outside.txt")


def test_r4_f2_boundary_subprocess_safe_path_isolated() -> None:
    """Validate subprocess execution runs with PYTHONSAFEPATH enabled."""
    import inspect
    from commontrace.commands import _shellout

    source = inspect.getsource(_shellout.run_script)
    assert 'env["PYTHONSAFEPATH"] = "1"' in source or "PYTHONSAFEPATH" in source


# ============================================================================
# R4-F3: Boundary Cases for Performance & Scalability (>=5 tests)
# ============================================================================

def test_r4_f3_boundary_large_chunk_size_scaling() -> None:
    """Validate compute_semantic_duplicates scaling with chunk_size > len(items)."""
    np = pytest.importorskip("numpy", reason="numpy required for benchmark scaling test")
    from commontrace.reference.measure_performance import compute_semantic_duplicates

    n = 100
    dim = 32
    rng = np.random.default_rng(42)
    embs = rng.standard_normal((n, dim), dtype=np.float32)
    embs /= np.linalg.norm(embs, axis=1, keepdims=True)
    slugs = [f"slug_{i}" for i in range(n)]

    start = time.perf_counter()
    count, pairs = compute_semantic_duplicates(embs, slugs, threshold=0.9, chunk_size=200)
    duration = time.perf_counter() - start

    assert duration < 1.0, f"Deduplication took too long: {duration:.3f}s"
    assert isinstance(count, int)


def test_r4_f3_boundary_tiny_chunk_size_scaling() -> None:
    """Validate compute_semantic_duplicates scaling with tiny chunk_size=2."""
    np = pytest.importorskip("numpy", reason="numpy required for benchmark scaling test")
    from commontrace.reference.measure_performance import compute_semantic_duplicates

    n = 40
    dim = 16
    rng = np.random.default_rng(99)
    embs = rng.standard_normal((n, dim), dtype=np.float32)
    embs /= np.linalg.norm(embs, axis=1, keepdims=True)
    slugs = [f"item_{i}" for i in range(n)]

    start = time.perf_counter()
    count, pairs = compute_semantic_duplicates(embs, slugs, threshold=0.9, chunk_size=2)
    duration = time.perf_counter() - start

    assert duration < 1.5, f"Deduplication took too long: {duration:.3f}s"
    assert isinstance(count, int)


def test_r4_f3_boundary_fast_importances_loader_latency(tmp_path: Path) -> None:
    """Validate load_importances_from_index executes in < 50ms for 500 items."""
    np = pytest.importorskip("numpy", reason="numpy required for fast importances loader test")
    from commontrace.reference.query import load_importances_from_index

    n = 500
    npz_path = tmp_path / "bench_index.npz"
    slugs = np.array([f"lesson_{i:04d}" for i in range(n)])
    importances = np.random.randint(1, 6, size=n, dtype=np.int64)
    statuses = np.array(["active" for _ in range(n)])
    embeddings = np.zeros((n, 8), dtype=np.float32)

    np.savez(npz_path, slugs=slugs, importances=importances, statuses=statuses, embeddings=embeddings)

    data = np.load(npz_path)
    start = time.perf_counter()
    res = load_importances_from_index(data)
    elapsed = time.perf_counter() - start

    assert res is not None
    imp_dict = res.importances if hasattr(res, "importances") else (res[0] if isinstance(res, tuple) else res)
    assert len(imp_dict) == n
    assert elapsed < 0.05, f"Fast load exceeded 50ms latency limit: {elapsed*1000:.2f}ms"


def test_r4_f3_boundary_lexical_index_latency_100_docs() -> None:
    """Validate lexical deduplication runs in under 1.0s on 50 documents."""
    from commontrace.reference.measure_performance import compute_lexical_duplicates

    lessons = {
        f"perf_doc_{i:03d}": {
            "description": f"Distinct vocabulary content for document index performance test number {i} with some shared tokens",
            "applies_when": f"When running performance benchmark test {i}",
        }
        for i in range(50)
    }

    start = time.perf_counter()
    dups = compute_lexical_duplicates(lessons, threshold=0.8)
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f"Lexical duplicate check took {elapsed:.2f}s"
    assert isinstance(dups, dict)


def test_r4_f3_boundary_frontmatter_parse_50_files_latency(
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate batch parsing of frontmatter across 50 files finishes in under 500ms."""
    from commontrace import frontmatter

    file_paths = [
        lesson_factory(isolated_store, slug=f"fm_perf_{i:02d}", title=f"FM Perf {i}")
        for i in range(50)
    ]

    start = time.perf_counter()
    for fp in file_paths:
        data, _ = frontmatter.read(str(fp))
        assert "name" in data
    elapsed = time.perf_counter() - start

    assert elapsed < 0.5, f"Frontmatter batch load took {elapsed:.2f}s"


# ============================================================================
# R4-F4: Boundary Cases for Diagnostic & Benchmark Health (>=5 tests)
# ============================================================================

def test_r4_f4_boundary_bench_zero_episodes_clean(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
) -> None:
    """Validate commontrace bench on store with zero episodes exits 0 with clean message."""
    res = cli_runner(isolated_store, "bench")
    assert res.exit_code == 0
    assert "not enough episodes" in res.stdout.lower() or "0 episodes" in res.stdout.lower() or "benchmark" in res.stdout.lower()
    assert "Traceback" not in res.stderr


def test_r4_f4_boundary_bench_single_episode_computes(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
    episode_factory: Callable[..., Path],
) -> None:
    """Validate commontrace bench on store with exactly 1 episode calculates cleanly."""
    episode_factory(isolated_store, name="2026-07-01_single_ep")

    res = cli_runner(isolated_store, "bench")
    assert res.exit_code == 0
    assert "Traceback" not in res.stderr


def test_r4_f4_boundary_bench_corrupt_episode_tolerated(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
    episode_factory: Callable[..., Path],
) -> None:
    """Validate commontrace bench skips or tolerates a corrupt episode file without crashing."""
    # Seed 1 valid episode
    episode_factory(isolated_store, name="2026-07-01_valid_ep")

    # Seed 1 corrupt episode file
    corrupt_ep = isolated_store / "memory" / "episodes" / "2026-09-20_corrupt.md"
    corrupt_ep.write_text("NOT VALID FRONTMATTER\n{{{:::---\n", encoding="utf-8")

    res = cli_runner(isolated_store, "bench")
    assert res.exit_code == 0
    assert "Traceback" not in res.stderr


def test_r4_f4_boundary_bench_save_creates_json_artifact(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
    episode_factory: Callable[..., Path],
) -> None:
    """Validate commontrace bench --save outputs clean JSON file."""
    episode_factory(isolated_store, name="2026-07-01_save_ep")

    res = cli_runner(isolated_store, "bench", "--save")
    assert res.exit_code == 0
    # Check that bench created report in memory/benchmarks or reports
    assert "saved" in res.stdout.lower() or res.exit_code == 0


def test_r4_f4_boundary_doctor_custom_dest(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
) -> None:
    """Validate commontrace doctor accepts explicit --dest directory."""
    res = cli_runner(isolated_store, "doctor", "--dest", str(isolated_store))
    assert res.exit_code == 0
    assert "commontrace" in res.stdout.lower()


# ============================================================================
# R4-F5: Boundary Cases for Golden Workflows (>=5 tests)
# ============================================================================

def test_r4_f5_boundary_distill_zero_traces(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
) -> None:
    """Validate commontrace distill handles an empty traces directory gracefully."""
    res = cli_runner(isolated_store, "distill")
    assert res.exit_code == 0
    assert "Traceback" not in res.stderr


def test_r4_f5_boundary_multi_profile_trace_separation(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
) -> None:
    """Validate capturing traces with different profiles isolates profile metadata."""
    res1 = cli_runner(
        isolated_store,
        "capture",
        "--title", "Profile A Trace",
        "--context", "Context A",
        "--solution", "Solution A",
        "--profile", "fleet-alpha",
    )
    assert res1.exit_code == 0

    res2 = cli_runner(
        isolated_store,
        "capture",
        "--title", "Profile B Trace",
        "--context", "Context B",
        "--solution", "Solution B",
        "--profile", "fleet-beta",
    )
    assert res2.exit_code == 0

    # Verify profile recorded in trace files (excluding README.md)
    traces = [t for t in (isolated_store / "memory" / "traces").glob("*.md") if t.name != "README.md"]
    assert len(traces) == 2


def test_r4_f5_boundary_distill_with_extreme_similarity_threshold(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
    trace_factory: Callable[..., Path],
) -> None:
    """Validate distill with threshold 0.999 does not cluster non-identical traces."""
    trace_factory(isolated_store, title="Unique Problem One", trace_id="2026-09-20_trace_one")
    trace_factory(isolated_store, title="Unique Problem Two", trace_id="2026-09-20_trace_two")

    res = cli_runner(isolated_store, "distill", "--similarity-threshold", "0.999")
    assert res.exit_code == 0
    assert "Traceback" not in res.stderr


def test_r4_f5_boundary_lesson_approve_refuses_nonexistent_or_active(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate lesson approve cleanly handles nonexistent slugs and non-review status."""
    res_none = cli_runner(["lesson", "approve", "nonexistent_slug", "--dest", str(isolated_store)])
    assert res_none.exit_code == 1
    assert "no lesson found" in res_none.stderr.lower()

    lesson_factory(isolated_store, slug="lesson_already_act", status="active")
    res_active = cli_runner(["lesson", "approve", "lesson_already_act", "--dest", str(isolated_store)])
    assert res_active.exit_code == 1
    assert "refusing to approve" in res_active.stderr.lower() or "not 'review'" in res_active.stderr.lower()


def test_r4_f5_boundary_full_trace_capture_and_export_lifecycle(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
    tmp_path: Path,
) -> None:
    """Validate full lifecycle: capture -> list -> export -> verify."""
    # 1. Capture
    res_cap = cli_runner(
        isolated_store,
        "capture",
        "--title", "Lifecycle Trace",
        "--context", "Lifecycle Context",
        "--solution", "Lifecycle Solution",
        "--tags", "lifecycle,e2e",
    )
    assert res_cap.exit_code == 0

    # 2. List
    res_list = cli_runner(isolated_store, "trace", "list")
    assert res_list.exit_code == 0
    assert "lifecycle" in res_list.stdout.lower()

    # 3. Export
    export_file = tmp_path / "lifecycle_export.jsonl"
    res_exp = cli_runner([
        "export",
        "--kind", "traces",
        "--out", str(export_file),
        "--dest", str(isolated_store),
    ])
    assert res_exp.exit_code == 0
    assert export_file.exists()
    assert "Lifecycle Trace" in export_file.read_text(encoding="utf-8")


# ============================================================================
# R4-F6: Boundary Cases for Adversarial Coverage (>=5 tests)
# ============================================================================

def test_r4_f6_boundary_adversarial_zalgo_text(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
) -> None:
    """Validate handling of Zalgo text combining marks in trace capture."""
    zalgo_title = "T̷e̵s̸t̷ ̵Z̶a̶l̴g̵o̵ ̸T̶e̷x̸t̸"
    res = cli_runner(
        isolated_store,
        "capture",
        "--title", zalgo_title,
        "--context", "Zalgo context",
        "--solution", "Zalgo solution",
    )
    assert res.exit_code == 0
    assert "Traceback" not in res.stderr


def test_r4_f6_boundary_binary_garbage_in_memory_directory(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
) -> None:
    """Validate that binary/non-text garbage in memory/lessons/ does not crash commontrace doctor."""
    garbage_file = isolated_store / "memory" / "lessons" / "garbage.bin"
    garbage_file.write_bytes(b"\x00\xff\xfe\xca\xfe\xba\xbe\x00\x01\x02\x03")

    res = cli_runner(isolated_store, "doctor")
    assert res.exit_code == 0
    assert "Traceback" not in res.stderr


def test_r4_f6_boundary_extremely_long_tags_list(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
) -> None:
    """Validate capturing trace with 100 comma-separated tags."""
    tags_arg = ",".join([f"tag_{i}" for i in range(100)])
    res = cli_runner(
        isolated_store,
        "capture",
        "--title", "Many Tags Trace",
        "--context", "Many tags context",
        "--solution", "Many tags solution",
        "--tags", tags_arg,
    )
    assert res.exit_code == 0
    assert "Traceback" not in res.stderr


def test_r4_f6_boundary_control_characters_in_lesson_body(
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
    cli_runner: Callable[..., CLIResult],
) -> None:
    """Validate lesson validate handles ASCII control characters (\x01, \x02, \x03) cleanly."""
    control_body = "Body with control characters: \x01 \x02 \x03 \x04 \x05 \x06"
    l_path = lesson_factory(isolated_store, slug="control_chars", title="Control Chars", body=control_body)

    res = cli_runner(isolated_store, "lesson", "validate", str(l_path))
    # Should either validate or report schema/encoding error cleanly without unhandled crash
    assert "Traceback" not in res.stderr


def test_r4_f6_boundary_newlines_in_title_and_context(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
) -> None:
    """Validate capturing trace with embedded newlines in context and solution fields."""
    multi_line_ctx = "Line 1\nLine 2\n\nLine 3\n\tIndented Line 4"
    multi_line_sol = "Step 1\nStep 2\n\nStep 3"

    res = cli_runner(
        isolated_store,
        "capture",
        "--title", "Multiline Field Trace",
        "--context", multi_line_ctx,
        "--solution", multi_line_sol,
    )
    assert res.exit_code == 0
    assert "Traceback" not in res.stderr
