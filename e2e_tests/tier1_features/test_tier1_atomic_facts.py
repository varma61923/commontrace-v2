from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from commontrace import hierarchical
from e2e_tests.harness.cli_runner import run_cli


def test_t1_fact_add_and_metadata(isolated_store: str):
    """E2E-T1-AF-1: Add an atomic proposition and verify metadata persistence in facts.jsonl."""
    res_add = run_cli(
        "fact", "add", "PostgreSQL database connection pool size must be 20.",
        "--category", "constraint",
        "--scope", "backend",
        "--confidence", "0.85",
        dest=isolated_store,
    )
    res_add.assert_success()
    assert "Added fact" in res_add.stdout

    # Verify JSONL file on disk
    facts_file = os.path.join(isolated_store, "memory", "facts", "facts.jsonl")
    assert os.path.exists(facts_file), "facts.jsonl must exist on disk"

    with open(facts_file, encoding="utf-8") as f:
        facts = [json.loads(line) for line in f if line.strip()]
    assert len(facts) >= 1
    f0 = facts[0]
    assert f0["category"] == "constraint"
    assert "backend" in f0["scopes"]
    assert f0["confidence"] == 0.85
    assert f0["status"] == "active"
    assert f0["confirmations"] == 1
    assert f0["valid_from"] is not None


def test_t1_fact_noop_reinforcement(isolated_store: str):
    """E2E-T1-AF-2: Adding identical proposition reinforces confirmation counter and increases confidence."""
    stmt = "Kafka consumer session timeout is 45000ms."
    res1 = run_cli("fact", "add", stmt, "--scope", "streaming", "--confidence", "0.7", dest=isolated_store)
    res1.assert_success()

    # Reinforce identical statement
    res2 = run_cli("fact", "add", stmt, "--scope", "streaming", dest=isolated_store)
    res2.assert_success()
    assert ("Reinforced" in res2.stdout) or ("Added fact" in res2.stdout) or ("NOOP" in res2.stdout)

    facts = hierarchical.list_facts(isolated_store, status="active")
    matching = [f for f in facts if stmt in f.statement]
    assert len(matching) == 1, "Should have exactly 1 deduplicated active fact"
    assert matching[0].confirmations >= 2
    assert matching[0].confidence > 0.7


def test_t1_fact_supersede_lifecycle(isolated_store: str):
    """E2E-T1-AF-3: Superseding a fact updates old status to superseded with valid_until and activates new fact."""
    f1, _ = hierarchical.add_fact(
        isolated_store,
        "Node.js runtime target is Node 18 LTS.",
        category="architecture",
        scopes=["frontend"],
    )
    assert f1.status == "active"
    assert f1.valid_until is None

    # Supersede via CLI
    res_super = run_cli(
        "fact", "supersede", f1.id,
        "Node.js runtime target is Node 20 LTS.",
        dest=isolated_store,
    )
    res_super.assert_success()

    # Verify old fact is superseded and has valid_until
    all_facts = hierarchical.list_facts(isolated_store, status="")
    old_fact = next(f for f in all_facts if f.id == f1.id)
    assert old_fact.status == "superseded"
    assert old_fact.valid_until is not None
    assert old_fact.superseded_by is not None

    # Verify new active fact
    new_fact = next(f for f in all_facts if f.id == old_fact.superseded_by)
    assert new_fact.status == "active"
    assert "Node 20" in new_fact.statement


def test_t1_fact_soft_delete(isolated_store: str):
    """E2E-T1-AF-4: Soft-deleting a fact marks status as deleted and records valid_until."""
    f1, _ = hierarchical.add_fact(
        isolated_store,
        "Temporary feature flag enable_beta_search is True.",
        category="general",
    )
    res_del = run_cli("fact", "delete", f1.id, dest=isolated_store)
    res_del.assert_success()

    # Active list should not include deleted fact
    active_facts = hierarchical.list_facts(isolated_store, status="active")
    assert not any(f.id == f1.id for f in active_facts)

    # Inactive list shows deleted status and expiration timestamp
    all_facts = hierarchical.list_facts(isolated_store, status="")
    deleted_fact = next(f for f in all_facts if f.id == f1.id)
    assert deleted_fact.status == "deleted"
    assert deleted_fact.valid_until is not None


def test_t1_fact_bitemporal_as_of_historical_query(isolated_store: str):
    """E2E-T1-AF-5: Historical as_of queries return the fact that was valid at that specific point in time."""
    t0 = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat()

    # Create a fact with explicit historical valid_from
    f_hist, _ = hierarchical.add_fact(
        isolated_store,
        "Redis cluster uses standalone master-replica.",
        category="architecture",
        valid_from=t0,
    )

    # Supersede at t1
    f_super, f_new = hierarchical.supersede_fact(
        isolated_store,
        f_hist.id,
        "Redis cluster uses Redis Sentinel failover.",
    )

    # Point-in-time check before supersession (at 2025-08-01) with status="" (bitemporal slice across statuses)
    as_of_2025 = "2025-08-01T00:00:00Z"
    facts_2025 = hierarchical.list_facts(isolated_store, status="", as_of=as_of_2025)
    hist_ids = {f.id for f in facts_2025}
    assert f_hist.id in hist_ids, "Original historical fact must be returned for historical as_of query"


def test_t1_fact_search_and_category_filter(isolated_store: str):
    """E2E-T1-AF-6: Search facts by keyword relevance and filter by category and scope."""
    hierarchical.add_fact(
        isolated_store,
        "Cassandra replication factor must be 3 across multi-region datacenters.",
        category="constraint",
        scopes=["infra", "database"],
        confidence=0.9,
    )
    hierarchical.add_fact(
        isolated_store,
        "User sessions expire after 30 minutes of inactivity.",
        category="preference",
        scopes=["auth"],
        confidence=0.8,
    )

    # CLI search by keyword
    res_search = run_cli("fact", "search", "cassandra replication datacenters", dest=isolated_store)
    res_search.assert_success()
    assert "Cassandra" in res_search.stdout

    # CLI list with category filter
    res_cat = run_cli("fact", "list", "--category", "constraint", dest=isolated_store)
    res_cat.assert_success()
    assert "Cassandra" in res_cat.stdout
    assert "User sessions" not in res_cat.stdout
