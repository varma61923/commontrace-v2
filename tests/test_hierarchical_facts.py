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


def test_fact_bitemporal_as_of_superseded(store):
    f1, _ = hierarchical.add_fact(
        store,
        "Use PostgreSQL 15 for microservices storage.",
        category="architecture",
        scopes=["db"],
        valid_from="2026-01-01T00:00:00Z",
    )

    old, new = hierarchical.supersede_fact(
        store,
        f1.id,
        "Use PostgreSQL 16 for microservices storage.",
        as_of="2026-06-01T00:00:00Z",
    )
    assert old.status == "superseded"
    assert new.status == "active"

    curr = hierarchical.list_facts(store, status="active")
    assert len(curr) == 1
    assert curr[0].id == new.id

    past_facts = hierarchical.list_facts(store, as_of="2026-03-01T00:00:00Z")
    assert len(past_facts) == 1
    assert past_facts[0].id == old.id
    assert past_facts[0].statement == "Use PostgreSQL 15 for microservices storage."

    prior_facts = hierarchical.list_facts(store, as_of="2025-12-01T00:00:00Z")
    assert len(prior_facts) == 0

    post_facts = hierarchical.list_facts(store, as_of="2026-09-01T00:00:00Z")
    assert len(post_facts) == 1
    assert post_facts[0].id == new.id
    assert post_facts[0].statement == "Use PostgreSQL 16 for microservices storage."


def test_fact_bitemporal_as_of_deleted(store):
    f1, _ = hierarchical.add_fact(
        store,
        "Temporary worker thread pool ceiling is 32.",
        category="constraint",
        valid_from="2026-02-01T00:00:00Z",
    )

    ok = hierarchical.delete_fact(store, f1.id)
    assert ok is True

    assert len(hierarchical.list_facts(store, status="active")) == 0

    past = hierarchical.list_facts(store, as_of="2026-02-15T00:00:00Z")
    assert len(past) == 1
    assert past[0].id == f1.id
    assert past[0].statement == "Temporary worker thread pool ceiling is 32."

    assert len(hierarchical.list_facts(store, as_of="2026-01-01T00:00:00Z")) == 0


def test_fact_bitemporal_search_as_of(store):
    f1, _ = hierarchical.add_fact(
        store,
        "Redis cluster replica count is 3 nodes.",
        category="architecture",
        valid_from="2026-01-01T00:00:00Z",
    )
    old, new = hierarchical.supersede_fact(
        store,
        f1.id,
        "Redis cluster replica count is 5 nodes.",
    )

    past_search = hierarchical.search_facts(store, "redis replica count", as_of="2026-02-01T00:00:00Z")
    assert len(past_search) == 1
    assert past_search[0][0].id == old.id
    assert "3 nodes" in past_search[0][0].statement

    curr_search = hierarchical.search_facts(store, "redis replica count")
    assert len(curr_search) == 1
    assert curr_search[0][0].id == new.id
    assert "5 nodes" in curr_search[0][0].statement


def test_fact_bitemporal_exact_boundaries(store):
    hierarchical.add_fact(
        store,
        "Staging cluster IP is 10.0.0.42",
        category="environment",
        valid_from="2026-04-01T12:00:00Z",
        valid_until="2026-04-30T12:00:00Z",
    )

    res_start = hierarchical.list_facts(store, as_of="2026-04-01T12:00:00Z")
    assert len(res_start) == 1

    res_before = hierarchical.list_facts(store, as_of="2026-04-01T11:59:59Z")
    assert len(res_before) == 0

    res_end = hierarchical.list_facts(store, as_of="2026-04-30T12:00:00Z")
    assert len(res_end) == 0

    res_just_before = hierarchical.list_facts(store, as_of="2026-04-30T11:59:59Z")
    assert len(res_just_before) == 1


def test_fact_cli_as_of(store):
    hierarchical.add_fact(
        store,
        "Nginx keepalive timeout is 65s",
        category="constraint",
        valid_from="2026-01-01T00:00:00Z",
    )
    facts = hierarchical.load_facts(store)
    f_id = list(facts.keys())[0]

    hierarchical.supersede_fact(store, f_id, "Nginx keepalive timeout is 120s")

    res = cli("fact", "list", "--as-of", "2026-01-15T00:00:00Z", "--dest", store)
    assert res.returncode == 0
    assert "65s" in res.stdout

    res_srch = cli("fact", "search", "keepalive", "--as-of", "2026-01-15T00:00:00Z", "--dest", store)
    assert res_srch.returncode == 0
    assert "65s" in res_srch.stdout


def test_atomic_fact_schema_compliance(store):
    from commontrace import validate

    schema = validate.load_schema("atomic_fact.schema.json")
    validate.assert_supported_schema(schema)

    f1, _ = hierarchical.add_fact(store, "Kafka partition count is 12", category="architecture", scopes=["backend"])
    errs = validate.validate(f1.to_dict(), schema)
    assert errs == []

    old, new = hierarchical.supersede_fact(store, f1.id, "Kafka partition count is 24")
    assert validate.validate(old.to_dict(), schema) == []
    assert validate.validate(new.to_dict(), schema) == []

    hierarchical.delete_fact(store, new.id)
    facts = hierarchical.load_facts(store)
    deleted_fact = facts[new.id]
    assert validate.validate(deleted_fact.to_dict(), schema) == []

    bad_fact = f1.to_dict()
    bad_fact["category"] = "unsupported_cat"
    assert len(validate.validate(bad_fact, schema)) >= 1

    bad_fact2 = f1.to_dict()
    bad_fact2["confidence"] = 1.5
    assert len(validate.validate(bad_fact2, schema)) >= 1


def test_entity_extraction_basic(store):
    entities = hierarchical.extract_entities("John visited New York last week.")
    assert isinstance(entities, list)


def test_entity_extraction_fallback_without_spacy():
    text = (
        'The "PaymentProcessor" uses api_key and auth.tokens.validate_session '
        'to communicate with Stripe API.'
    )
    entities = hierarchical.extract_entities(text)
    assert isinstance(entities, list)
    assert len(entities) > 0

    types = {t for t, _ in entities}
    texts = {val for _, val in entities}

    assert "PaymentProcessor" in texts or "QUOTED" in types
    assert any("api_key" in t or "auth.tokens" in t for t in texts)

