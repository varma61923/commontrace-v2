"""The ingest pipeline's ``embedding`` stage: pre-embed new chunks in bounded batches.

Off unless ``COMMONTRACE_INGEST_EMBEDDER`` names an embedder (any
`commontrace.embeddings` tag, e.g. ``openai:text-embedding-3-small`` or
``minilm``). When on, chunks stream through `ChunkEmbedder.stream` on their way
to the submitter:

- each chunk is keyed by the conversation layer's content hash
  (`conversation.store._hash`, sha256 of the text);
- hashes already in the vector cache, or repeated within the run, are not
  embedded again (``cached``);
- the rest are embedded in batches of ``COMMONTRACE_INGEST_EMBED_BATCH`` texts
  (default: the provider's own batch size), with at most
  ``COMMONTRACE_INGEST_EMBED_CONCURRENCY`` batches in flight
  (`parallel.bounded_map`; default 4 for hosted providers, 1 for local models);
- vectors are written to the same per-model content-hash cache the
  conversation layer reads (``memory/conversations/embeddings-<model>.db``,
  float16 rows), so recall never embeds an identical passage twice;
- a failing batch is counted (``failed``) and reported as a warning; ingestion
  itself continues, since vectors are an optimisation, not evidence.

Only a bounded window (batch x concurrency x 2 chunks) is held in memory, and
the SQLite cache is only touched from the consuming thread.
"""
from __future__ import annotations

import json
import os
import re
import struct
from collections.abc import Iterable, Iterator
from typing import Any

from commontrace.ingest import Chunk

ENV = "COMMONTRACE_INGEST_EMBEDDER"
BATCH_ENV = "COMMONTRACE_INGEST_EMBED_BATCH"
CONCURRENCY_ENV = "COMMONTRACE_INGEST_EMBED_CONCURRENCY"
MAX_BATCH = 512
MAX_CONCURRENCY = 16
_SCHEMA = "CREATE TABLE IF NOT EXISTS vec (hash TEXT PRIMARY KEY, v BLOB NOT NULL)"


def configured() -> str | None:
    """The configured ingest embedder tag, or None when the stage is off."""
    raw = os.environ.get(ENV, "").strip()
    return None if raw.lower() in ("", "none", "off", "0") else raw


def _int_env(name: str, default: int, upper: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if not 1 <= value <= upper:
        raise ValueError(f"{name} must be from 1 to {upper}")
    return value


def canonical(tag: str) -> str:
    """The cache identity of a tag; a registered custom provider's tag is kept as given."""
    from commontrace import embeddings
    from commontrace.conversation import embed

    if ":" in tag and tag.split(":", 1)[0] in embeddings._CUSTOM:
        return tag
    return embed.canonical(tag)


def cache_path(root: str, tag: str) -> str:
    """The shared content-hash vector cache file for one model (as conversation/embed.py names it)."""
    from commontrace.conversation import embed
    from commontrace.conversation import store as conv_store

    canonical_tag = canonical(tag)
    safe = canonical_tag if canonical_tag in embed.MODELS else re.sub(r"[^A-Za-z0-9._-]+", "_", canonical_tag)
    return os.path.join(conv_store.conversations_dir(root), f"embeddings-{safe}.db")


def _pack(vector) -> bytes:
    values = [float(x) for x in vector]
    return struct.pack(f"<{len(values)}e", *values)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 2}e", blob))


class ChunkEmbedder:
    """Embeds chunk text into the content-hash cache, skipping what is already there."""

    def __init__(self, root: str, tag: str, *, provider=None, batch_size: int | None = None,
                 max_workers: int | None = None):
        from commontrace import embeddings

        self.root, self.tag = root, tag
        self.canonical = canonical(tag)
        if provider is None:
            provider = embeddings.provider(self.canonical)
        self.provider = provider
        spec = getattr(provider, "spec", None)
        hosted = bool(getattr(spec, "hosted", False))
        default_batch = embeddings.BATCH.get(getattr(spec, "provider", "local"), 64)
        self.batch_size = batch_size or _int_env(BATCH_ENV, default_batch, MAX_BATCH)
        self.max_workers = max_workers or _int_env(CONCURRENCY_ENV, 4 if hosted else 1, MAX_CONCURRENCY)
        if not 1 <= self.batch_size <= MAX_BATCH or not 1 <= self.max_workers <= MAX_CONCURRENCY:
            raise ValueError("embedding batch size or concurrency is out of range")
        self.stats = {"embedded": 0, "cached": 0, "failed": 0, "batches": 0}
        self.errors: list[str] = []
        self._db = None

    # -- cache ---------------------------------------------------------------------------
    def _connection(self):
        if self._db is None:
            from commontrace.conversation import store as conv_store

            path = cache_path(self.root, self.tag)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._db = conv_store.connect(path, _SCHEMA)
        return self._db

    def cached(self, hashes: list[str]) -> set[str]:
        if not hashes:
            return set()
        rows = self._connection().execute(
            "SELECT hash FROM vec WHERE hash IN (SELECT value FROM json_each(?))", (json.dumps(hashes),))
        return {row[0] for row in rows}

    def vectors(self, hashes: list[str]) -> dict[str, list[float]]:
        """Stored vectors by content hash (for callers and tests)."""
        rows = self._connection().execute(
            "SELECT hash, v FROM vec WHERE hash IN (SELECT value FROM json_each(?))", (json.dumps(hashes),))
        return {h: unpack(v) for h, v in rows}

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    # -- embedding -----------------------------------------------------------------------
    def _embed(self, batch: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], list | None, str]:
        try:
            vectors = self.provider.embed([text for _h, text in batch], query=False)
            if len(vectors) != len(batch):
                raise ValueError(f"embedder returned {len(vectors)} vectors for {len(batch)} texts")
            return batch, vectors, ""
        except Exception as exc:  # noqa: BLE001 - one failed batch must not stop ingestion
            return batch, None, f"{type(exc).__name__}: {exc}"

    def _flush(self, window: list[Chunk]) -> None:
        from commontrace.conversation import store as conv_store
        from commontrace.parallel import bounded_map

        todo: dict[str, str] = {}
        for chunk in window:
            todo.setdefault(conv_store._hash(chunk.content), chunk.content)
        known = self.cached(list(todo))
        self.stats["cached"] += len(window) - len([h for h in todo if h not in known])
        pending = [(h, t) for h, t in todo.items() if h not in known]
        batches = [pending[i:i + self.batch_size] for i in range(0, len(pending), self.batch_size)]
        rows: list[tuple[str, bytes]] = []
        for batch, vectors, error in bounded_map(self._embed, batches, max_workers=self.max_workers):
            self.stats["batches"] += 1
            if vectors is None:
                self.stats["failed"] += len(batch)
                if len(self.errors) < 5:
                    self.errors.append(f"embedding batch of {len(batch)} failed: {error}")
                continue
            rows.extend((h, _pack(v)) for (h, _t), v in zip(batch, vectors))
        if rows:
            db = self._connection()
            with conv_store.write_txn(db):
                db.executemany("INSERT OR REPLACE INTO vec VALUES (?, ?)", rows)
            self.stats["embedded"] += len(rows)

    def stream(self, chunks: Iterable[Chunk]) -> Iterator[Chunk]:
        """Pass chunks through unchanged, embedding each bounded window before it moves on."""
        window: list[Chunk] = []
        limit = self.batch_size * self.max_workers * 2
        for chunk in chunks:
            window.append(chunk)
            if len(window) >= limit:
                self._flush(window)
                yield from window
                window = []
        if window:
            self._flush(window)
            yield from window

    def summary(self) -> dict[str, Any]:
        return {"embedder": self.canonical, **self.stats, "batch_size": self.batch_size,
                "concurrency": self.max_workers}


def from_env(root: str) -> ChunkEmbedder | None:
    """A ChunkEmbedder for the configured tag, or None when the stage is off."""
    tag = configured()
    return ChunkEmbedder(root, tag) if tag else None
