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


def test_fact_bitemporal_as_of_superseded(store):
    """Verify that as_of queries reconstruct superseded facts at past points in time."""
    # 1. Create Fact 1 valid from 2026-01-01
    f1, _ = hierarchical.add_fact(
        store,
        "Use PostgreSQL 15 for microservices storage.",
        category="architecture",
        scopes=["db"],
        valid_from="2026-01-01T00:00:00Z",
    )

    # 2. At 2026-06-01, supersede Fact 1 with Fact 2 (PostgreSQL 16)
    old, new = hierarchical.supersede_fact(
        store,
        f1.id,
        "Use PostgreSQL 16 for microservices storage.",
        as_of="2026-06-01T00:00:00Z",
    )
    assert old.status == "superseded"
    assert new.status == "active"

    # Current listing (as_of=None) should return only the active new fact
    curr = hierarchical.list_facts(store, status="active")
    assert len(curr) == 1
    assert curr[0].id == new.id

    # Historical query at 2026-03-01 (when Fact 1 was valid) MUST return Fact 1,
    # despite its current status being "superseded"!
    past_facts = hierarchical.list_facts(store, as_of="2026-03-01T00:00:00Z")
    assert len(past_facts) == 1
    assert past_facts[0].id == old.id
    assert past_facts[0].statement == "Use PostgreSQL 15 for microservices storage."

    # Historical query prior to Fact 1's creation MUST return 0 facts
    prior_facts = hierarchical.list_facts(store, as_of="2025-12-01T00:00:00Z")
    assert len(prior_facts) == 0

    # Historical query after supersession (e.g., 2026-09-01) MUST return only Fact 2
    post_facts = hierarchical.list_facts(store, as_of="2026-09-01T00:00:00Z")
    assert len(post_facts) == 1
    assert post_facts[0].id == new.id
    assert post_facts[0].statement == "Use PostgreSQL 16 for microservices storage."


def test_fact_bitemporal_as_of_deleted(store):
    """Verify that as_of queries reconstruct soft-deleted facts at past points in time."""
    # Create fact valid from 2026-02-01
    f1, _ = hierarchical.add_fact(
        store,
        "Temporary worker thread pool ceiling is 32.",
        category="constraint",
        valid_from="2026-02-01T00:00:00Z",
    )

    # Soft delete the fact
    ok = hierarchical.delete_fact(store, f1.id)
    assert ok is True

    # Current listing has 0 active facts
    assert len(hierarchical.list_facts(store, status="active")) == 0

    # Historical query while fact was alive MUST return the fact
    past = hierarchical.list_facts(store, as_of="2026-02-15T00:00:00Z")
    assert len(past) == 1
    assert past[0].id == f1.id
    assert past[0].statement == "Temporary worker thread pool ceiling is 32."

    # Historical query before creation returns 0
    assert len(hierarchical.list_facts(store, as_of="2026-01-01T00:00:00Z")) == 0


def test_fact_bitemporal_search_as_of(store):
    """Verify search_facts accurately retrieves past fact versions using as_of."""
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

    # Search in the past returns the old fact
    past_search = hierarchical.search_facts(store, "redis replica count", as_of="2026-02-01T00:00:00Z")
    assert len(past_search) == 1
    assert past_search[0][0].id == old.id
    assert "3 nodes" in past_search[0][0].statement

    # Search currently returns the new fact
    curr_search = hierarchical.search_facts(store, "redis replica count")
    assert len(curr_search) == 1
    assert curr_search[0][0].id == new.id
    assert "5 nodes" in curr_search[0][0].statement


def test_fact_bitemporal_exact_boundaries(store):
    """Verify bitemporal interval semantics: valid_from <= as_of < valid_until."""
    # Fact explicitly bounded
    hierarchical.add_fact(
        store,
        "Staging cluster IP is 10.0.0.42",
        category="environment",
        valid_from="2026-04-01T12:00:00Z",
        valid_until="2026-04-30T12:00:00Z",
    )

    # 1. Exact start instant: valid_from <= as_of (inclusive)
    res_start = hierarchical.list_facts(store, as_of="2026-04-01T12:00:00Z")
    assert len(res_start) == 1

    # 2. One second before start instant: not valid
    res_before = hierarchical.list_facts(store, as_of="2026-04-01T11:59:59Z")
    assert len(res_before) == 0

    # 3. Exact end instant: as_of < valid_until (exclusive)
    res_end = hierarchical.list_facts(store, as_of="2026-04-30T12:00:00Z")
    assert len(res_end) == 0

    # 4. One second before end instant: valid
    res_just_before = hierarchical.list_facts(store, as_of="2026-04-30T11:59:59Z")
    assert len(res_just_before) == 1


def test_fact_cli_as_of(store):
    """Verify CLI fact list and fact search with --as-of flag."""
    hierarchical.add_fact(
        store,
        "Nginx keepalive timeout is 65s",
        category="constraint",
        valid_from="2026-01-01T00:00:00Z",
    )
    facts = hierarchical.load_facts(store)
    f_id = list(facts.keys())[0]

    hierarchical.supersede_fact(store, f_id, "Nginx keepalive timeout is 120s")

    # CLI fact list --as-of
    res = cli("fact", "list", "--as-of", "2026-01-15T00:00:00Z", "--dest", store)
    assert res.returncode == 0
    assert "65s" in res.stdout

    # CLI fact search --as-of
    res_srch = cli("fact", "search", "keepalive", "--as-of", "2026-01-15T00:00:00Z", "--dest", store)
    assert res_srch.returncode == 0
    assert "65s" in res_srch.stdout


def test_atomic_fact_schema_compliance(store):
    """Verify that AtomicFact instances strictly validate against atomic_fact.schema.json."""
    from commontrace import validate

    schema = validate.load_schema("atomic_fact.schema.json")
    validate.assert_supported_schema(schema)

    # 1. Added fact
    f1, _ = hierarchical.add_fact(store, "Kafka partition count is 12", category="architecture", scopes=["backend"])
    errs = validate.validate(f1.to_dict(), schema)
    assert errs == []

    # 2. Superseded fact & new replacement fact
    old, new = hierarchical.supersede_fact(store, f1.id, "Kafka partition count is 24")
    assert validate.validate(old.to_dict(), schema) == []
    assert validate.validate(new.to_dict(), schema) == []

    # 3. Soft-deleted fact
    hierarchical.delete_fact(store, new.id)
    facts = hierarchical.load_facts(store)
    deleted_fact = facts[new.id]
    assert validate.validate(deleted_fact.to_dict(), schema) == []

    # 4. Schema rejection on invalid data
    bad_fact = f1.to_dict()
    bad_fact["category"] = "unsupported_cat"
    assert len(validate.validate(bad_fact, schema)) >= 1

    bad_fact2 = f1.to_dict()
    bad_fact2["confidence"] = 1.5
    assert len(validate.validate(bad_fact2, schema)) >= 1


def test_entity_extraction_basic(store):
    """Test basic entity extraction from text."""
    # Test with spaCy unavailable (graceful degradation)
    entities = hierarchical.extract_entities("John visited New York last week.")
    # Should return empty list if spaCy not available, or entities if available
    assert isinstance(entities, list)


def test_entity_extraction_batch(store):
    """Test batch entity extraction."""
    texts = [
        "Alice works at Google.",
        "Bob visited Paris.",
        "The system uses PostgreSQL database.",
    ]
    results = hierarchical.extract_entities_batch(texts)
    assert len(results) == len(texts)
    for result in results:
        assert isinstance(result, list)


def test_entity_store_add_and_reinforce(store):
    """Test adding entities to the store with deduplication."""
    # Add first entity
    e1 = hierarchical.add_entity(store, "PostgreSQL", "PROPER", source_trace_id="trace-1")
    assert e1.text == "PostgreSQL"
    assert e1.entity_type == "PROPER"
    assert "trace-1" in e1.source_traces

    # Add same entity (should reinforce, not duplicate)
    e2 = hierarchical.add_entity(store, "PostgreSQL", "PROPER", source_trace_id="trace-2")
    assert e2.id == e1.id  # Same entity
    assert "trace-2" in e2.source_traces
    assert "trace-1" in e2.source_traces  # Original trace preserved

    # Add different entity
    e3 = hierarchical.add_entity(store, "MySQL", "PROPER", source_trace_id="trace-1")
    assert e3.id != e1.id


def test_entity_store_batch(store):
    """Test batch entity addition."""
    entities = [
        ("PROPER", "Redis"),
        ("PROPER", "Kafka"),
        ("TOPIC", "machine learning"),
    ]
    results = hierarchical.add_entities_batch(store, entities, source_trace_id="trace-batch")
    assert len(results) == 3

    # Verify deduplication in batch
    entities2 = [
        ("PROPER", "Redis"),  # Duplicate
        ("PROPER", "Elasticsearch"),
    ]
    results2 = hierarchical.add_entities_batch(store, entities2, source_trace_id="trace-batch-2")
    assert len(results2) == 2

    # Redis should have both traces
    redis = next((e for e in results2 if e.text == "Redis"), None)
    assert redis is not None
    assert "trace-batch" in redis.source_traces
    assert "trace-batch-2" in redis.source_traces


def test_entity_list_and_filter(store):
    """Test listing entities with type filter."""
    hierarchical.add_entity(store, "PostgreSQL", "PROPER")
    hierarchical.add_entity(store, "Redis", "PROPER")
    hierarchical.add_entity(store, "machine learning", "TOPIC")

    all_entities = hierarchical.list_entities(store)
    assert len(all_entities) >= 3

    proper_entities = hierarchical.list_entities(store, entity_type="PROPER")
    assert len(proper_entities) >= 2
    for e in proper_entities:
        assert e.entity_type == "PROPER"

    topic_entities = hierarchical.list_entities(store, entity_type="TOPIC")
    assert len(topic_entities) >= 1
    for e in topic_entities:
        assert e.entity_type == "TOPIC"


def test_extract_and_store_entities(store):
    """Test the convenience function for extraction and storage."""
    text = "Alice works at Google on machine learning projects."
    entities = hierarchical.extract_and_store_entities(store, text, source_trace_id="trace-123")

    # Should return list of Entity objects
    assert isinstance(entities, list)
    # If spaCy is available, should have entities; if not, empty list is OK
    if entities:
        for e in entities:
            assert isinstance(e, hierarchical.Entity)
            assert "trace-123" in e.source_traces


def test_entity_normalization(store):
    """Test that entity text normalization works for deduplication."""
    # Add with different casing/spacing
    e1 = hierarchical.add_entity(store, "PostgreSQL", "PROPER")
    e2 = hierarchical.add_entity(store, "postgresql", "PROPER")
    e3 = hierarchical.add_entity(store, "  PostgreSQL  ", "PROPER")

    # All should map to the same entity due to normalization
    assert e1.id == e2.id == e3.id


def test_entity_boost_weight_constant():
    """Verify ENTITY_BOOST_WEIGHT constant is defined."""
    assert hasattr(hierarchical, "ENTITY_BOOST_WEIGHT")
    assert hierarchical.ENTITY_BOOST_WEIGHT == 0.5


def test_build_entity_index_from_store(store):
    """Test building entity index from store for retrieval integration."""
    # Add some entities with source traces
    hierarchical.add_entity(store, "PostgreSQL", "PROPER", source_trace_id="lesson-001.md")
    hierarchical.add_entity(store, "Redis", "PROPER", source_trace_id="lesson-002.md")
    hierarchical.add_entity(store, "Kafka", "PROPER", source_trace_id="lesson-001.md")

    # Mock lessons list
    lessons = [
        ("lesson-001.md", {"name": "postgres-config"}),
        ("lesson-002.md", {"name": "redis-cache"}),
        ("lesson-003.md", {"name": "nginx-setup"}),
    ]

    # Build index
    index = hierarchical.build_entity_index_from_store(store, lessons)

    # Should have entity tokens mapped to lesson indices
    assert isinstance(index, dict)

    # PostgreSQL and Kafka should map to lesson-001 (index 0)
    # Redis should map to lesson-002 (index 1)
    # lesson-003 should have no entities

    # Check that tokens exist
    tokens = list(index.keys())
    assert len(tokens) > 0

    # Verify structure: each token maps to a list of indices
    for token, doc_ids in index.items():
        assert isinstance(token, str)
        assert isinstance(doc_ids, list)
        for doc_id in doc_ids:
            assert isinstance(doc_id, int)
            assert 0 <= doc_id < len(lessons)


def test_deduplicate_entities_batch(store):
    """Test global batch deduplication across entity store."""
    hierarchical.add_entity(store, "PostgreSQL", "PROPER", source_trace_id="trace-1")
    hierarchical.add_entity(store, "Redis", "PROPER", source_trace_id="trace-2")
    # Manually inject duplicate with different ID but same normalized text
    entities = hierarchical.load_entities(store)
    dup = hierarchical.Entity(
        id="entity-dup-999",
        text="postgresql",
        entity_type="PROPER",
        normalized_text="postgresql",
        source_traces=["trace-3"],
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
    )
    entities[dup.id] = dup
    hierarchical.save_entities(store, entities)
    assert len(hierarchical.load_entities(store)) == 3

    # Run deduplication
    report = hierarchical.deduplicate_entities(store)
    assert report["initial_count"] == 3
    assert report["final_count"] == 2
    assert report["duplicates_removed"] == 1

    remaining = hierarchical.load_entities(store)
    pg = next(e for e in remaining.values() if e.normalized_text == "postgresql")
    assert "trace-1" in pg.source_traces
    assert "trace-3" in pg.source_traces


def test_entity_extraction_fallback_without_spacy():
    """Test that entity extraction fallback extracts identifiers, quoted text, and proper nouns."""
    text = (
        'The "PaymentProcessor" uses api_key and auth.tokens.validate_session '
        'to communicate with Stripe API.'
    )
    entities = hierarchical.extract_entities(text)
    assert isinstance(entities, list)
    assert len(entities) > 0

    types = {t for t, _ in entities}
    texts = {val for _, val in entities}

    # Should detect quoted text
    assert "PaymentProcessor" in texts or "QUOTED" in types
    # Should detect technical identifiers
    assert any("api_key" in t or "auth.tokens" in t for t in texts)

