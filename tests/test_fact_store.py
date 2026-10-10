"""Contracts for the explicit, event-backed local storage migration."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from commontrace import _jsonl, fact_store
from commontrace import hierarchical as h


def activate(root):
    return fact_store.migrate(str(root))


def test_migration_checksum_export_and_rebuild(tmp_path):
    old, _ = h.add_fact(str(tmp_path), "Project requires Python 3.10", scopes=["project"])
    source = open(h._facts_file(str(tmp_path)), "rb").read()
    report = activate(tmp_path)
    assert report["facts"] == 1
    assert report["source_sha256"]
    assert report["normalized_sha256"] == report["destination_sha256"]
    assert open(h._facts_file(str(tmp_path)), "rb").read() == source
    new, _ = h.add_fact(str(tmp_path), "SQLite uses a durable journal", scopes=["project"])
    assert set(h.load_facts(str(tmp_path))) == {old.id, new.id}
    assert open(h._facts_file(str(tmp_path)), "rb").read() == source
    destination = os.path.join(str(tmp_path), "export.jsonl")
    fact_store.export(str(tmp_path), destination)
    assert {row["id"] for row in _jsonl.read_rows(destination)} == {old.id, new.id}
    before = h.load_facts(str(tmp_path))
    with fact_store.transaction(str(tmp_path), write=True) as facts:
        facts.connection.execute("DELETE FROM facts")
        facts.connection.execute("DELETE FROM terms")
        facts.connection.execute("DELETE FROM term_counts")
        facts.connection.execute("DELETE FROM fact_fts")
    fact_store.rebuild(str(tmp_path))
    assert h.load_facts(str(tmp_path)) == before
    assert h.search_facts(str(tmp_path), "durable")[0][0].id == new.id


def test_migration_rejects_bad_rows_without_activation(tmp_path):
    _jsonl.write_rows(h._facts_file(str(tmp_path)), [{"statement": "valid"}, {"invalid": True}])
    with pytest.raises(ValueError, match="row"):
        activate(tmp_path)
    assert not fact_store.enabled(str(tmp_path))


def test_rollback_and_noop_versions(tmp_path):
    activate(tmp_path)
    fact, _ = h.add_fact(str(tmp_path), "alpha uses SQLite", source_trace_id="trace-one")
    count = len(fact_store.events(str(tmp_path)))
    h.add_fact(str(tmp_path), fact.statement, source_trace_id="trace-one")
    assert len(fact_store.events(str(tmp_path))) == count
    with pytest.raises(RuntimeError):
        with h.mutate_facts(str(tmp_path)) as facts:
            facts[fact.id].statement = "must roll back"
            raise RuntimeError("failed")
    assert h.load_facts(str(tmp_path))[fact.id].statement == fact.statement
    h.update_fact(str(tmp_path), fact.id, statement="alpha now uses PostgreSQL")
    history = fact_store.events(str(tmp_path))
    assert [event["record"]["version"] for event in history] == [1, 2]
    assert [event["record"]["is_latest"] for event in history] == [False, True]
    assert history[-1]["record"]["relations"]["updates"] == [fact.id]
    assert history[-1]["record"]["kind"] == "fact"


def test_scope_dedup_and_contradiction_parity(tmp_path, monkeypatch):
    legacy = os.path.join(str(tmp_path), "legacy")
    sqlite = os.path.join(str(tmp_path), "sqlite")
    activate(sqlite)
    monkeypatch.setenv("COMMONTRACE_FACT_CONFLICTS", "supersede")
    statements = [
        ("Project allows public access", ["a"]),
        ("Project does not allow public access", ["a"]),
        ("Project allows public access", ["b"]),
        ("Use durable SQLite transactions for every event", ["a"]),
        ("For every event use durable SQLite transactions", ["a"]),
    ]
    actions = []
    for root in (legacy, sqlite):
        rows = [h.add_fact(root, text, scopes=scopes, valid_from=f"2025-01-{n + 1:02}")[1]
                for n, (text, scopes) in enumerate(statements)]
        actions.append(rows)
    assert actions[0] == actions[1]
    def comparable(root):
        return [(f.id, f.statement, f.scopes, f.status, f.confirmations, f.superseded_by)
                for f in h.load_facts(root).values()]
    assert comparable(legacy) == comparable(sqlite)
    for scorer in ("overlap-v1", "bm25-v1"):
        for scope in ("a", "b"):
            a = [(f.id, score) for f, score in h.search_facts(legacy, "durable public access", scope=scope, scorer=scorer)]
            b = [(f.id, score) for f, score in h.search_facts(sqlite, "durable public access", scope=scope, scorer=scorer)]
            assert a == b


def test_concurrent_writers_and_restarts(tmp_path):
    activate(tmp_path)
    def write(n):
        h.add_fact(str(tmp_path), f"Component unique{n} has a durable journal", scopes=[str(n)])
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(24)))
    assert len(h.load_facts(str(tmp_path))) == 24
    assert len(fact_store.events(str(tmp_path))) == 24


def test_expiry_forgetting_and_temporal_retrieval(tmp_path):
    activate(tmp_path)
    a, _ = h.add_fact(str(tmp_path), "alpha is active", valid_from="2024-01-01")
    b, _ = h.add_fact(str(tmp_path), "alpha has expired", valid_from="2024-01-01", expires_at="2024-02-01")
    assert [f.id for f, _ in h.search_facts(str(tmp_path), "alpha")] == [a.id]
    h.invalidate_fact(str(tmp_path), a.id, at="2024-03-01")
    assert h.search_facts(str(tmp_path), "alpha") == []
    assert {f.id for f, _ in h.search_facts(str(tmp_path), "alpha", as_of="2024-01-15")} == {a.id, b.id}
    h.forget_fact(str(tmp_path), a.id)
    assert {f.id for f, _ in h.search_facts(str(tmp_path), "alpha", as_of="2024-01-15")} == {b.id}


def test_write_does_not_scan_bank(tmp_path, monkeypatch):
    activate(tmp_path)
    h.add_facts(str(tmp_path), [{"statement": f"unique{i} requires durable storage"} for i in range(100)])
    def forbidden(*args, **kwargs):
        raise AssertionError("whole-bank reconstruction")
    monkeypatch.setattr(h, "load_facts", forbidden)
    monkeypatch.setattr(h._StatementIndex, "__init__", forbidden)
    h.add_fact(str(tmp_path), "newcomponent requires durable storage")


def test_envelope_json_is_portable(tmp_path):
    activate(tmp_path)
    fact, _ = h.add_fact(str(tmp_path), "portable memory", scopes=["repo"], source_trace_id="run")
    record = fact_store.events(str(tmp_path))[0]["record"]
    assert json.loads(json.dumps(record))["payload"] == fact.to_dict()
    assert record["scope"]["labels"] == ["repo"]
    assert record["provenance"]["source_traces"] == ["run"]


def test_detached_views_and_recall_use_sqlite_generation(tmp_path):
    from commontrace import fact_index, recall
    activate(tmp_path)
    fact, _ = h.add_fact(str(tmp_path), "storage uses an event ledger")
    view = fact_index.snapshot_facts(str(tmp_path))
    assert view[fact.id].statement == fact.statement
    assert any(item.id == "fact:" + fact.id for item in recall.recall(
        str(tmp_path), "event ledger", channels=("facts",)).items)
    h.update_fact(str(tmp_path), fact.id, statement="storage uses a revised journal")
    with pytest.raises(fact_index.FactSnapshotChanged):
        view.ensure_current()
    assert fact_index.search(str(tmp_path), "revised")[0][0].id == fact.id


def test_killed_writer_cannot_publish_partial_projections(tmp_path):
    import subprocess
    import sys
    activate(tmp_path)
    code = """
import os, sys
from commontrace import hierarchical as h
with h.mutate_facts(sys.argv[1]) as facts:
    h._add_locked(facts, 'crashed write must disappear', 'general', [], None, None, None, 0.8, '')
    os._exit(9)
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=False)
    assert result.returncode == 9
    assert h.load_facts(str(tmp_path)) == {}
    assert fact_store.events(str(tmp_path)) == []
    h.add_fact(str(tmp_path), "next writer recovers safely")
    assert len(h.load_facts(str(tmp_path))) == 1


def test_multiple_process_writers_are_serialized(tmp_path):
    import subprocess
    import sys
    activate(tmp_path)
    code = """
import sys
from commontrace import hierarchical as h
for n in range(12):
    h.add_fact(sys.argv[1], 'worker ' + sys.argv[2] + ' records unique' + str(n), scopes=[sys.argv[2]])
"""
    processes = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(worker)]) for worker in range(3)]
    assert all(process.wait(timeout=30) == 0 for process in processes)
    assert len(h.load_facts(str(tmp_path))) == 36
    assert len(fact_store.events(str(tmp_path))) == 36


def test_storage_commands_are_executable(tmp_path):
    import subprocess
    import sys
    def cli(*args):
        return subprocess.run([sys.executable, "-m", "commontrace.cli", "fact", *args, "--dest", str(tmp_path)],
                              capture_output=True, text=True, check=False)
    assert cli("add", "portable event storage").returncode == 0
    result = cli("migrate")
    assert result.returncode == 0
    assert json.loads(result.stdout)["facts"] == 1
    assert cli("migrate").returncode == 1
    assert cli("rebuild").returncode == 0
    output = os.path.join(str(tmp_path), "export.jsonl")
    assert cli("export", "--output", output).returncode == 0
    assert len(list(_jsonl.read_rows(output))) == 1


@pytest.mark.parametrize("backend", ["jsonl", "sqlite"])
def test_empty_terminal_interval_is_preserved_for_lineage(tmp_path, backend):
    if backend == "sqlite":
        activate(tmp_path)
    old, _ = h.add_fact(str(tmp_path), "project allows public access", valid_from="2025-01-01")
    new, _ = h.add_fact(str(tmp_path), "project does not allow public access", valid_from="2025-01-01")
    h.resolve_contradiction(str(tmp_path), old.id, new.id)
    stored = h.load_facts(str(tmp_path))[old.id]
    assert stored.valid_until == stored.valid_from
    assert stored.superseded_by == new.id
    assert not h._valid_at(stored, h.lesson_cache.parse_moment("2025-01-01"))


def test_staged_add_then_remove_leaves_no_orphan_projection(tmp_path):
    activate(tmp_path)
    with h.mutate_facts(str(tmp_path)) as facts:
        fact, _ = h._add_locked(facts, "temporary staged record", "general", [], None, None, None, 0.8, "")
        assert len(facts) == 1
        del facts[fact.id]
        assert len(facts) == 0
    assert h.load_facts(str(tmp_path)) == {}
    assert h.search_facts(str(tmp_path), "temporary") == []
    assert fact_store.events(str(tmp_path)) == []
    fact_store.rebuild(str(tmp_path))
    assert h.load_facts(str(tmp_path)) == {}
