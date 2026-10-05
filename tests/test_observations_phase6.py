"""Consolidation respects source validity, scope identity, and provenance."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from commontrace import _jsonl, hierarchical, observations

NOW = "2026-10-04T00:00:00+00:00"


def _fact(identity="fact", statement="Prefer concise explanations.", **kwargs):
    args = dict(id=identity, statement=statement, category="preference", scopes=[],
                confidence=0.8, confirmations=2, valid_from="2026-01-01T00:00:00Z",
                valid_until=None, source_traces=[identity + "-trace"], created_at=NOW,
                updated_at=NOW, revision="imported-revision")
    args.update(kwargs)
    return hierarchical.AtomicFact(**args)


def test_identical_statement_in_different_scopes_has_separate_provenance(tmp_path):
    root = str(tmp_path)
    facts = [_fact("global"), _fact("tenant-a", scopes=["a"]), _fact("tenant-b", scopes=["b"])]
    hierarchical.save_facts(root, {fact.id: fact for fact in facts})
    output = observations.consolidate_facts(root, now=NOW)
    assert len(output) == 3
    assert len({observation.id for observation in output}) == 3
    for observation in output:
        identity = {(): "global", ("a",): "tenant-a", ("b",): "tenant-b"}[tuple(observation.scopes)]
        assert observation.source_fact_ids == [identity]
        assert [item["source_id"] for item in observation.evidence] == [identity + "-trace"]
    global_id = observations._observation_id(facts[0].statement)
    assert {item.id for item in observations.load_observations(root, scope="a").values()} == {
        global_id, observations._observation_id(facts[0].statement, ["a"])}
    tenant_b_id = observations._observation_id(facts[0].statement, ["b"])
    assert observations.get_observation(root, tenant_b_id, scope="a") is None


def test_scoped_id_cannot_collide_with_unscoped_statement_containing_boundary_suffix(tmp_path):
    root = str(tmp_path)
    statement = "Prefer concise explanations."
    crafted = statement + '\0["tenant-a"]'
    scoped = _fact("scoped", statement, scopes=["tenant-a"])
    global_fact = _fact("global", crafted)
    hierarchical.save_facts(root, {fact.id: fact for fact in (scoped, global_fact)})
    output = observations.consolidate_facts(root, now=NOW)
    scoped_id = observations._observation_id(statement, ["tenant-a"])
    unscoped_id = observations._observation_id(crafted)
    assert scoped_id.startswith("obs-scoped-")
    assert scoped_id != unscoped_id
    assert unscoped_id == "obs-" + hashlib.sha256(crafted.lower().encode()).hexdigest()[:12]
    assert {observation.id: observation.source_fact_ids for observation in output} == {
        scoped_id: ["scoped"], unscoped_id: ["global"]}


@pytest.mark.parametrize("changes", [
    {"valid_from": "2026-10-05T00:00:00Z"},
    {"valid_until": NOW},
    {"expires_at": NOW},
    {"forgotten": True},
    {"status": "superseded"},
    {"status": "deleted"},
])
def test_noncurrent_sources_do_not_become_current_observations(tmp_path, changes):
    root = str(tmp_path)
    current = _fact("current")
    excluded = _fact("excluded", "Excluded claim.", **changes)
    hierarchical.save_facts(root, {fact.id: fact for fact in (current, excluded)})
    output = observations.consolidate_facts(root, now=NOW)
    assert [observation.source_fact_ids for observation in output] == [["current"]]


def test_old_recorded_time_does_not_override_valid_time(tmp_path):
    root = str(tmp_path)
    future = _fact("future", created_at="2020-01-01T00:00:00Z", valid_from="2030-01-01T00:00:00Z")
    hierarchical.save_facts(root, {future.id: future})
    assert observations.consolidate_facts(root, now=NOW) == []


def test_legacy_observation_fields_remain_readable_and_malformed_count_is_skipped(tmp_path):
    root = str(tmp_path)
    _jsonl.write_rows(observations._observations_file(root), [
        {"id": "legacy", "statement": "Durable claim", "proof_count": 2},
        {"id": "invalid", "statement": "Invalid count", "proof_count": float("inf")},
    ])
    result = observations.load_observations(root, scope="tenant")
    assert list(result) == ["legacy"]
    assert result["legacy"].scopes == []
    assert result["legacy"].source_fact_ids == []
    assert observations.observation_boost(float("inf")) == 0


def test_imported_ids_and_stale_revisions_cannot_poison_other_store_token_cache(tmp_path):
    first, second = str(tmp_path / "first"), str(tmp_path / "second")
    fact = _fact(statement="Postgres database connections")
    hierarchical.save_facts(first, {fact.id: fact})
    hierarchical.save_facts(second, {fact.id: replace(fact, statement="Redis cache expiry")})
    hierarchical._FACT_TOKENS.clear()
    assert hierarchical.search_facts(first, "postgres")
    assert hierarchical.search_facts(second, "postgres") == []
    assert hierarchical.search_facts(second, "redis")


def test_current_fact_search_keeps_valid_windows_and_preserves_historical_and_ttl_options(tmp_path, monkeypatch):
    root = str(tmp_path)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = datetime.fromisoformat(NOW)
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(hierarchical, "datetime", Clock)
    facts = [_fact("current", "Matching indexed document"),
             _fact("future", "Matching indexed document", valid_from="2026-10-05T00:00:00Z"),
             _fact("ended", "Matching indexed document", valid_until=NOW),
             _fact("expired", "Matching indexed document", expires_at="2026-10-03T00:00:00Z")]
    hierarchical.save_facts(root, {fact.id: fact for fact in facts})
    assert {fact.id for fact, _score in hierarchical.search_facts(root, "matching")} == {"current"}
    assert {fact.id for fact, _score in hierarchical.search_facts(root, "matching", as_of="")} == {"current"}
    assert {fact.id for fact, _score in hierarchical.search_facts(root, "matching", show_expired=True)} == {
        "current", "expired"}
    assert {fact.id for fact, _score in hierarchical.search_facts(root, "matching", as_of="2026-01-02")} == {
        "current", "ended", "expired"}
    assert {fact.id for fact, _score in hierarchical.search_facts(root, "matching", as_of="2026-10-06")} == {
        "current", "future"}
    # Administrative listing retains scheduled/ended active-status records.
    assert {fact.id for fact in hierarchical.list_facts(root, now=datetime.now(timezone.utc))} == {
        "current", "future", "ended"}


def test_malformed_reversed_fact_window_is_skipped_without_losing_valid_rows(tmp_path):
    root = str(tmp_path)
    valid = _fact("valid")
    malformed = _fact("malformed", valid_until="2025-01-01T00:00:00Z")
    _jsonl.write_rows(hierarchical._facts_file(root), [valid.to_dict(), malformed.to_dict()])
    assert list(hierarchical.load_facts(root)) == ["valid"]
