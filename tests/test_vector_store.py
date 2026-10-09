from __future__ import annotations

import asyncio
import os
import threading
import uuid
from collections.abc import Awaitable, Callable

import pytest

from commontrace.vector_store import PostgresVectorIndex, SQLiteVectorIndex, VectorIndex, VectorRecord


@pytest.fixture(params=["sqlite", "pgexact", "pghnsw"])
def factory(request, tmp_path) -> Callable[..., Awaitable[VectorIndex]]:
    dsn = os.environ.get("COMMONTRACE_TEST_PGVECTOR_DSN")
    if request.param != "sqlite" and not dsn:
        pytest.skip("set COMMONTRACE_TEST_PGVECTOR_DSN to exercise real pgvector")
    owner = "test-" + uuid.uuid4().hex

    async def opening(*, tenant=owner, namespace="memory", model="test-model", dimension=3):
        if request.param == "sqlite":
            return await SQLiteVectorIndex.open(str(tmp_path / "vectors.db"), tenant=tenant,
                                                namespace=namespace, model=model, dimension=dimension)
        return await PostgresVectorIndex.open(dsn, tenant=tenant, namespace=namespace,
                                              model=model, dimension=dimension,
                                              approximate=request.param == "pghnsw")
    return opening


def test_search_upsert_filter_and_delete_contract(factory):
    async def run():
        index = await factory()
        try:
            assert await index.upsert([
                VectorRecord("exact", [2, 0, 0], "revision1"),
                VectorRecord("similar", [1, 1, 0], "revision1"),
                VectorRecord("opposite", [-1, 0, 0], "revision1"),
            ]) == 3
            hits = await index.search([1, 0, 0], top_k=3, generation="revision1")
            assert [h.key for h in hits] == ["exact", "similar", "opposite"]
            assert [h.score for h in hits] == pytest.approx([1, 2**-0.5, -1], abs=1e-6)
            assert [h.key for h in await index.search([1, 0, 0], allowed_ids=["opposite"])] == ["opposite"]
            assert not await index.search([1, 0, 0], allowed_ids=[])
            assert not await index.search([1, 0, 0], generation="different")
            await index.upsert([VectorRecord("exact", [0, 0, 1], "revision2")])
            assert [h.key for h in await index.search([1, 0, 0], generation="revision2")] == ["exact"]
            assert await index.delete(["exact", "exact", "missing"]) == 1
            assert await index.delete(["exact"]) == 0
            assert not await index.search([1, 0, 0], generation="revision2")
            assert await index.prune("revision3") == 2
            # Search without a generation must not reveal erased stale IDs.
            assert not await index.search([1, 0, 0])
        finally:
            await index.close()
    asyncio.run(run())


def test_namespace_tenant_model_and_dimensions_are_isolated(factory):
    async def run():
        # Injection-shaped owner labels must remain data, not SQL identifiers.
        first = await factory(tenant="owner'; SELECT 1; --" + uuid.uuid4().hex, namespace="a")
        other_namespace = await factory(tenant=first.tenant, namespace="b")
        other_tenant = await factory(tenant="other-owner-" + uuid.uuid4().hex, namespace="a")
        other_model = await factory(tenant=first.tenant, namespace="a", model="other-model")
        other_dimension = await factory(tenant=first.tenant, namespace="a", dimension=2)
        indices = [first, other_namespace, other_tenant, other_model, other_dimension]
        try:
            for index in indices:
                await index.upsert([VectorRecord("same-id", [1] + [0] * (index.dimension - 1), "r")])
            await first.delete(["same-id"])
            assert not await first.search([1, 0, 0])
            for index in indices[1:]:
                hits = await index.search([1] + [0] * (index.dimension - 1))
                assert [h.key for h in hits] == ["same-id"]
        finally:
            for index in indices:
                await index.close()
    asyncio.run(run())


def test_invalid_batch_is_atomic_and_bad_queries_fail(factory):
    async def run():
        index = await factory()
        try:
            for bad in ([0, 0, 0], [float("nan"), 0, 1], [float("inf"), 1, 0], [1, 2]):
                with pytest.raises(ValueError):
                    await index.upsert([VectorRecord("partial", [1, 0, 0]), VectorRecord("invalid", bad)])
                assert not await index.search([1, 0, 0])
                with pytest.raises(ValueError):
                    await index.search(bad)
            for limit in (-1, True, 10_001):
                with pytest.raises(ValueError):
                    await index.search([1, 0, 0], top_k=limit)
        finally:
            await index.close()
    asyncio.run(run())


def test_concurrent_batches_remain_durable(factory):
    async def run():
        index = await factory()
        try:
            writes = await asyncio.gather(*[
                index.upsert([VectorRecord(f"key-{i:04}", [1, i / 1000, 0])]) for i in range(32)
            ])
            assert sum(writes) == 32
            hits = await index.search([1, 0, 0], top_k=32)
            assert len(hits) == 32
            assert hits[0].key == "key-0000"
        finally:
            await index.close()
    asyncio.run(run())


def test_invalid_scope_fails_before_driver_or_database(tmp_path):
    async def run():
        for dimension in (0, True, 2001):
            with pytest.raises(ValueError, match="dimension"):
                await SQLiteVectorIndex.open(str(tmp_path / "invalid.db"), tenant="owner", namespace="space",
                                             model="model", dimension=dimension)
        assert not (tmp_path / "invalid.db").exists()
    asyncio.run(run())


def test_canonical_source_binding_is_atomic_and_immutable(factory):
    async def run():
        first = await factory()
        second = await factory(tenant=first.tenant, namespace=first.namespace)
        try:
            outcomes = await asyncio.gather(first.bind_source("source-one"), second.bind_source("source-two"),
                                            return_exceptions=True)
            assert sum(result is None for result in outcomes) == 1
            assert sum(isinstance(result, ValueError) for result in outcomes) == 1
            winner = "source-one" if outcomes[0] is None else "source-two"
            await first.bind_source(winner)
            await second.bind_source(winner)
        finally:
            await first.close()
            await second.close()
    asyncio.run(run())


def test_cancelled_sqlite_write_finishes_before_close(tmp_path):
    started, release = threading.Event(), threading.Event()
    path = str(tmp_path / "cancelled.sqlite")
    common = {"tenant": "owner", "namespace": "memory", "model": "model", "dimension": 3}
    def records():
        started.set()
        assert release.wait(5)
        yield VectorRecord("accepted", [1, 0, 0])
    async def run():
        index = await SQLiteVectorIndex.open(path, **common)
        write = asyncio.create_task(index.upsert(records()))
        while not started.is_set():
            await asyncio.sleep(0.001)
        write.cancel()
        await asyncio.gather(write, return_exceptions=True)
        closing = asyncio.create_task(index.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await closing
        reopened = await SQLiteVectorIndex.open(path, **common)
        try:
            assert [hit.key for hit in await reopened.search([1, 0, 0])] == ["accepted"]
        finally:
            await reopened.close()
    try:
        asyncio.run(run())
    finally:
        release.set()


def test_repeated_searches_keep_a_custom_plan(factory):
    # After five executions Postgres may cache a generic plan for asyncpg's prepared
    # statement; that plan casts the query vector per row and cannot use HNSW, and
    # was measured 60-85x slower. Later searches must stay as fast as the first.
    import random
    import time

    async def run():
        index = await factory(dimension=64)
        try:
            rng = random.Random(4)
            await index.upsert([VectorRecord(f"k{i}", [rng.gauss(0, 1) for _ in range(64)], "r")
                                for i in range(4000)])
            query = [rng.gauss(0, 1) for _ in range(64)]
            first, timings = None, []
            for _ in range(12):
                started = time.perf_counter()
                hits = await index.search(query, top_k=10)
                timings.append(time.perf_counter() - started)
                first = first or [h.key for h in hits]
                assert [h.key for h in hits] == first
            assert sum(timings[8:]) / 4 < 5 * max(sum(timings[1:4]) / 3, 0.002)
        finally:
            await index.close()
    asyncio.run(run())
