from __future__ import annotations

import os
import subprocess
import sys

import pytest

from commontrace import entity_store, hierarchical
from commontrace.conversation import Options, Store, recall
from commontrace.conversation import search as conv_search

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEXICAL = Options(embedder=None)


def cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "commontrace.cli", *argv],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


@pytest.fixture
def store(tmp_path):
    root = str(tmp_path / "fleet")
    assert cli("init", "--dest", root, "--agent-type", "coding").returncode == 0
    return root


def _snapshot(entities):
    return {name: (e["type"], list(e["memory_ids"]), e["count"]) for name, e in entities.items()}


def test_extraction_kinds():
    pairs = entity_store.extract_entities('Alice met Bob in New York near the "PaymentProcessor" service.')
    texts = {value for _, value in pairs}
    assert "Alice" in texts
    assert "Bob" in texts
    assert "New York" in texts
    assert "PaymentProcessor" in texts
    by_text = {value: etype for etype, value in pairs}
    assert by_text["PaymentProcessor"] in ("QUOTED", "IDENTIFIER")
    assert by_text["New York"] == "PROPER"


def test_extraction_identifiers():
    pairs = entity_store.extract_entities("Check auth.tokens.validate_session and api_key before deploy.")
    texts = {value for _, value in pairs}
    assert "auth.tokens.validate_session" in texts
    assert "api_key" in texts
    by_text = {value: etype for etype, value in pairs}
    assert by_text["auth.tokens.validate_session"] == "IDENTIFIER"
    assert by_text["api_key"] == "IDENTIFIER"


def test_extraction_deterministic_and_empty():
    text = 'Bob reviewed the "CheckoutFlow" in New York with auth.tokens.retry.'
    first = entity_store.extract_entities(text)
    assert entity_store.extract_entities(text) == first
    assert entity_store.extract_entities("") == []
    assert entity_store.extract_entities("   ") == []
    assert entity_store.normalize_entity("  New   York ") == "new york"


def test_link_on_add_and_exact_lookup(store):
    fact, action = hierarchical.add_fact(store, "Postgres max connections is 100 for CheckoutFlow.")
    assert action == "ADD"
    entities = entity_store.load_entities(store)
    assert "postgres" in entities
    entry = entities["postgres"]
    assert entry["memory_ids"] == [fact.id]
    assert entry["count"] == 1
    assert entry["type"] in ("PROPER", "IDENTIFIER", "QUOTED")
    assert entity_store.confirmed_query_entities(store, "How do we tune Postgres?") == {"postgres"}
    assert entity_store.confirmed_query_entities(store, "How do we tune Redis?") == set()


def test_link_idempotent_on_reinforce(store):
    fact, _ = hierarchical.add_fact(store, "Postgres max connections is 100 for CheckoutFlow.")
    hierarchical.add_fact(store, "Postgres max connections is 100 for CheckoutFlow.")
    entry = entity_store.load_entities(store)["postgres"]
    assert entry["memory_ids"] == [fact.id]
    assert entry["count"] == 1


def test_unlink_on_delete(store):
    f1, _ = hierarchical.add_fact(store, "Postgres connection pool size is 20.")
    f2, _ = hierarchical.add_fact(store, "Postgres statement timeout is 30s.")
    assert set(entity_store.load_entities(store)["postgres"]["memory_ids"]) == {f1.id, f2.id}
    assert hierarchical.delete_fact(store, f1.id) is True
    remaining = entity_store.load_entities(store)["postgres"]
    assert remaining["memory_ids"] == [f2.id]
    assert hierarchical.delete_fact(store, f2.id) is True
    assert "postgres" not in entity_store.load_entities(store)


def test_forget_unlinks_and_undo_relinks(store):
    fact, _ = hierarchical.add_fact(store, "Postgres maintenance window is Sunday.")
    assert "postgres" in entity_store.load_entities(store)
    hierarchical.forget_fact(store, fact.id)
    assert "postgres" not in entity_store.load_entities(store)
    hierarchical.forget_fact(store, fact.id, undo=True)
    entities = entity_store.load_entities(store)
    assert entities["postgres"]["memory_ids"] == [fact.id]


def test_rebuild_determinism(store):
    hierarchical.add_fact(store, "Postgres max connections is 100.")
    hierarchical.add_fact(store, "Redis cache TTL is 3600 seconds.")
    lessons_dir = os.path.join(store, "memory", "lessons")
    os.makedirs(lessons_dir, exist_ok=True)
    with open(os.path.join(lessons_dir, "lesson_postgres_tuning.md"), "w", encoding="utf-8") as fh:
        fh.write("# Tuning\n\nPostgres shared buffers need care.\n")
    first = entity_store.rebuild_entity_index(store)
    snap1 = _snapshot(entity_store.load_entities(store))
    second = entity_store.rebuild_entity_index(store)
    snap2 = _snapshot(entity_store.load_entities(store))
    assert snap1 == snap2
    assert first == second
    assert "postgres" in snap1
    assert "postgres_tuning" in snap1["postgres"][1]
    assert len(snap1["postgres"][1]) >= 2


def test_boost_bounded_and_empty_store():
    assert entity_store.entity_boost_for("/nonexistent-root-xyz", "Postgres tuning", ["fact-1"]) == {}
    assert entity_store.confirmed_query_entities("/nonexistent-root-xyz", "Postgres") == set()


def test_boost_exact_beats_fallback_and_respects_cap(store):
    f1, _ = hierarchical.add_fact(store, "Postgres max connections is 100.")
    f2, _ = hierarchical.add_fact(store, "Postgres connection pool size is 20.")
    f3, _ = hierarchical.add_fact(store, "Redis cache TTL is 3600 seconds.")
    f4, _ = hierarchical.add_fact(store, "New York office wifi password rotates weekly.")
    boosts = entity_store.entity_boost_for(store, "Postgres York office", [f1.id, f2.id, f3.id, f4.id])
    assert boosts
    assert all(b <= entity_store.ENTITY_BOOST_CAP for b in boosts.values())
    assert all(b <= 0.5 for b in boosts.values())
    assert f3.id not in boosts
    assert boosts[f1.id] > boosts[f4.id] > 0.0
    assert boosts[f1.id] == boosts[f2.id]
    top = max(score for _, score in hierarchical.search_facts(store, "Postgres York office"))
    assert all(b <= 0.5 * max(top, 1.0) for b in boosts.values())
    assert entity_store.entity_boost_for(store, "completely unrelated zebra query", [f1.id]) == {}


def test_caps(store):
    many = {
        f"entity-{i:05d}": {
            "name": f"entity-{i:05d}",
            "type": "PROPER",
            "memory_ids": ["m"],
            "count": 1,
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        for i in range(entity_store.MAX_ENTITIES + 5)
    }
    entity_store.save_entities(store, many)
    assert len(entity_store.load_entities(store)) == entity_store.MAX_ENTITIES
    for i in range(entity_store.MAX_IDS_PER_ENTITY + 5):
        entity_store.link_memory(store, f"mem-{i:04d}", "CheckoutFlow needs review.")
    entry = entity_store.load_entities(store)["checkoutflow"]
    assert len(entry["memory_ids"]) == entity_store.MAX_IDS_PER_ENTITY
    assert entry["memory_ids"][-1] == f"mem-{entity_store.MAX_IDS_PER_ENTITY + 4:04d}"


def _seed_conversation(root):
    store = Store(root, "alice")
    store.add(
        "s1",
        [
            {"speaker": "Alice", "text": "I adopted a beagle named Biscuit yesterday!"},
            {"speaker": "Bob", "text": "Congrats! How is Biscuit settling in?"},
        ],
        session_at="2023-05-08 10:00",
    )
    return store


def test_search_empty_store_noop(tmp_path):
    root = str(tmp_path / "conv")
    store = _seed_conversation(root)
    conv_search._RECALL_CACHE.clear()
    before = recall(store, "How is Biscuit doing?", options=LEXICAL)
    entity_store.save_entities(root, {})
    conv_search._RECALL_CACHE.clear()
    after = recall(store, "How is Biscuit doing?", options=LEXICAL)
    assert entity_store.confirmed_query_entities(root, "How is Biscuit doing?") == set()
    assert after.ranked == before.ranked
    assert after.context == before.context
    assert after.explain.get("entities") == before.explain.get("entities")


def test_stability_filter_and_legacy_default(store):
    s, _ = hierarchical.add_fact(store, "Postgres is our primary datastore.", stability="stable")
    d, _ = hierarchical.add_fact(store, "Postgres replica lag is 2s right now.", stability="dynamic")
    u, _ = hierarchical.add_fact(store, "Postgres backups run nightly.")
    assert s.stability == "stable"
    assert d.stability == "dynamic"
    assert u.stability == ""
    default = hierarchical.search_facts(store, "Postgres")
    explicit = hierarchical.search_facts(store, "Postgres", stability="")
    assert [(f.id, score) for f, score in default] == [(f.id, score) for f, score in explicit]
    assert {f.id for f, _ in default} == {s.id, d.id, u.id}
    stable_only = hierarchical.search_facts(store, "Postgres", stability="stable")
    assert [f.id for f, _ in stable_only] == [s.id]
    dynamic_only = hierarchical.search_facts(store, "Postgres", stability="dynamic")
    assert [f.id for f, _ in dynamic_only] == [d.id]
    listed = hierarchical.list_facts(store, stability="stable")
    assert [f.id for f in listed] == [s.id]
    with pytest.raises(ValueError):
        hierarchical.search_facts(store, "Postgres", stability="frozen")
    assert (
        hierarchical.AtomicFact(
            id="x",
            statement="y",
            category="general",
            scopes=[],
            confidence=0.5,
            confirmations=1,
            valid_from="2026-01-01T00:00:00+00:00",
            valid_until=None,
        ).stability
        == ""
    )


def test_coerce_tolerance():
    base = {
        "id": "fact-1",
        "statement": "s",
        "category": "general",
        "scopes": [],
        "confidence": 0.5,
        "confirmations": 1,
        "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_until": None,
        "source_traces": [],
        "status": "active",
        "superseded_by": None,
        "revision": "r",
        "created_at": "c",
        "updated_at": "u",
    }
    assert hierarchical._coerce_fact(dict(base)).stability == ""
    legacy = dict(base)
    legacy["stability"] = "frozen"
    assert hierarchical._coerce_fact(legacy).stability == ""
    tiered = dict(base)
    tiered["stability"] = "stable"
    assert hierarchical._coerce_fact(tiered).stability == "stable"


def test_format_default_byte_identical(store):
    hierarchical.add_fact(store, "Postgres max connections is 100.", scopes=["db"], stability="stable")
    hierarchical.add_fact(store, "Redis cache TTL is 3600 seconds.", stability="dynamic")
    scored = hierarchical.search_facts(store, "Postgres Redis connections cache")
    assert scored
    manual = []
    for fact, _score in scored:
        scope_str = f" [{','.join(fact.scopes)}]" if fact.scopes else ""
        manual.append(f"- {fact.statement} (conf: {fact.confidence:.2f}){scope_str}")
    assert hierarchical.format_fact_lines(scored) == manual
    assert hierarchical.format_fact_lines(scored, group_stability=False) == manual
    grouped = hierarchical.format_fact_lines(scored, group_stability=True)
    assert "## Stable" in grouped
    assert "## Recent" in grouped
    assert grouped.index("## Stable") < grouped.index("## Recent")
    assert sorted(line for line in grouped if line.startswith("- ")) == sorted(manual)


def test_add_never_breaks_when_entity_hook_fails(store, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(entity_store, "link_memory", _boom)
    fact, action = hierarchical.add_fact(store, "Postgres max connections is 100.")
    assert action == "ADD"
    assert fact.id
