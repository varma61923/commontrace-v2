"""Tier 3: Cross-Feature Combinations & State/Workflow Interactions.

Tests feature interactions across multiple lifecycle stages, multi-store operations,
export/import roundtrips, sync/pilot workflows, holdout experiments, and system diagnostics.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Callable

from tests.e2e.conftest import CLIResult

# ============================================================================
# 1. Full Lifecycle Pipeline: Init -> Capture -> Distill -> Approve -> Validate -> Query -> Bench
# ============================================================================

def test_tier3_full_lifecycle_pipeline(
    cli_runner: Callable[..., CLIResult],
    tmp_path: Path,
) -> None:
    """Validate full lifecycle pipeline: init -> capture -> distill -> approve -> validate -> query -> bench."""
    store = tmp_path / "lifecycle_store"
    store.mkdir(parents=True, exist_ok=True)

    # 1. Init store
    res_init = cli_runner(["init", "--dest", str(store), "--agent-type", "code"])
    assert res_init.exit_code == 0
    assert (store / "memory" / "INDEX.md").is_file()

    # 2. Capture multiple related traces (e.g. redis connection timeouts)
    res_cap1 = cli_runner([
        "capture",
        "--title", "Redis Pool Connection Timeout",
        "--context", "High concurrency caused redis connection pool exhaustion and timeout",
        "--solution", "Increase redis connection pool max_connections and set connect_timeout=5",
        "--tags", "redis,database,networking",
        "--resolved",
        "--tokens-used", "420",
        "--llm-calls", "3",
        "--dest", str(store),
    ])
    assert res_cap1.exit_code == 0

    res_cap2 = cli_runner([
        "capture",
        "--title", "Redis Connection Pool Starvation",
        "--context", "Worker threads starved waiting for available redis connection pool socket",
        "--solution", "Configure connection pool max_connections=50 with health check ping",
        "--tags", "redis,database,networking",
        "--resolved",
        "--tokens-used", "510",
        "--llm-calls", "2",
        "--dest", str(store),
    ])
    assert res_cap2.exit_code == 0

    # 3. Distill into candidate lesson
    res_distill = cli_runner([
        "distill",
        "--similarity-threshold", "0.2",
        "--min-cluster-size", "2",
        "--dest", str(store),
    ])
    assert res_distill.exit_code == 0
    assert "Proposed" in res_distill.stdout or "candidate" in res_distill.stdout.lower()

    # Check that a candidate lesson file was generated in memory/lessons/
    candidate_files = [
        f for f in (store / "memory" / "lessons").glob("lesson_*.md")
        if f.name != "lesson_template.md"
    ]
    assert len(candidate_files) >= 1, "Expected distill to propose at least one candidate lesson"
    proposed_file = candidate_files[0]
    proposed_slug = proposed_file.stem

    # Verify proposed lesson starts at status review
    content = proposed_file.read_text(encoding="utf-8")
    assert "status: review" in content

    # 4. Curator edits the candidate lesson to replace TODO scaffolding with real guidance
    content = proposed_file.read_text(encoding="utf-8")
    content = content.replace("TODO: precise activation condition (auto-proposed, needs human review)", "When redis connection pool under concurrency exhausts sockets.")
    content = content.replace("TODO: explicit counter-condition (auto-proposed, needs human review)", "When redis is not used.")
    content = content.replace("TODO: one actionable sentence, derived from `What worked` below.", "Configure max_connections and connection timeouts on redis client pools.")
    content = content.replace("TODO: when to invoke it, how to use it concretely.", "Apply when initializing redis connection pool.")
    content = content.replace("TODO: cases where the rule does NOT apply.", "Does not apply to single-threaded embedded scripts.")
    proposed_file.write_text(content, encoding="utf-8")

    # Approve the curated lesson to transition from review -> active
    res_approve = cli_runner([
        "lesson", "approve",
        proposed_slug,
        "--dest", str(store),
    ])
    assert res_approve.exit_code == 0
    assert "approved" in res_approve.stdout.lower() or "active" in res_approve.stdout.lower()

    # Verify status changed to active
    content_approved = proposed_file.read_text(encoding="utf-8")
    assert "status: active" in content_approved

    # 5. Validate the lesson against canonical JSON Schema
    res_val = cli_runner([
        "lesson", "validate",
        str(proposed_file),
        "--dest", str(store),
    ])
    assert res_val.exit_code == 0
    assert "Valid" in res_val.stdout or "valid" in res_val.stdout.lower()

    # 6. Query the store for the topic (lexical retrieval is self-contained and fast)
    res_query = cli_runner([
        "query",
        "redis connection pool timeout starvation",
        "--lexical",
        "--top-k", "5",
        "--dest", str(store),
    ])
    assert res_query.exit_code == 0
    assert proposed_slug in res_query.stdout or "redis" in res_query.stdout.lower()

    # 7. Run bench on the memory store to verify health score
    res_bench = cli_runner(["bench", "--dest", str(store)])
    assert res_bench.exit_code == 0
    assert "not enough episodes" in res_bench.stdout.lower() or "active lessons:" in res_bench.stdout.lower() or "health" in res_bench.stdout.lower()


# ============================================================================
# 2. Multi-Store Isolation & Cross-Store Operations (Commons & Overlap)
# ============================================================================

def test_tier3_multi_store_isolation_and_cross_store_commons(
    cli_runner: Callable[..., CLIResult],
    tmp_path: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate isolation between multiple stores and cross-store analysis via commons/overlap."""
    # Create Store A (backend)
    store_a = tmp_path / "store_backend"
    store_a.mkdir()
    res_a = cli_runner(["init", "--dest", str(store_a), "--agent-type", "backend"])
    assert res_a.exit_code == 0

    # Create Store B (frontend)
    store_b = tmp_path / "store_frontend"
    store_b.mkdir()
    res_b = cli_runner(["init", "--dest", str(store_b), "--agent-type", "frontend"])
    assert res_b.exit_code == 0

    # Add backend-specific lesson to Store A
    lesson_factory(
        store_a,
        slug="lesson_db_transactions",
        title="Always rollback aborted transactions",
        description="Ensure database connection state is cleaned up after transaction abort.",
        domain="database",
        agent_type="backend",
        extra_fm={"tags": ["database", "sql"]},
    )

    # Add shared-concept lesson to Store A
    lesson_factory(
        store_a,
        slug="lesson_jwt_validation",
        title="Verify JWT expiry before processing payload",
        description="Reject expired tokens immediately with 401 Unauthorized status code.",
        domain="security",
        agent_type="backend",
        extra_fm={"tags": ["security", "auth", "jwt"]},
    )

    # Add shared-concept lesson to Store B
    lesson_factory(
        store_b,
        slug="lesson_client_auth_expiry",
        title="Check JWT expiry before dispatching API request",
        description="Trigger token refresh loop when JWT expires before making network request.",
        domain="security",
        agent_type="frontend",
        extra_fm={"tags": ["security", "auth", "jwt"]},
    )

    # Generate overlap signatures for Store A and Store B
    sig_a = tmp_path / "sig_a.json"
    sig_b = tmp_path / "sig_b.json"

    res_sign_a = cli_runner([
        "overlap", "sign",
        "--fleet-label", "backend-fleet",
        "--out", str(sig_a),
        "--dest", str(store_a),
    ])
    assert res_sign_a.exit_code == 0
    assert sig_a.is_file()

    res_sign_b = cli_runner([
        "overlap", "sign",
        "--fleet-label", "frontend-fleet",
        "--out", str(sig_b),
        "--dest", str(store_b),
    ])
    assert res_sign_b.exit_code == 0
    assert sig_b.is_file()

    # Compare signatures via overlap report
    res_report = cli_runner([
        "overlap", "report",
        "--ours", str(sig_a),
        "--theirs", str(sig_b),
    ])
    assert res_report.exit_code == 0
    assert "overlap" in res_report.stdout.lower() or "signature" in res_report.stdout.lower() or "benefit" in res_report.stdout.lower()

    # Verify Store A and Store B remain strictly isolated on disk
    assert (store_a / "memory" / "lessons" / "lesson_db_transactions.md").is_file()
    assert not (store_b / "memory" / "lessons" / "lesson_db_transactions.md").exists()
    assert (store_b / "memory" / "lessons" / "lesson_client_auth_expiry.md").is_file()
    assert not (store_a / "memory" / "lessons" / "lesson_client_auth_expiry.md").exists()


# ============================================================================
# 3. Export / Import Roundtrip Across Diverse Formats & Stores
# ============================================================================

def test_tier3_export_import_traces_roundtrip(
    cli_runner: Callable[..., CLIResult],
    tmp_path: Path,
    trace_factory: Callable[..., Path],
) -> None:
    """Validate full export -> import roundtrip for traces between two stores."""
    store_src = tmp_path / "store_source"
    store_src.mkdir()
    cli_runner(["init", "--dest", str(store_src), "--agent-type", "code"])

    # Create 3 synthetic traces in source store
    for i in range(3):
        trace_factory(
            store_src,
            trace_id=f"2026-09-20_src-trace-{i:03d}",
            title=f"Source Trace Incident {i}",
            context=f"Context details for incident {i} involving service latency.",
            solution=f"Solution resolved incident {i} by optimizing database indexes.",
            tags=["performance", "database", f"batch-{i}"],
            outcome={"resolved": True, "tokens_used": 300 + i * 50, "llm_calls": 2},
        )

    # Export traces to JSONL file
    export_file = tmp_path / "exported_traces.jsonl"
    res_export = cli_runner([
        "export",
        "--kind", "traces",
        "--out", str(export_file),
        "--dest", str(store_src),
    ])
    assert res_export.exit_code == 0
    assert export_file.is_file()

    # Verify exported JSONL content has 3 lines
    lines = export_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3
    for line in lines:
        record = json.loads(line)
        assert record.get("kind") == "trace" or "title" in record
        assert "context" in record or "context_text" in record

    # Initialize a new destination store
    store_dst = tmp_path / "store_dest"
    store_dst.mkdir()
    cli_runner(["init", "--dest", str(store_dst), "--agent-type", "code"])

    # Import the exported JSONL into destination store
    res_import = cli_runner([
        "import",
        str(export_file),
        "--format", "jsonl",
        "--source", "generic",
        "--agent-type", "code",
        "--dest", str(store_dst),
    ])
    assert res_import.exit_code == 0
    assert "Imported" in res_import.stdout or "3" in res_import.stdout

    # Verify all 3 traces exist in destination store
    dst_traces = list((store_dst / "memory" / "traces").glob("*.md"))
    # Filter out README.md
    dst_trace_files = [f for f in dst_traces if f.name != "README.md"]
    assert len(dst_trace_files) == 3

    # Validate each imported trace in the destination store
    for trace_file in dst_trace_files:
        res_val = cli_runner([
            "trace", "validate",
            str(trace_file),
            "--dest", str(store_dst),
        ])
        assert res_val.exit_code == 0


def test_tier3_export_lessons_with_status_filtering(
    cli_runner: Callable[..., CLIResult],
    tmp_path: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate export filtering lessons by status (active vs review vs deprecated)."""
    store = tmp_path / "store_lessons_export"
    store.mkdir()
    cli_runner(["init", "--dest", str(store), "--agent-type", "code"])

    # Create active lessons
    lesson_factory(store, slug="lesson_act_1", status="active", title="Active Rule 1")
    lesson_factory(store, slug="lesson_act_2", status="active", title="Active Rule 2")

    # Create review lesson
    lesson_factory(store, slug="lesson_rev_1", status="review", title="Under Review Rule")

    # Create deprecated lesson
    lesson_factory(store, slug="lesson_dep_1", status="deprecated", title="Obsolete Rule")

    # Export active lessons only
    active_out = tmp_path / "active_lessons.jsonl"
    res_act = cli_runner([
        "export",
        "--kind", "lessons",
        "--status", "active",
        "--out", str(active_out),
        "--dest", str(store),
    ])
    assert res_act.exit_code == 0
    lines_act = [json.loads(line) for line in active_out.read_text(encoding="utf-8").strip().split("\n") if line.strip()]
    assert len(lines_act) == 2
    assert all(rec.get("status") == "active" for rec in lines_act)

    # Export all lessons without status filter
    all_out = tmp_path / "all_lessons.jsonl"
    res_all = cli_runner([
        "export",
        "--kind", "lessons",
        "--out", str(all_out),
        "--dest", str(store),
    ])
    assert res_all.exit_code == 0
    lines_all = [json.loads(line) for line in all_out.read_text(encoding="utf-8").strip().split("\n") if line.strip()]
    assert len(lines_all) == 4


def test_tier3_csv_import_with_custom_column_mapping(
    cli_runner: Callable[..., CLIResult],
    tmp_path: Path,
) -> None:
    """Validate importing external CSV incident logs with custom column headers."""
    store = tmp_path / "csv_import_store"
    store.mkdir()
    cli_runner(["init", "--dest", str(store), "--agent-type", "support"])

    # Create a realistic CSV file from an external helpdesk/monitoring system
    csv_file = tmp_path / "incidents.csv"
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["incident_id", "summary", "root_cause_context", "remediation", "labels"])
        writer.writerow([
            "INC-1001",
            "Disk Space Exhaustion on Log Partition",
            "Logrotate daemon stopped due to corrupt config file, filling /var/log",
            "Purged expired logs, repaired syntax error in logrotate.d/nginx, restarted daemon",
            "storage,linux,ops",
        ])
        writer.writerow([
            "INC-1002",
            "SSL Certificate Renewal Failure",
            "Certbot renew failed because HTTP-01 challenge was blocked by WAF rule",
            "Added temporary WAF exclusion for /.well-known/acme-challenge/ and renewed certificate",
            "security,ssl,waf",
        ])

    # Import with custom column mappings
    res_import = cli_runner([
        "import",
        str(csv_file),
        "--format", "csv",
        "--source", "generic",
        "--agent-type", "support",
        "--title-field", "summary",
        "--context-field", "root_cause_context",
        "--solution-field", "remediation",
        "--tags-field", "labels",
        "--id-field", "incident_id",
        "--dest", str(store),
    ])
    assert res_import.exit_code == 0
    assert "wrote 2 trace" in res_import.stdout or "2 row(s) parseable" in res_import.stdout

    # Verify traces exist and validate
    traces = [p for p in (store / "memory" / "traces").glob("*.md") if p.name != "README.md"]
    assert len(traces) == 2
    for t in traces:
        res_val = cli_runner(["trace", "validate", str(t), "--dest", str(store)])
        assert res_val.exit_code == 0


# ============================================================================
# 4. Sync & Hub Integration Flows
# ============================================================================

def test_tier3_sync_command_diagnostics_without_hub(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Validate `commontrace sync` execution without configured Hub provides clear guidance."""
    # When no COMMONTRACE_HUB_URL or --hub-url is provided, sync prints helpful instructions and exits cleanly (0)
    res = cli_runner(["sync", "--dest", str(isolated_store)], env={"COMMONTRACE_HUB_URL": "", "COMMONTRACE_HUB_API_KEY": ""})
    assert res.exit_code == 0
    assert "bridges the local store to the CommonTrace Hub" in res.stdout
    assert "No Hub is configured" in res.stdout


def test_tier3_sync_command_handles_unreachable_hub_gracefully(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Validate `commontrace sync --push` with unreachable URL reports operational error without crash."""
    res = cli_runner([
        "sync",
        "--push",
        "--hub-url", "http://127.0.0.1:59999/mcp",
        "--hub-api-key", "test-key",
        "--dest", str(isolated_store),
    ])
    # Should report error gracefully with exit code 1
    assert res.exit_code in (0, 1)
    if res.exit_code == 1:
        assert "error" in res.stderr.lower() or "connection" in res.stderr.lower() or "refused" in res.stderr.lower() or "error" in res.stdout.lower()


# ============================================================================
# 5. Pilot & Impact Analysis Workflows
# ============================================================================

def test_tier3_pilot_evaluation_end_to_end(
    cli_runner: Callable[..., CLIResult],
    tmp_path: Path,
    trace_factory: Callable[..., Path],
    lesson_factory: Callable[..., Path],
    episode_factory: Callable[..., Path],
) -> None:
    """Validate `commontrace pilot` end-to-end report generation with telemetry metrics."""
    store = tmp_path / "pilot_store"
    store.mkdir()
    cli_runner(["init", "--dest", str(store), "--agent-type", "code"])

    # Add active lessons
    l1 = lesson_factory(
        store,
        slug="lesson_query_optimization",
        title="Use index scans instead of sequential table scans",
        description="Add composite index on frequent query WHERE clauses.",
        domain="database",
        importance=4,
        extra_fm={"tags": ["database", "performance"]},
    )
    l2 = lesson_factory(
        store,
        slug="lesson_input_sanitization",
        title="Sanitize user inputs to prevent injection",
        description="Escape all SQL parameters before query execution.",
        domain="security",
        importance=5,
        extra_fm={"tags": ["security", "validation"]},
    )

    # Add historical traces with outcomes
    for i in range(4):
        trace_factory(
            store,
            trace_id=f"2026-09-01_trace-{i:02d}",
            title=f"Slow query on user lookup {i}",
            context="Sequential scan on 10M rows caused 15s latency",
            solution="Added index on user_id",
            outcome={"resolved": True, "tokens_used": 400, "llm_calls": 2},
            tags=["database", "performance"],
        )

    for i in range(2):
        trace_factory(
            store,
            trace_id=f"2026-09-05_trace-fail-{i:02d}",
            title=f"Unresolved query timeout {i}",
            context="Query timed out after 30s",
            solution="Investigated without resolution",
            outcome={"resolved": False, "tokens_used": 600, "llm_calls": 4},
            tags=["database", "timeout"],
        )

    # Add episode linking lessons
    episode_factory(
        store,
        name="2026-09-10_perf-tune-episode",
        verdict="CONFORM",
        lessons_retrieved=["lesson_query_optimization"],
        lessons_hit=["lesson_query_optimization"],
    )

    # Run pilot with economic assumptions
    res_pilot = cli_runner([
        "pilot",
        "--cost-per-1k-tokens", "0.003",
        "--value-per-error-avoided", "45.0",
        "--dest", str(store),
    ])
    assert res_pilot.exit_code == 0
    assert "CommonTrace Pilot Report" in res_pilot.stdout or "Taxonomy" in res_pilot.stdout or "Resolution" in res_pilot.stdout

    # Run pilot in JSON mode
    res_json = cli_runner([
        "pilot",
        "--json",
        "--dest", str(store),
    ])
    assert res_json.exit_code == 0
    data = json.loads(res_json.stdout)
    assert isinstance(data, dict)
    assert "taxonomy" in data or "categories" in data or "clusters" in data or "summary" in data or "pilot" in data


def test_tier3_impact_and_reliability_reports(
    cli_runner: Callable[..., CLIResult],
    tmp_path: Path,
    trace_factory: Callable[..., Path],
    lesson_factory: Callable[..., Path],
    episode_factory: Callable[..., Path],
) -> None:
    """Validate `commontrace impact` and `commontrace reliability` reporting."""
    store = tmp_path / "impact_store"
    store.mkdir()
    cli_runner(["init", "--dest", str(store), "--agent-type", "code"])

    # Create lessons and episodes
    lesson_factory(store, slug="lesson_retry_logic", title="Add jitter to retries", importance=3)
    episode_factory(
        store,
        name="2026-09-15_retry-episode",
        verdict="CONFORM",
        lessons_retrieved=["lesson_retry_logic"],
        lessons_hit=["lesson_retry_logic"],
    )

    # Run impact command
    res_impact = cli_runner(["impact", "--dest", str(store)])
    assert res_impact.exit_code == 0
    assert "impact" in res_impact.stdout.lower() or "lesson" in res_impact.stdout.lower() or "retrieval" in res_impact.stdout.lower()

    # Run reliability command
    res_rel = cli_runner(["reliability", "--dest", str(store)])
    assert res_rel.exit_code == 0
    assert "reliability" in res_rel.stdout.lower() or "precision" in res_rel.stdout.lower() or "evidence" in res_rel.stdout.lower() or "score" in res_rel.stdout.lower()


# ============================================================================
# 6. Experimentation & Holdout Cross-Feature Integration
# ============================================================================

def test_tier3_experiment_holdout_capture_join_workflow(
    cli_runner: Callable[..., CLIResult],
    tmp_path: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate randomized holdout experiment: query with occasion-id, capture outcome, inspect experiment."""
    store = tmp_path / "experiment_store"
    store.mkdir()
    cli_runner(["init", "--dest", str(store), "--agent-type", "code"])

    # Add active lesson
    lesson_factory(
        store,
        slug="lesson_cache_invalidation",
        title="Invalidate cache after database write",
        description="Purge cached entry on entity modification to avoid stale reads.",
        domain="database",
        importance=4,
    )

    # 1. Query under experiment mode with an occasion ID
    occasion_id = "occ_tier3_test_001"
    res_query = cli_runner([
        "query",
        "cache invalidation after entity update",
        "--lexical",
        "--experiment",
        "--occasion-id", occasion_id,
        "--holdout-rate", "0.5",
        "--dest", str(store),
    ])
    assert res_query.exit_code == 0

    # Verify holdout log exists in memory/
    exp_log = store / "memory" / "holdout_log.jsonl"
    assert exp_log.is_file(), "Expected holdout_log.jsonl to be written"
    entries = [json.loads(line) for line in exp_log.read_text(encoding="utf-8").strip().split("\n") if line.strip()]
    assert any(e.get("occasion_id") == occasion_id for e in entries)

    # 2. Capture the outcome of the occasion, joining by occasion-id
    res_cap = cli_runner([
        "capture",
        "--title", "Entity Update With Cache Invalidation",
        "--context", "Entity was updated and cache was purged immediately",
        "--solution", "Used cache.delete(key) inside database transaction hook",
        "--occasion-id", occasion_id,
        "--resolved",
        "--tokens-used", "280",
        "--dest", str(store),
    ])
    assert res_cap.exit_code == 0

    # 3. Inspect experiment report to confirm joined outcome
    res_exp = cli_runner(["experiment", "--dest", str(store)])
    assert res_exp.exit_code == 0
    assert occasion_id in res_exp.stdout or "occasions" in res_exp.stdout.lower() or "holdout" in res_exp.stdout.lower()


# ============================================================================
# 7. Doctor Diagnostics & Store Integrity Maintenance
# ============================================================================

def test_tier3_doctor_diagnostics_across_corruption_and_repair(
    cli_runner: Callable[..., CLIResult],
    tmp_path: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate that `commontrace doctor` flags corrupt frontmatter and recovers upon repair."""
    store = tmp_path / "doctor_store"
    store.mkdir()
    cli_runner(["init", "--dest", str(store), "--agent-type", "code"])

    # Initially doctor reports clean state
    res_clean = cli_runner(["doctor", "--dest", str(store)])
    assert res_clean.exit_code == 0
    assert "error" not in res_clean.stdout.lower() or "[OK]" in res_clean.stdout

    # Introduce a corrupt lesson file with broken YAML frontmatter
    bad_lesson = store / "memory" / "lessons" / "lesson_corrupt.md"
    bad_lesson.write_text("---\ntitle: [Unclosed list syntax\nstatus: active\n---\nBody\n", encoding="utf-8")

    # Doctor should flag corruption
    res_corrupt = cli_runner(["doctor", "--dest", str(store)])
    assert res_corrupt.exit_code in (0, 1)
    assert "corrupt" in res_corrupt.stdout.lower() or "error" in res_corrupt.stdout.lower() or "yaml" in res_corrupt.stdout.lower() or "[WARN]" in res_corrupt.stdout or "[FAIL]" in res_corrupt.stdout

    # Repair file with valid lesson content
    lesson_factory(store, slug="lesson_corrupt", title="Repaired Lesson", status="active")

    # Doctor now passes cleanly
    res_repaired = cli_runner(["doctor", "--dest", str(store)])
    assert res_repaired.exit_code == 0


# ============================================================================
# 8. Consolidate & Release Versioning Lifecycle
# ============================================================================

def test_tier3_consolidate_and_release_lifecycle(
    cli_runner: Callable[..., CLIResult],
    tmp_path: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate `commontrace consolidate` and `commontrace release` snapshot workflow."""
    store = tmp_path / "release_store"
    store.mkdir()
    cli_runner(["init", "--dest", str(store), "--agent-type", "code"])

    lesson_factory(store, slug="lesson_release_rule", title="Release rule", status="active", importance=4)

    # 1. Run consolidate
    res_cons = cli_runner(["consolidate", "--dest", str(store)])
    assert res_cons.exit_code == 0

    # 2. Run release to create a snapshot
    res_rel = cli_runner([
        "release",
        "cut",
        "--reason", "Test snapshot release",
        "--dest", str(store),
    ])
    assert res_rel.exit_code == 0
    assert "release" in res_rel.stdout.lower() or "rel_" in res_rel.stdout

    # 3. List releases
    res_list = cli_runner(["release", "list", "--dest", str(store)])
    assert res_list.exit_code == 0
    assert "rel_" in res_list.stdout or "release" in res_list.stdout.lower()
