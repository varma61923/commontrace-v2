"""Dense scores for canonical facts from any embedding provider, with a persistent vector cache.

The embedder is, in order: an injected adapter (``model``: anything with
``encode(texts, normalize_embeddings=True)``, or a `commontrace.embeddings`
tag string), ``COMMONTRACE_FACT_EMBEDDER`` (any tag, e.g. ``arctic-m`` or
``openai:text-embedding-3-small``), or the legacy ``COMMONTRACE_FACT_EMBEDDER_PATH``
(a local sentence-transformers directory). With none configured, scores are
None and fact search keeps its lexical, entity and temporal signals.

Fact vectors are cached by statement hash: in memory, and on disk under
``memory/facts/embeddings-<model>.db`` when a store root is given, so a
restart does not re-embed every fact. Encoding never holds the shared lock,
so queries against warm vectors are not serialized behind a cold batch.
"""
from __future__ import annotations

import array
import hashlib
import math
import os
import sqlite3
import threading
from collections import OrderedDict

_LOCK = threading.Lock()
_MODELS: OrderedDict = OrderedDict()
_VECTORS: OrderedDict = OrderedDict()
_WORK: dict = {}
MAX_CACHED = 50_000


class _Adapter:
    """A provider from `commontrace.embeddings`, behind the legacy ``encode`` shape."""

    def __init__(self, tag: str):
        from commontrace import embeddings

        self.provider = embeddings.provider(tag)
        self.key = ("provider", self.provider.spec.tag)

    def encode(self, texts, normalize_embeddings=True, query=False):
        return self.provider.embed(list(texts), query=query)


def _resolve(model, model_path):
    if model is not None:
        if isinstance(model, str):
            return _cached_model(("tag", model), lambda: _Adapter(model))
        return model, model
    tag = os.environ.get("COMMONTRACE_FACT_EMBEDDER", "").strip()
    if tag and tag.lower() not in ("none", "off"):
        return _cached_model(("tag", tag), lambda: _Adapter(tag))
    selected = model_path or os.environ.get("COMMONTRACE_FACT_EMBEDDER_PATH")
    if not selected:
        return None, None
    if not os.path.isdir(selected):
        raise ValueError("fact embeddings require an existing local model directory")
    selected = os.path.realpath(selected)
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return None, None  # Core installation retains its lexical fallback.
    # Cache by local weights' file identity; replacements reload the model.
    identity = tuple((name, os.stat(os.path.join(selected, name)).st_mtime_ns,
                      os.stat(os.path.join(selected, name)).st_size)
                     for name in sorted(os.listdir(selected)) if os.path.isfile(os.path.join(selected, name)))
    return _cached_model((selected, identity), lambda: SentenceTransformer(selected, local_files_only=True))


def _cached_model(key, build):
    with _LOCK:
        if key in _MODELS:
            _MODELS.move_to_end(key)
            return _MODELS[key], key
    built = build()
    with _LOCK:
        _MODELS.setdefault(key, built)
        while len(_MODELS) > 4:
            _MODELS.popitem(last=False)
        return _MODELS[key], key


def _store(root: str | None, model_key) -> sqlite3.Connection | None:
    if not root or not isinstance(model_key, tuple) or model_key[:1] not in (("tag",), ("provider",)):
        return None
    from commontrace import paths

    name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(model_key[1]))[:120]
    directory = os.path.join(paths.memory_dir(root), "facts")
    os.makedirs(directory, exist_ok=True)
    db = sqlite3.connect(os.path.join(directory, f"embeddings-{name}.db"), timeout=30)
    db.execute("CREATE TABLE IF NOT EXISTS vec (hash TEXT PRIMARY KEY, v BLOB NOT NULL)")
    return db


def _encode(model, texts: list[str], *, query: bool) -> list[tuple[float, ...]]:
    try:
        raw = model.encode(texts, normalize_embeddings=True, query=query)
    except TypeError:  # legacy adapters take no query flag
        raw = model.encode(texts, normalize_embeddings=True)
    if len(raw) != len(texts):
        raise ValueError("embedding adapter changed the source count")
    out = []
    for values in raw:
        vector = tuple(float(v) for v in values)
        if not vector or not all(math.isfinite(v) for v in vector):
            raise ValueError("embedding adapter returned invalid vectors")
        out.append(vector)
    return out


def vectors(facts, *, model=None, model_path: str | None = None, root: str | None = None):
    """(model key, {fact id: unit vector}) for `facts`, embedding only what no cache holds."""
    model, model_key = _resolve(model, model_path)
    if model is None:
        return None, {}
    keys = {f.id: (model_key, hashlib.sha256(f.statement.encode()).hexdigest()) for f in facts}
    found: dict = {}
    with _LOCK:
        for fid, key in keys.items():
            if key in _VECTORS:
                _VECTORS.move_to_end(key)
                found[key] = _VECTORS[key]
    missing = {key: f.statement for f in facts if (key := keys[f.id]) not in found}
    if missing:
        db = _store(root, model_key)
        try:
            if db is not None:
                hashes = [k[1] for k in missing]
                for start in range(0, len(hashes), 500):
                    chunk = hashes[start:start + 500]
                    marks = ",".join("?" * len(chunk))
                    for digest, blob in db.execute(f"SELECT hash, v FROM vec WHERE hash IN ({marks})", chunk):  # nosec B608
                        found[(model_key, digest)] = tuple(array.array("f", blob))
            todo = {k: t for k, t in missing.items() if k not in found}
            if todo:
                with _LOCK:
                    work = _WORK.setdefault(model_key, threading.Lock())
                with work:  # one cold batch per model at a time; warm readers never wait on it
                    encoded = _encode(model, list(todo.values()), query=False)
                for key, vector in zip(todo, encoded):
                    found[key] = vector
                if db is not None:
                    with db:
                        db.executemany("INSERT OR REPLACE INTO vec VALUES (?, ?)",
                                       [(k[1], array.array("f", v).tobytes()) for k, v in zip(todo, encoded)])
        finally:
            if db is not None:
                db.close()
        with _LOCK:
            for key in missing:
                _VECTORS[key] = found[key]
            while len(_VECTORS) > MAX_CACHED:
                _VECTORS.popitem(last=False)
    return model_key, {fid: found[key] for fid, key in keys.items()}


def scores(query: str, facts, *, model_path: str | None = None, model=None,
           root: str | None = None) -> dict[str, float] | None:
    """Cosine similarity of `query` to each fact, or None when no embedder is configured."""
    facts = list(facts)
    model_obj, _key = _resolve(model, model_path)
    if model_obj is None:
        return None
    _key, by_id = vectors(facts, model=model, model_path=model_path, root=root)
    query_vector = _encode(model_obj, [query], query=True)[0]
    result = {}
    for fact in facts:
        vector = by_id[fact.id]
        if len(vector) != len(query_vector):
            raise ValueError("embedding dimensions differ")
        norm = math.sqrt(sum(v * v for v in vector) * sum(v * v for v in query_vector))
        result[fact.id] = max(-1, min(1, sum(a * b for a, b in zip(vector, query_vector)) / norm)) if norm else 0
    return result


def similar(statement: str, facts, *, model=None, root: str | None = None, threshold: float = 0.92):
    """Facts within `threshold` cosine of `statement` (best first), or None without an embedder.

    Both sides are embedded as documents: this compares statements with each
    other, not a question with candidate answers.
    """
    facts = list(facts)
    model_obj, _key = _resolve(model, None)
    if model_obj is None:
        return None
    _key, by_id = vectors(facts, model=model, root=root)
    probe = _encode(model_obj, [statement], query=False)[0]
    pairs = []
    for fact in facts:
        vector = by_id[fact.id]
        if len(vector) == len(probe):
            cosine = sum(a * b for a, b in zip(vector, probe))
            if cosine >= threshold:
                pairs.append((fact, cosine))
    return sorted(pairs, key=lambda p: (-p[1], p[0].id))
