from __future__ import annotations

import os
import subprocess
import sys

import pytest

from commontrace import hierarchical

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "commontrace.cli", *argv],
        capture_output=True, text=True, cwd=REPO_ROOT, check=False,
    )


@pytest.fixture
def store(tmp_path):
    root = str(tmp_path / "fleet")
    assert cli("init", "--dest", root, "--agent-type", "coding").returncode == 0
    return root


def test_fact_add_and_reinforce(store):
    f1, act1 = hierarchical.add_fact(
        store, "Database connection timeout should be 5 seconds.",
        category="constraint", scopes=["backend", "db"], confidence=0.8,
    )
    assert act1 == "ADD"
    assert f1.confirmations == 1
    assert f1.confidence == 0.8
    assert "backend" in f1.scopes

    # Reinforce exact statement in same scope
    f2, act2 = hierarchical.add_fact(
        store, "Database connection timeout should be 5 seconds.",
        scopes=["db"],
    )
    assert act2 == "NOOP"
    assert f2.confirmations == 2
    assert f2.confidence > 0.8


def test_fact_supersede_and_temporal(store):
    f1, _ = hierarchical.add_fact(
        store, "Use Python 3.10 for microservices.",
        category="architecture",
    )
    old, new = hierarchical.supersede_fact(
        store, f1.id, "Use Python 3.12 for microservices.",
    )
    assert old.status == "superseded"
    assert old.valid_until is not None
    assert old.superseded_by == new.id
    assert new.status == "active"

    # Active listing only returns active
    active = hierarchical.list_facts(store, status="active")
    assert len(active) == 1
    assert active[0].id == new.id


def test_fact_search(store):
    hierarchical.add_fact(store, "Kafka consumer group max poll interval is 300000ms", category="constraint")
    hierarchical.add_fact(store, "Redis cache TTL is 3600 seconds", category="architecture")

    results = hierarchical.search_facts(store, "kafka consumer interval")
    assert len(results) >= 1
    top_fact, score = results[0]
    assert "Kafka" in top_fact.statement
    assert score > 0.4


def test_fact_cli(store):
    res = cli("fact", "add", "Postgres max connections is 100", "--category", "constraint", "--scope", "db", "--dest", store)
    assert res.returncode == 0
    assert "Added fact" in res.stdout

    res = cli("fact", "list", "--dest", store)
    assert res.returncode == 0
    assert "Postgres max connections" in res.stdout

    res = cli("fact", "search", "postgres connections", "--dest", store)
    assert res.returncode == 0
    assert "Postgres" in res.stdout
