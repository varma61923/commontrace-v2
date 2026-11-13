"""Failed commits and cleanup errors cannot retain uncommitted state or handles."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from unimem.conversation import AsyncStore, Store
from unimem.conversation import store as store_module
from unimem.conversation.store import write_txn


def test_failed_deferred_commit_rolls_back_before_reusing_connection():
    db = sqlite3.connect(":memory:", isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE child(id REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")
        with pytest.raises(sqlite3.IntegrityError), write_txn(db):
            db.execute("INSERT INTO child VALUES(1)")
        assert not db.in_transaction
        assert db.execute("SELECT count(*) FROM child").fetchone()[0] == 0
        with write_txn(db):
            db.execute("INSERT INTO parent VALUES(1)")
            db.execute("INSERT INTO child VALUES(1)")
        assert not db.in_transaction
        assert db.execute("SELECT count(*) FROM child").fetchone()[0] == 1
    finally:
        db.close()


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_transaction_preserves_original_error_after_rollback_failure_or_auto_rollback(rollback_fails):
    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if rollback_fails and sql == "ROLLBACK":
                raise sqlite3.OperationalError("injected rollback failure")
            return super().execute(sql, parameters)

    db = sqlite3.connect(":memory:", isolation_level=None, factory=Connection)
    try:
        with pytest.raises(ValueError, match="original failure"), write_txn(db):
            if not rollback_fails:
                db.execute("ROLLBACK")  # SQLite may also roll back on an I/O failure.
            raise ValueError("original failure")
        if rollback_fails:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                db.execute("SELECT 1")
        else:
            assert not db.in_transaction
    finally:
        db.close()


@pytest.mark.parametrize("read_only, setup", [(False, ""), (True, ""), (False, "invalid SQL")])
def test_connection_setup_failure_closes_handle(tmp_path, monkeypatch, read_only, setup):
    created = []
    original = sqlite3.connect

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if not setup and sql.startswith("PRAGMA"):
                raise sqlite3.OperationalError("injected setup failure")
            return super().execute(sql, parameters)

    def connect(*args, **kwargs):
        db = original(*args, **kwargs, factory=Connection)
        created.append(db)
        return db

    path = tmp_path / "store.db"
    path.touch()
    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError):
        store_module.connect(str(path), setup, read_only=read_only)
    assert len(created) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        created[0].execute("SELECT 1")


@pytest.mark.parametrize("read_only", [False, True])
def test_failed_store_initialization_closes_handle(tmp_path, monkeypatch, read_only):
    with Store(str(tmp_path), "resources"):
        pass
    created = []
    original = store_module.connect

    def connect(*args, **kwargs):
        db = original(*args, **kwargs)
        created.append(db)
        return db

    def fail(_self):
        raise RuntimeError("injected schema inspection failure")

    monkeypatch.setattr(store_module, "connect", connect)
    monkeypatch.setattr(Store, "_inspect_schema", fail)
    with pytest.raises(RuntimeError, match="schema inspection"):
        Store(str(tmp_path), "resources", read_only=read_only)
    assert len(created) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        created[0].execute("SELECT 1")


def test_provider_close_failure_does_not_leak_database_or_other_providers(tmp_path):
    closed = []

    class Provider:
        def __init__(self, name, fails=False):
            self.name, self.fails = name, fails

        def close(self):
            closed.append(self.name)
            if self.fails:
                raise RuntimeError("injected provider close failure")

    store = Store(str(tmp_path), "resources")
    store._embedders = {"first": Provider("first"), "last": Provider("last", fails=True)}
    with pytest.raises(RuntimeError, match="provider close"):
        store.close()
    assert sorted(closed) == ["first", "last"]
    assert not store._embedders
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        store.db.execute("SELECT 1")
    store.close()  # Cleanup remains idempotent after a provider failure.


def test_async_store_recovers_after_a_shared_commit_failure(tmp_path, monkeypatch):
    original = Store.add

    def add(self, session, *args, **kwargs):
        result = original(self, session, *args, **kwargs)
        if session == "bad":
            self.db.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
            self.db.execute("CREATE TABLE child(id REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")
            self.db.execute("INSERT INTO child VALUES(1)")
        return result

    monkeypatch.setattr(Store, "add", add)

    async def run():
        async with await AsyncStore.open(str(tmp_path), "commit") as store:
            with pytest.raises(sqlite3.IntegrityError):
                await store.add("bad", [{"text": "Rejected evidence"}])
            assert (await store.stats())["turns"] == 0
            await store.add("good", [{"text": "Committed evidence"}])
            assert (await store.stats())["turns"] == 1
            with Store(str(tmp_path), "commit", read_only=True) as reader:
                assert reader.stats()["turns"] == 1
                assert not reader.session_turns("bad")

    asyncio.run(run())


def test_async_close_shuts_down_executor_even_when_store_cleanup_fails(tmp_path, monkeypatch):
    original = Store.close

    def close(self):
        original(self)
        raise RuntimeError("injected cleanup failure")

    async def run():
        store = await AsyncStore.open(str(tmp_path), "close")
        monkeypatch.setattr(Store, "close", close)
        with pytest.raises(RuntimeError, match="cleanup failure"):
            await store.close()
        with pytest.raises(RuntimeError, match="shutdown"):
            store._executor.submit(lambda: None)

    asyncio.run(run())
