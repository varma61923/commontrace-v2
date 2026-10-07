"""Real vector engines feed the existing hybrid/context-security pipeline."""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import uuid

import pytest

from commontrace.conversation import AsyncStore, ConversationError, Options, Store, embed, recall
from commontrace.conversation.search import DenseCandidates
from commontrace.conversation.store import db_path
from commontrace.vector_store import PostgresVectorIndex, SQLiteVectorIndex

np = pytest.importorskip("numpy")


@pytest.fixture
def encoder(monkeypatch):
    class Encoder:
        def encode(self, texts, **kwargs):
            vectors = []
            for text in texts:
                words = set(re.findall(r"[a-z]+", text.lower()))
                values = np.array([len(words & {"cat", "feline", "lynx"}),
                                   len(words & {"rocket", "space"}),
                                   len(words & {"sea", "waves"})], dtype=np.float32) + 0.01
                vectors.append(values / np.linalg.norm(values))
            return np.asarray(vectors, dtype=np.float32)
    model = Encoder()
    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "_model", lambda _tag: model)


@pytest.fixture(params=["sqlite", "pgexact", "pghnsw"])
def engine(request, tmp_path):
    dsn = os.environ.get("COMMONTRACE_TEST_PGVECTOR_DSN")
    if request.param != "sqlite" and not dsn:
        pytest.skip("set COMMONTRACE_TEST_PGVECTOR_DSN to exercise real pgvector")
    tenant = "conversation-test-" + uuid.uuid4().hex
    async def opening():
        common = {"tenant": tenant, "namespace": "memory", "model": embed.MODELS["minilm"][0], "dimension": 3}
        if request.param == "sqlite":
            return await SQLiteVectorIndex.open(str(tmp_path / "vectors.db"), **common)
        return await PostgresVectorIndex.open(dsn, approximate=request.param == "pghnsw", **common)
    return tenant, opening


def options(**kwargs):
    return Options(embedder="minilm", rerank=None, neighbours_before=0, neighbours_after=0,
                   profile_facts=0, instructions=0, summaries=False, entity_boost=0, recency_boost=0, **kwargs)


def test_real_backend_hybrid_recall_and_canonical_deletion(tmp_path, engine, encoder):
    tenant, opening = engine
    async def run():
        index = await opening()
        try:
            async with await AsyncStore.open(str(tmp_path), "memory", vector_index=index, tenant=tenant) as store:
                await store.add("cats", [{"text": "A lynx rests beside the window."}], extract_profile=False)
                await store.add("rockets", [{"text": "A rocket launches toward space."}], extract_profile=False)
                found = await store.recall("feline", options=options())
                assert "lynx" in found.context  # No sparse token overlap: the external dense arm found it.
                filtered = await store.recall("feline", options=options(sessions=("rockets",)))
                assert "lynx" not in filtered.context
                assert "rocket" in filtered.context
                with Store(str(tmp_path), "memory") as canonical:
                    canonical.delete_session("cats")
                await store.recall("rocket", options=options())
                assert store.vector_generation is not None
                # Owner-controlled maintenance runs after all readers/builders
                # of this scope have quiesced; recall never prunes automatically.
                await index.prune(store.vector_generation)
                remaining = await index.search([1, 0, 0], top_k=10)
                with Store(str(tmp_path), "memory") as canonical:
                    valid = {str(row[0]) for row in canonical.units()}
                assert {hit.key for hit in remaining} == valid
        finally:
            await index.close()
    asyncio.run(run())


def test_concurrent_source_change_rejects_prefetched_candidates(tmp_path, engine, encoder, monkeypatch):
    tenant, opening = engine
    async def run():
        index = await opening()
        try:
            async with await AsyncStore.open(str(tmp_path), "memory", vector_index=index, tenant=tenant) as store:
                await store.add("cats", [{"text": "A lynx rests."}], extract_profile=False)
                original = index.search
                changed = False
                async def changing(*args, **kwargs):
                    nonlocal changed
                    if not changed:
                        changed = True
                        await store.add("more", [{"text": "Sea waves roll."}], extract_profile=False)
                    return await original(*args, **kwargs)
                monkeypatch.setattr(index, "search", changing)
                with pytest.raises(ConversationError, match="stale"):
                    await store.recall("feline", options=options())
                assert "lynx" in (await store.recall("feline", options=options())).context
        finally:
            await index.close()
    asyncio.run(run())


def test_wrong_owner_namespace_or_model_fails_closed(tmp_path, engine, encoder):
    tenant, opening = engine
    async def run():
        index = await opening()
        try:
            with pytest.raises(ValueError, match="tenant"):
                await AsyncStore.open(str(tmp_path), "memory", vector_index=index, tenant="another-owner")
            with pytest.raises(ValueError, match="space"):
                await AsyncStore.open(str(tmp_path), "another-space", vector_index=index, tenant=tenant)
            async with await AsyncStore.open(str(tmp_path), "memory", vector_index=index, tenant=tenant) as store:
                await store.add("s", [{"text": "Lynx resting"}], extract_profile=False)
                with pytest.raises(ConversationError, match="model"):
                    await store.recall("feline", options=Options(embedder="arctic-m"))
        finally:
            await index.close()
    asyncio.run(run())


def test_precomputed_ids_are_rejoined_and_untrusted_text_never_injected(tmp_path):
    with Store(str(tmp_path), "memory") as store:
        store.add("s", [{"text": "Safe canonical evidence"}])
        dense = DenseCandidates(store._units_identity, store.unit_stamp(), embed.MODELS["minilm"][0],
                                {"question": (999999, 1)})
        found = recall(store, "question", options=options(), dense_candidates=dense)
        assert "canonical evidence" in found.context
        assert 999999 not in found.turns
        stale = DenseCandidates(store._units_identity, ("old",), embed.MODELS["minilm"][0], {"question": (1,)})
        with pytest.raises(ConversationError, match="stale"):
            recall(store, "question", options=options(), dense_candidates=stale)


def test_filtered_corpus_larger_than_engine_batch_budget(tmp_path, encoder):
    async def run():
        index = await SQLiteVectorIndex.open(str(tmp_path / "vectors.db"), tenant="large", namespace="memory",
                                            model=embed.MODELS["minilm"][0], dimension=3)
        try:
            with Store(str(tmp_path), "memory") as stored:
                stored.add("large", ({"id": str(i), "text": "Lynx resting"} for i in range(10001)),
                           extract_profile=False)
            async with await AsyncStore.open(str(tmp_path), "memory", vector_index=index, tenant="large") as store:
                found = await store.recall("feline", options=options(sessions=("large",), pool=3))
                assert "Lynx" in found.context
                assert len(found.turns) == 3
        finally:
            await index.close()
    asyncio.run(run())


def test_distinct_canonical_roots_cannot_overwrite_one_vector_scope(tmp_path, engine, encoder):
    tenant, opening = engine
    async def run():
        index = await opening()
        try:
            async with await AsyncStore.open(str(tmp_path / "first"), "memory", vector_index=index,
                                            tenant=tenant) as first:
                await first.add("s", [{"text": "A lynx rests."}], extract_profile=False)
                assert "lynx" in (await first.recall("feline", options=options())).context
                async with await AsyncStore.open(str(tmp_path / "second"), "memory", vector_index=index,
                                                tenant=tenant) as second:
                    await second.add("s", [{"text": "Rocket launches."}], extract_profile=False)
                    with pytest.raises(ValueError, match="different canonical source"):
                        await second.recall("feline", options=options())
                assert "lynx" in (await first.recall("feline", options=options())).context
        finally:
            await index.close()
    asyncio.run(run())


def test_copied_database_identity_does_not_bypass_source_binding(tmp_path, engine, encoder):
    tenant, opening = engine
    async def run():
        index = await opening()
        first_root, second_root = str(tmp_path / "original"), str(tmp_path / "copied")
        try:
            async with await AsyncStore.open(first_root, "memory", vector_index=index, tenant=tenant) as first:
                await first.add("s", [{"text": "Lynx resting."}], extract_profile=False)
                await first.recall("feline", options=options())
                destination = db_path(second_root, "memory")
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                with Store(first_root, "memory") as source:
                    target = sqlite3.connect(destination)
                    try:
                        source.db.backup(target)
                    finally:
                        target.close()
                    original_identity = source._units_identity
                with Store(second_root, "memory") as copied:
                    assert copied._units_identity == original_identity
                async with await AsyncStore.open(second_root, "memory", vector_index=index, tenant=tenant) as second:
                    with pytest.raises(ValueError, match="different canonical source"):
                        await second.recall("feline", options=options())
        finally:
            await index.close()
    asyncio.run(run())
