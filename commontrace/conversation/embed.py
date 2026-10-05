"""The semantic arm of conversation memory. Vectors are cached by content hash in
one file per model, shared by every space, so a message is embedded once."""
from __future__ import annotations

import heapq
import importlib.util
import json
import os
import threading
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass

from commontrace.conversation import store as _store

MODELS = {
    "arctic-m": ("Snowflake/snowflake-arctic-embed-m-v1.5",
                 "Represent this sentence for searching relevant passages: "),
    "minilm": ("sentence-transformers/all-MiniLM-L6-v2", ""),
}
DEFAULT = "arctic-m"
MAX_SEQ = 256
BATCH = 64

_LOCK = threading.Lock()
_MODELS: dict[str, object] = {}
_WORK_LOCKS = weakref.WeakValueDictionary()
_WORK_LOCKS_GUARD = threading.Lock()
_QUERY_CACHE: OrderedDict = OrderedDict()
_QUERY_LOCK = threading.Lock()
_QUERY_IDENTITIES = weakref.WeakKeyDictionary()
QUERY_CACHE_ENTRIES = 512
QUERY_CACHE_BYTES = 8 * 1024 * 1024
QUERY_CACHE_SECONDS = 300


def _work_lock(key):
    """Coalesce reusable work across request-scoped connections, without leaking locks."""
    with _WORK_LOCKS_GUARD:
        lock = _WORK_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WORK_LOCKS[key] = lock
        return lock


def available() -> bool:
    return (importlib.util.find_spec("numpy") is not None
            and importlib.util.find_spec("sentence_transformers") is not None)


def configured() -> str | None:
    """The embedder this host uses for conversation memory, or None for lexical only."""
    tag = os.environ.get("COMMONTRACE_CONVERSATION_EMBEDDER", DEFAULT).strip().lower()
    if tag in ("", "none", "off", "0"):
        return None
    if tag not in MODELS:
        raise _store.ConversationError(
            f"COMMONTRACE_CONVERSATION_EMBEDDER must be one of {sorted(MODELS)} or none, got {tag!r}")
    return tag if available() else None


def _model(tag: str):
    with _LOCK:
        if tag not in _MODELS:
            from sentence_transformers import SentenceTransformer

            from commontrace.rerank_arm import _no_progress_bars

            name = MODELS[tag][0]
            with _no_progress_bars():
                try:
                    model = SentenceTransformer(name, device="cpu", local_files_only=True)
                except Exception:  # noqa: BLE001 - not cached yet: fetch it once
                    model = SentenceTransformer(name, device="cpu")
            model.max_seq_length = MAX_SEQ
            _MODELS[tag] = model
        return _MODELS[tag]


class Embedder:
    """Unit-normalised float32 vectors for texts, through the shared cache."""

    def __init__(self, root: str, tag: str, *, read_only: bool = False):
        import numpy as np

        self.np, self.tag, self.read_only = np, tag, read_only
        directory = _store.conversations_dir(root)
        path = os.path.join(directory, f"embeddings-{tag}.db")
        if read_only:
            # vectors missing from a frozen cache are computed but never written back
            self.db = _store.connect(path, read_only=True) if os.path.isfile(path) else \
                _store.connect(":memory:", "CREATE TABLE IF NOT EXISTS vec (hash TEXT PRIMARY KEY, v BLOB NOT NULL)")
        else:
            os.makedirs(directory, exist_ok=True)
            self.db = _store.connect(path, "CREATE TABLE IF NOT EXISTS vec (hash TEXT PRIMARY KEY, v BLOB NOT NULL)")
        # Separate connections share the same content-hash cache. Serialise the
        # read/miss/encode/write sequence, so concurrent requests encode a hash
        # once in this process. A SQLite writer lock is never held during encode.
        self._lock = _work_lock(("vectors", os.path.realpath(path)))

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def encode(self, texts: list[str], query: bool = False):
        prefix = MODELS[self.tag][1] if query else ""
        model = _model(self.tag)

        def compute(items):
            return model.encode([prefix + t for t in items], batch_size=BATCH,
                                normalize_embeddings=True, convert_to_numpy=True,
                                show_progress_bar=False).astype(self.np.float32)

        if not query or not texts:
            return compute(texts)
        with _QUERY_LOCK:
            identity = _QUERY_IDENTITIES.get(model)
            if identity is None:
                identity = object()
                _QUERY_IDENTITIES[model] = identity
        keys = [(self.tag, identity, _store._hash(prefix + text)) for text in texts]

        def cached():
            found = {}
            with _QUERY_LOCK:
                now = time.monotonic()
                for key in list(_QUERY_CACHE):
                    if _QUERY_CACHE[key][0] <= now:
                        del _QUERY_CACHE[key]
                for key in keys:
                    if key in _QUERY_CACHE:
                        _QUERY_CACHE.move_to_end(key)
                        found[key] = _QUERY_CACHE[key][1]
            return found

        found = cached()
        if len(found) < len(set(keys)):
            # Identical facet batches coalesce even when callers order them
            # differently. Cache hits never wait for unrelated model inference.
            with _work_lock(("query", self.tag, identity, tuple(sorted(k[2] for k in set(keys))))):
                found = cached()
                missing = {key: text for key, text in zip(keys, texts) if key not in found}
                if missing:
                    vecs = compute(list(missing.values()))
                    with _QUERY_LOCK:
                        expires = time.monotonic() + QUERY_CACHE_SECONDS
                        for key, vector in zip(missing, vecs):
                            vector = vector.copy()
                            vector.flags.writeable = False
                            _QUERY_CACHE[key] = (expires, vector)
                            found[key] = vector
                        while _QUERY_CACHE and (len(_QUERY_CACHE) > QUERY_CACHE_ENTRIES or sum(
                                v[1].nbytes for v in _QUERY_CACHE.values()) > QUERY_CACHE_BYTES):
                            _QUERY_CACHE.popitem(last=False)
        # Only hashes and immutable vectors are shared, never raw query text or
        # retrieved evidence. Stacking gives each caller an independent array.
        return self.np.stack([found[key] for key in keys])

    def vectors(self, items: list[tuple[str, str]]):
        """Vectors for (content hash, text) pairs, embedding only what is not cached."""
        np = self.np
        found: dict[str, bytes] = {}
        hashes = list(dict.fromkeys(h for h, _t in items))
        with self._lock:
            if hashes:
                found.update(self.db.execute(
                    "SELECT hash, v FROM vec WHERE hash IN (SELECT value FROM json_each(?))", (json.dumps(hashes),)))
            todo = {h: t for h, t in items if h not in found}
            if todo:
                keys = list(todo)
                for start in range(0, len(keys), 1024):
                    batch = keys[start:start + 1024]
                    vecs = self.encode([todo[h] for h in batch]).astype(np.float16)
                    rows = [(h, v.tobytes()) for h, v in zip(batch, vecs)]
                    if not self.read_only:
                        with _store.write_txn(self.db):
                            self.db.executemany("INSERT OR REPLACE INTO vec VALUES (?, ?)", rows)
                    found.update(rows)
        if not items:
            return np.zeros((0, 0), dtype=np.float32)
        return np.stack([np.frombuffer(found[h], dtype=np.float16) for h, _t in items]).astype(np.float32)


@dataclass
class _Index:
    stamp: tuple
    ids: list[int]
    hashes: list[str]
    turns: list[int]
    matrix: object
    mapped: bool = False


_INDEX: OrderedDict = OrderedDict()
_INDEX_LOCK = threading.Lock()
MAX_CACHED_SPACES = 16
MAX_INDEX_BYTES = 128 * 1024 * 1024


def forget_store(store: _store.Store) -> None:
    with _INDEX_LOCK:
        for key in list(_INDEX):
            if key[:2] == (store.path, store._units_identity or store.cache_identity):
                del _INDEX[key]


def release_store(store: _store.Store) -> None:
    # Legacy frozen stores have no persisted revision. Their indexes cannot be
    # reused safely across connections. New indexes are process-wide bounded
    # caches of vectors/hashes, so request-scoped stores can share them.
    if not store._units_identity:
        forget_store(store)


SCAN_BATCH = 1024
QUERY_BATCH = 16


def prepare(store: _store.Store, embedder: Embedder, *, sessions=()) -> dict:
    """Persist local passage vectors before serving queries, in bounded batches.

    Incremental and restartable: already cached content is reused, and a failed
    batch cannot mark the space prepared. Raw messages and sparse recall remain
    available throughout. The caller owns the embedder and its lifetime.
    """
    if embedder.read_only:
        raise _store.ConversationError("index preparation requires a writable embedding cache")
    start = time.perf_counter()
    units = 0
    with store.read_snapshot():
        allowed = store.allowed(sessions=tuple(sessions))
        for batch in store.unit_batches(SCAN_BATCH, allowed=allowed):
            embedder.vectors([(h, body) for _u, _t, body, h in batch])
            units += len(batch)
        revision = store.unit_stamp()
    return {"space": store.space, "model": embedder.tag, "units": units,
            "revision": list(revision), "elapsed_seconds": round(time.perf_counter() - start, 6)}


def search(store: _store.Store, embedder: Embedder, query_vec, limit: int, *,
           allowed: set[int] | None = None) -> list[tuple[int, float]]:
    """Exact search for one query, using the same filtered engine as batched recall."""
    return search_many(store, embedder, embedder.np.asarray(query_vec)[None, :], limit, allowed=allowed)[0]


def search_many(store: _store.Store, embedder: Embedder, query_vecs, limit: int, *,
                allowed: set[int] | None = None) -> list[list[tuple[int, float]]]:
    """Exact independent top-k rankings, sharing a scan across query facets.

    Both corpus rows and query columns are bounded. No approximate candidate
    index or cross-query fusion changes the retrieval objective.
    """
    queries = embedder.np.asarray(query_vecs)
    if queries.size == 0:
        return [] if len(queries) == 0 else [[] for _ in queries]
    if queries.ndim != 2 or not embedder.np.isfinite(queries).all():
        raise _store.ConversationError("query vectors must be a finite two-dimensional matrix")
    with store.read_snapshot():
        results = []
        for start in range(0, len(queries), QUERY_BATCH):
            results.extend(_search_many(store, embedder, queries[start:start + QUERY_BATCH], limit, allowed=allowed))
        return results


def _search_many(store: _store.Store, embedder: Embedder, query_vecs, limit: int, *,
                 allowed: set[int] | None = None, _building: bool = False) -> list[list[tuple[int, float]]]:
    """Exact top-k over compact vectors, with bounded working memory.

    Persistent vectors already have float16 precision. Keep that representation
    in RAM and convert only a scoring batch to float32. Oversized indexes use
    generation-validated mapped files, falling back to SQLite streaming where
    persistence is unavailable. Filtered cold queries embed only eligible passages.
    """
    np = embedder.np
    if limit <= 0 or allowed == set():
        return [[] for _ in query_vecs]
    key = (store.path, store._units_identity or store.cache_identity, embedder.tag)
    stamp = store.unit_stamp()
    with _INDEX_LOCK:
        cached = _INDEX.get(key)
        if cached is not None:
            _INDEX.move_to_end(key)
    if cached is None or cached.stamp != stamp:
        from commontrace.conversation import vector_index

        records = vector_index.load(store, embedder.tag, np, stamp[0], query_vecs.shape[1])
        if records is not None:
            cached = _Index(stamp, records["id"][1:], records["hash"][1:], records["turn"][1:],
                            records["vector"][1:], mapped=True)
            with _INDEX_LOCK:
                current = _INDEX.get(key)
                if current is None or int(current.stamp[0]) <= int(stamp[0]):
                    while len(_INDEX) >= MAX_CACHED_SPACES:
                        _INDEX.popitem(last=False)
                    _INDEX[key] = cached
    if allowed is None and not _building and (cached is None or cached.stamp != stamp):
        # Build one matrix per space/model at a time. Waiters recheck the cache
        # under their own consistent source snapshot. Warm scoring stays parallel.
        with _work_lock(("index", key)):
            return _search_many(store, embedder, query_vecs, limit, allowed=allowed, _building=True)
    best: list[list[tuple[float, int]]] = [[] for _ in query_vecs]

    def results():
        return [[(-uid, value) for value, uid in sorted(page, reverse=True)] for page in best]

    def score(ids, matrix):
        # Convert each compact corpus batch once, then score all facets together.
        # A fixed reduction order also gives identical passages identical scores
        # across row/column batch boundaries. BLAS GEMV/GEMM can otherwise differ
        # enough in float32 rounding to break boundary ties in different ways.
        values = np.einsum("ij,kj->ik", matrix.astype(np.float32), query_vecs, optimize=False)
        count = min(limit, len(ids))
        if not count:
            return
        identifiers = np.asarray(ids)
        for column, page in enumerate(best):
            scores = values[:, column]
            if count < len(ids):
                # Partition in linear time; fully sort only the winning rows.
                # Include boundary ties explicitly so low ids always win ties.
                threshold = np.partition(scores, len(ids) - count)[len(ids) - count]
                above = np.flatnonzero(scores > threshold)
                ties = np.flatnonzero(scores == threshold)
                ties = ties[np.argsort(identifiers[ties], kind="stable")[:count - len(above)]]
                selected = np.concatenate((above, ties))
            else:
                selected = np.arange(len(ids))
            order = selected[np.lexsort((identifiers[selected], -scores[selected]))]
            for i in order:
                item = (float(scores[i]), -int(ids[i]))
                if len(page) < limit:
                    heapq.heappush(page, item)
                elif item > page[0]:
                    heapq.heapreplace(page, item)

    if cached is not None and cached.stamp == stamp:
        if allowed is None:
            for start in range(0, len(cached.ids), SCAN_BATCH):
                end = min(start + SCAN_BATCH, len(cached.ids))
                # A contiguous view avoids copying the float16 corpus and
                # constructing per-row eligibility/selection lists.
                score(cached.ids[start:end], cached.matrix[start:end])
        else:
            # The indexed source lookup can find a small eligible scope without
            # visiting every vector/turn. Resolve its sorted ids into the exact
            # current snapshot; selection still happens before top-k scoring.
            identifiers = np.asarray(cached.ids)
            cursor = store.db.execute("SELECT id FROM units WHERE turn IN "
                                      "(SELECT value FROM json_each(?)) ORDER BY id", (json.dumps(sorted(allowed)),))
            while rows := cursor.fetchmany(SCAN_BATCH):
                ids = np.array([row[0] for row in rows], dtype=np.int64)
                positions = np.searchsorted(identifiers, ids)
                score(ids, cached.matrix[positions])
    else:
        # Reuse unchanged content, never an id which SQLite may recycle.
        # A stale mapped generation may contain millions of rows. Reuse its
        # vectors through the bounded persistent hash cache, rather than
        # materializing a corpus-sized dictionary of NumPy row objects.
        known = {} if cached is None or cached.mapped else {
            h.decode("ascii") if isinstance(h, bytes) else h: v for h, v in zip(cached.hashes, cached.matrix)}
        count = store.db.execute("SELECT COUNT(*) FROM units").fetchone()[0]
        ids, hashes, turns, matrix = [], [], [], None
        cacheable = allowed is None
        offset, builder, mapped = 0, None, None
        try:
            for batch in store.unit_batches(SCAN_BATCH, allowed=allowed):
                missing = list(dict.fromkeys((h, body) for _u, _t, body, h in batch if h not in known))
                fresh = dict(zip([h for h, _b in missing], embedder.vectors(missing) if missing else []))
                vectors = np.stack([known[h] if h in known else fresh[h] for _u, _t, _b, h in batch]).astype(np.float16)
                batch_ids = [u for u, _t, _b, _h in batch]
                score(batch_ids, vectors)
                if allowed is None and matrix is None and builder is None:
                    cacheable = count * vectors.shape[1] * np.dtype(np.float16).itemsize <= MAX_INDEX_BYTES
                    if cacheable:
                        matrix = np.empty((count, vectors.shape[1]), dtype=np.float16)
                    else:
                        from commontrace.conversation import vector_index

                        builder = vector_index.Build(store, embedder.tag, np, stamp[0], count, vectors.shape[1])
                if cacheable:
                    matrix[offset:offset + len(batch)] = vectors
                    ids.extend(batch_ids)
                    turns.extend(t for _u, t, _b, _h in batch)
                    hashes.extend(h for _u, _t, _b, h in batch)
                if builder is not None:
                    builder.write(offset, batch, vectors)
                offset += len(batch)
            if builder is not None:
                mapped = builder.publish()
        finally:
            if builder is not None:
                builder.close()
        with _INDEX_LOCK:
            current = _INDEX.get(key)
            if store._units_identity and current is not None and int(current.stamp[0]) > int(stamp[0]):
                # An older source snapshot must not replace a newer generation.
                return results()
            _INDEX.pop(key, None)
            if mapped is not None:
                while len(_INDEX) >= MAX_CACHED_SPACES:
                    _INDEX.popitem(last=False)
                _INDEX[key] = _Index(stamp, mapped["id"][1:], mapped["hash"][1:], mapped["turn"][1:],
                                    mapped["vector"][1:], mapped=True)
            elif matrix is not None and cacheable:
                while _INDEX and (len(_INDEX) >= MAX_CACHED_SPACES
                                  or sum(v.matrix.nbytes for v in _INDEX.values() if not v.mapped)
                                  + matrix.nbytes > MAX_INDEX_BYTES):
                    _INDEX.popitem(last=False)
                _INDEX[key] = _Index(stamp, ids, hashes, turns, matrix[:offset])
    return results()
