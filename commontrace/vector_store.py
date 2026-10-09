"""Owner-scoped optional vector engines for canonical conversation retrieval.

SQLite performs exact cosine search using stdlib float32 blobs. PostgreSQL uses
an explicitly configured asyncpg pool and pgvector, with opt-in HNSW approximate
search. Engines retain vector IDs, not untrusted instruction text. The caller
owns engine lifetime; applications authenticate before choosing a tenant.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import math
import os
import sqlite3
import struct
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from commontrace.async_workers import STORE_WORKERS
from commontrace.conversation.store import connect, write_txn

if TYPE_CHECKING:
    from commontrace.vector_snapshots import SnapshotBackend

MAX_BATCH = 10_000
MAX_DIMENSION = 2000  # pgvector HNSW's vector type limit
T = TypeVar("T")


@dataclass(frozen=True)
class VectorRecord:
    key: str
    vector: Sequence[float]
    generation: str = ""
    checksum: str = ""


@dataclass(frozen=True)
class VectorHit:
    key: str
    score: float


class VectorIndex(Protocol):
    """A pinned owner/namespace/model/dimension; callers cannot override scope."""

    @property
    def tenant(self) -> str: ...
    @property
    def namespace(self) -> str: ...
    @property
    def model(self) -> str: ...
    @property
    def dimension(self) -> int: ...
    async def upsert(self, records: Iterable[VectorRecord]) -> int: ...
    async def bind_source(self, identity: str) -> None: ...
    async def delete(self, keys: Iterable[str]) -> int: ...
    async def prune(self, generation: str) -> int: ...
    async def search(self, vector: Sequence[float], *, top_k: int = 10,
                     generation: str | None = None, allowed_ids: Sequence[str] | None = None) -> list[VectorHit]: ...
    async def close(self) -> None: ...


async def _published_search(snapshots: SnapshotBackend, vector: Sequence[float], *, top_k: int,
                            generation: str | None, allowed_ids: Sequence[str] | None) -> list[VectorHit] | None:
    """Read published MVCC data when present; preserve independent legacy CRUD."""
    from commontrace.vector_snapshots import SnapshotConflict

    head = await snapshots.head()
    wanted = generation if generation is not None else head.generation
    if wanted is None:
        return None
    for _attempt in range(3):
        try:
            read = await snapshots.pin(wanted)
        except SnapshotConflict:
            if generation is not None:
                return None  # This can be a legacy upsert generation.
            head = await snapshots.head()
            assert head.generation is not None
            wanted = head.generation
            continue
        try:
            return await snapshots.search(read, vector, top_k=top_k, allowed_ids=allowed_ids)
        finally:
            await asyncio.shield(snapshots.release(read))
    raise SnapshotConflict("published snapshot changed repeatedly; retry search")


def _label(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} must contain 1-512 non-control characters")
    return value


def _generation(value: str) -> str:
    if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise ValueError("generation must contain at most 512 non-control characters")
    return value


@dataclass(frozen=True)
class _Scope:
    tenant: str
    namespace: str
    model: str
    dimension: int

    def __post_init__(self) -> None:
        for name in ("tenant", "namespace", "model"):
            _label(getattr(self, name), name)
        if isinstance(self.dimension, bool) or not isinstance(self.dimension, int) \
                or not 1 <= self.dimension <= MAX_DIMENSION:
            raise ValueError(f"dimension must be an integer from 1 to {MAX_DIMENSION}")

    def sql(self) -> tuple[str, str, str, int]:
        return self.tenant, self.namespace, self.model, self.dimension


def _vector(values: Sequence[float], dimension: int) -> tuple[float, ...]:
    if len(values) != dimension:
        raise ValueError(f"expected {dimension} vector dimensions, received {len(values)}")
    try:
        numbers = tuple(float(v) for v in values)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("vector components must be finite numbers") from exc
    if not all(math.isfinite(v) for v in numbers):
        raise ValueError("vector components must be finite numbers")
    norm = math.hypot(*numbers)
    if not norm or not math.isfinite(norm):
        raise ValueError("vector norm must be finite and nonzero")
    # Both engines store the same float32 unit vectors and score by cosine.
    return tuple(struct.unpack("<f", struct.pack("<f", v / norm))[0] for v in numbers)


def _records(records: Iterable[VectorRecord], dimension: int) -> list[tuple[str, tuple[float, ...], str]]:
    rows: list[tuple[str, tuple[float, ...], str]] = []
    for record in records:
        if len(rows) >= MAX_BATCH:
            raise ValueError(f"one vector batch may contain at most {MAX_BATCH} records")
        rows.append((_label(record.key, "key"), _vector(record.vector, dimension), _generation(record.generation)))
    return rows


def _keys(keys: Iterable[str]) -> list[str]:
    result: list[str] = []
    for key in keys:
        if len(result) >= MAX_BATCH:
            raise ValueError(f"one key batch may contain at most {MAX_BATCH} IDs")
        result.append(_label(key, "key"))
    return list(dict.fromkeys(result))


def _limit(top_k: int) -> None:
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 0 <= top_k <= MAX_BATCH:
        raise ValueError(f"top_k must be an integer from 0 to {MAX_BATCH}")


class _Scoped:
    def __init__(self, scope: _Scope) -> None:
        self._scope = scope

    @property
    def tenant(self) -> str:
        return self._scope.tenant

    @property
    def namespace(self) -> str:
        return self._scope.namespace

    @property
    def model(self) -> str:
        return self._scope.model

    @property
    def dimension(self) -> int:
        return self._scope.dimension


class SQLiteVectorIndex(_Scoped):
    """Exact cosine search, streaming vectors under a per-connection lock.

    Scope and generation use a relational index. Memory for ranking is O(k),
    and vector buffers are read in batches, without loading a corpus matrix.
    Calls use the shared bounded executor and close drains active operations.
    """

    def __init__(self, db: sqlite3.Connection, scope: _Scope) -> None:
        super().__init__(scope)
        self._db, self._lock = db, threading.Lock()
        self._closed = False
        self._activity = threading.Condition()
        self._closing = False
        self._pending = 0
        from commontrace.sqlite_vector_snapshots import SQLiteSnapshots

        self._snapshots: SnapshotBackend = SQLiteSnapshots(db, self._scope.sql(), self._snapshot_run)

    @property
    def snapshots(self) -> SnapshotBackend:
        """Optional durable incremental indexing, independently of legacy CRUD."""
        return self._snapshots

    async def _snapshot_run(self, function: Callable[[], T]) -> T:
        def execute() -> T:
            with self._lock:
                self._check()
                return function()
        return await self._run(execute)

    @classmethod
    async def open(cls, path: str, *, tenant: str, namespace: str, model: str,
                   dimension: int) -> SQLiteVectorIndex:
        scope = _Scope(tenant, namespace, model, dimension)

        def opening() -> SQLiteVectorIndex:
            if path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            db = connect(path)
            try:
                db.execute("CREATE TABLE IF NOT EXISTS commontrace_vectors ("
                           "tenant TEXT NOT NULL, namespace TEXT NOT NULL, model TEXT NOT NULL, "
                           "dimension INTEGER NOT NULL, key TEXT NOT NULL, generation TEXT NOT NULL, "
                           "embedding BLOB NOT NULL, "
                           "PRIMARY KEY(tenant, namespace, model, dimension, key)) WITHOUT ROWID")
                db.execute("CREATE INDEX IF NOT EXISTS commontrace_vectors_generation ON commontrace_vectors "
                           "(tenant, namespace, model, dimension, generation)")
                db.execute("CREATE TABLE IF NOT EXISTS commontrace_vector_sources ("
                           "tenant TEXT NOT NULL, namespace TEXT NOT NULL, model TEXT NOT NULL, "
                           "dimension INTEGER NOT NULL, source TEXT NOT NULL, "
                           "PRIMARY KEY(tenant, namespace, model, dimension)) WITHOUT ROWID")
                return cls(db, scope)
            except BaseException:
                db.close()
                raise

        task = asyncio.create_task(STORE_WORKERS.run(opening))
        try:
            index = await asyncio.shield(task)
        except asyncio.CancelledError:
            def dispose(future: asyncio.Task[SQLiteVectorIndex]) -> None:
                if not future.cancelled() and future.exception() is None:
                    index = future.result()
                    # Initialization is finished; no request can own this connection.
                    index._db.close()
            task.add_done_callback(dispose)
            raise
        try:
            await index.snapshots.initialize()
        except BaseException:
            await index.close()
            raise
        return index

    def _check(self) -> None:
        if self._closed:
            raise RuntimeError("vector index is closed")

    async def _run(self, function: Callable[[], T]) -> T:
        with self._activity:
            if self._closing:
                raise RuntimeError("vector index is closing or closed")
            self._pending += 1
        finished = False
        def finish() -> None:
            nonlocal finished
            with self._activity:
                if not finished:
                    finished = True
                    self._pending -= 1
                    self._activity.notify_all()
        def execute() -> T:
            try:
                return function()
            finally:
                finish()
        try:
            return await STORE_WORKERS.run(execute)
        except asyncio.CancelledError:
            # The accepted worker retains its pending slot until it actually
            # completes. Closing cannot abandon a cancelled caller's write.
            raise
        except BaseException:
            finish()  # Also handles rejection before a worker was submitted.
            raise

    async def bind_source(self, identity: str) -> None:
        """Atomically pin this scope to one canonical source-store identity."""
        source = _label(identity, "source")
        def execute() -> None:
            with self._lock:
                self._check()
                with write_txn(self._db):
                    self._db.execute("INSERT OR IGNORE INTO commontrace_vector_sources VALUES(?,?,?,?,?)",
                                     (*self._scope.sql(), source))
                    stored = self._db.execute("SELECT source FROM commontrace_vector_sources WHERE tenant=? "
                                              "AND namespace=? AND model=? AND dimension=?",
                                              self._scope.sql()).fetchone()[0]
                    if stored != source:
                        raise ValueError("vector namespace is bound to a different canonical source store")
        await self._run(execute)

    async def upsert(self, records: Iterable[VectorRecord]) -> int:
        def execute() -> int:
            rows = _records(records, self.dimension)
            with self._lock:
                self._check()
                with write_txn(self._db):
                    self._db.executemany("INSERT INTO commontrace_vectors VALUES (?,?,?,?,?,?,?) "
                                         "ON CONFLICT(tenant,namespace,model,dimension,key) DO UPDATE SET "
                                         "generation=excluded.generation, embedding=excluded.embedding",
                                         [(*self._scope.sql(), key, generation,
                                           struct.pack(f"<{self.dimension}f", *vector))
                                          for key, vector, generation in rows])
            return len(rows)
        return await self._run(execute)

    async def prune(self, generation: str) -> int:
        """Erase obsolete scoped vectors during owner-controlled maintenance.

        Quiesce all users/builders of this scoped index before pruning; this is
        not a distributed generation publication or compare-and-swap operation.
        """
        wanted = _generation(generation)
        def execute() -> int:
            with self._lock:
                self._check()
                with write_txn(self._db):
                    return self._db.execute(
                        "DELETE FROM commontrace_vectors WHERE tenant=? AND namespace=? AND model=? "
                        "AND dimension=? AND generation<>?", (*self._scope.sql(), wanted)).rowcount
        return await self._run(execute)

    async def delete(self, keys: Iterable[str]) -> int:
        def execute() -> int:
            ids = _keys(keys)
            with self._lock:
                self._check()
                with write_txn(self._db):
                    before = self._db.total_changes
                    self._db.executemany("DELETE FROM commontrace_vectors WHERE tenant=? AND namespace=? "
                                         "AND model=? AND dimension=? AND key=?",
                                         [(*self._scope.sql(), key) for key in ids])
                    return self._db.total_changes - before
        return await self._run(execute)

    async def search(self, vector: Sequence[float], *, top_k: int = 10,
                     generation: str | None = None, allowed_ids: Sequence[str] | None = None) -> list[VectorHit]:
        published = await _published_search(self.snapshots, vector, top_k=top_k,
                                            generation=generation, allowed_ids=allowed_ids)
        if published is not None:
            return published
        def execute() -> list[VectorHit]:
            import json

            from commontrace.vector_scoring import cosine_topk

            _limit(top_k)
            query = _vector(vector, self.dimension)
            ids = _keys(allowed_ids) if allowed_ids is not None else None
            statement = "SELECT key, embedding FROM commontrace_vectors WHERE tenant=? AND namespace=? " \
                "AND model=? AND dimension=?"
            params: list[Any] = list(self._scope.sql())
            if generation is not None:
                statement += " AND generation=?"
                params.append(_generation(generation))
            if ids is not None:
                statement += " AND key IN (SELECT value FROM json_each(?))"
                params.append(json.dumps(ids))
            with self._lock:
                self._check()
                if not top_k or ids == []:
                    return []
                cursor = self._db.execute(statement, params)
                def vector_rows() -> Iterator[tuple[str, bytes]]:
                    while rows := cursor.fetchmany(256):
                        for key, blob in rows:
                            yield key, blob
                try:
                    return [VectorHit(key, score) for key, score in cosine_topk(
                        vector_rows(), query, dimension=self.dimension, top_k=top_k)]
                finally:
                    cursor.close()
        return await self._run(execute)

    async def close(self) -> None:
        with self._activity:
            self._closing = True
        def closing() -> None:
            with self._activity:
                self._activity.wait_for(lambda: self._pending == 0)
            with self._lock:
                if not self._closed:
                    self._closed = True
                    self._db.close()
        # Closing must remain possible when the shared admission budget is full.
        await asyncio.to_thread(closing)


def _pg_vector(vector: Sequence[float]) -> str:
    return "[" + ",".join(repr(v) for v in vector) + "]"


class PostgresVectorIndex(_Scoped):
    """Pooled asyncpg + pgvector cosine search, with optional HNSW.

    pgvector must be installed by the database administrator. Initialization
    creates only CommonTrace's table and indexes, not a privileged extension.
    ``approximate=False`` guarantees exhaustive nearest neighbours; HNSW uses
    iterative scanning after owner/generation filters and may return fewer
    candidates at its configured scan budget. Use application-specific recall
    evaluation before opting into approximate retrieval.
    """

    def __init__(self, pool: Any, scope: _Scope, *, approximate: bool) -> None:
        super().__init__(scope)
        self._pool, self._approximate = pool, approximate
        from commontrace.postgres_vector_snapshots import PostgresSnapshots

        self._snapshots: SnapshotBackend = PostgresSnapshots(pool, scope.sql(), approximate=approximate)

    @property
    def snapshots(self) -> SnapshotBackend:
        """Durable revision-fenced vector materializations and pinned readers."""
        return self._snapshots

    @classmethod
    async def open(cls, dsn: str, *, tenant: str, namespace: str, model: str,
                   dimension: int, min_size: int = 1, max_size: int = 8,
                   approximate: bool = False, command_timeout: float = 30) -> PostgresVectorIndex:
        scope = _Scope(tenant, namespace, model, dimension)
        if isinstance(min_size, bool) or isinstance(max_size, bool) or not isinstance(min_size, int) \
                or not isinstance(max_size, int) or not 0 <= min_size <= max_size or max_size < 1:
            raise ValueError("pool sizes must be integers with 0 <= min_size <= max_size and max_size >= 1")
        if not math.isfinite(command_timeout) or command_timeout <= 0:
            raise ValueError("command_timeout must be finite and positive")
        try:
            asyncpg = importlib.import_module("asyncpg")
        except ImportError as exc:
            raise ImportError("PostgresVectorIndex requires the optional asyncpg package") from exc
        # Retain the pool before awaiting initialization. Prewarm sequentially:
        # asyncpg's parallel min_size initialization can otherwise leave an
        # in-flight connection behind after another connection fails.
        pool = asyncpg.create_pool(dsn, min_size=0, max_size=max_size, command_timeout=command_timeout)
        warming: list[Any] = []
        try:
            await pool
            try:
                for _ in range(min_size):
                    warming.append(await pool.acquire())
            finally:
                for connection in warming:
                    await pool.release(connection)
            async with pool.acquire() as db, db.transaction():
                if not await db.fetchval("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')"):
                    raise RuntimeError("pgvector extension is not installed; ask the database administrator")
                await db.execute("CREATE TABLE IF NOT EXISTS commontrace_vectors ("
                                 "tenant TEXT NOT NULL, namespace TEXT NOT NULL, model TEXT NOT NULL, "
                                 "dimension INTEGER NOT NULL, key TEXT NOT NULL, generation TEXT NOT NULL, "
                                 "embedding vector NOT NULL, CHECK(vector_dims(embedding)=dimension), "
                                 "PRIMARY KEY(tenant,namespace,model,dimension,key))")
                await db.execute("CREATE INDEX IF NOT EXISTS commontrace_vectors_generation ON "
                                 "commontrace_vectors(tenant,namespace,model,dimension,generation)")
                await db.execute("CREATE TABLE IF NOT EXISTS commontrace_vector_sources ("
                                 "tenant TEXT NOT NULL, namespace TEXT NOT NULL, model TEXT NOT NULL, "
                                 "dimension INTEGER NOT NULL, source TEXT NOT NULL, "
                                 "PRIMARY KEY(tenant,namespace,model,dimension))")
                if approximate:
                    # The literal is a validated integer; never interpolate owner labels.
                    digest = hashlib.sha256(str(dimension).encode()).hexdigest()[:16]
                    await db.execute(f"CREATE INDEX IF NOT EXISTS commontrace_vectors_hnsw_{digest} "
                                     f"ON commontrace_vectors USING hnsw ((embedding::vector({dimension})) "
                                     f"vector_cosine_ops) WHERE dimension={dimension}")
        except BaseException:
            await pool.close()
            raise
        index = cls(pool, scope, approximate=approximate)
        try:
            await index.snapshots.initialize()
        except BaseException:
            await pool.close()
            raise
        return index

    async def bind_source(self, identity: str) -> None:
        """Atomically bind a shared namespace to one canonical source identity."""
        source = _label(identity, "source")
        async with self._pool.acquire() as db, db.transaction():
            await db.execute("INSERT INTO commontrace_vector_sources VALUES($1,$2,$3,$4,$5) "
                             "ON CONFLICT(tenant,namespace,model,dimension) DO NOTHING", *self._scope.sql(), source)
            stored = await db.fetchval("SELECT source FROM commontrace_vector_sources WHERE tenant=$1 "
                                       "AND namespace=$2 AND model=$3 AND dimension=$4", *self._scope.sql())
            if stored != source:
                raise ValueError("vector namespace is bound to a different canonical source store")

    async def upsert(self, records: Iterable[VectorRecord]) -> int:
        rows = await STORE_WORKERS.run(lambda: _records(records, self.dimension))
        async with self._pool.acquire() as db, db.transaction():
            for start in range(0, len(rows), 256):
                await db.executemany("INSERT INTO commontrace_vectors VALUES($1,$2,$3,$4,$5,$6,$7::text::vector) "
                                     "ON CONFLICT(tenant,namespace,model,dimension,key) DO UPDATE SET "
                                     "generation=excluded.generation, embedding=excluded.embedding",
                                     [(*self._scope.sql(), key, generation, _pg_vector(vector))
                                      for key, vector, generation in rows[start:start + 256]])
        return len(rows)

    async def delete(self, keys: Iterable[str]) -> int:
        ids = await STORE_WORKERS.run(lambda: _keys(keys))
        async with self._pool.acquire() as db, db.transaction():
            status = await db.execute("DELETE FROM commontrace_vectors WHERE tenant=$1 AND namespace=$2 "
                                      "AND model=$3 AND dimension=$4 AND key=ANY($5::text[])",
                                      *self._scope.sql(), ids)
        return int(status.split()[-1])

    async def search(self, vector: Sequence[float], *, top_k: int = 10,
                     generation: str | None = None, allowed_ids: Sequence[str] | None = None) -> list[VectorHit]:
        published = await _published_search(self.snapshots, vector, top_k=top_k,
                                            generation=generation, allowed_ids=allowed_ids)
        if published is not None:
            return published
        _limit(top_k)
        query = _vector(vector, self.dimension)
        ids = _keys(allowed_ids) if allowed_ids is not None else None
        if generation is not None:
            _generation(generation)
        if not top_k or ids == []:
            return []
        async with self._pool.acquire() as db, db.transaction():
            # asyncpg prepares this statement; after five runs Postgres may switch to a
            # generic plan, which casts the query vector per row and cannot pick HNSW.
            # Measured at 20k vectors: 1.2 s per query generic against 6-20 ms custom.
            await db.execute("SET LOCAL plan_cache_mode=force_custom_plan")
            if self._approximate:
                await db.execute("SET LOCAL hnsw.iterative_scan='strict_order'")
                # A matching expression and partial predicate permit the HNSW index.
                distance = f"embedding::vector({self.dimension}) <=> $5::text::vector({self.dimension})"
                dimension = f"dimension=$4 AND dimension={self.dimension}"
            else:
                distance, dimension = "embedding <=> $5::text::vector", "dimension=$4"
                # No approximate index may influence the exact mode.
                await db.execute("SET LOCAL enable_indexscan=off")
                await db.execute("SET LOCAL enable_bitmapscan=off")
            # Only fixed expressions and a validated integer dimension are
            # interpolated. All owner/query values are bound. The ANN inner
            # order contains only the distance operator, allowing HNSW; tie
            # ordering is applied after the bounded candidate page.
            ordering = distance if self._approximate else distance + ", key"
            rows = await db.fetch(
                f"SELECT key, score FROM (SELECT key, 1-({distance}) AS score FROM commontrace_vectors "  # nosec B608
                f"WHERE tenant=$1 AND namespace=$2 AND model=$3 AND {dimension} "
                "AND ($6::text IS NULL OR generation=$6) AND ($7::text[] IS NULL OR key=ANY($7)) "
                f"ORDER BY {ordering} LIMIT $8) nearest ORDER BY score DESC,key",
                *self._scope.sql(), _pg_vector(query), generation, ids, top_k)
        return [VectorHit(row["key"], min(1.0, max(-1.0, float(row["score"])))) for row in rows]

    async def prune(self, generation: str) -> int:
        """Erase obsolete generations after quiescing all scoped readers/builders.

        Other tenants/models are untouched. This owner-maintenance operation
        does not fence concurrent generation publication across processes.
        """
        wanted = _generation(generation)
        async with self._pool.acquire() as db, db.transaction():
            status = await db.execute("DELETE FROM commontrace_vectors WHERE tenant=$1 AND namespace=$2 "
                                      "AND model=$3 AND dimension=$4 AND generation<>$5", *self._scope.sql(), wanted)
        return int(status.split()[-1])

    async def close(self) -> None:
        """Wait for checked-out connections before closing the owned pool."""
        await self._pool.close()
