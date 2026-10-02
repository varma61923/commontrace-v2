"""Semantic matching for the Knowledge Base, computed entirely locally."""
from __future__ import annotations

from typing import Sequence

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


_model = None


def available() -> bool:
    """Whether semantic matching can run, without loading anything heavy."""
    try:
        import numpy  # noqa: F401
        import sentence_transformers  # noqa: F401
    except ImportError:
        return False
    return True


def load_model():
    """Load (once) and return the encoder."""
    global _model
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


def encode(texts: Sequence[str]):
    """Unit-normalized embeddings, so a dot product IS cosine similarity."""
    if not texts:
        raise SemanticUnavailable("nothing to encode")
    model = load_model()
    return model.encode(list(texts), normalize_embeddings=True)


def best_matches(
    queries: Sequence[str],
    corpus: Sequence[str],
    *,
    threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
    top_k: int = 5,
) -> list[dict]:
    """For each query, the ranked corpus entries above `threshold`."""
    import numpy as np

    q = np.asarray(encode(queries))
    c = np.asarray(encode(corpus))
    sims = q @ c.T

    out: list[dict] = []
    for row in sims:
        order = list(np.argsort(-row)[:top_k])
        best_i = int(order[0]) if order else -1
        out.append({
            "best": (best_i, float(row[best_i])) if best_i >= 0 else None,
            "matches": [(int(i), float(row[i])) for i in order if float(row[i]) >= threshold],
        })
    return out
