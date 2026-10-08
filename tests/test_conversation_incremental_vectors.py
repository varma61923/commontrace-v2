"""Incremental materialization exercises real canonical and vector databases."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from commontrace.conversation import AsyncStore, ConversationError, Store, embed
from commontrace.conversation import store as store_module
from commontrace.conversation.store import write_txn
from tests.test_conversation_vector_backend import encoder as vector_encoder
from tests.test_conversation_vector_backend import engine as vector_engine
from tests.test_conversation_vector_backend import options

encoder, engine = vector_encoder, vector_engine


def test_restart_and_append_stage_only_changed_units(tmp_path, engine, encoder, monkeypatch):
    tenant, opening = engine
    embedded_documents = []
    model = embed._model('minilm')
    encode = model.encode
    def counting_encode(texts, **kwargs):
        embedded_documents.extend(text for text in texts if text.startswith('user:'))
        return encode(texts, **kwargs)
    monkeypatch.setattr(model, 'encode', counting_encode)
    async def run():
        index = await opening()
        staged = []
        original = index.snapshots.stage
        async def counting(lease, records, deleted_keys=()):
            records, deleted = list(records), list(deleted_keys)
            staged.append((len(records), len(deleted), lease.full))
            return await original(lease, records, deleted)
        monkeypatch.setattr(index.snapshots, 'stage', counting)
        try:
            with Store(str(tmp_path), 'memory') as canonical:
                canonical.add('initial', ({'id': str(i), 'text': f'Lynx resting on branch number {i}.'}
                                         for i in range(1000)), extract_profile=False)
            started = time.perf_counter()
            async with await AsyncStore.open(str(tmp_path), 'memory', vector_index=index, tenant=tenant) as store:
                assert 'Lynx' in (await store.recall('feline', options=options(pool=3))).context
                cold_seconds = time.perf_counter() - started
                assert sum(row[0] for row in staged) == 1000 and all(row[2] for row in staged)
                assert len(embedded_documents) == 1000
            staged.clear()
            embedded_documents.clear()
            async with await AsyncStore.open(str(tmp_path), 'memory', vector_index=index, tenant=tenant) as reopened:
                await reopened.recall('feline', options=options(pool=3))
                assert staged == []  # Durable cursor reuse, independent of instance cache.
                assert embedded_documents == []
                await reopened.add('changed', [{'text': 'Lynx rests by a violet waterfall.'}], extract_profile=False)
                started = time.perf_counter()
                await reopened.recall('feline', options=options(pool=3))
                warm_seconds = time.perf_counter() - started
                assert staged == [(1, 0, False)]
                assert embedded_documents == ['user: Lynx rests by a violet waterfall.']
                staged.clear()
                embedded_documents.clear()
                with Store(str(tmp_path), 'memory') as canonical:
                    canonical.delete_session('changed')
                await reopened.recall('feline', options=options(pool=3))
                assert staged == [(0, 1, False)]
                assert embedded_documents == []
            print(json.dumps({'units': 1000, 'cold_seconds': cold_seconds,
                              'one_unit_delta_seconds': warm_seconds, 'delta_vectors_written': 1}))
        finally:
            await index.close()
    asyncio.run(run())


def test_independent_instances_same_revision_both_recall_complete_snapshot(tmp_path, engine, encoder, monkeypatch):
    tenant, opening = engine
    async def run():
        first, second = await opening(), await opening()
        try:
            with Store(str(tmp_path), 'memory') as canonical:
                canonical.add('cats', [{'text': 'Lynx resting'}], extract_profile=False)
            barrier = asyncio.Event()
            arrivals = 0
            for index in (first, second):
                original = index.snapshots.begin
                async def simultaneous(*args, _original=original, **kwargs):
                    nonlocal arrivals
                    lease = await _original(*args, **kwargs)
                    arrivals += 1
                    if arrivals == 2:
                        barrier.set()
                    await asyncio.wait_for(barrier.wait(), 5)
                    return lease
                monkeypatch.setattr(index.snapshots, 'begin', simultaneous)
            async with await AsyncStore.open(str(tmp_path), 'memory', vector_index=first, tenant=tenant) as a:
                async with await AsyncStore.open(str(tmp_path), 'memory', vector_index=second, tenant=tenant) as b:
                    recalls = await asyncio.gather(a.recall('feline', options=options()),
                                                   b.recall('feline', options=options()))
                    assert all('Lynx' in result.context for result in recalls)
                    assert a.vector_generation == b.vector_generation
        finally:
            await first.close()
            await second.close()
    asyncio.run(run())


def test_journal_gap_bootstraps_current_units_without_deleted_evidence(tmp_path, engine, encoder, monkeypatch):
    monkeypatch.setattr(store_module, 'UNIT_JOURNAL_LIMIT', 2)
    tenant, opening = engine
    async def run():
        index = await opening()
        try:
            async with await AsyncStore.open(str(tmp_path), 'memory', vector_index=index, tenant=tenant) as store:
                await store.add('removed', [{'text': 'Lynx by the window.'}], extract_profile=False)
                await store.recall('feline', options=options())
                with Store(str(tmp_path), 'memory') as canonical:
                    canonical.delete_session('removed')
                    canonical.add('new', [{'text': f'Rocket in orbit number {i}.'} for i in range(6)],
                                  extract_profile=False)
                result = await store.recall('space', options=options())
                assert 'Lynx' not in result.context and 'Rocket' in result.context
                assert len(await index.search([1, 0, 0], top_k=10)) == 6
        finally:
            await index.close()
    asyncio.run(run())


def test_journal_rolls_back_and_resolves_reused_ids_to_target_state(tmp_path):
    with Store(str(tmp_path), 'memory') as canonical:
        canonical.add('old', [{'text': 'Original body'}], extract_profile=False)
        old_id = canonical.units()[0][0]
        before = int(canonical.unit_stamp()[0])
        with pytest.raises(RuntimeError):
            with write_txn(canonical.db):
                canonical.db.execute('DELETE FROM units')
                raise RuntimeError('rollback')
        assert int(canonical.unit_stamp()[0]) == before
        assert canonical.unit_changes(before, before) == []
        canonical.delete_session('old')
        canonical.add('new', [{'text': 'Replacement body'}], extract_profile=False)
        # Explicit ID moves must close the old vector as well as add the new one.
        with write_txn(canonical.db):
            canonical.db.execute('UPDATE units SET id=?', (old_id + 10,))
            canonical.db.execute('UPDATE units SET id=?', (old_id,))
        changes = canonical.unit_changes(before, int(canonical.unit_stamp()[0]))
        assert len(changes) == 6
        assert all(change.body.endswith('Replacement body') for change in changes if change.unit == old_id)
        assert all(change.body is None for change in changes if change.unit != old_id)
        with pytest.raises(ValueError):
            canonical.unit_changes(-1, 1)
        with pytest.raises(ConversationError, match='retention gap'):
            canonical.db.execute("UPDATE meta SET value='100' WHERE key='unit_journal_floor'")
            canonical.unit_changes(before, before)


def test_heartbeat_preserves_live_builder_through_slow_encoding(tmp_path, engine, encoder, monkeypatch):
    from commontrace.conversation import async_store as async_module
    monkeypatch.setattr(async_module, 'VECTOR_HEARTBEAT_SECONDS', 0.05)
    tenant, opening = engine
    async def run():
        index = await opening()
        try:
            begin = index.snapshots.begin
            async def short_lease(*args, **kwargs):
                return await begin(*args, **kwargs, lease_seconds=1)
            monkeypatch.setattr(index.snapshots, 'begin', short_lease)
            async with await AsyncStore.open(str(tmp_path), 'memory', vector_index=index, tenant=tenant) as store:
                await store.add('cats', [{'text': 'Lynx resting'}], extract_profile=False)
                request = store._request
                slow = True
                async def delayed(operation, *args, **kwargs):
                    nonlocal slow
                    if operation == 'vector_batch' and slow:
                        slow = False
                        await asyncio.sleep(1.2)  # Longer than the initial DB lease.
                    return await request(operation, *args, **kwargs)
                monkeypatch.setattr(store, '_request', delayed)
                assert 'Lynx' in (await store.recall('feline', options=options())).context
                assert not [task for task in asyncio.all_tasks() if task.get_name() == 'commontrace-vector-build-lease']
        finally:
            await index.close()
    asyncio.run(run())


def test_stalled_build_budget_aborts_private_state_and_recovers(tmp_path, engine, encoder, monkeypatch):
    from commontrace.conversation import async_store as async_module
    monkeypatch.setattr(async_module, 'VECTOR_BUILD_TIMEOUT_SECONDS', 0.03)
    monkeypatch.setattr(async_module, 'VECTOR_HEARTBEAT_SECONDS', 0.01)
    tenant, opening = engine
    async def run():
        index = await opening()
        try:
            async with await AsyncStore.open(str(tmp_path), 'memory', vector_index=index, tenant=tenant) as store:
                await store.add('cats', [{'text': 'Lynx resting'}], extract_profile=False)
                request = store._request
                async def stalled(operation, *args, **kwargs):
                    if operation == 'vector_batch':
                        await asyncio.sleep(1)
                    return await request(operation, *args, **kwargs)
                monkeypatch.setattr(store, '_request', stalled)
                with pytest.raises(ConversationError, match='budget'):
                    await store.recall('feline', options=options())
                assert (await index.snapshots.head()).revision == -1
                monkeypatch.setattr(store, '_request', request)
                monkeypatch.setattr(async_module, 'VECTOR_BUILD_TIMEOUT_SECONDS', 10)
                assert 'Lynx' in (await store.recall('feline', options=options())).context
                assert not [task for task in asyncio.all_tasks() if task.get_name() == 'commontrace-vector-build-lease']
        finally:
            await index.close()
    asyncio.run(run())
