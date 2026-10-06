"""Bounded legacy backfills and indexed repairs retain exact temporal evidence."""
import sqlite3

import pytest

from commontrace.conversation import profile
from commontrace.conversation import store as store_module
from commontrace.conversation.store import Store, fact_hash, write_txn


def test_legacy_backfill_streams_turns_and_bounds_fact_batches(tmp_path, monkeypatch):
    with Store(str(tmp_path), "legacy") as store:
        store.add("s", [{"speaker": "Ana", "text": "I live in Paris."}], session_at="2024-01-01")
        with write_txn(store.db):
            store.db.executemany(
                "INSERT INTO facts (id, turn, kind, subject, statement, owner, slot, at) "
                "VALUES (?, 1, 'identity', 'home', ?, 'ana', 'home', ?)",
                [(i, f"Residence update {i}", "2024-01-01T00:00")
                 for i in [-2**63, -1, 0, *range(2, 604), 2**63 - 1]])
            store.db.execute("UPDATE facts SET statement_hash=NULL")
            store.db.execute("DELETE FROM entities")
            store.db.execute("DELETE FROM meta WHERE key IN ('entities', 'belief_chains')")
        expected_entities = profile.entities(store.session_turns("s")[0].text)

    batches = []

    class GuardedCursor(sqlite3.Cursor):
        def execute(self, sql, parameters=()):
            self.sql = sql
            return super().execute(sql, parameters)

        def fetchall(self):
            assert self.sql != "SELECT id, text FROM turns", "raw log was materialized"
            rows = super().fetchall()
            if self.sql.startswith("SELECT id, statement FROM facts"):
                assert "LIMIT 256" in self.sql
                batches.append(len(rows))
                assert len(rows) <= 256
            return rows

    class GuardedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            return self.cursor(factory=GuardedCursor).execute(sql, parameters)

    connect = sqlite3.connect
    monkeypatch.setattr(store_module.sqlite3, "connect",
                        lambda *args, **kw: connect(*args, factory=GuardedConnection, **kw))
    with Store(str(tmp_path), "legacy") as upgraded:
        facts = upgraded.facts(history=True)
        assert len(facts) == 607
        assert all(f["statement_hash"] == fact_hash(f["statement"]) for f in facts)
        assert batches == [256, 256, 95, 0]
        assert {r[0] for r in upgraded.db.execute("SELECT name FROM entities")} == set(expected_entities)
        assert upgraded.facts()[0]["id"] == 2**63 - 1
        assert upgraded.fact_evidence(1)[0]["text"] == "I live in Paris."
        before = upgraded.facts(history=True)
    with Store(str(tmp_path), "legacy") as reopened:
        assert reopened.facts(history=True) == before
        assert batches == [256, 256, 95, 0], "completed backfill ran again"


def test_chain_repair_stages_order_and_does_not_write_unchanged_beliefs(tmp_path):
    with Store(str(tmp_path), "people") as store:
        for session, at, city in [("late", "2025-01-01", "Paris"),
                                   ("early", None, "Oslo"),
                                   ("same-date", "2025-01-01", "Rome"),
                                   ("middle", "2024-01-01", "London")]:
            store.add(session, [{"speaker": "Ana", "text": f"I live in {city}."}], session_at=at)
        store.add("ben", [{"speaker": "Ben", "text": "I live in Boston."}], session_at="2024-01-01")
        before = store.facts(history=True)
        store.db.execute("CREATE TEMP TRIGGER forbid_noop BEFORE UPDATE ON facts "
                         "WHEN old.superseded_by IS new.superseded_by "
                         "BEGIN SELECT RAISE(ABORT, 'unchanged belief written'); END")
        with write_txn(store.db):
            store._reinstate()
        assert store.facts(history=True) == before
        store.db.execute("UPDATE facts SET superseded_by=NULL WHERE owner='ana' AND superseded_by IS NOT NULL")
        store.db.execute("CREATE TEMP TRIGGER preserve_ben BEFORE UPDATE ON facts WHEN old.owner='ben' "
                         "BEGIN SELECT RAISE(ABORT, 'unrelated belief written'); END")
        with write_txn(store.db):
            store._reinstate([("ana", "home"), ("ana", "home")])
        assert store.facts(history=True) == before
        assert not store.db.execute("SELECT 1 FROM sqlite_temp_master "
                                    "WHERE name='commontrace_belief_successors'").fetchone()


def test_failed_chain_repair_rolls_back_and_releases_staging_table(tmp_path):
    with Store(str(tmp_path), "people") as store:
        store.add("old", [{"text": "I live in Oslo."}], session_at="2023-01-01")
        store.add("new", [{"text": "I live in Paris."}], session_at="2025-01-01")
        before = store.facts(history=True)
        store.db.execute("CREATE TEMP TRIGGER fail_repair BEFORE UPDATE ON facts "
                         "BEGIN SELECT RAISE(ABORT, 'repair rejected'); END")
        with pytest.raises(sqlite3.IntegrityError, match="repair rejected"):
            store.delete_session("new")
        assert store.facts(history=True) == before
        assert not store.db.execute("SELECT 1 FROM sqlite_temp_master "
                                    "WHERE name='commontrace_belief_successors'").fetchone()
        store.db.execute("DROP TRIGGER fail_repair")
        assert store.delete_session("new") == 1
        assert store.facts()[0]["statement"] == "I live in Oslo."
