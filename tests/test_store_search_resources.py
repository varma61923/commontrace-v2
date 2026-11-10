"""Literal search semantics and deterministic SQLite descriptor ownership."""

import sqlite3

import pytest

from unimem.store import FilesStore, SQLiteStore


class OrderedStore(SQLiteStore):
    def all(self):
        return sorted(super().all(), key=lambda row: row["id"])


@pytest.fixture
def store(tmp_path):
    value = OrderedStore(str(tmp_path))
    value.append({"id": "a", "text": "plain"})
    value.append({"id": "z", "text": "50%_off Straße"})
    value.rebuild()
    return value


@pytest.mark.parametrize("query", ["%", "_", "STRASSE", "straße", "50%_off"])
def test_search_matches_literal_unicode_substrings_before_limit(store, query):
    assert [row["id"] for row in store.search(query, limit=1)] == ["z"]


@pytest.mark.parametrize("limit", [-1, True, 1.5])
def test_invalid_limits_are_rejected_without_creating_an_index(tmp_path, limit):
    value = SQLiteStore(str(tmp_path))
    with pytest.raises(ValueError, match="limit"):
        value.search("plain", limit=limit)
    assert not tmp_path.joinpath(value.index).exists()


def test_zero_limit_never_reads_or_rebuilds(tmp_path, monkeypatch):
    value = SQLiteStore(str(tmp_path))

    def forbidden():
        pytest.fail("a zero-result search must not scan records")

    monkeypatch.setattr(value, "all", forbidden)
    assert value.search("anything", limit=0) == []


@pytest.mark.parametrize("writer_type", [FilesStore, SQLiteStore])
def test_search_refreshes_after_append_and_text_revision(store, writer_type):
    writer = writer_type(store.root)
    writer.append({"id": "new", "text": "new searchable phrase"})
    assert [row["id"] for row in store.search("searchable")] == ["new"]
    writer.append({"id": "a", "text": "replacement wording"})
    assert [row["id"] for row in store.search("replacement")] == ["a"]
    assert store.search("plain") == []
    assert SQLiteStore(store.root).search("replacement")[0]["id"] == "a"


def test_warm_search_does_not_rewrite_an_unchanged_index(store, monkeypatch):
    original = store._synchronize
    changed = []

    def synchronize(db, records, **kwargs):
        before = db.total_changes
        original(db, records, **kwargs)
        changed.append(db.total_changes - before)

    monkeypatch.setattr(store, "_synchronize", synchronize)
    assert store.search("plain") and store.search("plain")
    assert changed == [0, 0]


@pytest.mark.parametrize("operation", ["search", "rebuild"])
@pytest.mark.parametrize("fail", [False, True])
def test_connections_close_on_success_and_failure(store, monkeypatch, operation, fail):
    original = sqlite3.connect
    connections = []

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if fail and operation == "search" and sql.startswith("SELECT id"):
                raise sqlite3.OperationalError("injected search failure")
            return super().execute(sql, parameters)

        def executemany(self, sql, parameters, /):
            if fail and operation == "rebuild":
                raise sqlite3.OperationalError("injected rebuild failure")
            return super().executemany(sql, parameters)

    def connect(path, *args, **kwargs):
        if str(path) == store.index:
            kwargs["factory"] = Connection
            connection = original(path, *args, **kwargs)
            connections.append(connection)
            return connection
        return original(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    if fail:
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            getattr(store, operation)(*(["plain"] if operation == "search" else []))
    else:
        getattr(store, operation)(*(["plain"] if operation == "search" else []))
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
