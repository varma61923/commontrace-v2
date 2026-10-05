"""The semantic arm of conversation memory. Vectors are cached by content hash in
one file per model, shared by every space, so a message is embedded once."""
from __future__ import annotations

import heapq
import importlib.util
import json
import os
import threading
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
        self._lock = threading.Lock()

    def close(self) -> None:
        self.db.close()

    def encode(self, texts: list[str], query: bool = False):
        prefix = MODELS[self.tag][1] if query else ""
        vecs = _model(self.tag).encode([prefix + t for t in texts], batch_size=BATCH,
                                       normalize_embeddings=True, convert_to_numpy=True,
                                       show_progress_bar=False)
        return vecs.astype(self.np.float32)

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


def search(store: _store.Store, embedder: Embedder, query_vec, limit: int, *,
           allowed: set[int] | None = None) -> list[tuple[int, float]]:
    with store.read_snapshot():
        return _search(store, embedder, query_vec, limit, allowed=allowed)


def _search(store: _store.Store, embedder: Embedder, query_vec, limit: int, *,
            allowed: set[int] | None = None) -> list[tuple[int, float]]:
    """Exact top-k over compact vectors, with bounded working memory.

    Persistent vectors already have float16 precision. Keep that representation
    in RAM and convert only a scoring batch to float32. Oversized indexes stream
    from SQLite; filtered cold queries embed only eligible passages.
    """
    np = embedder.np
    if limit <= 0 or allowed == set():
        return []
    key = (store.path, store._units_identity or store.cache_identity, embedder.tag)
    stamp = store.unit_stamp()
    with _INDEX_LOCK:
        cached = _INDEX.get(key)
        if cached is not None:
            _INDEX.move_to_end(key)
    best: list[tuple[float, int]] = []

    def score(ids, matrix):
        values = matrix.astype(np.float32) @ query_vec
        count = min(limit, len(ids))
        if not count:
            return
        # Stable ties across streaming batches and cached indexes.
        order = np.lexsort((np.asarray(ids), -values))[:count]
        for i in order:
            item = (float(values[i]), -int(ids[i]))
            if len(best) < limit:
                heapq.heappush(best, item)
            elif item > best[0]:
                heapq.heapreplace(best, item)

    if cached is not None and cached.stamp == stamp:
        for start in range(0, len(cached.ids), SCAN_BATCH):
            end = start + SCAN_BATCH
            positions = [i for i in range(start, min(end, len(cached.ids)))
                         if allowed is None or cached.turns[i] in allowed]
            if positions:
                score([cached.ids[i] for i in positions], cached.matrix[positions])
    else:
        # Reuse unchanged content, never an id which SQLite may recycle.
        known = {} if cached is None else dict(zip(cached.hashes, cached.matrix))
        count = store.db.execute("SELECT COUNT(*) FROM units").fetchone()[0]
        ids, hashes, turns, matrix = [], [], [], None
        cacheable = allowed is None
        offset = 0
        for batch in store.unit_batches(SCAN_BATCH, allowed=allowed):
            missing = list(dict.fromkeys((h, body) for _u, _t, body, h in batch if h not in known))
            fresh = dict(zip([h for h, _b in missing], embedder.vectors(missing) if missing else []))
            vectors = np.stack([known[h] if h in known else fresh[h] for _u, _t, _b, h in batch]).astype(np.float16)
            batch_ids = [u for u, _t, _b, _h in batch]
            score(batch_ids, vectors)
            if cacheable and matrix is None:
                cacheable = count * vectors.shape[1] * np.dtype(np.float16).itemsize <= MAX_INDEX_BYTES
                if cacheable:
                    matrix = np.empty((count, vectors.shape[1]), dtype=np.float16)
            if cacheable:
                matrix[offset:offset + len(batch)] = vectors
                offset += len(batch)
                ids.extend(batch_ids)
                turns.extend(t for _u, t, _b, _h in batch)
                hashes.extend(h for _u, _t, _b, h in batch)
        with _INDEX_LOCK:
            _INDEX.pop(key, None)
            if matrix is not None and cacheable:
                while _INDEX and (len(_INDEX) >= MAX_CACHED_SPACES
                                  or sum(v.matrix.nbytes for v in _INDEX.values()) + matrix.nbytes > MAX_INDEX_BYTES):
                    _INDEX.popitem(last=False)
                _INDEX[key] = _Index(stamp, ids, hashes, turns, matrix[:offset])
    return [(-uid, value) for value, uid in sorted(best, reverse=True)]
