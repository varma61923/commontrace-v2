"""Fenced PostgreSQL/pgvector publication with DB-clock reader/build leases.

Staging is private until a compare-and-swap publication transaction commits.
Reader leases preserve historical MVCC intervals across replacement and bounded
collection. Mutations share a scoped head lock; readers hold independent lease-row locks.
"""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from commontrace.async_workers import STORE_WORKERS
from commontrace.vector_snapshots import (
    BuildLease,
    ReadLease,
    SnapshotConflict,
    SnapshotHead,
    lease_duration,
    snapshot_deletes,
    snapshot_records,
)
from commontrace.vector_snapshots import (
    target_revision as validate_revision,
)
from commontrace.vector_store import (
    VectorHit,
    VectorRecord,
    _generation,
    _keys,
    _limit,
    _pg_vector,
    _Scope,
    _vector,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS commontrace_vector_heads (
 tenant TEXT NOT NULL, namespace TEXT NOT NULL, model TEXT NOT NULL, dimension INTEGER NOT NULL,
 revision BIGINT NOT NULL DEFAULT -1, generation TEXT, fence BIGINT NOT NULL DEFAULT 0,
 PRIMARY KEY(tenant,namespace,model,dimension));
CREATE TABLE IF NOT EXISTS commontrace_vector_builds (
 tenant TEXT NOT NULL, namespace TEXT NOT NULL, model TEXT NOT NULL, dimension INTEGER NOT NULL,
 token TEXT NOT NULL, fence BIGINT NOT NULL, base_revision BIGINT NOT NULL,
 target_revision BIGINT NOT NULL, generation TEXT NOT NULL, full_build BOOLEAN NOT NULL,
 expires TIMESTAMPTZ NOT NULL, duration DOUBLE PRECISION NOT NULL DEFAULT 60,
 PRIMARY KEY(tenant,namespace,model,dimension,token));
ALTER TABLE commontrace_vector_builds ADD COLUMN IF NOT EXISTS duration DOUBLE PRECISION NOT NULL DEFAULT 60;
CREATE TABLE IF NOT EXISTS commontrace_vector_stages (
 tenant TEXT NOT NULL, namespace TEXT NOT NULL, model TEXT NOT NULL, dimension INTEGER NOT NULL,
 token TEXT NOT NULL, key TEXT NOT NULL, embedding vector, checksum TEXT NOT NULL, deleted BOOLEAN NOT NULL,
 CHECK(embedding IS NULL OR vector_dims(embedding)=dimension),
 PRIMARY KEY(tenant,namespace,model,dimension,token,key));
CREATE TABLE IF NOT EXISTS commontrace_vector_snapshots (
 tenant TEXT NOT NULL, namespace TEXT NOT NULL, model TEXT NOT NULL, dimension INTEGER NOT NULL,
 generation TEXT NOT NULL, revision BIGINT NOT NULL,
 PRIMARY KEY(tenant,namespace,model,dimension,generation),
 UNIQUE(tenant,namespace,model,dimension,revision));
CREATE TABLE IF NOT EXISTS commontrace_vector_readers (
 tenant TEXT NOT NULL, namespace TEXT NOT NULL, model TEXT NOT NULL, dimension INTEGER NOT NULL,
 token TEXT NOT NULL, revision BIGINT NOT NULL, generation TEXT NOT NULL,
 expires TIMESTAMPTZ NOT NULL, duration DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(tenant,namespace,model,dimension,token));
CREATE TABLE IF NOT EXISTS commontrace_vector_versions (
 tenant TEXT NOT NULL, namespace TEXT NOT NULL, model TEXT NOT NULL, dimension INTEGER NOT NULL,
 key TEXT NOT NULL, valid_from BIGINT NOT NULL, valid_to BIGINT,
 embedding vector NOT NULL, checksum TEXT NOT NULL,
 CHECK(vector_dims(embedding)=dimension), CHECK(valid_to IS NULL OR valid_to>valid_from),
 PRIMARY KEY(tenant,namespace,model,dimension,key,valid_from));
CREATE UNIQUE INDEX IF NOT EXISTS commontrace_vector_versions_current ON commontrace_vector_versions
 (tenant,namespace,model,dimension,key) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS commontrace_vector_versions_visibility ON commontrace_vector_versions
 (tenant,namespace,model,dimension,valid_to,valid_from);
"""



class PostgresSnapshots:
    """Process-safe snapshot search on a caller-owned asyncpg pool.

    Exact search is the default. Optional HNSW uses bounded iterative scanning;
    selective MVCC/owner filters can return fewer than top_k candidates. Evaluate
    application recall before enabling approximate mode.

    An expired build is terminal: start a new build rather than replaying a
    stale token. Published generations survive until collection; pinning one
    grants a bounded lease independent of subsequent head publications.
    """

    def __init__(self, pool: Any, scope: tuple[str, str, str, int], *, approximate: bool = False) -> None:
        self._pool = pool
        self._scope = _Scope(*scope).sql()
        self._approximate = approximate

    async def initialize(self) -> None:
        async with self._pool.acquire() as db, db.transaction():
            # Serialize shared DDL: IF NOT EXISTS alone races system catalog inserts.
            await db.execute("SELECT pg_advisory_xact_lock(736482091503)")
            if not await db.fetchval("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')"):
                raise RuntimeError("pgvector extension is not installed; ask the database administrator")
            await db.execute(_SCHEMA)
            if self._approximate:
                dimension = self._scope[3]  # validated integer, never owner-controlled SQL
                digest = hashlib.sha256(str(dimension).encode()).hexdigest()[:16]
                await db.execute(f"CREATE INDEX IF NOT EXISTS commontrace_vector_versions_hnsw_{digest} "
                                 f"ON commontrace_vector_versions USING hnsw ((embedding::vector({dimension})) "
                                 f"vector_cosine_ops) WHERE dimension={dimension}")
            await db.execute("INSERT INTO commontrace_vector_heads(tenant,namespace,model,dimension) "
                             "VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING", *self._scope)

    async def _locked(self, db: Any) -> Any:
        await self._schema_reader(db)
        row = await db.fetchrow("SELECT revision,generation,fence FROM commontrace_vector_heads "
                                "WHERE tenant=$1 AND namespace=$2 AND model=$3 AND dimension=$4 FOR UPDATE",
                                *self._scope)
        if row is None:
            raise RuntimeError("snapshot backend is not initialized")
        return row

    async def _schema_reader(self, db: Any) -> None:
        # Initialization may ALTER shared tables. Acquire the shared schema
        # barrier before any table/row lock so DDL cannot invert the runtime
        # head -> builds order. Shared locks retain concurrent pooled readers
        # and builders from independent scopes until their transaction ends.
        await db.execute("SELECT pg_advisory_xact_lock_shared(736482091503)")

    async def head(self) -> SnapshotHead:
        async with self._pool.acquire() as db, db.transaction():
            await self._schema_reader(db)
            row = await db.fetchrow("SELECT revision,generation FROM commontrace_vector_heads "
                                    "WHERE tenant=$1 AND namespace=$2 AND model=$3 AND dimension=$4", *self._scope)
        if row is None:
            raise RuntimeError("snapshot backend is not initialized")
        return SnapshotHead(row["revision"], row["generation"])

    async def begin(self, target_revision: int, generation: str, *, full: bool = False,
                    lease_seconds: float = 60) -> BuildLease | None:
        validate_revision(target_revision, generation)
        duration = lease_duration(lease_seconds)
        if not isinstance(full, bool):
            raise ValueError("full must be a boolean")
        async with self._pool.acquire() as db, db.transaction():
            head = await self._locked(db)
            if target_revision <= head["revision"]:
                if target_revision == head["revision"] and generation != head["generation"]:
                    raise SnapshotConflict("revision is already published under another generation")
                return None
            if await db.fetchval("SELECT EXISTS(SELECT 1 FROM commontrace_vector_snapshots "
                                 "WHERE tenant=$1 AND namespace=$2 AND model=$3 AND dimension=$4 "
                                 "AND generation=$5)", *self._scope, generation):
                raise SnapshotConflict("generation is already published")
            fence = await db.fetchval("UPDATE commontrace_vector_heads SET fence=fence+1 "
                                     "WHERE tenant=$1 AND namespace=$2 AND model=$3 AND dimension=$4 RETURNING fence",
                                     *self._scope)
            lease = BuildLease(uuid.uuid4().hex, fence, head["revision"], target_revision, generation, full)
            await db.execute("INSERT INTO commontrace_vector_builds VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,"
                             "clock_timestamp()+$11::double precision*interval '1 second',$11)",
                             *self._scope, lease.token, lease.fence, lease.base_revision,
                             lease.target_revision, lease.generation, lease.full, duration)
        return lease

    async def _live_build(self, db: Any, lease: BuildLease) -> bool:
        return bool(await db.fetchval("SELECT EXISTS(SELECT 1 FROM commontrace_vector_builds "
                                     "WHERE tenant=$1 AND namespace=$2 AND model=$3 AND dimension=$4 "
                                     "AND token=$5 AND fence=$6 AND base_revision=$7 AND target_revision=$8 "
                                     "AND generation=$9 AND full_build=$10 AND expires>clock_timestamp())",
                                     *self._scope, lease.token, lease.fence, lease.base_revision,
                                     lease.target_revision, lease.generation, lease.full))

    async def stage(self, lease: BuildLease, records: Iterable[VectorRecord],
                    deleted_keys: Iterable[str] = ()) -> int:
        rows, deleted = await STORE_WORKERS.run(
            lambda: (snapshot_records(records, self._scope[3]), snapshot_deletes(deleted_keys)))
        if set(deleted).intersection(key for key, _, _ in rows):
            raise ValueError("a staged batch cannot both write and delete the same key")
        async with self._pool.acquire() as db, db.transaction():
            await self._locked(db)
            if not await self._live_build(db, lease):
                raise SnapshotConflict("build lease is expired, consumed, or invalid")
            await db.executemany("INSERT INTO commontrace_vector_stages "
                                 "VALUES($1,$2,$3,$4,$5,$6,$7::text::vector,$8,false) "
                                 "ON CONFLICT(tenant,namespace,model,dimension,token,key) DO UPDATE SET "
                                 "embedding=excluded.embedding,checksum=excluded.checksum,deleted=false",
                                 [(*self._scope, lease.token, key, _pg_vector(vector), checksum)
                                  for key, vector, checksum in rows])
            await db.executemany("INSERT INTO commontrace_vector_stages VALUES($1,$2,$3,$4,$5,$6,NULL,'',true) "
                                 "ON CONFLICT(tenant,namespace,model,dimension,token,key) DO UPDATE SET "
                                 "embedding=NULL,checksum='',deleted=true",
                                 [(*self._scope, lease.token, key) for key in deleted])
            # Progress extends the original bounded idle timeout; an expired
            # lease cannot be resurrected even if staging began while live.
            renewed = await db.fetchval("UPDATE commontrace_vector_builds SET "
                                        "expires=clock_timestamp()+duration*interval '1 second' "
                                        "WHERE tenant=$1 AND namespace=$2 AND model=$3 AND dimension=$4 "
                                        "AND token=$5 AND fence=$6 AND base_revision=$7 AND target_revision=$8 "
                                        "AND generation=$9 AND full_build=$10 AND expires>clock_timestamp() "
                                        "RETURNING token", *self._scope, lease.token, lease.fence,
                                        lease.base_revision, lease.target_revision, lease.generation, lease.full)
            if renewed is None:
                raise SnapshotConflict("build lease expired during staging")
        return len(rows) + len(deleted)

    async def publish(self, lease: BuildLease) -> bool:
        async with self._pool.acquire() as db, db.transaction():
            head = await self._locked(db)
            if head["revision"] != lease.base_revision or lease.target_revision <= head["revision"] \
                    or not await self._live_build(db, lease):
                return False
            if await db.fetchval("SELECT EXISTS(SELECT 1 FROM commontrace_vector_snapshots "
                                 "WHERE tenant=$1 AND namespace=$2 AND model=$3 AND dimension=$4 AND generation=$5)",
                                 *self._scope, lease.generation):
                return False
            # Retire changed/deleted keys. Full builds also retire absent keys.
            await db.execute("UPDATE commontrace_vector_versions v SET valid_to=$6 "
                             "WHERE v.tenant=$1 AND v.namespace=$2 AND v.model=$3 AND v.dimension=$4 "
                             "AND v.valid_to IS NULL AND (EXISTS(SELECT 1 FROM commontrace_vector_stages s "
                             "WHERE s.tenant=$1 AND s.namespace=$2 AND s.model=$3 AND s.dimension=$4 "
                             "AND s.token=$5 AND s.key=v.key AND (s.deleted OR s.checksum<>v.checksum)) "
                             "OR ($7 AND NOT EXISTS(SELECT 1 FROM commontrace_vector_stages s "
                             "WHERE s.tenant=$1 AND s.namespace=$2 AND s.model=$3 AND s.dimension=$4 "
                             "AND s.token=$5 AND s.key=v.key AND NOT s.deleted)))",
                             *self._scope, lease.token, lease.target_revision, lease.full)
            await db.execute("INSERT INTO commontrace_vector_versions "
                             "SELECT s.tenant,s.namespace,s.model,s.dimension,s.key,$6,NULL,s.embedding,s.checksum "
                             "FROM commontrace_vector_stages s WHERE s.tenant=$1 AND s.namespace=$2 "
                             "AND s.model=$3 AND s.dimension=$4 AND s.token=$5 AND NOT s.deleted "
                             "AND NOT EXISTS(SELECT 1 FROM commontrace_vector_versions v "
                             "WHERE v.tenant=$1 AND v.namespace=$2 AND v.model=$3 AND v.dimension=$4 "
                             "AND v.key=s.key AND v.valid_to IS NULL)",
                             *self._scope, lease.token, lease.target_revision)
            await db.execute("INSERT INTO commontrace_vector_snapshots VALUES($1,$2,$3,$4,$5,$6)",
                             *self._scope, lease.generation, lease.target_revision)
            await db.execute("UPDATE commontrace_vector_heads SET revision=$5,generation=$6 "
                             "WHERE tenant=$1 AND namespace=$2 AND model=$3 AND dimension=$4",
                             *self._scope, lease.target_revision, lease.generation)
            await self._remove_build(db, lease.token)
        return True

    async def _remove_build(self, db: Any, token: str) -> None:
        await db.execute("DELETE FROM commontrace_vector_stages WHERE tenant=$1 AND namespace=$2 "
                         "AND model=$3 AND dimension=$4 AND token=$5", *self._scope, token)
        await db.execute("DELETE FROM commontrace_vector_builds WHERE tenant=$1 AND namespace=$2 "
                         "AND model=$3 AND dimension=$4 AND token=$5", *self._scope, token)

    async def abort(self, lease: BuildLease) -> None:
        async with self._pool.acquire() as db, db.transaction():
            await self._locked(db)
            # Match the complete lease identity even for expired-build cleanup.
            if await db.fetchval("SELECT EXISTS(SELECT 1 FROM commontrace_vector_builds "
                                 "WHERE tenant=$1 AND namespace=$2 AND model=$3 AND dimension=$4 "
                                 "AND token=$5 AND fence=$6 AND base_revision=$7 AND target_revision=$8 "
                                 "AND generation=$9 AND full_build=$10)",
                                 *self._scope, lease.token, lease.fence, lease.base_revision,
                                 lease.target_revision, lease.generation, lease.full):
                await self._remove_build(db, lease.token)

    async def pin(self, generation: str, *, lease_seconds: float = 60) -> ReadLease:
        _generation(generation)
        duration = lease_duration(lease_seconds)
        async with self._pool.acquire() as db, db.transaction():
            await self._locked(db)
            revision = await db.fetchval("SELECT revision FROM commontrace_vector_snapshots "
                                         "WHERE tenant=$1 AND namespace=$2 AND model=$3 AND dimension=$4 "
                                         "AND generation=$5", *self._scope, generation)
            if revision is None:
                raise SnapshotConflict("generation is unavailable; pin a committed generation")
            lease = ReadLease(uuid.uuid4().hex, revision, generation)
            await db.execute("INSERT INTO commontrace_vector_readers VALUES($1,$2,$3,$4,$5,$6,$7,"
                             "clock_timestamp()+$8::double precision*interval '1 second',$8)",
                             *self._scope, lease.token, lease.revision, lease.generation, duration)
        return lease

    async def renew(self, read: ReadLease) -> None:
        async with self._pool.acquire() as db, db.transaction():
            await self._locked(db)
            changed = await db.fetchval("UPDATE commontrace_vector_readers SET "
                                       "expires=clock_timestamp()+duration*interval '1 second' "
                                       "WHERE tenant=$1 AND namespace=$2 AND model=$3 AND dimension=$4 "
                                       "AND token=$5 AND revision=$6 AND generation=$7 AND expires>clock_timestamp() "
                                       "RETURNING token", *self._scope, read.token, read.revision, read.generation)
            if changed is None:
                raise SnapshotConflict("reader lease is expired, released, or invalid")

    async def release(self, read: ReadLease) -> None:
        async with self._pool.acquire() as db, db.transaction():
            await self._locked(db)
            await db.execute("DELETE FROM commontrace_vector_readers WHERE tenant=$1 AND namespace=$2 "
                             "AND model=$3 AND dimension=$4 AND token=$5 AND revision=$6 AND generation=$7",
                             *self._scope, read.token, read.revision, read.generation)

    async def search(self, read: ReadLease, vector: Sequence[float], *, top_k: int = 10,
                     allowed_ids: Sequence[str] | None = None) -> list[VectorHit]:
        _limit(top_k)
        query = _vector(vector, self._scope[3])
        ids = _keys(allowed_ids) if allowed_ids is not None else None
        async with self._pool.acquire() as db, db.transaction():
            await self._schema_reader(db)
            # Share-lock only this reader: independent pooled readers remain concurrent.
            # GC must erase expired lease rows before reclaiming their vector intervals.
            live = await db.fetchval("SELECT token FROM commontrace_vector_readers "
                                     "WHERE tenant=$1 AND namespace=$2 AND model=$3 AND dimension=$4 "
                                     "AND token=$5 AND revision=$6 AND generation=$7 "
                                     "AND expires>clock_timestamp() FOR SHARE",
                                     *self._scope, read.token, read.revision, read.generation)
            if not live:
                raise SnapshotConflict("reader lease is expired, released, or invalid")
            if not top_k or ids == []:
                return []
            # A generic plan would cast the query vector per row (see vector_store.search).
            await db.execute("SET LOCAL plan_cache_mode=force_custom_plan")
            if self._approximate:
                await db.execute("SET LOCAL hnsw.iterative_scan='strict_order'")
                dimension = self._scope[3]
                distance = f"embedding::vector({dimension}) <=> $5::text::vector({dimension})"
                dimension_filter = f"dimension=$4 AND dimension={dimension}"
                ordering = distance
            else:
                await db.execute("SET LOCAL enable_indexscan=off")
                await db.execute("SET LOCAL enable_bitmapscan=off")
                distance = "embedding <=> $5::text::vector"
                dimension_filter = "dimension=$4"
                ordering = distance + ",key"
            # Expressions contain only fixed SQL and a validated integer; all
            # owner, lease, query-vector and allowed-ID values remain parameters.
            rows = await db.fetch(
                f"SELECT key,score FROM (SELECT key,1-({distance}) AS score "  # nosec B608
                "FROM commontrace_vector_versions WHERE tenant=$1 AND namespace=$2 "
                f"AND model=$3 AND {dimension_filter} AND valid_from<=$6 "
                "AND (valid_to IS NULL OR valid_to>$6) "
                "AND ($7::text[] IS NULL OR key=ANY($7)) "
                f"ORDER BY {ordering} LIMIT $8) nearest ORDER BY score DESC,key",
                *self._scope, _pg_vector(query), read.revision, ids, top_k)
        return [VectorHit(row["key"], min(1.0, max(-1.0, float(row["score"])))) for row in rows]

    async def collect_garbage(self, *, limit: int = 1000) -> int:
        _limit(limit)
        if not limit:
            return 0
        async with self._pool.acquire() as db, db.transaction():
            await self._locked(db)
            # Metadata cleanup is bounded separately; vector/stage erasures share
            # the requested budget. Live leases always use the database clock.
            await db.execute("DELETE FROM commontrace_vector_readers WHERE ctid IN "
                             "(SELECT ctid FROM commontrace_vector_readers WHERE tenant=$1 AND namespace=$2 "
                             "AND model=$3 AND dimension=$4 AND expires<=clock_timestamp() LIMIT $5)",
                             *self._scope, limit)
            await db.execute("DELETE FROM commontrace_vector_snapshots WHERE ctid IN "
                             "(SELECT s.ctid FROM commontrace_vector_snapshots s "
                             "WHERE s.tenant=$1 AND s.namespace=$2 AND s.model=$3 AND s.dimension=$4 "
                             "AND s.revision<>(SELECT revision FROM commontrace_vector_heads "
                             "WHERE tenant=$1 AND namespace=$2 AND model=$3 AND dimension=$4) "
                             "AND NOT EXISTS(SELECT 1 FROM commontrace_vector_readers r "
                             "WHERE r.tenant=$1 AND r.namespace=$2 AND r.model=$3 AND r.dimension=$4 "
                             "AND r.revision=s.revision) LIMIT $5)",
                             *self._scope, limit)
            status = await db.execute("DELETE FROM commontrace_vector_stages WHERE ctid IN "
                                      "(SELECT s.ctid FROM commontrace_vector_stages s "
                                      "WHERE s.tenant=$1 AND s.namespace=$2 AND s.model=$3 AND s.dimension=$4 "
                                      "AND NOT EXISTS(SELECT 1 FROM commontrace_vector_builds b "
                                      "WHERE b.tenant=$1 AND b.namespace=$2 AND b.model=$3 AND b.dimension=$4 "
                                      "AND b.token=s.token AND b.expires>clock_timestamp()) LIMIT $5)",
                                      *self._scope, limit)
            removed = int(status.split()[-1])
            await db.execute("DELETE FROM commontrace_vector_builds WHERE ctid IN "
                             "(SELECT b.ctid FROM commontrace_vector_builds b "
                             "WHERE b.tenant=$1 AND b.namespace=$2 AND b.model=$3 AND b.dimension=$4 "
                             "AND b.expires<=clock_timestamp() AND NOT EXISTS "
                             "(SELECT 1 FROM commontrace_vector_stages s WHERE s.tenant=$1 AND s.namespace=$2 "
                             "AND s.model=$3 AND s.dimension=$4 AND s.token=b.token) LIMIT $5)",
                             *self._scope, limit)
            if removed < limit:
                status = await db.execute("DELETE FROM commontrace_vector_versions WHERE ctid IN "
                                          "(SELECT v.ctid FROM commontrace_vector_versions v "
                                          "WHERE v.tenant=$1 AND v.namespace=$2 AND v.model=$3 AND v.dimension=$4 "
                                          "AND v.valid_to IS NOT NULL AND NOT EXISTS "
                                          "(SELECT 1 FROM commontrace_vector_snapshots p WHERE p.tenant=$1 "
                                          "AND p.namespace=$2 AND p.model=$3 AND p.dimension=$4 "
                                          "AND p.revision>=v.valid_from AND p.revision<v.valid_to) "
                                          "AND NOT EXISTS "
                                          "(SELECT 1 FROM commontrace_vector_readers r WHERE r.tenant=$1 "
                                          "AND r.namespace=$2 AND r.model=$3 AND r.dimension=$4 "
                                          "AND r.revision>=v.valid_from "
                                          "AND r.revision<v.valid_to) AND NOT EXISTS "
                                          "(SELECT 1 FROM commontrace_vector_builds b WHERE b.tenant=$1 "
                                          "AND b.namespace=$2 AND b.model=$3 AND b.dimension=$4 "
                                          "AND b.expires>clock_timestamp() AND b.base_revision>=v.valid_from "
                                          "AND b.base_revision<v.valid_to) LIMIT $5)",
                                          *self._scope, limit - removed)
                removed += int(status.split()[-1])
        return removed
