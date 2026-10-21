"""Facts reach the knowledge graph on their own: entities and relations are projected
incrementally, with provenance, and closed when the fact that stated them retires."""
import json

import pytest

from unimem import cli, graph, graph_sync, hierarchical, provenance


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("UNIMEM_GRAPH_AUTO", "UNIMEM_FACT_CONFLICTS", "UNIMEM_FACT_EMBEDDER"):
        monkeypatch.delenv(name, raising=False)


def open_edges(root, relation=None):
    return [e for e in graph.load_edges(root) if e.invalid_at is None and (relation is None or e.relation == relation)]


def test_sync_projects_entities_relations_and_provenance(tmp_path):
    root = str(tmp_path)
    fact, _ = hierarchical.add_fact(root, "Alice Smith works at Acme Corp and uses Postgres for billing.")
    report = graph_sync.sync(root)
    assert report["facts"] == 1 and report["relations"] >= 1 and not report["errors"]
    nodes = graph.load_nodes(root)
    assert nodes[f"memory:{fact.id}"].entity_type == "memory"
    assert nodes["person:alice_smith"].entity_type == "person"
    assert nodes["organization:acme_corp"].entity_type == "organization"
    mentions = {e.target for e in open_edges(root, "mentions") if e.source == f"memory:{fact.id}"}
    assert {"person:alice_smith", "organization:acme_corp", "service:postgres"} <= mentions
    works = [e for e in open_edges(root, "works_at")]
    assert [(e.source, e.target) for e in works] == [("person:alice_smith", "organization:acme_corp")]
    assert works[0].properties["source"] == f"fact:{fact.id}"
    assert provenance.list_provenance(root, f"memory:{fact.id}")[0]["source_path"] == f"fact:{fact.id}"


def test_sync_is_incremental_and_idempotent(tmp_path):
    root = str(tmp_path)
    hierarchical.add_fact(root, "The Payments Service depends on Redis.")
    first = graph_sync.sync(root)
    edges = len(graph.load_edges(root))
    assert first["facts"] == 1 and not graph_sync.due(root)
    assert graph_sync.sync(root)["facts"] == 0 and len(graph.load_edges(root)) == edges
    hierarchical.add_fact(root, "Marcus approved the Orders Database migration.")
    assert graph_sync.due(root)
    assert graph_sync.sync(root)["facts"] == 1  # only the new fact


def test_a_retired_fact_closes_what_it_produced_at_the_time_it_ended(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIMEM_FACT_CONFLICTS", "supersede")
    root = str(tmp_path)
    old, _ = hierarchical.add_fact(root, "The Payments Service depends on Redis.")
    graph_sync.sync(root)
    [dep] = open_edges(root, "depends_on")
    new, _ = hierarchical.add_fact(root, "The Payments Service does not depend on Redis.")
    stored = hierarchical.load_facts(root)[old.id]
    assert stored.status == "superseded"
    report = graph_sync.sync(root)
    assert report["retired"] == 1 and report["closed"] >= 2
    closed = {e.relation: e for e in graph.load_edges(root) if e.invalid_at is not None}
    assert closed["depends_on"].invalid_at == stored.valid_until  # graph history agrees with fact history
    assert closed["depends_on"].properties["closed_reason"] == "fact no longer current"
    assert not any(e.source == f"memory:{old.id}" for e in open_edges(root))
    assert any(e.source == f"memory:{new.id}" for e in open_edges(root, "mentions"))
    assert dep.source == "service:payments_service"


def test_auto_mode_switch_and_due_check(tmp_path, monkeypatch):
    root = str(tmp_path)
    hierarchical.add_fact(root, "Aisha is on call this week for the Billing API.")
    monkeypatch.setenv("UNIMEM_GRAPH_AUTO", "off")
    assert graph_sync.run_if_due(root) is None and graph.load_nodes(root) == {}
    monkeypatch.setenv("UNIMEM_GRAPH_AUTO", "on")
    assert graph_sync.run_if_due(root)["facts"] == 1
    assert graph_sync.run_if_due(root) is None  # nothing changed since
    monkeypatch.setenv("UNIMEM_GRAPH_AUTO", "sometimes")
    assert "must be on or off" in graph_sync.run_if_due(root)["error"]  # reported, never raised


def test_sqlite_store_and_cli(tmp_path, capsys):
    root = str(tmp_path)
    assert cli.main(["init", "--dest", root]) == 0
    capsys.readouterr()
    from unimem import fact_store

    assert fact_store.enabled(root)  # new stores start on the indexed backend
    hierarchical.add_fact(root, "Kenji moved the Search API to us-east-1.")
    assert cli.main(["graph", "sync", "--json", "--dest", root]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["facts"] == 1 and out["entities"] >= 2
    assert {"person:kenji", "place:us_east_1"} <= set(graph.load_nodes(root))
    assert cli.main(["graph", "sync", "--limit", "0", "--dest", root]) == 2


def test_init_can_keep_the_jsonl_backend(tmp_path, monkeypatch, capsys):
    from unimem import fact_store

    root = str(tmp_path / "a")
    assert cli.main(["init", "--fact-store", "jsonl", "--dest", root]) == 0
    assert not fact_store.enabled(root)
    monkeypatch.setenv("UNIMEM_FACT_STORE", "jsonl")
    assert cli.main(["init", "--dest", str(tmp_path / "b")]) == 0
    assert not fact_store.enabled(str(tmp_path / "b"))
    monkeypatch.setenv("UNIMEM_FACT_STORE", "parquet")
    assert cli.main(["init", "--dest", str(tmp_path / "c")]) == 2


def test_an_exclusive_relation_exposes_a_contradiction_words_cannot(tmp_path, monkeypatch):
    from unimem import fact_conflicts

    root = str(tmp_path)
    old, _ = hierarchical.add_fact(root, "Alice Smith works at Acme Corp.", valid_from="2024-01-01T00:00:00Z")
    new, _ = hierarchical.add_fact(root, "Alice Smith works at Globex Inc.", valid_from="2025-01-01T00:00:00Z")
    assert fact_conflicts.pending(root) == []  # no shared slot words: the lexical rule is blind here
    report = graph_sync.sync(root)
    assert report["conflicts"] == 1
    [conflict] = fact_conflicts.pending(root)
    assert (conflict.older, conflict.newer, conflict.kind, conflict.action) == (old.id, new.id, "exclusive",
                                                                                "flagged")
    assert hierarchical.load_facts(root)[old.id].status == "active"  # flag mode changes nothing


def test_supersede_mode_resolves_an_exclusive_contradiction(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIMEM_FACT_CONFLICTS", "supersede")
    root = str(tmp_path)
    old, _ = hierarchical.add_fact(root, "Alice Smith works at Acme Corp.", valid_from="2024-01-01T00:00:00Z")
    graph_sync.sync(root)
    new, _ = hierarchical.add_fact(root, "Alice Smith works at Globex Inc.", valid_from="2025-01-01T00:00:00Z")
    assert graph_sync.sync(root)["conflicts"] == 1
    stored = hierarchical.load_facts(root)[old.id]
    assert stored.status == "superseded" and stored.superseded_by == new.id
    graph_sync.sync(root)  # the next pass retires the superseded fact's links
    assert not any(e.source == f"memory:{old.id}" for e in open_edges(root))
    [works] = open_edges(root, "works_at")
    assert works.target == "organization:globex_inc"
