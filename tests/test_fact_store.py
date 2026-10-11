"""Contracts for the explicit, event-backed local storage migration."""
from __future__ import annotations

import json
import os
import sqlite3
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


def test_projection_counts_survive_revisions_rollback_and_rebuild(tmp_path):
    root = str(tmp_path)
    activate(root)
    first, _ = h.add_fact(root, "alpha records durable durable journals", scopes=["a"])
    second, _ = h.add_fact(root, "beta records durable journals", scopes=["b"])

    def projections():
        with fact_store.transaction(root) as facts:
            connection = facts.connection
            assert connection.execute(
                "SELECT count(*) FROM lexicon l WHERE l.documents != "
                "(SELECT count(*) FROM postings p WHERE p.term_id=l.id)"
            ).fetchone() == (0,)
            return connection.execute(
                "SELECT kind,term,fact_id,count FROM terms ORDER BY kind,term,fact_id"
            ).fetchall()

    original = projections()
    assert ("bm25-v1", "durabl", first.id, 2) in original
    events = fact_store.events(root)
    with pytest.raises(RuntimeError, match="projection failed"):
        with h.mutate_facts(root) as facts:
            h._add_locked(facts, "uncommitted unique journal", "general", [], None, None, None, 0.8, "")
            raise RuntimeError("projection failed")
    assert projections() == original
    assert fact_store.events(root) == events
    h.update_fact(root, first.id, statement="alpha records revised journals")
    with h.mutate_facts(root) as facts:
        del facts[second.id]
    revised = projections()
    assert not any(term in {"durable", "durabl"} for _, term, _, _ in revised)
    fact_store.rebuild(root)
    assert projections() == revised


def test_idle_connection_reuse_keeps_transactions_and_facts_separate(tmp_path):
    root = str(tmp_path)
    activate(root)
    with fact_store.transaction(root) as first:
        connection = first.connection
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert not connection.in_transaction
    with fact_store.transaction(root) as second:
        assert second is not first
        assert second.connection is connection
    fact_store.close_connections(root)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_failed_connection_is_discarded_and_next_write_recovers(tmp_path):
    root = str(tmp_path)
    activate(root)
    with pytest.raises(RuntimeError, match="abort"):
        with fact_store.transaction(root, write=True) as facts:
            connection = facts.connection
            h._add_locked(facts, "must not survive an abort", "general", [], None, None, None, 0.8, "")
            raise RuntimeError("abort")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    assert not h.load_facts(root)
    h.add_fact(root, "a fresh writer recovers safely")
    assert len(fact_store.events(root)) == 1


def test_nested_readers_do_not_share_active_connections_or_snapshots(tmp_path):
    root = str(tmp_path)
    activate(root)
    with fact_store.transaction(root) as older:
        assert len(older) == 0
        h.add_fact(root, "concurrent committed journal")
        with fact_store.transaction(root) as newer:
            assert newer.connection is not older.connection
            assert len(newer) == 1
            assert len(older) == 0
    assert len(h.load_facts(root)) == 1


@pytest.mark.parametrize("explicit_cursor", [False, True])
def test_finished_transaction_closes_partial_cursors_and_releases_checkpoint(tmp_path, explicit_cursor):
    root = str(tmp_path)
    activate(root)
    h.add_facts(root, [{"statement": f"component item{n} has a journal"} for n in range(4)])
    with fact_store.transaction(root) as facts:
        cursor = facts.connection.cursor() if explicit_cursor else facts.connection
        cursor = cursor.execute("SELECT id FROM facts")
        assert cursor.fetchone() is not None
    with sqlite3.connect(fact_store.database_path(root), isolation_level=None, timeout=0.1) as other:
        other.execute("INSERT INTO meta VALUES('checkpoint-probe','committed')")
        assert other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        cursor.fetchone()


@pytest.mark.parametrize("explicit_cursor", [False, True])
def test_aborted_transaction_releases_cursor_checkpoint_lock(tmp_path, explicit_cursor):
    root = str(tmp_path)
    activate(root)
    h.add_facts(root, [{"statement": f"component item{n} has a journal"} for n in range(4)])
    with pytest.raises(RuntimeError, match="abort read"):
        with fact_store.transaction(root) as facts:
            cursor = facts.connection.cursor() if explicit_cursor else facts.connection
            cursor = cursor.execute("SELECT id FROM facts")
            assert cursor.fetchone() is not None
            raise RuntimeError("abort read")
    with sqlite3.connect(fact_store.database_path(root), isolation_level=None, timeout=0.1) as other:
        other.execute("INSERT INTO meta VALUES('abort-checkpoint-probe','committed')")
        assert other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        cursor.fetchone()


def test_idle_connections_are_bounded_and_evict_closed_handles(tmp_path):
    fact_store.close_connections()
    connections = []
    for n in range(6):
        root = str(tmp_path / str(n))
        activate(root)
        with fact_store.transaction(root) as facts:
            connections.append(facts.connection)
    for connection in connections[:-4]:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    assert all(connection.execute("SELECT 1").fetchone() == (1,) for connection in connections[-4:])
    fact_store.close_connections()


def test_idle_connections_follow_canonical_root_after_symlink_retarget(tmp_path):
    roots = [str(tmp_path / name) for name in ("a", "b")]
    for root in roots:
        activate(root)
    one, _ = h.add_fact(roots[0], "first independent store")
    two, _ = h.add_fact(roots[1], "second independent store")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(roots[0], target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    assert set(h.load_facts(str(alias))) == {one.id}
    alias.unlink()
    alias.symlink_to(roots[1], target_is_directory=True)
    assert set(h.load_facts(str(alias))) == {two.id}
    assert set(h.load_facts(roots[0])) == {one.id}


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_warm_idle_connections_are_closed_before_fork(tmp_path):
    import multiprocessing

    root = str(tmp_path)
    activate(root)
    with fact_store.transaction(root) as facts:
        parent_connection = facts.connection

    def child():
        h.add_fact(root, "child opens its own durable journal")
        grandchild = multiprocessing.get_context("fork").Process(
            target=h.add_fact, args=(root, "grandchild also opens its own connection")
        )
        grandchild.start()
        grandchild.join(timeout=10)
        try:
            assert grandchild.exitcode == 0
        finally:
            if grandchild.is_alive():
                grandchild.terminate()
                grandchild.join(timeout=5)
        h.add_fact(root, "child remains usable after a second fork")

    worker = multiprocessing.get_context("fork").Process(target=child)
    worker.start()
    worker.join(timeout=30)
    try:
        assert worker.exitcode == 0
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            parent_connection.execute("SELECT 1")
        assert len(h.load_facts(root)) == len(fact_store.events(root)) == 3
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)


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


def test_legacy_postings_upgrade_is_atomic_for_concurrent_readers(tmp_path):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor

    from commontrace import fact_store, hierarchical
    root = str(tmp_path)
    fact_store.migrate(root)
    fact, _ = hierarchical.add_fact(root, 'A durable assertion exists', category='test')
    db = sqlite3.connect(fact_store.database_path(root))
    db.executescript('DROP TRIGGER clear_terms; DROP TRIGGER clear_counts; '
                     'CREATE TABLE legacy_terms AS SELECT * FROM terms; '
                     'CREATE TABLE legacy_counts AS SELECT * FROM term_counts; '
                     'DROP VIEW terms; DROP VIEW term_counts; '
                     'ALTER TABLE legacy_terms RENAME TO terms; '
                     'ALTER TABLE legacy_counts RENAME TO term_counts; '
                     'DROP TABLE postings; DROP TABLE lexicon;')
    db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: hierarchical.load_facts(root)[fact.id].statement, range(2)))
    assert results == ['A durable assertion exists'] * 2
    with fact_store.transaction(root) as canonical:
        assert canonical.connection.execute('SELECT COUNT(*) FROM postings').fetchone()[0] > 0


def _restore_blocked_near_projection(root):
    """Build the pre-global derived layout without changing ledger/payload bytes."""
    fact_store.close_connections(root)
    with sqlite3.connect(fact_store.database_path(root)) as connection:
        connection.execute(
            "INSERT INTO lexicon(kind,term,documents) "
            "SELECT 'near:'||f.block,l.term,COUNT(*) FROM postings p "
            "JOIN lexicon l ON l.id=p.term_id JOIN facts f ON f.rowid=p.fact_row "
            "WHERE l.kind='near-v2' GROUP BY f.block,l.term"
        )
        connection.execute(
            "INSERT INTO postings SELECT old.id,p.fact_row,p.count FROM postings p "
            "JOIN lexicon l ON l.id=p.term_id JOIN facts f ON f.rowid=p.fact_row "
            "JOIN lexicon old ON old.kind='near:'||f.block AND old.term=l.term WHERE l.kind='near-v2'"
        )
        connection.execute("DELETE FROM postings WHERE term_id IN (SELECT id FROM lexicon WHERE kind='near-v2')")
        connection.execute("DELETE FROM lexicon WHERE kind='near-v2'")
        connection.execute("DELETE FROM meta WHERE key='near_postings'")
        connection.execute("DROP INDEX IF EXISTS fact_block")


def _authoritative_fact_bytes(connection):
    return (
        connection.execute("SELECT seq,fact_id,version,operation,record FROM events ORDER BY seq").fetchall(),
        connection.execute("SELECT id,payload FROM facts ORDER BY id").fetchall(),
    )


def test_global_near_upgrade_preserves_authoritative_bytes(tmp_path, monkeypatch):
    root = str(tmp_path)
    activate(root)
    monkeypatch.setenv("COMMONTRACE_FACT_CONFLICTS", "off")
    h.add_facts(root, [{"statement": f"Component {n} requires a durable replicated journal"} for n in range(30)])
    _restore_blocked_near_projection(root)
    with sqlite3.connect(fact_store.database_path(root)) as connection:
        before = _authoritative_fact_bytes(connection)
        blocked = connection.execute("SELECT COUNT(*) FROM lexicon WHERE kind>='near:' AND kind<'near;'").fetchone()[0]
    with fact_store.transaction(root) as facts:
        connection = facts.connection
        assert _authoritative_fact_bytes(connection) == before
        assert connection.execute("SELECT value FROM meta WHERE key='near_postings'").fetchone() == ("global-v2",)
        assert connection.execute("SELECT 1 FROM lexicon WHERE kind>='near:' AND kind<'near;' LIMIT 1").fetchone() is None
        assert connection.execute("SELECT COUNT(*) FROM lexicon WHERE kind='near-v2'").fetchone()[0] < blocked
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute(
            "SELECT COUNT(*) FROM lexicon l WHERE l.documents!=(SELECT COUNT(*) FROM postings p WHERE p.term_id=l.id)"
        ).fetchone() == (0,)


def test_global_near_upgrade_rolls_back_and_retries(tmp_path, monkeypatch):
    root = str(tmp_path)
    activate(root)
    h.add_fact(root, "Component 7 requires a durable replicated journal")
    _restore_blocked_near_projection(root)
    with sqlite3.connect(fact_store.database_path(root)) as connection:
        before = _authoritative_fact_bytes(connection)
        terms = connection.execute("SELECT kind,term,fact_id,count FROM terms ORDER BY kind,term,fact_id").fetchall()

    class FailingMarker(fact_store._Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.lstrip().upper().startswith("INSERT") and "'near_postings'" in sql:
                raise sqlite3.OperationalError("projection marker fault")
            return super().execute(sql, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(fact_store, "_Connection", FailingMarker)
        with pytest.raises(sqlite3.OperationalError, match="projection marker fault"):
            h.load_facts(root)
    with sqlite3.connect(fact_store.database_path(root)) as connection:
        assert _authoritative_fact_bytes(connection) == before
        assert connection.execute("SELECT kind,term,fact_id,count FROM terms ORDER BY kind,term,fact_id").fetchall() == terms
        assert connection.execute("SELECT value FROM meta WHERE key='near_postings'").fetchone() is None
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='fact_block'").fetchone() is None
    assert len(h.load_facts(root)) == 1
    with fact_store.transaction(root) as facts:
        assert facts.connection.execute("SELECT value FROM meta WHERE key='near_postings'").fetchone() == ("global-v2",)


def test_global_near_upgrade_preserves_reader_snapshot_and_concurrent_openers(tmp_path):
    import threading

    root = str(tmp_path)
    activate(root)
    h.add_fact(root, "Component 7 requires a durable replicated journal")
    _restore_blocked_near_projection(root)
    reader = sqlite3.connect(fact_store.database_path(root), isolation_level=None)
    try:
        reader.execute("BEGIN")
        old_terms = reader.execute("SELECT * FROM terms ORDER BY kind,term,fact_id").fetchall()
        before = _authoritative_fact_bytes(reader)
        barrier = threading.Barrier(2)

        def open_projection(_):
            barrier.wait(timeout=5)
            connection = fact_store._connect(fact_store.database_path(root))
            try:
                return connection.execute("SELECT value FROM meta WHERE key='near_postings'").fetchone()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(open_projection, range(2))) == [("global-v2",)] * 2
        assert reader.execute("SELECT * FROM terms ORDER BY kind,term,fact_id").fetchall() == old_terms
        assert _authoritative_fact_bytes(reader) == before
        reader.rollback()
        assert reader.execute("SELECT value FROM meta WHERE key='near_postings'").fetchone() == ("global-v2",)
        assert _authoritative_fact_bytes(reader) == before
    finally:
        reader.close()


@pytest.mark.parametrize("scopes", [[], ["a"], ["b"], ["a", "b"]])
def test_global_near_matches_brute_force_across_blocks_and_scopes(tmp_path, monkeypatch, scopes):
    root = str(tmp_path)
    activate(root)
    monkeypatch.setenv("COMMONTRACE_FACT_CONFLICTS", "off")
    monkeypatch.setenv("COMMONTRACE_FACT_DEDUP", "exact")
    items = [
        {"statement": f"Component {n} requires a durable replicated journal", "scopes": ["a"]}
        for n in range(30)
    ] + [
        {"statement": "Component 7 requires durable replicated journal", "scopes": lane}
        for lane in ([], ["a"], ["b"])
    ] + [
        {"statement": "Component 7 does not require a durable replicated journal", "scopes": ["a"]},
        {"statement": "Component 7 needs a blue operator's testing platform", "scopes": ["a"]},
        {"statement": "Component 7 requires 9 durable replicated journal", "scopes": ["a"]},
        {"statement": "Component 9 requires 7 durable replicated journal", "scopes": ["a"]},
    ]
    h.add_facts(root, items)
    _restore_blocked_near_projection(root)
    query = "A replicated durable journal requires Component 7"
    with fact_store.transaction(root) as facts:
        expected = sorted(
            (fact for fact in facts.values() if fact.status == "active" and not fact.forgotten
             and h._scopes_compatible(frozenset(scopes), fact.scopes)
             and h.near_duplicate(query, fact.statement)),
            key=lambda fact: (-fact.confirmations, fact.id),
        )
        assert expected
        actual = fact_store.StatementIndex(facts).near(query, scopes)
        assert actual is not None and actual.id == expected[0].id


def test_global_near_rebuild_updates_removals_and_forgotten_candidates(tmp_path, monkeypatch):
    root = str(tmp_path)
    activate(root)
    monkeypatch.setenv("COMMONTRACE_FACT_CONFLICTS", "off")
    first, _ = h.add_fact(root, "Component 7 requires a durable replicated journal")
    second, _ = h.add_fact(root, "Component 7 records a durable replicated journal")
    _restore_blocked_near_projection(root)
    h.forget_fact(root, first.id)
    h.update_fact(root, second.id, statement="Component 9 records a revised durable journal")
    with fact_store.transaction(root, write=True) as facts:
        assert fact_store.StatementIndex(facts).near("A durable replicated journal requires Component 7", []) is None
        del facts[second.id]
    with fact_store.transaction(root) as facts:
        before = facts.connection.execute("SELECT kind,term,fact_id,count FROM terms ORDER BY kind,term,fact_id").fetchall()
        assert all(kind == "near-v2" for kind, _, _, _ in before if kind.startswith("near"))
    events = fact_store.events(root)
    fact_store.rebuild(root)
    with fact_store.transaction(root) as facts:
        assert facts.connection.execute("SELECT kind,term,fact_id,count FROM terms ORDER BY kind,term,fact_id").fetchall() == before
        assert facts.connection.execute(
            "SELECT 1 FROM lexicon l WHERE l.documents!=(SELECT COUNT(*) FROM postings p WHERE p.term_id=l.id) LIMIT 1"
        ).fetchone() is None
    assert fact_store.events(root) == events


def test_global_near_keeps_strongest_match_beyond_first_hundred(tmp_path, monkeypatch):
    root = str(tmp_path)
    activate(root)
    monkeypatch.setenv("COMMONTRACE_FACT_CONFLICTS", "off")
    monkeypatch.setenv("COMMONTRACE_FACT_DEDUP", "exact")
    query = "Component requires durable replicated journal storage"
    rows = h.add_facts(root, [
        {"statement": query + " tag" + chr(97 + n // 26) + chr(97 + n % 26)} for n in range(150)
    ])
    strongest = rows[-1][0]
    with h.mutate_facts(root) as facts:
        facts[strongest.id].confirmations = 99
    with fact_store.transaction(root) as facts:
        actual = fact_store.StatementIndex(facts).near(query, [])
        assert actual is not None and actual.id == strongest.id
    h.forget_fact(root, strongest.id)
    with fact_store.transaction(root) as facts:
        actual = fact_store.StatementIndex(facts).near(query, [])
        assert actual is not None and actual.id != strongest.id
