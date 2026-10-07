"""Snapshot consistency checks against real pooled PostgreSQL and pgvector."""
from __future__ import annotations

import asyncio
import importlib
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import pytest

from commontrace.postgres_vector_snapshots import PostgresSnapshots
from commontrace.vector_snapshots import SnapshotConflict, SnapshotHead
from commontrace.vector_store import VectorRecord


@asynccontextmanager
async def backend(*, scope: tuple[str, str, str, int] | None = None,
                  approximate: bool = False) -> AsyncIterator[tuple[PostgresSnapshots, Any]]:
    dsn = os.environ.get("COMMONTRACE_TEST_PGVECTOR_DSN")
    if not dsn:
        pytest.skip("set COMMONTRACE_TEST_PGVECTOR_DSN to exercise real pgvector snapshots")
    asyncpg = importlib.import_module("asyncpg")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
    snapshots = PostgresSnapshots(pool, scope or ("snapshot-test-" + uuid.uuid4().hex, "memory", "embedding", 3),
                                  approximate=approximate)
    initialized = False
    try:
        await snapshots.initialize()
        initialized = True
        yield snapshots, pool
    finally:
        try:
            if initialized:
                async with pool.acquire() as db, db.transaction():
                    for table in ("commontrace_vector_stages", "commontrace_vector_readers", "commontrace_vector_builds",
                                  "commontrace_vector_snapshots", "commontrace_vector_versions", "commontrace_vector_heads"):
                        await db.execute(f"DELETE FROM {table} WHERE tenant=$1 AND namespace=$2 "
                                         "AND model=$3 AND dimension=$4", *snapshots._scope)
        finally:
            await pool.close()


async def publish(snapshots: PostgresSnapshots, revision: int, generation: str,
                  records: list[VectorRecord], *, deleted: tuple[str, ...] = (), full: bool = False) -> None:
    lease = await snapshots.begin(revision, generation, full=full)
    assert lease is not None
    await snapshots.stage(lease, records, deleted)
    assert await snapshots.publish(lease)


@pytest.mark.parametrize("approximate", [False, True])
def test_private_staging_and_stable_readers_across_incremental_publication(approximate: bool) -> None:
    async def run() -> None:
        async with backend(approximate=approximate) as (snapshots, _):
            assert await snapshots.head() == SnapshotHead(-1, None)
            lease = await snapshots.begin(0, "first", full=True)
            assert lease is not None
            assert await snapshots.stage(lease, [VectorRecord("a", [1, 0, 0]), VectorRecord("b", [0, 1, 0])]) == 2
            with pytest.raises(SnapshotConflict):
                await snapshots.pin("first")
            assert await snapshots.publish(lease)
            old = await snapshots.pin("first")
            await publish(snapshots, 3, "next", [VectorRecord("a", [-1, 0, 0])], deleted=("b",))
            current = await snapshots.pin("next")
            assert [(h.key, h.score) for h in await snapshots.search(old, [1, 0, 0])] == [("a", 1), ("b", 0)]
            assert [(h.key, h.score) for h in await snapshots.search(current, [1, 0, 0])] == [("a", -1)]
            assert await snapshots.collect_garbage() == 0
            await snapshots.release(old)
            assert await snapshots.collect_garbage() == 2
            with pytest.raises(SnapshotConflict):
                await snapshots.pin("first")
            assert (await snapshots.search(current, [1, 0, 0]))[0].key == "a"
    asyncio.run(run())


def test_independent_builds_same_base_have_one_atomic_winner() -> None:
    async def run() -> None:
        async with backend() as (snapshots, pool):
            other = PostgresSnapshots(pool, snapshots._scope)
            await asyncio.gather(*(other.initialize() for _ in range(4)))
            first, second = await asyncio.gather(snapshots.begin(1, "left"), other.begin(2, "right"))
            assert first is not None and second is not None
            assert first.fence != second.fence and first.base_revision == second.base_revision == -1
            await asyncio.gather(snapshots.stage(first, [VectorRecord("left", [1, 0, 0])]),
                                 other.stage(second, [VectorRecord("right", [0, 1, 0])]))
            winners = await asyncio.gather(snapshots.publish(first), other.publish(second))
            assert sorted(winners) == [False, True]
            head = await snapshots.head()
            read = await snapshots.pin(head.generation or "")
            assert [hit.key for hit in await snapshots.search(read, [1, 0, 0])] == [head.generation]
            await snapshots.abort(first)
            await other.abort(second)
            assert not await snapshots.publish(first)
    asyncio.run(run())


def test_full_publication_checksum_reuse_and_partial_replacement() -> None:
    async def run() -> None:
        async with backend() as (snapshots, pool):
            await publish(snapshots, 0, "zero", [VectorRecord("a", [1, 0, 0]), VectorRecord("b", [0, 1, 0])], full=True)
            old = await snapshots.pin("zero")
            await publish(snapshots, 1, "one", [VectorRecord("a", [2, 0, 0])], full=True)
            async with pool.acquire() as db:
                assert await db.fetchval("SELECT count(*) FROM commontrace_vector_versions "
                                         "WHERE tenant=$1 AND namespace=$2 AND model=$3 AND dimension=$4",
                                         *snapshots._scope) == 2
            read = await snapshots.pin("one")
            assert [hit.key for hit in await snapshots.search(read, [1, 0, 0])] == ["a"]
            assert len(await snapshots.search(old, [1, 0, 0])) == 2
            await publish(snapshots, 2, "two", [VectorRecord("a", [0, 0, 1])])
            latest = await snapshots.pin("two")
            assert (await snapshots.search(latest, [1, 0, 0]))[0].score == 0
            assert (await snapshots.search(read, [1, 0, 0]))[0].score == 1
    asyncio.run(run())


def test_expired_and_forged_leases_fail_closed_and_cannot_resurrect() -> None:
    async def run() -> None:
        async with backend() as (snapshots, pool):
            lease = await snapshots.begin(0, "zero")
            assert lease is not None
            with pytest.raises(SnapshotConflict):
                await snapshots.stage(replace(lease, fence=lease.fence + 1), [])
            await snapshots.abort(replace(lease, fence=lease.fence + 1))
            await snapshots.stage(lease, [VectorRecord("a", [1, 0, 0])])
            async with pool.acquire() as db:
                await db.execute("UPDATE commontrace_vector_builds SET expires=clock_timestamp()-interval '1 second' "
                                 "WHERE tenant=$1 AND token=$2", snapshots._scope[0], lease.token)
            assert not await snapshots.publish(lease)
            with pytest.raises(SnapshotConflict):
                await snapshots.stage(lease, [])
            assert await snapshots.collect_garbage() == 1
            await publish(snapshots, 0, "zero", [VectorRecord("a", [1, 0, 0])])
            read = await snapshots.pin("zero")
            with pytest.raises(SnapshotConflict):
                await snapshots.search(replace(read, revision=99), [1, 0, 0])
            await snapshots.renew(read)
            async with pool.acquire() as db:
                await db.execute("UPDATE commontrace_vector_readers SET expires=clock_timestamp()-interval '1 second' "
                                 "WHERE tenant=$1 AND token=$2", snapshots._scope[0], read.token)
            with pytest.raises(SnapshotConflict):
                await snapshots.renew(read)
            with pytest.raises(SnapshotConflict):
                await snapshots.search(read, [1, 0, 0])
    asyncio.run(run())


def test_scope_isolation_and_parameterized_injection_shaped_identifiers() -> None:
    async def run() -> None:
        async with backend(scope=("owner'; DROP TABLE nope; --" + uuid.uuid4().hex, "memory", "model", 3)) as (first, pool):
            second = PostgresSnapshots(pool, (first._scope[0], "other", "model", 3))
            await second.initialize()
            try:
                await publish(first, 0, "same", [VectorRecord("key", [1, 0, 0])])
                await publish(second, 0, "same", [VectorRecord("key", [0, 1, 0])])
                read = await first.pin("same")
                with pytest.raises(SnapshotConflict):
                    await second.search(read, [1, 0, 0])
                own = await second.pin("same")
                assert (await second.search(own, [1, 0, 0]))[0].score == 0
                await second.collect_garbage()
                assert (await first.search(read, [1, 0, 0]))[0].score == 1
            finally:
                async with pool.acquire() as db:
                    for table in ("commontrace_vector_versions", "commontrace_vector_readers",
                                  "commontrace_vector_snapshots", "commontrace_vector_heads"):
                        await db.execute(f"DELETE FROM {table} WHERE tenant=$1 AND namespace=$2", first._scope[0], "other")
    asyncio.run(run())


def test_validation_is_atomic_and_allowed_ids_preserve_exact_order() -> None:
    async def run() -> None:
        async with backend() as (snapshots, _):
            for bad in (-1, True, 2**63):
                with pytest.raises(ValueError):
                    await snapshots.begin(bad, "g")
            for seconds in (0, 301, float("nan"), float("inf"), True):
                with pytest.raises(ValueError):
                    await snapshots.begin(0, "g", lease_seconds=seconds)
            lease = await snapshots.begin(0, "g")
            assert lease is not None
            with pytest.raises(ValueError):
                await snapshots.stage(lease, [VectorRecord("a", [1, 0, 0]), VectorRecord("b", [0, 0, 0])])
            with pytest.raises(ValueError):
                await snapshots.stage(lease, [VectorRecord("a", [1, 0, 0])], ["a"])
            await snapshots.stage(lease, [VectorRecord("b", [1, 0, 0]), VectorRecord("a", [1, 0, 0])])
            assert await snapshots.publish(lease)
            read = await snapshots.pin("g")
            assert [h.key for h in await snapshots.search(read, [1, 0, 0])] == ["a", "b"]
            assert [h.key for h in await snapshots.search(read, [1, 0, 0], allowed_ids=["b"])] == ["b"]
            assert not await snapshots.search(read, [1, 0, 0], allowed_ids=[])
            assert not await snapshots.search(read, [1, 0, 0], top_k=0)
            assert await snapshots.begin(0, "g") is None
            with pytest.raises(SnapshotConflict):
                await snapshots.begin(0, "other")
            with pytest.raises(SnapshotConflict):
                await snapshots.begin(1, "g")
    asyncio.run(run())


def test_garbage_collection_bound_and_active_builder_base_preservation() -> None:
    async def run() -> None:
        async with backend() as (snapshots, _):
            await publish(snapshots, 0, "zero", [VectorRecord(f"a{i}", [1, 0, 0]) for i in range(12)])
            retained = await snapshots.begin(2, "retained")
            assert retained is not None
            await publish(snapshots, 1, "one", [], full=True)
            assert await snapshots.collect_garbage(limit=3) == 0
            await snapshots.abort(retained)
            assert await snapshots.collect_garbage(limit=3) == 3
            assert await snapshots.collect_garbage(limit=3) == 3
            assert await snapshots.collect_garbage(limit=100) == 6
            assert await snapshots.collect_garbage() == 0
    asyncio.run(run())


def test_concurrent_readers_do_not_lock_owner_head_or_each_other() -> None:
    async def run() -> None:
        async with backend() as (snapshots, pool):
            await publish(snapshots, 0, "zero", [VectorRecord("a", [1, 0, 0])])
            readers = await asyncio.gather(*(snapshots.pin("zero") for _ in range(4)))
            # A long-lived reader lock must not prevent publication or other
            # readers: this simulates an independent pooled request/process.
            async with pool.acquire() as db, db.transaction():
                await db.execute("SELECT token FROM commontrace_vector_readers WHERE token=$1 FOR SHARE", readers[0].token)
                await asyncio.wait_for(publish(snapshots, 1, "one", [VectorRecord("a", [0, 1, 0])]), 3)
                hits = await asyncio.wait_for(asyncio.gather(*[
                    snapshots.search(read, [1, 0, 0]) for read in readers]), 3)
                assert all(page[0].score == 1 for page in hits)
            await asyncio.gather(*(snapshots.release(read) for read in readers))
            with pytest.raises(SnapshotConflict):
                await snapshots.search(readers[0], [1, 0, 0])
    asyncio.run(run())


def test_bounded_metadata_cleanup_preserves_remaining_pinnable_generations() -> None:
    async def run() -> None:
        async with backend() as (snapshots, _):
            for revision in range(6):
                await publish(snapshots, revision, f"r{revision}",
                              [VectorRecord("a", [1, revision + 1, 0])])
            assert await snapshots.collect_garbage(limit=1) <= 1
            surviving = 0
            for revision in range(6):
                try:
                    read = await snapshots.pin(f"r{revision}")
                except SnapshotConflict:
                    continue
                surviving += 1
                hits = await snapshots.search(read, [1, revision + 1, 0])
                assert len(hits) == 1 and hits[0].score == pytest.approx(1, abs=1e-6)
                await snapshots.release(read)
            assert surviving == 5
    asyncio.run(run())


def test_collector_waits_for_in_flight_reader_even_after_lease_expiry() -> None:
    async def run() -> None:
        async with backend() as (snapshots, pool):
            await publish(snapshots, 0, "zero", [VectorRecord("a", [1, 0, 0])])
            read = await snapshots.pin("zero")
            await publish(snapshots, 1, "one", [VectorRecord("a", [0, 1, 0])])
            async with pool.acquire() as db:
                await db.execute("UPDATE commontrace_vector_readers SET expires=clock_timestamp()-interval '1 second' "
                                 "WHERE tenant=$1 AND token=$2", snapshots._scope[0], read.token)
            # Simulate a search that acquired its lease before DB-clock expiry
            # and still holds FOR SHARE while reading immutable old intervals.
            async with pool.acquire() as db, db.transaction():
                await db.execute("SELECT token FROM commontrace_vector_readers WHERE token=$1 FOR SHARE", read.token)
                collection = asyncio.create_task(snapshots.collect_garbage())
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(collection), 0.1)
                assert await db.fetchval("SELECT count(*) FROM commontrace_vector_versions "
                                         "WHERE tenant=$1 AND valid_to IS NOT NULL", snapshots._scope[0]) == 1
            assert await asyncio.wait_for(collection, 3) == 1
    asyncio.run(run())


def test_successful_staging_renews_original_build_duration_without_resurrection() -> None:
    async def run() -> None:
        async with backend() as (snapshots, pool):
            lease = await snapshots.begin(0, "progress", lease_seconds=30)
            assert lease is not None
            async with pool.acquire() as db:
                previous = await db.fetchval("UPDATE commontrace_vector_builds SET "
                                             "expires=clock_timestamp()+interval '5 seconds' "
                                             "WHERE tenant=$1 AND token=$2 RETURNING expires",
                                             snapshots._scope[0], lease.token)
            await snapshots.stage(lease, [VectorRecord("a", [1, 0, 0])])
            async with pool.acquire() as db:
                renewed = await db.fetchval("SELECT expires FROM commontrace_vector_builds "
                                            "WHERE tenant=$1 AND token=$2", snapshots._scope[0], lease.token)
                assert (renewed - previous).total_seconds() > 24
                assert await db.fetchval("SELECT duration FROM commontrace_vector_builds "
                                         "WHERE tenant=$1 AND token=$2", snapshots._scope[0], lease.token) == 30
                await db.execute("UPDATE commontrace_vector_builds SET expires=clock_timestamp()-interval '1 second' "
                                 "WHERE tenant=$1 AND token=$2", snapshots._scope[0], lease.token)
            with pytest.raises(SnapshotConflict):
                await snapshots.stage(lease, [VectorRecord("b", [0, 1, 0])])
            assert not await snapshots.publish(lease)
    asyncio.run(run())


@pytest.mark.parametrize("forged_field", range(6))
def test_forged_abort_cannot_consume_legitimate_build(forged_field: int) -> None:
    async def run() -> None:
        async with backend() as (snapshots, _):
            lease = await snapshots.begin(0, "legitimate")
            assert lease is not None
            await snapshots.stage(lease, [VectorRecord("a", [1, 0, 0])])
            forged = (replace(lease, token="forged-token"), replace(lease, fence=999),
                      replace(lease, base_revision=999), replace(lease, target_revision=999),
                      replace(lease, generation="forged-generation"), replace(lease, full=True))
            await snapshots.abort(forged[forged_field])
            assert await snapshots.publish(lease)
            read = await snapshots.pin("legitimate")
            assert [hit.key for hit in await snapshots.search(read, [1, 0, 0])] == ["a"]
    asyncio.run(run())


def test_initialize_waits_before_ddl_while_builder_owns_head(monkeypatch) -> None:
    """Force the former head/builds DDL inversion using independent connections."""
    async def run() -> None:
        async with backend() as (snapshots, pool):
            head_owned, continue_builder = asyncio.Event(), asyncio.Event()
            original = snapshots._locked
            builder_pid = 0

            async def hold_head(db: Any) -> Any:
                nonlocal builder_pid
                row = await original(db)
                builder_pid = db.get_server_pid()
                head_owned.set()
                await continue_builder.wait()
                return row

            monkeypatch.setattr(snapshots, "_locked", hold_head)
            builder = asyncio.create_task(snapshots.begin(1, "builder"))
            initializer: asyncio.Task[None] | None = None
            try:
                await asyncio.wait_for(head_owned.wait(), 5)
                other = PostgresSnapshots(pool, snapshots._scope)
                initializer = asyncio.create_task(other.initialize())
                async with pool.acquire() as observer:
                    async def initialization_blocked_at_barrier() -> None:
                        while not await observer.fetchval(
                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE "
                            "$1::integer=ANY(pg_blocking_pids(pid)) AND wait_event_type='Lock' "
                            "AND query='SELECT pg_advisory_xact_lock(736482091503)')", builder_pid):
                            if initializer is not None and initializer.done():
                                initializer.result()
                                raise AssertionError("initializer bypassed the live builder schema barrier")
                            await asyncio.sleep(0.01)
                    await asyncio.wait_for(initialization_blocked_at_barrier(), 5)
                    # Shared barrier was taken before the head lock. DDL has
                    # not acquired builds-table locks, so this INSERT completes.
                    continue_builder.set()
                    lease = await asyncio.wait_for(builder, 5)
                    await asyncio.wait_for(initializer, 5)
                assert lease is not None
                monkeypatch.setattr(snapshots, "_locked", original)
                await snapshots.stage(lease, [VectorRecord("evidence", [1, 0, 0])])
                assert await snapshots.publish(lease)
                read = await snapshots.pin("builder")
                assert [hit.key for hit in await snapshots.search(read, [1, 0, 0])] == ["evidence"]
                await snapshots.release(read)
            finally:
                continue_builder.set()
                tasks = [builder] + ([initializer] if initializer is not None else [])
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["head", "search"])
def test_metadata_and_vector_reads_wait_before_schema_ddl(operation: str) -> None:
    async def run() -> None:
        async with backend() as (snapshots, pool):
            await publish(snapshots, 1, "first", [VectorRecord("evidence", [1, 0, 0])])
            read = await snapshots.pin("first")
            pending: asyncio.Task[Any] | None = None
            try:
                async with pool.acquire() as schema_connection:
                    async with schema_connection.transaction():
                        await schema_connection.execute("SELECT pg_advisory_xact_lock(736482091503)")
                        schema_pid = schema_connection.get_server_pid()
                        pending = asyncio.create_task(snapshots.head() if operation == "head" else
                                                      snapshots.search(read, [1, 0, 0]))
                        async with pool.acquire() as observer:
                            async def reader_blocked_at_barrier() -> None:
                                while not await observer.fetchval(
                                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE "
                                    "$1::integer=ANY(pg_blocking_pids(pid)) AND wait_event_type='Lock' "
                                    "AND query='SELECT pg_advisory_xact_lock_shared(736482091503)')", schema_pid):
                                    if pending is not None and pending.done():
                                        pending.result()
                                        raise AssertionError("read bypassed schema initialization barrier")
                                    await asyncio.sleep(0.01)
                            await asyncio.wait_for(reader_blocked_at_barrier(), 5)
                        assert not pending.done()
                    result = await asyncio.wait_for(pending, 5)
                if operation == "head":
                    assert result == SnapshotHead(1, "first")
                else:
                    assert [hit.key for hit in result] == ["evidence"]
            finally:
                if pending is not None:
                    if not pending.done():
                        pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                await snapshots.release(read)
    asyncio.run(run())
