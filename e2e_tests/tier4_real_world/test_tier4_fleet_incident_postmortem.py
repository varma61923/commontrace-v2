from __future__ import annotations

import os

from commontrace import frontmatter
from e2e_tests.harness.cli_runner import run_cli


def test_t4_fleet_incident_postmortem_to_governance_loop(isolated_store: str):
    """E2E-T4-RW-1: Fleet encounters cascading outage, captures trace, dreams, and establishes governed lesson."""
    # Step 1: Fleet agent encounters an outage in production and captures an episodic trace
    res_cap = run_cli(
        "capture",
        "--title", "Cascading outage due to PostgreSQL client connection starvation",
        "--context", (
            "During flash sale traffic spike, microservices opened 500 connections without pooling, "
            "exhausting PostgreSQL max_connections and dropping all incoming customer requests."
        ),
        "--solution", (
            "Deploy PgBouncer transaction-level connection pooler in front of primary database "
            "and enforce max_connections per service pod to 10."
        ),
        "--agent-type", "coding",
        dest=isolated_store,
    )
    res_cap.assert_success()

    # Step 2: Operator records an architectural constraint fact
    res_fact = run_cli(
        "fact", "add",
        "All production database clients must route queries through PgBouncer connection pooler.",
        "--category", "constraint",
        "--scope", "database",
        "--confidence", "0.95",
        dest=isolated_store,
    )
    res_fact.assert_success()

    # Step 3: Run dynamic dreaming pass to synthesize knowledge
    res_dream = run_cli("dream", dest=isolated_store)
    res_dream.assert_success()
    assert "profile synthesized" in res_dream.stdout

    # Step 4: Add governed lesson
    lessons_dir = os.path.join(isolated_store, "memory", "lessons")
    os.makedirs(lessons_dir, exist_ok=True)
    lesson_path = os.path.join(lessons_dir, "lesson_pgbouncer_pooling.md")
    frontmatter.write(
        lesson_path,
        {
            "name": "lesson_pgbouncer_pooling",
            "description": "Prevent connection starvation during traffic spikes using PgBouncer",
            "applies_when": "High database connection traffic or connection exhaustion",
            "tags": ["postgres", "database", "pgbouncer", "connection-pool"],
            "status": "active",
        },
        "Always use PgBouncer in transaction mode to pool connections under high traffic.",
    )

    # Step 5: A second fleet agent starts a new task and queries CommonTrace for guidance
    res_query = run_cli("query", "configure postgresql connection traffic spike", "--lexical", dest=isolated_store)
    res_query.assert_success()
    assert ("pgbouncer" in res_query.stdout) or ("PostgreSQL" in res_query.stdout)
