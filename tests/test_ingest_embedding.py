"""The ingest pipeline's embedding stage: bounded batches, bounded concurrency, content-hash cache."""
from __future__ import annotations

import os
import threading
import time

import pytest

from commontrace import embeddings
from commontrace.conversation import store as conv_store
from commontrace.ingest import Chunk, catalog
from commontrace.ingest import embedding as ingest_embedding
from commontrace.ingest import pipeline as pl


class FakeProvider:
    """Deterministic 4-d vectors; records batch sizes and peak concurrency; fails on 'boom'."""

    instances: list["FakeProvider"] = []

    def __init__(self, spec=None, *, delay=0.0):
        self.spec = spec or embeddings.Spec("fakeingest", "m")
        self.batches: list[int] = []
        self.delay = delay
        self.active = self.peak = 0
        self._lock = threading.Lock()
        FakeProvider.instances.append(self)

    def embed(self, texts, *, query):
        assert query is False
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.batches.append(len(texts))
        try:
            time.sleep(self.delay)
            if any("boom" in t for t in texts):
                raise RuntimeError("provider rejected the batch")
            return [[float(len(t)), 1.0, 0.5, 0.25] for t in texts]
        finally:
            with self._lock:
                self.active -= 1


if "fakeingest" not in embeddings._CUSTOM:
    embeddings.register("fakeingest", lambda spec: FakeProvider(spec))


def _docs(tmp_path):
    src = tmp_path / "docs"
    src.mkdir()
    (src / "runbook.md").write_text("# Failover\n\nWhen the primary fails, promote the replica.\n", encoding="utf-8")
    (src / "deploy.md").write_text("# Deploys\n\nDeploys run on Fridays after the freeze lifts.\n", encoding="utf-8")
    store = tmp_path / "store"
    os.makedirs(store / "memory")
    return str(src), str(store)


def _chunks(texts):
    return [Chunk(content=t, source_path="x.md", chunk_id=f"c{i}") for i, t in enumerate(texts)]


def test_stage_is_off_unless_configured(tmp_path, monkeypatch):
    monkeypatch.delenv(ingest_embedding.ENV, raising=False)
    src, store = _docs(tmp_path)
    result = pl.create_document_pipeline(src, store, contextualize="none").run()
    assert result.chunks_extracted >= 1 and "embeddings" not in result.to_dict()
    assert not os.path.exists(ingest_embedding.cache_path(store, "minilm"))


def test_configured_stage_embeds_new_chunks_and_skips_cached(tmp_path, monkeypatch):
    monkeypatch.setenv(ingest_embedding.ENV, "fakeingest:m")
    FakeProvider.instances.clear()
    src, store = _docs(tmp_path)
    job = catalog.create_ingest_job(store, src)
    pipeline = pl.create_document_pipeline(src, store, contextualize="none")
    first = pipeline.run(job_id=job.id)
    stats = first.to_dict()["embeddings"]
    assert stats["embedded"] == first.chunks_extracted and stats["cached"] == 0 and stats["failed"] == 0
    assert stats["embedder"] == "fakeingest:m"
    assert catalog.get_ingest_job(store, job.id).stats["embedded"] == first.chunks_extracted
    # The vectors land in the conversation layer's shared content-hash cache.
    assert os.path.basename(ingest_embedding.cache_path(store, "fakeingest:m")) == "embeddings-fakeingest_m.db"
    # Re-ingesting the same content embeds nothing.
    again = pl.create_document_pipeline(src, store, contextualize="none", force=True).run()
    stats = again.to_dict()["embeddings"]
    assert stats["embedded"] == 0 and stats["cached"] == again.chunks_extracted
    assert sum(sum(p.batches) for p in FakeProvider.instances) == first.chunks_extracted


def test_bounded_batches_and_concurrency(tmp_path):
    provider = FakeProvider(delay=0.02)
    embedder = ingest_embedding.ChunkEmbedder(str(tmp_path), "fakeingest:m", provider=provider,
                                              batch_size=2, max_workers=3)
    texts = [f"chunk number {i} about failover" for i in range(13)] + ["chunk number 0 about failover"]
    out = list(embedder.stream(_chunks(texts)))
    assert [c.content for c in out] == texts  # passes chunks through unchanged and in order
    assert max(provider.batches) <= 2 and provider.peak <= 3 and provider.peak >= 2
    assert embedder.stats == {"embedded": 13, "cached": 1, "failed": 0, "batches": 7}
    stored = embedder.vectors([conv_store._hash(texts[0])])
    assert stored[conv_store._hash(texts[0])] == pytest.approx([float(len(texts[0])), 1.0, 0.5, 0.25])
    embedder.close()


def test_failed_batches_are_counted_and_ingestion_continues(tmp_path):
    provider = FakeProvider()
    embedder = ingest_embedding.ChunkEmbedder(str(tmp_path), "fakeingest:m", provider=provider,
                                              batch_size=1, max_workers=2)
    out = list(embedder.stream(_chunks(["fine text one", "boom text", "fine text two"])))
    assert len(out) == 3
    assert embedder.stats["embedded"] == 2 and embedder.stats["failed"] == 1
    assert "provider rejected the batch" in embedder.errors[0]
    embedder.close()


def test_invalid_concurrency_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(ingest_embedding.CONCURRENCY_ENV, "99")
    with pytest.raises(ValueError):
        ingest_embedding.ChunkEmbedder(str(tmp_path), "fakeingest:m", provider=FakeProvider())
