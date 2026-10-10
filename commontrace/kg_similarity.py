"""Similarity for knowledge-graph resolution and typing: semantic when an embedder is
configured, character n-grams (stdlib) when not.

An embedder is configured by passing one (an `embeddings.provider` tag such as
``arctic-m`` or ``openai:text-embedding-3-small``, or any object with
``embed(texts, *, query) -> unit vectors``), or by ``COMMONTRACE_GRAPH_EMBEDDER``.
Nothing here loads a model or calls a network until an embedder is asked to embed."""
from __future__ import annotations

import math
import os
import re

ENV = "COMMONTRACE_GRAPH_EMBEDDER"
_WORD = re.compile(r"[a-z0-9]+")


def embedder(spec=None):
    """The embedder `spec` names, the one ``COMMONTRACE_GRAPH_EMBEDDER`` names, or None.
    An object with an `embed` method is used as given (tests inject fakes this way)."""
    if spec is not None and not isinstance(spec, str):
        if not callable(getattr(spec, "embed", None)):
            raise TypeError("an embedder needs an embed(texts, *, query) method")
        return spec
    tag = (spec if spec is not None else os.environ.get(ENV, "")).strip()
    if not tag or tag.lower() == "none":
        return None
    from commontrace import embeddings

    return embeddings.provider(tag)


def embed(provider, texts: list[str], *, query: bool) -> list[list[float]]:
    """Unit vectors for `texts`, checked: one finite vector per text, one dimension."""
    vectors = [list(map(float, v)) for v in provider.embed(list(texts), query=query)]
    if len(vectors) != len(texts) or len({len(v) for v in vectors}) > 1:
        raise ValueError("the embedder returned a vector per text of one dimension")
    out = []
    for vector in vectors:
        norm = math.sqrt(sum(x * x for x in vector))
        if not vector or not math.isfinite(norm) or norm == 0.0:
            raise ValueError("the embedder returned an empty, zero or non-finite vector")
        out.append([x / norm for x in vector])
    return out


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine of two unit vectors (their dot product), clamped to [-1, 1]."""
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))


def pairwise(vectors: list[list[float]]) -> list[list[float]]:
    """The full cosine matrix of unit vectors; numpy when installed, else pure Python."""
    try:
        import numpy as np
    except ImportError:
        return [[cosine(a, b) for b in vectors] for a in vectors]
    matrix = np.asarray(vectors, dtype=float)
    return np.clip(matrix @ matrix.T, -1.0, 1.0).tolist()


def normalize(text: str) -> str:
    """Lower-cased words joined by single spaces: the form n-grams are taken over."""
    return " ".join(_WORD.findall((text or "").lower()))


def ngrams(text: str, n: int = 3) -> frozenset[str]:
    """Character n-grams of the normalized text, padded so short names still have some."""
    padded = f" {normalize(text)} "
    if len(padded.strip()) == 0:
        return frozenset()
    return frozenset(padded[i:i + n] for i in range(max(1, len(padded) - n + 1)))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """|a & b| / |a | b|; 0 when both are empty."""
    union = len(a | b)
    return len(a & b) / union if union else 0.0
