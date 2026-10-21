"""The Hub binds dense vectors to the loaded model and bounds its tenant pools."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from hub.memory_embedding import LocalEmbedder, ProjectionFactory
from hub.memory_projection import ProjectionIdentity
from unimem.embeddings import EmbeddingError, Spec
from unimem.vector_store import MAX_DIMENSION


@pytest.mark.asyncio
async def test_local_embedder_identity_uses_loaded_immutable_model_revision(monkeypatch):
    revision = "a" * 40

    class Provider:
        spec = Spec("local", "bge-small")

        def __init__(self):
            self.calls = []

        def _load(self):
            return SimpleNamespace(
                _modules={"transformer": SimpleNamespace(auto_model=SimpleNamespace(
                    config=SimpleNamespace(_commit_hash=revision)))},
                get_sentence_embedding_dimension=lambda: 384,
            )

        def embed(self, texts, *, query):
            self.calls.append((texts, query))
            return [[1.0, 0.0] for _ in texts]

    provider = Provider()
    called = []

    def provider_for(tag, **kwargs):
        called.append((tag, kwargs))
        return provider

    monkeypatch.setattr("hub.memory_embedding.embeddings.provider", provider_for)
    embedder = await LocalEmbedder.load("bge-small")

    assert embedder.identity == ProjectionIdentity(
        "memory-records", "bge-small:hf-" + revision + ":adapter-v1", 384)
    assert await embedder.encode_documents(["fact"]) == [[1.0, 0.0]]
    assert await embedder.encode_query("question") == [1.0, 0.0]
    assert provider.calls == [(["fact"], False), (["question"], True)]
    assert called == [("bge-small", {"local_files_only": True})]


@pytest.mark.asyncio
async def test_fastembed_hub_projection_binds_model_revision_and_cache_only(monkeypatch):
    revision = "fastembed-BAAI/bge-small-en-v1.5-v0.8.0-sha256-" + "a" * 64

    class Provider:
        spec = Spec("fastembed", "BAAI/bge-small-en-v1.5")

        def _load(self):
            return object()

        def projection_identity(self):
            return revision, 384

    provider = Provider()
    called = []

    def provider_for(tag, **kwargs):
        called.append((tag, kwargs))
        return provider

    monkeypatch.setattr("hub.memory_embedding.embeddings.provider", provider_for)
    embedder = await LocalEmbedder.load("fastembed:BAAI/bge-small-en-v1.5")

    assert embedder.identity == ProjectionIdentity(
        "memory-records", "fastembed:BAAI/bge-small-en-v1.5:" + revision + ":adapter-v1", 384)
    assert called == [("fastembed:BAAI/bge-small-en-v1.5", {"local_files_only": True})]


@pytest.mark.asyncio
async def test_local_embedder_refuses_unrevisioned_and_hosted_providers(monkeypatch):
    class Hosted:
        spec = Spec("openai", "embedding-model")

    monkeypatch.setattr("hub.memory_embedding.embeddings.provider", lambda tag, **kwargs: Hosted())
    with pytest.raises(EmbeddingError, match="revision-bound"):
        await LocalEmbedder.load("openai:embedding-model")

    class Unrevisioned:
        spec = Spec("local", "bge-small")

        def _load(self):
            return SimpleNamespace(_modules={}, get_sentence_embedding_dimension=lambda: 384)

    monkeypatch.setattr("hub.memory_embedding.embeddings.provider", lambda tag, **kwargs: Unrevisioned())
    with pytest.raises(EmbeddingError, match="immutable revision"):
        await LocalEmbedder.load("bge-small")


@pytest.mark.asyncio
async def test_projection_checks_pgvector_before_loading_model(monkeypatch):
    factory = ProjectionFactory(object(), "postgresql+asyncpg://unused", capacity=1)
    attempted = []

    class Result:
        def scalar_one(self):
            return False

    class Session:
        async def execute(self, _statement):
            return Result()

    @asynccontextmanager
    async def session_scope(_factory):
        yield Session()

    async def load(_tag):
        attempted.append(True)
        raise AssertionError("weights must not load without pgvector")

    monkeypatch.setattr("hub.memory_embedding.session_scope", session_scope)
    monkeypatch.setattr(LocalEmbedder, "load", load)
    with pytest.raises(EmbeddingError, match="pgvector extension"):
        await factory._prepare()
    assert not attempted


@pytest.mark.asyncio
async def test_projection_pool_reuses_and_bounds_idle_and_busy_tenants(monkeypatch):
    identity = ProjectionIdentity("memory-records", "fixture:revision-1", 2)
    embedder = SimpleNamespace(identity=identity)
    closed = []

    class Index:
        def __init__(self, tenant):
            self.tenant, self.namespace, self.model, self.dimension = (
                tenant, identity.namespace, identity.model, identity.dimension)

        async def close(self):
            closed.append(self.tenant)

    factory = ProjectionFactory(object(), "unused", capacity=1)

    async def prepare():
        return embedder

    async def open_index(org, _identity):
        return Index(org)

    monkeypatch.setattr(factory, "_prepare", prepare)
    monkeypatch.setattr(factory, "_open", open_index)

    async with factory.acquire("org-a") as first:
        async with factory.acquire("org-a") as reused:
            assert first is reused
        with pytest.raises(RuntimeError, match="all projection pools are busy"):
            async with factory.acquire("org-b"):
                pass
    async with factory.acquire("org-b") as second:
        assert second.org_id == "org-b"
    assert closed == ["org-a"]
    await factory.close()
    assert closed == ["org-a", "org-b"]
    with pytest.raises(RuntimeError, match="factory is closed"):
        async with factory.acquire("org-b"):
            pass


def test_projection_dimension_is_bounded_to_vector_backend_capacity():
    with pytest.raises(ValueError):
        ProjectionIdentity("memory-records", "fixture:revision-1", MAX_DIMENSION + 1)
