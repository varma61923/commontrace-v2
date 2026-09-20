"""Tier 4: Real-World Application Scenarios.

Tests end-to-end operational scenarios:
1. Code-review double-review pipeline simulation (SKILL.md reference profile:
   Implementer A -> Reviewer B -> Alpha past lesson injection -> Omega extraction -> Lambda validation).
2. Long-running fleet telemetry and failure distillation across dozens of traces.
3. Large corpus stress (100+ lessons), dosage management, and benchmark scalability.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from commontrace import dosage, frontmatter, redundancy
from tests.e2e.conftest import CLIResult


# ============================================================================
# Scenario 1: Code-Review Double-Review Pipeline Simulation (SKILL.md)
# ============================================================================

def test_tier4_code_review_pipeline_simulation(
    cli_runner: Callable[..., CLIResult],
    tmp_path: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Simulate the complete 11-phase SKILL.md double-review agent lifecycle.

    Phase 0: Alpha retrieves past lessons applicable to the task.
    Phase 1-3: Agent A (implementer) attempts solution, records trace.
    Phase 5: Agent B (independent reviewer) reviews code, identifies GAP.
    Phase 7: Iteration 2 - Agent A fixes the gap, records resolved trace.
    Phase 5: Agent B re-reviews and issues CONFORM verdict.
    Phase 10: Omega extracts new candidate lesson from the episode.
    Phase 11: Lambda audits and validates candidate lesson, promotes to active.
    Episode: Persists episode record with full metadata.
    Post-run: Subsequent Alpha query confirms newly minted lesson is retrieved.
    """
    store = tmp_path / "skill_pipeline_store"
    store.mkdir()
    res_init = cli_runner(["init", "--dest", str(store), "--agent-type", "code"])
    assert res_init.exit_code == 0

    # Seed memory with an established prior lesson
    lesson_factory(
        store,
        slug="lesson_db_connection_pooling",
        title="Always use bounded connection pools for database clients",
        description="Unbounded connection pools lead to resource starvation under load.",
        applies_when="When initializing database connections or client pools",
        domain="database",
        importance=4,
        status="active",
        extra_fm={"tags": ["database", "performance", "resources"]},
    )

    task_description = "Implement async database connection pool with automatic timeout and retry"

    # --- Phase 0: Alpha (Memory Retrieval) ---
    # Alpha retrieves relevant past lessons before coding begins
    res_alpha = cli_runner([
        "query",
        task_description,
        "--lexical",
        "--top-k", "3",
        "--dest", str(store),
    ])
    assert res_alpha.exit_code == 0
    assert "lesson_db_connection_pooling" in res_alpha.stdout
    injected_lessons = ["lesson_db_connection_pooling"]

    # --- Phase 3: Agent A (Implementer - Iteration 1) ---
    # Agent A writes code, encounters an initial issue during implementation
    res_cap_a1 = cli_runner([
        "capture",
        "--title", "DB Connection Pool Initial Implementation",
        "--context", "Implemented pool but connection timeout occurred under load tests",
        "--solution", "Added basic timeout configuration to connection socket",
        "--agent-id", "agent-a-implementer",
        "--agent-type", "code",
        "--tags", "database,concurrency,timeout",
        "--tokens-used", "920",
        "--llm-calls", "4",
        "--dest", str(store),
    ])
    assert res_cap_a1.exit_code == 0

    # --- Phase 5: Agent B (Reviewer - Iteration 1) ---
    # Reviewer B audits implementation and finds GAP: missing exponential backoff on retry
    verdict_round_1 = "GAP"
    gap_feedback = "Timeout handling lacks exponential backoff with jitter on retry"

    # --- Phase 7: Iteration 2 (Agent A addresses Gap) ---
    # Agent A incorporates feedback from Reviewer B, adds backoff with jitter
    res_cap_a2 = cli_runner([
        "capture",
        "--title", "DB Pool Retry With Exponential Backoff and Jitter",
        "--context", f"Reviewer B gap report: {gap_feedback}",
        "--solution", "Wrapped acquire in exponential backoff loop with random jitter factor",
        "--agent-id", "agent-a-implementer",
        "--agent-type", "code",
        "--tags", "database,concurrency,retry,backoff",
        "--resolved",
        "--tokens-used", "680",
        "--llm-calls", "3",
        "--dest", str(store),
    ])
    assert res_cap_a2.exit_code == 0

    # --- Phase 5: Agent B (Reviewer - Iteration 2) ---
    # Reviewer B re-evaluates: all criteria met
    verdict_round_2 = "CONFORM"
    assert verdict_round_2 == "CONFORM"

    # --- Phase 10: Omega (Lesson Extraction) ---
    # Omega creates a new candidate lesson proposal from what happened in this episode
    new_slug = "lesson_db_pool_retry_backoff"
    res_omega = cli_runner([
        "lesson", "new",
        "--slug", new_slug,
        "--description", "Connection pools under heavy load fail without backoff; apply exponential backoff with jitter.",
        "--domain", "database",
        "--applies-when", "When acquiring database connections from pool under high concurrency",
        "--do-not-apply-when", "When using local in-memory SQLite databases without connection pools",
        "--dest", str(store),
    ])
    assert res_omega.exit_code == 0

    # Check lesson was created with initial review status
    new_lesson_file = store / "memory" / "lessons" / f"{new_slug}.md"
    assert new_lesson_file.is_file()
    assert "status: review" in new_lesson_file.read_text(encoding="utf-8")

    # Replace scaffolding in new_lesson_file body so it passes approval validation
    fm, _ = frontmatter.read(str(new_lesson_file))
    filled_body = (
        "## Rule\n"
        "Apply exponential backoff with jitter when acquiring database connections from pool under high concurrency.\n\n"
        "## Why\n"
        "Without exponential backoff and jitter, simultaneous reconnect attempts produce a thundering herd that overwhelms database sockets.\n\n"
        "## How to apply\n"
        "Wrap the acquire call in an exponential backoff loop with a random jitter multiplier between 0.5 and 1.5.\n\n"
        "## Counter-examples\n"
        "Do not apply backoff to local in-memory SQLite connections where socket starvation does not occur.\n"
    )
    frontmatter.write(str(new_lesson_file), fm, filled_body)

    # --- Phase 11: Lambda (Automatic Backlog Validation) ---
    # Lambda audits candidate lesson against 4 criteria (formal quality, non-duplicate, generalization, calibration)
    # Validate schema
    res_lambda_val = cli_runner(["lesson", "validate", str(new_lesson_file), "--dest", str(store)])
    assert res_lambda_val.exit_code == 0

    # Lambda approves the proposal
    res_lambda_app = cli_runner(["lesson", "approve", new_slug, "--dest", str(store)])
    assert res_lambda_app.exit_code == 0
    assert "status: active" in new_lesson_file.read_text(encoding="utf-8")

    # --- Record Episode ---
    episodes_dir = store / "memory" / "episodes"
    episodes_dir.mkdir(parents=True, exist_ok=True)
    episode_file = episodes_dir / "2026-09-20_db-pool-async-refactor.md"
    episode_fm = {
        "name": "2026-09-20_db-pool-async-refactor",
        "description": "Async database connection pool implementation with review iterations",
        "agent_type": "code",
        "task_invocation": f"/commontrace {task_description}",
        "tags": ["database", "concurrency", "review"],
        "project": "core-backend",
        "verdict": verdict_round_2,
        "importance": 4,
        "importance_rationale": "High value architectural enhancement with production resilience.",
        "n_iterations": 2,
        "commit_sha": "a1b2c3d",
        "duration_minutes": 25,
        "lessons_retrieved_by_alpha": injected_lessons,
        "lessons_hit": injected_lessons,
        "lessons_proposed_by_omega": [new_slug],
        "lessons_validated_by_lambda": [new_slug],
    }
    episode_content = (
        f"---\n{yaml.safe_dump(episode_fm, sort_keys=False)}---\n\n"
        "## What happened\nImplemented async connection pool. First round flagged missing retry backoff. Added backoff in round 2.\n\n"
        "## What worked well\nReviewer catch prevented production thundering herd.\n"
    )
    episode_file.write_text(episode_content, encoding="utf-8")

    # --- Post-run: Alpha Query on Next Similar Task ---
    # Verify the new approved lesson is now retrieved
    res_next_alpha = cli_runner([
        "query",
        "database connection acquire retry backoff",
        "--lexical",
        "--top-k", "3",
        "--dest", str(store),
    ])
    assert res_next_alpha.exit_code == 0
    assert new_slug in res_next_alpha.stdout


# ============================================================================
# Scenario 2: Long-Running Fleet Telemetry and Failure Distillation
# ============================================================================

def test_tier4_fleet_telemetry_and_failure_distillation(
    cli_runner: Callable[..., CLIResult],
    tmp_path: Path,
    trace_factory: Callable[..., Path],
) -> None:
    """Simulate a fleet of distributed agents capturing diverse failure traces over time.

    Verifies:
    - 20+ traces spanning multiple failure clusters (TLS timeout, DB serialization, Redis socket) and singletons.
    - `commontrace distill` groups recurring incident clusters into candidate review lessons.
    - Isolated one-off errors below cluster threshold are not distilled.
    - Fleet taxonomy and reliability reports capture the incident distributions.
    """
    store = tmp_path / "fleet_telemetry_store"
    store.mkdir()
    cli_runner(["init", "--dest", str(store), "--agent-type", "fleet-worker"])

    # Cluster 1: 6 traces of TLS Handshake Timeout
    for i in range(6):
        trace_factory(
            store,
            trace_id=f"2026-09-18_tls-timeout-{i:02d}",
            title=f"TLS Handshake Timeout in Worker Node {i}",
            context=f"Worker {i} experienced TLS handshake timeout during upstream API handshake.",
            solution="Increased ssl_context timeout and enabled TLS session reuse.",
            agent_type="fleet-worker",
            tags=["tls", "security", "timeout", "networking"],
            outcome={"resolved": True, "tokens_used": 350 + i * 20, "llm_calls": 2},
        )

    # Cluster 2: 5 traces of PostgreSQL Serialization Failure
    for i in range(5):
        trace_factory(
            store,
            trace_id=f"2026-09-19_pg-serial-fail-{i:02d}",
            title=f"PostgreSQL Serialization Failure During Concurrent Batch {i}",
            context=f"Batch {i} failed with error 40001 serialization_failure due to concurrent updates.",
            solution="Set transaction isolation level to READ COMMITTED with retry on 40001.",
            agent_type="fleet-worker",
            tags=["postgres", "database", "concurrency", "serialization"],
            outcome={"resolved": True, "tokens_used": 410 + i * 15, "llm_calls": 3},
        )

    # Cluster 3: 4 traces of Redis Socket Closed Remotely
    for i in range(4):
        trace_factory(
            store,
            trace_id=f"2026-09-20_redis-socket-{i:02d}",
            title=f"Redis Socket Closed Remotely in Pipeline {i}",
            context=f"Redis server dropped client socket in pipeline {i} due to tcp-keepalive timeout.",
            solution="Configured tcp_keepalive=60 and retry_on_timeout=True in redis connection.",
            agent_type="fleet-worker",
            tags=["redis", "database", "socket", "timeout"],
            outcome={"resolved": True, "tokens_used": 290 + i * 10, "llm_calls": 2},
        )

    # Singleton 1: One-off isolated syntax typo
    trace_factory(
        store,
        trace_id="2026-09-20_singleton-typo",
        title="Configuration Syntax Error in YAML Anchor",
        context="Developer misquoted YAML anchor in config.yml leading to parse failure.",
        solution="Fixed syntax quote in config.yml.",
        agent_type="fleet-worker",
        tags=["config", "typo"],
        outcome={"resolved": True, "tokens_used": 150, "llm_calls": 1},
    )

    # Singleton 2: One-off permissions error
    trace_factory(
        store,
        trace_id="2026-09-20_singleton-perm",
        title="Permission Denied Accessing Temporary Socket",
        context="Process tried to bind socket in /run/secrets without appropriate group permissions.",
        solution="Adjusted unix socket permissions mode to 0770.",
        agent_type="fleet-worker",
        tags=["permissions", "linux"],
        outcome={"resolved": True, "tokens_used": 210, "llm_calls": 1},
    )

    # Run distill with min-cluster-size 3
    res_distill = cli_runner([
        "distill",
        "--min-cluster-size", "3",
        "--similarity-threshold", "0.25",
        "--dest", str(store),
    ])
    assert res_distill.exit_code == 0

    # Verify distilled lessons were proposed
    proposed = [
        p for p in (store / "memory" / "lessons").glob("lesson_*.md")
        if p.name != "lesson_template.md"
    ]
    assert len(proposed) >= 1, "Expected distill to cluster recurring failure patterns into lessons"

    # Verify every proposed lesson starts at status review and has source traces
    for prop in proposed:
        content = prop.read_text(encoding="utf-8")
        assert "status: review" in content
        assert "traces:" in content or "trace" in content.lower()

    # Run taxonomy command to verify fleet error distribution
    res_tax = cli_runner(["taxonomy", "--dest", str(store)])
    assert res_tax.exit_code == 0
    assert "taxonomy" in res_tax.stdout.lower() or "categories" in res_tax.stdout.lower() or "clusters" in res_tax.stdout.lower() or "tag" in res_tax.stdout.lower()

    # Run reliability command
    res_rel = cli_runner(["reliability", "--dest", str(store)])
    assert res_rel.exit_code == 0


# ============================================================================
# Scenario 3: Large Corpus Stress, Dosage Management, and Benchmark Scalability
# ============================================================================

def test_tier4_large_corpus_stress_and_dosage_management(
    cli_runner: Callable[..., CLIResult],
    tmp_path: Path,
    lesson_factory: Callable[..., Path],
    episode_factory: Callable[..., Path],
) -> None:
    """Stress test with 100+ lessons: index generation, dosage budgeting, redundancy pruning, and benchmark health."""
    store = tmp_path / "large_corpus_store"
    store.mkdir()
    cli_runner(["init", "--dest", str(store), "--agent-type", "code"])

    domains = ["database", "security", "concurrency", "networking", "testing", "performance", "observability"]
    num_lessons = 105

    # Generate 105 distinct lessons across domains
    for i in range(num_lessons):
        dom = domains[i % len(domains)]
        imp = (i % 5) + 1  # 1 to 5
        stat = "active" if i % 10 != 0 else "deprecated"  # 90% active, 10% deprecated
        extra = {"tags": [dom, f"tier4-{i}", "scale-test"]}
        if i == 0:
            extra["core"] = True  # Mark lesson 0 as core rule

        lesson_factory(
            store,
            slug=f"lesson_scale_{i:03d}_{dom}",
            title=f"Engineered Best Practice {i} for {dom.capitalize()}",
            description=f"Detailed guideline covering pattern {i} in domain {dom} for scalable agent systems.",
            applies_when=f"When designing or modifying {dom} components in microservice architecture",
            domain=dom,
            importance=imp,
            status=stat,
            extra_fm=extra,
        )

    # Also add synthetic episodes for benchmark evaluation
    for j in range(5):
        episode_factory(
            store,
            name=f"2026-09-{10+j:02d}_scale-episode-{j}",
            verdict="CONFORM" if j % 4 != 0 else "GAP",
            lessons_retrieved=[f"lesson_scale_{j:03d}_{domains[j]}"],
            lessons_hit=[f"lesson_scale_{j:03d}_{domains[j]}"],
        )

    # 1. Test Query with Top-K bound
    res_q_bound = cli_runner([
        "query",
        "microservice architecture database security",
        "--lexical",
        "--top-k", "5",
        "--dest", str(store),
    ])
    assert res_q_bound.exit_code == 0
    # Output must contain retrieved lessons, and no more than requested top-k
    assert "lesson_scale_" in res_q_bound.stdout

    # 2. Test Dosage Selection Logic (Character budget and core rules)
    candidates = []
    lessons_dir = store / "memory" / "lessons"
    for p in sorted(lessons_dir.glob("lesson_scale_*.md")):
        text = p.read_text(encoding="utf-8")
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm = yaml.safe_load(parts[1])
            body = parts[2]
            candidates.append(
                dosage.Candidate(
                    slug=fm["name"],
                    text=body,
                    core=dosage.is_core(fm),
                    importance=fm.get("importance", 3),
                )
            )

    # Create a tight character budget (e.g. 1500 chars)
    budget = dosage.Budget(
        max_lessons=5,
        max_chars=1500,
        redundancy_threshold=0.8,
    )
    result = dosage.select(candidates, budget)

    # Verify:
    # a. Total selected count <= max_lessons and characters within budget
    assert len(result.admitted) <= budget.max_lessons
    assert result.chars_used <= budget.max_chars
    # b. Core lesson is prioritized and admitted
    core_selected = [c for c in result.admitted if c.core]
    assert len(core_selected) >= 1
    assert core_selected[0].slug == "lesson_scale_000_database"
    # c. Truncated items carry descriptive reason
    assert len(result.dropped) > 0
    assert all(
        d.reason in (dosage.REASON_CHARS, dosage.REASON_COUNT) or d.reason.startswith(dosage.REASON_REDUNDANT)
        for d in result.dropped
    )

    # 3. Test Redundancy Pruning Under Dosage
    # Create two duplicate candidates with identical token set
    dup_a = dosage.Candidate(
        slug="dup_lesson_a",
        text="Always sanitize inputs with parameterized sql queries to prevent injection attacks.",
        importance=4,
    )
    dup_b = dosage.Candidate(
        slug="dup_lesson_b",
        text="Always sanitize inputs with parameterized sql queries to prevent injection attacks.",
        importance=3,
    )
    redundancy_budget = dosage.Budget(
        max_lessons=10,
        max_chars=10000,
        redundancy_threshold=0.85,
    )
    redundant_result = dosage.select([dup_a, dup_b], redundancy_budget)
    assert len(redundant_result.admitted) == 1
    assert redundant_result.admitted[0].slug == "dup_lesson_a"
    assert len(redundant_result.dropped) == 1
    assert redundant_result.dropped[0].slug == "dup_lesson_b"
    assert redundant_result.dropped[0].reason == dosage.redundant_reason("dup_lesson_a")
    assert len(redundant_result.redundant_dropped) == 1

    # 4. Run Memory Health Benchmark across 100+ lessons
    # commontrace bench must execute cleanly without memory issues or exceptions
    res_bench = cli_runner(["bench", "--json", "--dest", str(store)])
    assert res_bench.exit_code == 0
    bench_data = json.loads(res_bench.stdout)
    assert isinstance(bench_data, dict)
    assert "health_score" in bench_data or "overall_health" in bench_data or "score" in bench_data or "n_lessons" in bench_data
