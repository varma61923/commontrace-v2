"""Incremental dense fact candidates using the existing scoped vector engine.

The fact event sequence is an outbox. Publish vectors before advancing its
watermark; interrupted work is idempotently replayed. Search always rechecks
canonical scope, time and evidence through search_recipes. Local SQLite uses
exact cosine; the server VectorIndex may use HNSW.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os

from commontrace import _jsonl, configuration, fact_embeddings, fact_store, paths
from commontrace.vector_store import SQLiteVectorIndex, VectorRecord


def index_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "facts", "vectors.sqlite3")


def _backend(root):
    with configuration.using(root):
        selected = configuration.getenv("COMMONTRACE_VECTOR_BACKEND", "sqlite")
    if selected not in ("sqlite", "lance"):
        raise ValueError("vector backend must be sqlite or lance")
    return selected


def _exists(root, backend):
    return os.path.isfile(index_path(root)) if backend == "sqlite" else os.path.isdir(
        os.path.join(paths.memory_dir(root), "lance"))


def _watermark(model, backend):
    return "vector-watermark:" + ("lance:" if backend == "lance" else "") + model


async def _index(root, model, dimension, backend="sqlite"):
    if backend == "lance":
        from commontrace.vector_lance import LanceVectorIndex
        return LanceVectorIndex(root, tenant="local", namespace="facts", model=model, dimension=dimension)
    filename = index_path(root)
    paths.enforce_boundary(root, filename)
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    try:
        descriptor = os.open(filename, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    except FileExistsError:
        pass
    index = await SQLiteVectorIndex.open(filename, tenant="local", namespace="facts", model=model, dimension=dimension)
    os.chmod(filename, 0o600)
    return index


def drain(root: str, *, model="bge-small", limit=128) -> dict:
    if not fact_store.enabled(root) or model in ("none", "off"):
        return {"processed": 0}
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError("projection batch must be in 1..10000")
    backend = _backend(root)
    key = _watermark(model, backend)
    with _jsonl.locked(index_path(root) + ".projection"):
        with fact_store.transaction(root) as canonical:
            previous = canonical.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            cursor = int(previous[0]) if previous and _exists(root, backend) else 0
            events = canonical.connection.execute(
                "SELECT seq,fact_id FROM events WHERE seq>? ORDER BY seq LIMIT ?", (cursor, limit)
            ).fetchall()
            if not events:
                return {"processed": 0, "through": cursor}
            facts, removed = [], []
            for identity in dict.fromkeys(row[1] for row in events):
                fact = canonical.get(identity)
                if fact is None or fact.forgotten or fact.status != "active":
                    removed.append(identity)
                else:
                    facts.append(fact)
        _, vectors = fact_embeddings.vectors(facts, model=model, root=root)
        # For a deletion-only batch recover dimension from the existing index.
        if vectors:
            dimension = len(next(iter(vectors.values())))
        else:
            resolved, _ = fact_embeddings._resolve(model, None)
            if resolved is None:
                raise RuntimeError("dense projection provider unavailable")
            dimension = len(fact_embeddings._encode(resolved, ["index dimension"], query=True)[0])

        async def publish():
            index = await _index(root, model, dimension, backend)
            try:
                await index.upsert(
                    [
                        VectorRecord(
                            fact.id, vectors[fact.id], checksum=hashlib.sha256(fact.statement.encode()).hexdigest()
                        )
                        for fact in facts
                    ]
                )
                await index.delete(removed)
                if backend == "lance":
                    await index.maintain()
            finally:
                await index.close()

        asyncio.run(publish())
        through = events[-1][0]
        with fact_store.transaction(root, write=True) as canonical:
            canonical.connection.execute(
                "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(through))
            )
        return {"processed": len(events), "through": through, "model": model, "dimension": dimension}


def search(root: str, query: str, *, model="bge-small", limit=200) -> list[tuple[str, float]]:
    backend = _backend(root)
    if model in ("none", "off") or not _exists(root, backend):
        return []
    resolved, _ = fact_embeddings._resolve(model, None)
    if resolved is None:
        return []
    vector = fact_embeddings._encode(resolved, [query], query=True)[0]

    async def recall():
        index = await _index(root, model, len(vector), backend)
        try:
            return [(hit.key, hit.score) for hit in await index.search(vector, top_k=limit)]
        finally:
            await index.close()

    hits = asyncio.run(recall())
    if not hits:
        return []
    # A canonical edit is immediately ineligible for stale dense scores until its outbox is published.
    with fact_store.transaction(root) as canonical:
        row = canonical.connection.execute(
            "SELECT value FROM meta WHERE key=?", (_watermark(model, backend),)
        ).fetchone()
        cursor = int(row[0]) if row else 0
        identities = [identity for identity, _score in hits]
        pending = {
            row[0]
            for row in canonical.connection.execute(
                "SELECT DISTINCT fact_id FROM events WHERE seq>? AND fact_id IN (SELECT value FROM json_each(?))",
                (cursor, json.dumps(identities)),
            )
        }
    return [(identity, score) for identity, score in hits if identity not in pending]
