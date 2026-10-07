"""SQLite MVCC vector snapshots, fenced publication and bounded reclamation."""
from __future__ import annotations

import heapq
import json
import math
import secrets
import sqlite3
import struct
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from typing import Protocol, TypeVar

from commontrace.conversation.store import write_txn
from commontrace.vector_snapshots import (
    BuildLease,
    ReadLease,
    SnapshotConflict,
    SnapshotHead,
    lease_duration,
    snapshot_deletes,
    snapshot_records,
    target_revision,
)
from commontrace.vector_store import VectorHit, VectorRecord, _generation, _limit, _Scope, _vector

T = TypeVar("T")
# B608 annotations below cover only concatenation of these fixed SQL literals.
# Scope, tokens, revisions, generations, filters and limits remain parameters.
PREDICATE = "tenant=? AND namespace=? AND model=? AND dimension=?"
DB_NOW = "(julianday('now')-2440587.5)*86400.0"


class Runner(Protocol):
    def __call__(self, function: Callable[[], T]) -> Awaitable[T]: ...


_DDL = (
    "CREATE TABLE IF NOT EXISTS commontrace_snapshot_heads ("
    "tenant TEXT, namespace TEXT, model TEXT, dimension INTEGER, revision INTEGER NOT NULL DEFAULT -1, "
    "generation TEXT, fence INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(tenant,namespace,model,dimension)) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS commontrace_snapshot_builds ("
    "tenant TEXT, namespace TEXT, model TEXT, dimension INTEGER, token TEXT, fence INTEGER NOT NULL, "
    "base INTEGER NOT NULL, target INTEGER NOT NULL, generation TEXT NOT NULL, full INTEGER NOT NULL, "
    "duration REAL NOT NULL, expires REAL NOT NULL, PRIMARY KEY(tenant,namespace,model,dimension,token)) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS commontrace_snapshot_stage ("
    "tenant TEXT, namespace TEXT, model TEXT, dimension INTEGER, token TEXT, key TEXT, "
    "deleted INTEGER NOT NULL, embedding BLOB, checksum TEXT, "
    "PRIMARY KEY(tenant,namespace,model,dimension,token,key)) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS commontrace_snapshot_generations ("
    "tenant TEXT, namespace TEXT, model TEXT, dimension INTEGER, generation TEXT, revision INTEGER NOT NULL, "
    "PRIMARY KEY(tenant,namespace,model,dimension,generation)) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS commontrace_snapshot_readers ("
    "tenant TEXT, namespace TEXT, model TEXT, dimension INTEGER, token TEXT, revision INTEGER NOT NULL, "
    "generation TEXT NOT NULL, duration REAL NOT NULL, expires REAL NOT NULL, "
    "PRIMARY KEY(tenant,namespace,model,dimension,token)) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS commontrace_snapshot_versions ("
    "tenant TEXT, namespace TEXT, model TEXT, dimension INTEGER, key TEXT, valid_from INTEGER, valid_to INTEGER, "
    "embedding BLOB NOT NULL, checksum TEXT NOT NULL, "
    "PRIMARY KEY(tenant,namespace,model,dimension,key,valid_from)) WITHOUT ROWID",
    "CREATE INDEX IF NOT EXISTS commontrace_snapshot_live ON commontrace_snapshot_versions "
    "(tenant,namespace,model,dimension,valid_to,key)",
    "CREATE UNIQUE INDEX IF NOT EXISTS commontrace_snapshot_one_live ON commontrace_snapshot_versions "
    "(tenant,namespace,model,dimension,key) WHERE valid_to IS NULL",
)


class SQLiteSnapshots:
    """Version intervals prevent private builds or stale writers changing reads.

    Every mutation runs under SQLite's cross-process write transaction. A build
    token is renewed only while it is live; publication compares its base with
    the durable head. A reader pin protects its exact revision. Reclamation
    uses database time and at most ``limit`` vector/staging rows per call.
    """

    def __init__(self, db: sqlite3.Connection, scope: tuple[str, str, str, int], run: Runner) -> None:
        self._db, self._scope, self._run = db, _Scope(*scope).sql(), run

    async def initialize(self) -> None:
        def execute() -> None:
            with write_txn(self._db):
                for statement in _DDL:
                    self._db.execute(statement)
                self._db.execute("INSERT OR IGNORE INTO commontrace_snapshot_heads "
                                 "(tenant,namespace,model,dimension) VALUES(?,?,?,?)", self._scope)
        await self._run(execute)

    def _head(self) -> SnapshotHead:
        row = self._db.execute("SELECT revision,generation FROM commontrace_snapshot_heads WHERE "  # nosec B608
                               + PREDICATE, self._scope).fetchone()
        if row is None:
            raise SnapshotConflict("snapshot scope was not initialized")
        return SnapshotHead(int(row[0]), row[1])

    async def head(self) -> SnapshotHead:
        return await self._run(self._head)

    async def begin(self, revision: int, generation: str, *, full: bool = False,
                    lease_seconds: float = 60) -> BuildLease | None:
        if not isinstance(full, bool):
            raise ValueError("full must be a boolean")
        target_revision(revision, generation)
        duration = lease_duration(lease_seconds)
        def execute() -> BuildLease | None:
            with write_txn(self._db):
                head = self._head()
                if head.revision >= revision:
                    if head.revision == revision and head.generation != generation:
                        raise SnapshotConflict("revision is already bound to a different generation")
                    return None
                self._db.execute("UPDATE commontrace_snapshot_heads SET fence=fence+1 WHERE "  # nosec B608
                                 + PREDICATE, self._scope)
                fence = self._db.execute("SELECT fence FROM commontrace_snapshot_heads WHERE "  # nosec B608
                                        + PREDICATE, self._scope).fetchone()[0]
                token = secrets.token_hex(16)
                self._db.execute("INSERT INTO commontrace_snapshot_builds VALUES(?,?,?,?,?,?,?,?,?,?,?,"  # nosec B608
                                 + DB_NOW + "+?)", (*self._scope, token, fence, head.revision, revision,
                                                    generation, int(full), duration, duration))
                return BuildLease(token, fence, head.revision, revision, generation, full)
        return await self._run(execute)

    def _build(self, lease: BuildLease) -> None:
        found = self._db.execute("SELECT fence,base,target,generation,full FROM commontrace_snapshot_builds WHERE "  # nosec B608
                                + PREDICATE + " AND token=? AND expires>" + DB_NOW,
                                (*self._scope, lease.token)).fetchone()
        if found is None or tuple(found) != (lease.fence, lease.base_revision, lease.target_revision,
                                            lease.generation, int(lease.full)):
            raise SnapshotConflict("build lease is expired, revoked or belongs to another scope")

    async def stage(self, lease: BuildLease, records: Iterable[VectorRecord],
                    deleted_keys: Iterable[str] = ()) -> int:
        def execute() -> int:
            rows = snapshot_records(records, self._scope[3])
            deleted = snapshot_deletes(deleted_keys)
            if set(deleted).intersection(key for key, _vector, _checksum in rows):
                raise ValueError("a staged batch cannot both write and delete the same key")
            with write_txn(self._db):
                self._build(lease)
                self._db.executemany("INSERT INTO commontrace_snapshot_stage VALUES(?,?,?,?,?,?,?,?,?) "
                                     "ON CONFLICT(tenant,namespace,model,dimension,token,key) DO UPDATE SET "
                                     "deleted=excluded.deleted,embedding=excluded.embedding,checksum=excluded.checksum",
                                     [(*self._scope, lease.token, key, 0,
                                       struct.pack(f"<{self._scope[3]}f", *vector), checksum)
                                      for key, vector, checksum in rows]
                                     + [(*self._scope, lease.token, key, 1, None, None) for key in deleted])
                self._build(lease)  # A long batch cannot resurrect an expired builder.
                self._db.execute("UPDATE commontrace_snapshot_builds SET expires=" + DB_NOW  # nosec B608
                                 + "+duration WHERE " + PREDICATE + " AND token=?", (*self._scope, lease.token))
            return len(rows) + len(deleted)
        return await self._run(execute)

    async def publish(self, lease: BuildLease) -> bool:
        def execute() -> bool:
            with write_txn(self._db):
                try:
                    self._build(lease)
                except SnapshotConflict:
                    return False
                if self._head().revision != lease.base_revision:
                    return False
                if self._db.execute("SELECT 1 FROM commontrace_snapshot_generations WHERE " + PREDICATE  # nosec B608
                                    + " AND generation=?", (*self._scope, lease.generation)).fetchone():
                    return False
                params = (*self._scope, lease.token)
                # Close only changed/deleted versions; equal checksums preserve
                # their original interval and vector without a rewrite.
                self._db.execute("UPDATE commontrace_snapshot_versions AS v SET valid_to=? WHERE "
                                 + PREDICATE + " AND valid_to IS NULL AND EXISTS(SELECT 1 FROM "  # nosec B608
                                 "commontrace_snapshot_stage s WHERE s.tenant=v.tenant AND s.namespace=v.namespace "
                                 "AND s.model=v.model AND s.dimension=v.dimension AND s.token=? AND s.key=v.key "
                                 "AND (s.deleted=1 OR s.checksum<>v.checksum))",
                                 (lease.target_revision, *self._scope, lease.token))
                if lease.full:
                    self._db.execute("UPDATE commontrace_snapshot_versions AS v SET valid_to=? WHERE "
                                     + PREDICATE + " AND valid_to IS NULL AND NOT EXISTS(SELECT 1 FROM "  # nosec B608
                                     "commontrace_snapshot_stage s WHERE s.tenant=v.tenant AND s.namespace=v.namespace "
                                     "AND s.model=v.model AND s.dimension=v.dimension AND s.token=? "
                                     "AND s.key=v.key AND s.deleted=0)",
                                     (lease.target_revision, *self._scope, lease.token))
                self._db.execute("INSERT INTO commontrace_snapshot_versions "
                                 "SELECT s.tenant,s.namespace,s.model,s.dimension,s.key,?,NULL,s.embedding,s.checksum "
                                 "FROM commontrace_snapshot_stage s WHERE "
                                 + "s.tenant=? AND s.namespace=? AND s.model=? AND s.dimension=? AND s.token=? "  # nosec B608
                                 "AND s.deleted=0 AND NOT EXISTS(SELECT 1 FROM commontrace_snapshot_versions v "
                                 "WHERE v.tenant=s.tenant AND v.namespace=s.namespace AND v.model=s.model "
                                 "AND v.dimension=s.dimension AND v.key=s.key AND v.valid_to IS NULL)",
                                 (lease.target_revision, *params))
                self._db.execute("INSERT INTO commontrace_snapshot_generations VALUES(?,?,?,?,?,?)",
                                 (*self._scope, lease.generation, lease.target_revision))
                self._db.execute("UPDATE commontrace_snapshot_heads SET revision=?,generation=? WHERE "  # nosec B608
                                 + PREDICATE, (lease.target_revision, lease.generation, *self._scope))
                self._db.execute("DELETE FROM commontrace_snapshot_builds WHERE " + PREDICATE + " AND token=?",  # nosec B608
                                 params)
                self._db.execute("DELETE FROM commontrace_snapshot_stage WHERE " + PREDICATE + " AND token=?",  # nosec B608
                                 params)
                return True
        return await self._run(execute)

    async def abort(self, lease: BuildLease) -> None:
        def execute() -> None:
            with write_txn(self._db):
                removed = self._db.execute(
                    "DELETE FROM commontrace_snapshot_builds WHERE " + PREDICATE  # nosec B608
                    + " AND token=? AND fence=? AND base=? AND target=? AND generation=? AND full=?",
                    (*self._scope, lease.token, lease.fence, lease.base_revision, lease.target_revision,
                     lease.generation, int(lease.full))).rowcount
                if removed:
                    self._db.execute("DELETE FROM commontrace_snapshot_stage WHERE " + PREDICATE + " AND token=?",  # nosec B608
                                     (*self._scope, lease.token))
        await self._run(execute)

    async def pin(self, generation: str, *, lease_seconds: float = 60) -> ReadLease:
        duration, wanted = lease_duration(lease_seconds), _generation(generation)
        def execute() -> ReadLease:
            with write_txn(self._db):
                found = self._db.execute("SELECT revision FROM commontrace_snapshot_generations WHERE "  # nosec B608
                                        + PREDICATE + " AND generation=?", (*self._scope, wanted)).fetchone()
                if found is None:
                    raise SnapshotConflict("snapshot is not committed or has been reclaimed")
                token = secrets.token_hex(16)
                self._db.execute("INSERT INTO commontrace_snapshot_readers VALUES(?,?,?,?,?,?,?,?,"  # nosec B608
                                 + DB_NOW + "+?)", (*self._scope, token, found[0], wanted, duration, duration))
                return ReadLease(token, found[0], wanted)
        return await self._run(execute)

    def _reader(self, lease: ReadLease) -> None:
        found = self._db.execute("SELECT revision,generation FROM commontrace_snapshot_readers WHERE "  # nosec B608
                                + PREDICATE + " AND token=? AND expires>" + DB_NOW,
                                (*self._scope, lease.token)).fetchone()
        if found is None or tuple(found) != (lease.revision, lease.generation):
            raise SnapshotConflict("read lease is expired, revoked or belongs to another scope")

    async def renew(self, lease: ReadLease) -> None:
        def execute() -> None:
            with write_txn(self._db):
                self._reader(lease)
                self._db.execute("UPDATE commontrace_snapshot_readers SET expires=" + DB_NOW  # nosec B608
                                 + "+duration WHERE " + PREDICATE + " AND token=?", (*self._scope, lease.token))
        await self._run(execute)

    async def release(self, lease: ReadLease) -> None:
        def execute() -> None:
            with write_txn(self._db):
                self._db.execute("DELETE FROM commontrace_snapshot_readers WHERE " + PREDICATE  # nosec B608
                                 + " AND token=? AND revision=? AND generation=?",
                                 (*self._scope, lease.token, lease.revision, lease.generation))
        await self._run(execute)

    async def search(self, lease: ReadLease, vector: Sequence[float], *, top_k: int = 10,
                     allowed_ids: Sequence[str] | None = None) -> list[VectorHit]:
        def execute() -> list[VectorHit]:
            _limit(top_k)
            query = _vector(vector, self._scope[3])
            ids = snapshot_deletes(allowed_ids) if allowed_ids is not None else None
            # A read transaction pins SQLite's own MVCC pages while validating
            # the logical lease and scanning; another process can safely GC.
            self._db.execute("BEGIN")
            try:
                self._reader(lease)
                statement = ("SELECT key,embedding FROM commontrace_snapshot_versions WHERE " + PREDICATE  # nosec B608
                    + " AND valid_from<=? AND (valid_to IS NULL OR valid_to>?)")
                params: tuple[object, ...] = (*self._scope, lease.revision, lease.revision)
                if ids is not None:
                    statement += " AND key IN (SELECT value FROM json_each(?))"
                    params += (json.dumps(ids),)
                cursor = self._db.execute(statement, params)
                def scores() -> Iterator[VectorHit]:
                    while rows := cursor.fetchmany(256):
                        for key, blob in rows:
                            if len(blob) != self._scope[3] * 4:
                                raise ValueError("stored vector dimension is corrupt")
                            stored = struct.unpack(f"<{self._scope[3]}f", blob)
                            norm = math.hypot(*stored) * math.hypot(*query)
                            if not norm or not math.isfinite(norm):
                                raise ValueError("stored vector is corrupt")
                            score = math.fsum(a * b for a, b in zip(stored, query)) / norm
                            yield VectorHit(key, min(1.0, max(-1.0, score)))
                try:
                    result = heapq.nsmallest(top_k, scores(), key=lambda hit: (-hit.score, hit.key))
                finally:
                    cursor.close()
                self._db.execute("COMMIT")
                return result
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return await self._run(execute)

    async def collect_garbage(self, *, limit: int = 1000) -> int:
        _limit(limit)
        if not limit:
            return 0
        def execute() -> int:
            with write_txn(self._db):
                # Table identifiers come exclusively from this internal literal allowlist.
                for table in ("commontrace_snapshot_readers", "commontrace_snapshot_builds"):
                    self._db.execute("DELETE FROM " + table + " WHERE " + PREDICATE + " AND token IN "  # nosec B608
                                     "(SELECT token FROM " + table + " WHERE " + PREDICATE + " AND expires<="
                                     + DB_NOW + " LIMIT ?)", (*self._scope, *self._scope, limit))
                self._db.execute("DELETE FROM commontrace_snapshot_generations WHERE " + PREDICATE  # nosec B608
                                 + " AND generation IN (SELECT g.generation FROM commontrace_snapshot_generations g "
                                 "JOIN commontrace_snapshot_heads h ON h.tenant=g.tenant AND h.namespace=g.namespace "
                                 "AND h.model=g.model AND h.dimension=g.dimension WHERE g.tenant=? AND g.namespace=? "
                                 "AND g.model=? AND g.dimension=? AND g.generation<>h.generation "
                                 "AND NOT EXISTS(SELECT 1 FROM commontrace_snapshot_readers r WHERE r.tenant=g.tenant "
                                 "AND r.namespace=g.namespace AND r.model=g.model AND r.dimension=g.dimension "
                                 "AND r.generation=g.generation) LIMIT ?)",
                                 (*self._scope, *self._scope, limit))
                removed = self._db.execute("DELETE FROM commontrace_snapshot_stage WHERE " + PREDICATE  # nosec B608
                                          + " AND (token,key) IN (SELECT s.token,s.key "
                                          "FROM commontrace_snapshot_stage s "
                                          "WHERE s.tenant=? AND s.namespace=? AND s.model=? AND s.dimension=? "
                                          "AND NOT EXISTS(SELECT 1 FROM commontrace_snapshot_builds b WHERE "
                                          "b.tenant=s.tenant AND b.namespace=s.namespace AND b.model=s.model "
                                          "AND b.dimension=s.dimension AND b.token=s.token AND b.expires>" + DB_NOW
                                          + ") LIMIT ?)", (*self._scope, *self._scope, limit)).rowcount
                remaining = max(0, limit - removed)
                if remaining:
                    removed += self._db.execute(
                        "DELETE FROM commontrace_snapshot_versions WHERE " + PREDICATE  # nosec B608
                        + " AND (key,valid_from) IN (SELECT v.key,v.valid_from FROM commontrace_snapshot_versions v "
                        "WHERE v.tenant=? AND v.namespace=? AND v.model=? AND v.dimension=? AND v.valid_to IS NOT NULL "
                        "AND NOT EXISTS(SELECT 1 FROM commontrace_snapshot_generations g WHERE g.tenant=v.tenant "
                        "AND g.namespace=v.namespace AND g.model=v.model AND g.dimension=v.dimension "
                        "AND v.valid_from<=g.revision AND v.valid_to>g.revision) "
                        "AND NOT EXISTS(SELECT 1 FROM commontrace_snapshot_readers r WHERE r.tenant=v.tenant "
                        "AND r.namespace=v.namespace AND r.model=v.model AND r.dimension=v.dimension "
                        "AND v.valid_from<=r.revision AND v.valid_to>r.revision) "
                        "AND NOT EXISTS(SELECT 1 FROM commontrace_snapshot_builds b WHERE b.tenant=v.tenant "
                        "AND b.namespace=v.namespace AND b.model=v.model AND b.dimension=v.dimension "
                        "AND b.expires>" + DB_NOW + " AND v.valid_from<=b.base AND v.valid_to>b.base) LIMIT ?)",
                        (*self._scope, *self._scope, remaining)).rowcount
                return removed
        return await self._run(execute)
