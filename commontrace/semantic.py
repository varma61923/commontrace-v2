"""Semantic matching for the Knowledge Base, computed entirely locally."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import threading
import weakref
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, cast

from commontrace.runtime_cache import RuntimeCache

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray


class Encoder(Protocol):
    """The local encoder seam; implementations return one vector per input."""

    def encode(self, texts: list[str], /, *, normalize_embeddings: bool) -> Any: ...

DEFAULT_SEMANTIC_THRESHOLD = 0.65

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

_INSTALL_HINT = (
    "semantic matching needs the optional model stack:\n"
    "    python -m pip install 'commontrace[attention]'\n"
    "It is optional because it pulls torch, which is a large download. The "
    "lexical matcher ships by default and needs nothing extra."
)


class SemanticUnavailable(RuntimeError):
    """The optional model stack is not installed, or the model is missing."""


_model: Encoder | None = None
_model_lock = threading.Lock()
_identity_lock = threading.Lock()
_identities: weakref.WeakKeyDictionary[Any, object] = weakref.WeakKeyDictionary()
_vectors: RuntimeCache[Any] = RuntimeCache(
    max_entries=128, max_bytes=64 * 1024 * 1024, ttl=300,
    weigh=lambda _key, value: int(value.nbytes) + 128,
)


def available() -> bool:
    """Whether semantic matching can run, without loading anything heavy."""
    return (importlib.util.find_spec("numpy") is not None
            and importlib.util.find_spec("sentence_transformers") is not None)


def load_model() -> Encoder:
    """Load (once) and return the encoder."""
    global _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise SemanticUnavailable(_INSTALL_HINT) from exc
        try:
            try:
                _model = SentenceTransformer(MODEL_NAME, local_files_only=True)
            except Exception:  # noqa: BLE001 - not cached, or an older library: fetch it
                _model = SentenceTransformer(MODEL_NAME)
        except Exception as exc:  # noqa: BLE001 - network, disk, corrupt cache
            raise SemanticUnavailable(
                f"could not load {MODEL_NAME}: {exc}\n"
                "The first run downloads the model; after that it is cached and "
                "this command works offline."
            ) from exc
        return _model


def encode(texts: Sequence[str]) -> NDArray[np.floating[Any]]:
    """Unit-normalized embeddings with bounded, singleflight batch caching.

    Model identity and a structured content digest isolate encoder changes and
    text boundaries. Cached arrays are immutable; callers receive independent
    arrays. Failed inference is propagated and never cached.
    """
    if not texts:
        raise SemanticUnavailable("nothing to encode")
    model = load_model()
    items = list(texts)
    digest = hashlib.sha256(json.dumps(items, ensure_ascii=False, separators=(",", ":"))
                            .encode("utf-8")).digest()
    def compute() -> NDArray[np.floating[Any]]:
        import numpy as np

        vectors = np.asarray(model.encode(items, normalize_embeddings=True)).copy()
        if vectors.ndim != 2 or vectors.shape[0] != len(items) or vectors.shape[1] == 0 \
                or not np.isfinite(vectors).all():
            raise SemanticUnavailable("encoder returned invalid vectors")
        vectors.flags.writeable = False
        return vectors

    with _identity_lock:
        try:
            identity = _identities.get(model)
            if identity is None:
                identity = object()
                _identities[model] = identity
        except TypeError:
            # Structural encoders may be unhashable or lack weak references.
            # Skip retention rather than keying on a recyclable Python id.
            identity = None
    if identity is None:
        return compute().copy()
    return cast("NDArray[np.floating[Any]]", _vectors.get_or_load((identity, digest), compute).copy())


def best_matches(
    queries: Sequence[str],
    corpus: Sequence[str],
    *,
    threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """For each query, the ranked corpus entries above `threshold`."""
    import numpy as np

    q = np.asarray(encode(queries))
    c = np.asarray(encode(corpus))
    sims = q @ c.T

    out: list[dict[str, Any]] = []
    for row in sims:
        order = list(np.argsort(-row)[:top_k])
        best_i = int(order[0]) if order else -1
        out.append({
            "best": (best_i, float(row[best_i])) if best_i >= 0 else None,
            "matches": [(int(i), float(row[i])) for i in order if float(row[i]) >= threshold],
        })
    return out
