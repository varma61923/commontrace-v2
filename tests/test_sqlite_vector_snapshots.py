"""Exact SQLite publication, immutable readers and bounded reclamation."""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace

import pytest

from commontrace.vector_snapshots import SnapshotConflict
from commontrace.vector_store import SQLiteVectorIndex, VectorRecord


def test_private_builds_atomic_winner_stable_reads_and_erasure(tmp_path):
    async def run():
        index = await SQLiteVectorIndex.open(str(tmp_path / 'vectors.db'), tenant='owner', namespace='memory',
                                            model='model', dimension=3)
        try:
            snapshots = index.snapshots
            first = await snapshots.begin(0, 'first', full=True)
            assert first is not None
            await snapshots.stage(first, [VectorRecord('a', [1, 0, 0]), VectorRecord('b', [0, 1, 0])])
            with pytest.raises(SnapshotConflict):
                await snapshots.pin('first')
            assert await snapshots.publish(first)
            old = await snapshots.pin('first')
            left, right = await asyncio.gather(snapshots.begin(1, 'left'), snapshots.begin(2, 'right'))
            assert left is not None and right is not None and left.fence != right.fence
            await snapshots.stage(left, [VectorRecord('a', [-1, 0, 0])], ['b'])
            await snapshots.stage(right, [VectorRecord('c', [0, 0, 1])])
            assert await snapshots.publish(left)
            assert not await snapshots.publish(right)
            await snapshots.abort(right)
            current = await snapshots.pin('left')
            assert [h.key for h in await snapshots.search(current, [1, 0, 0])] == ['a']
            assert [h.key for h in await snapshots.search(old, [1, 0, 0])] == ['a', 'b']
            await snapshots.collect_garbage(limit=1)
            assert [h.key for h in await snapshots.search(old, [1, 0, 0])] == ['a', 'b']
            await snapshots.release(old)
            erased = sum([await snapshots.collect_garbage(limit=1) for _ in range(4)])
            assert erased == 2
            with pytest.raises(SnapshotConflict):
                await snapshots.pin('first')
            assert (await index.search([1, 0, 0]))[0].key == 'a'
            await snapshots.release(current)
            # Direct legacy CRUD generations remain readable independently.
            await index.upsert([VectorRecord('legacy', [0, 1, 0], 'legacy-generation')])
            assert (await index.search([0, 1, 0], generation='legacy-generation'))[0].key == 'legacy'
        finally:
            await index.close()
    asyncio.run(run())


@pytest.mark.parametrize('field,value', [('fence', 999), ('base_revision', 99), ('target_revision', 99),
                                       ('generation', 'forged'), ('full', True)])
def test_forged_abort_cannot_consume_build(tmp_path, field, value):
    async def run():
        index = await SQLiteVectorIndex.open(str(tmp_path / 'vectors.db'), tenant='owner', namespace='memory',
                                            model='model', dimension=3)
        try:
            lease = await index.snapshots.begin(1, 'valid')
            assert lease is not None
            await index.snapshots.stage(lease, [VectorRecord('a', [1, 0, 0])])
            await index.snapshots.abort(replace(lease, **{field: value}))
            assert await index.snapshots.publish(lease)
            read = await index.snapshots.pin('valid')
            await index.snapshots.release(replace(read, revision=999))
            await index.snapshots.release(replace(read, generation='forged'))
            assert (await index.snapshots.search(read, [1, 0, 0]))[0].key == 'a'
            await index.snapshots.release(read)
        finally:
            await index.close()
    asyncio.run(run())


def test_expiry_cannot_be_resurrected_and_live_stage_renews(tmp_path):
    async def run():
        path = str(tmp_path / 'vectors.db')
        index = await SQLiteVectorIndex.open(path, tenant='owner', namespace='memory', model='model', dimension=3)
        try:
            lease = await index.snapshots.begin(1, 'valid', lease_seconds=10)
            assert lease is not None
            with sqlite3.connect(path) as db:
                db.execute('UPDATE commontrace_snapshot_builds SET expires=expires-9')
                before = db.execute('SELECT expires FROM commontrace_snapshot_builds').fetchone()[0]
            await index.snapshots.stage(lease, [VectorRecord('a', [1, 0, 0])])
            with sqlite3.connect(path) as db:
                after = db.execute('SELECT expires FROM commontrace_snapshot_builds').fetchone()[0]
                assert after > before + 8
                db.execute('UPDATE commontrace_snapshot_builds SET expires=0')
            with pytest.raises(SnapshotConflict):
                await index.snapshots.stage(lease, [])
            assert not await index.snapshots.publish(lease)
            assert await index.snapshots.collect_garbage() == 1
        finally:
            await index.close()
    asyncio.run(run())
