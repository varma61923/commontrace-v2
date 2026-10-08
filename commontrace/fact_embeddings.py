"""Opt-in, offline dense scores for canonical facts; no service or model download."""
from __future__ import annotations

import hashlib
import math
import os
import threading
from collections import OrderedDict

_LOCK = threading.Lock()
_MODELS: OrderedDict = OrderedDict()
_VECTORS: OrderedDict = OrderedDict()


def scores(query: str, facts, *, model_path: str | None = None, model=None) -> dict[str, float] | None:
    selected = model_path or os.environ.get("COMMONTRACE_FACT_EMBEDDER_PATH")
    if model is None and not selected:
        return None
    if model is None:
        if not os.path.isdir(selected):
            raise ValueError("fact embeddings require an existing local model directory")
        selected = os.path.realpath(selected)
    with _LOCK:
        if model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                return None  # Core installation retains its lexical fallback.
            # Cache by local weights' file identity; replacements reload the model.
            identity = tuple((name, os.stat(os.path.join(selected, name)).st_mtime_ns,
                               os.stat(os.path.join(selected, name)).st_size)
                for name in sorted(os.listdir(selected)) if os.path.isfile(os.path.join(selected, name)))
            model_key = (selected, identity)
            if model_key not in _MODELS:
                _MODELS[model_key] = SentenceTransformer(selected, local_files_only=True)
                while len(_MODELS) > 2:
                    _MODELS.popitem(last=False)
            model = _MODELS[model_key]
        else:
            model_key = model  # Hold the injected adapter itself; avoid object-id reuse collisions.
        keys = [(model_key, hashlib.sha256(f.statement.encode()).hexdigest()) for f in facts]
        missing = [(key, f.statement) for key, f in zip(keys, facts) if key not in _VECTORS]
        if missing:
            vectors = model.encode([text for _key, text in missing], normalize_embeddings=True)
            if len(vectors) != len(missing):
                raise ValueError("embedding adapter changed the source count")
            for (key, _text), values in zip(missing, vectors):
                vector = tuple(float(v) for v in values)
                if not vector or not all(math.isfinite(v) for v in vector):
                    raise ValueError("embedding adapter returned invalid vectors")
                _VECTORS[key] = vector
        query_vectors = model.encode([query], normalize_embeddings=True)
        if len(query_vectors) != 1:
            raise ValueError("embedding adapter changed the query count")
        query_vector = tuple(float(v) for v in query_vectors[0])
        if not all(math.isfinite(v) for v in query_vector):
            raise ValueError("embedding adapter returned an invalid query")
        result = {}
        for key, fact in zip(keys, facts):
            vector = _VECTORS[key]
            if len(vector) != len(query_vector):
                raise ValueError("embedding dimensions differ")
            norm = math.sqrt(sum(v * v for v in vector) * sum(v * v for v in query_vector))
            result[fact.id] = max(-1, min(1, sum(a * b for a, b in zip(vector, query_vector)) / norm)) if norm else 0
            _VECTORS.move_to_end(key)
        while len(_VECTORS) > 8192:
            _VECTORS.popitem(last=False)
        return result
