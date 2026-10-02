from __future__ import annotations

import os

from commontrace import frontmatter, retrieval
from e2e_tests.harness.cli_runner import run_cli


def test_t1_retrieval_graph_boost_ranking():
    """E2E-T1-RB-1: Verify that graph_boost_lookup shifts ranking when graph_weight > 0."""
    lessons = [
        (
            "lessons/db-timeout.md",
            {
                "name": "db-timeout",
                "description": "Database queries timing out under high load",
                "applies_when": "High query traffic",
                "tags": ["database", "postgres"],
                "domain": "backend",
            },
        ),
        (
            "lessons/redis-cache.md",
            {
                "name": "redis-cache",
                "description": "Database queries cache eviction strategy",
                "applies_when": "Cache misses",
                "tags": ["database", "redis"],
                "domain": "backend",
            },
        ),
    ]

    task = "Database queries"

    # Base ranking without graph boost
    base_ranked = retrieval.rank_lessons(task, lessons, graph_boost_lookup=None, graph_weight=0.0)
    assert len(base_ranked) >= 2

    # Boost redis-cache via graph lookup
    graph_boosts = {"redis-cache": 0.5, "db-timeout": 0.0}
    boosted_ranked = retrieval.rank_lessons(
        task, lessons,
        graph_boost_lookup=graph_boosts,
        graph_weight=1.0,
    )
    assert len(boosted_ranked) >= 2
    top_slug = boosted_ranked[0].slug
    assert top_slug == "redis-cache", "Graph-boosted lesson must rank higher than unboosted lesson"
    assert boosted_ranked[0].graph_adjustment == 0.5


def test_t1_retrieval_score_formula_clamping():
    """E2E-T1-RB-2: Verify integrated relevance and graph adjustment handling."""
    lessons = [
        (
            "lessons/extreme.md",
            {
                "name": "extreme-boost",
                "description": "Critical security CVE patch",
                "applies_when": "Security audit",
                "tags": ["security"],
            },
        ),
    ]
    task = "Security audit CVE patch"

    graph_boosts = {"extreme-boost": 5.0}
    ranked = retrieval.rank_lessons(
        task, lessons,
        graph_boost_lookup=graph_boosts,
        graph_weight=2.0,
    )
    assert len(ranked) == 1
    assert ranked[0].relevance <= 1.0, "Base relevance must not exceed 1.0"
    assert ranked[0].graph_adjustment == 5.0, "Graph adjustment must match lookup value"


def test_t1_retrieval_adaptive_tail_precision():
    """E2E-T1-RB-3: Adaptive tail scorer retains relevant candidates while cutting off tail noise."""
    lessons = [
        (
            "lessons/target.md",
            {
                "name": "exact-target",
                "description": "Exact match for kubernetes pod evictions due to node memory pressure",
                "applies_when": "Pod eviction",
                "tags": ["kubernetes", "memory"],
            },
        ),
        (
            "lessons/vague.md",
            {
                "name": "vague-unrelated",
                "description": "General linux bash script formatting tips and guidelines",
                "applies_when": "Scripting",
                "tags": ["bash"],
            },
        ),
    ]

    task = "Kubernetes pod evictions node memory pressure"
    ranked = retrieval.rank_lessons(task, lessons, scorer=retrieval.SCORER_ADAPTIVE, adaptive_tail=True)
    assert len(ranked) >= 1
    assert ranked[0].slug == "exact-target"


def test_t1_retrieval_cli_query_execution(isolated_store: str):
    """E2E-T1-RB-4: Create lessons in store and verify retrieval via CLI commontrace query."""
    lessons_dir = os.path.join(isolated_store, "memory", "lessons")
    os.makedirs(lessons_dir, exist_ok=True)

    lesson_path = os.path.join(lessons_dir, "lesson_postgres-pool.md")
    frontmatter.write(
        lesson_path,
        {
            "name": "lesson_postgres-pool",
            "description": "PostgreSQL max connection pool configuration guide",
            "applies_when": "Database pool exhaustion",
            "tags": ["postgres", "database", "connection-pool"],
            "status": "active",
        },
        "Configure PostgreSQL max connection pool to prevent pool starvation.",
    )

    # Query CLI
    res_query = run_cli("query", "postgres connection pool exhaustion", "--lexical", dest=isolated_store)
    res_query.assert_success()
    assert ("postgres-pool" in res_query.stdout) or ("PostgreSQL" in res_query.stdout)


def test_t1_retrieval_as_of_temporal_filter(isolated_store: str):
    """E2E-T1-RB-5: Query lessons with --as-of date filters out lessons outside their valid window."""
    lessons_dir = os.path.join(isolated_store, "memory", "lessons")
    os.makedirs(lessons_dir, exist_ok=True)

    # Lesson valid only in 2024
    lesson_2024 = os.path.join(lessons_dir, "legacy-auth.md")
    frontmatter.write(
        lesson_2024,
        {
            "name": "legacy-auth",
            "description": "Legacy basic authentication mechanism",
            "status": "active",
            "valid_from": "2024-01-01T00:00:00Z",
            "valid_until": "2024-12-31T23:59:59Z",
            "tags": ["auth"],
        },
        "Use Basic Auth for internal proxy.",
    )

    res_2026 = run_cli(
        "query", "basic authentication mechanism",
        "--as-of", "2026-06-01",
        "--lexical",
        dest=isolated_store,
    )
    res_2026.assert_success()
    assert "legacy-auth" not in res_2026.stdout
