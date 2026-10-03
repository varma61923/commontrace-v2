"""The semantic arm of conversation memory. Vectors are cached by content hash in
one file per model, shared by every space, so a message is embedded once."""
from __future__ import annotations

import importlib.util
import json
import os
import threading

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

    def __init__(self, root: str, tag: str):
        import numpy as np

        self.np, self.tag = np, tag
        directory = _store.conversations_dir(root)
        os.makedirs(directory, exist_ok=True)
        self.db = _store.connect(os.path.join(directory, f"embeddings-{tag}.db"),
                                 "CREATE TABLE IF NOT EXISTS vec (hash TEXT PRIMARY KEY, v BLOB NOT NULL)")
        self._lock = threading.Lock()

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
                    with _store.write_txn(self.db):
                        self.db.executemany("INSERT OR REPLACE INTO vec VALUES (?, ?)", rows)
                    found.update(rows)
        if not items:
            return np.zeros((0, 0), dtype=np.float32)
        return np.stack([np.frombuffer(found[h], dtype=np.float16) for h, _t in items]).astype(np.float32)


_INDEX: dict[tuple[str, str], tuple[tuple, list[int], object]] = {}
MAX_CACHED_SPACES = 16


def search(store: _store.Store, embedder: Embedder, query_vec, limit: int) -> list[tuple[int, float]]:
    """(unit id, cosine) for the store's units nearest the query, best first."""
    np = embedder.np
    key = (store.path, embedder.tag)
    top = store.db.execute("SELECT COALESCE(MAX(id), 0), COUNT(*) FROM units").fetchone()
    cached = _INDEX.get(key)
    if cached is None or cached[0] != tuple(top):
        units = store.units()
        known = {} if cached is None else dict(zip(cached[1], cached[2]))
        missing = [(h, body) for uid, _t, body, h in units if uid not in known]
        fresh = dict(zip([uid for uid, *_ in units if uid not in known],
                         embedder.vectors(missing) if missing else []))
        ids = [uid for uid, *_ in units]
        matrix = np.stack([known[i] if i in known else fresh[i] for i in ids]) if ids else \
            np.zeros((0, 1), dtype=np.float32)
        cached = (tuple(top), ids, matrix)
        _INDEX.pop(key, None)
        while len(_INDEX) >= MAX_CACHED_SPACES:
            _INDEX.pop(next(iter(_INDEX)))
        _INDEX[key] = cached
    _top, ids, matrix = cached
    if not ids:
        return []
    scores = matrix @ query_vec
    limit = min(limit, len(ids))
    best = np.argpartition(-scores, limit - 1)[:limit]
    best = best[np.argsort(-scores[best])]
    return [(ids[i], float(scores[i])) for i in best]
