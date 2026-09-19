"""Semantic matching for the Knowledge Base, computed entirely locally.

WHY THIS EXISTS. `commontrace/overlap.py`'s MinHash is lexical: it compares
content words. Two engineers describing the same substrate failure --
"connection pool exhausted during a retry storm" and "during a spike
everything starts timing out waiting to acquire a connection" -- share
almost no words, so the measured recall of the thresholded coverage figure
is 8.7% on the held-out probe set (commons/eval/RESULTS.md).

Measured against the same corpus, the same dev/held-out split and the same
negative controls, cosine similarity over sentence embeddings reaches 32.6%
recall at the same 0% false-positive bar, with every match it made landing
on the correct record (`python commons/eval/semantic.py`). That is the
number this module exists to make available.

WHY IT COSTS NO PRIVACY -- AND ACTUALLY BUYS SOME
--------------------------------------------------
RESULTS.md once recorded semantic matching as blocked because "an embedding
is computed by a model that has to see the text". That is true and
harmless. What the guarantee forbids is the OPERATOR seeing a customer's
failure text -- not a model running on the customer's own machine, on text
that machine already holds.

The other side of the comparison is not secret either: the Knowledge Base
is operator-curated substrate knowledge, published openly. So both halves
can live on the client, and this module never opens a socket:

    corpus      read from a local file (public content)
    embedding   computed here, from text already on this machine
    similarity  computed here, in memory

Nothing is sent anywhere. Compared with the shipped path -- which transmits
a MinHash signature to a Hub -- this discloses strictly less: not the text,
not an embedding, not even a signature. A fleet can measure what the
Knowledge Base knows about its incidents without the operator learning that
it asked, or what it asked about.

WHY THE MODEL IS AN OPTIONAL EXTRA
----------------------------------
`sentence-transformers` pulls torch, which is a large download nobody should
pay for to try the protocol. It is already declared as the `attention`
extra for this project's own attention layer, with its transformers pin
carrying a documented CVE floor, so this module takes on no new dependency
-- it reuses one that is already opt-in and already reviewed.

Absent the extra, every entry point here raises `SemanticUnavailable` with
the install line. Nothing degrades silently to a worse matcher: a coverage
number computed by a different matcher than the caller asked for is exactly
the kind of quiet substitution this codebase refuses elsewhere.
"""
from __future__ import annotations

from typing import Sequence

# The operating point measured in commons/eval/semantic.py: the most recall
# available while BOTH probe sets still report zero false positives. 0.60
# scores higher recall (58.7% held-out) and is deliberately NOT the default,
# because it leaks 4.5% false positives on dev -- the "bar dropped, gain is
# fake" failure that evaluation exists to catch. A coverage figure a
# customer may quote does not get to buy recall with false positives.
DEFAULT_SEMANTIC_THRESHOLD = 0.65

# Small, CPU-viable, and the same family memory/attention/query.py already
# loads, so a fleet that installed the extra for one feature pays no second
# download for this one. Hardcoded rather than configurable for the reason
# that file documents: a model name taken from untrusted input is a remote
# code execution vector through checkpoint deserialization.
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
    """Load (once) and return the encoder.

    Memoized at module scope: loading is seconds of CPU and hundreds of MB
    of RAM, and a report signs a corpus plus a probe set in one run.
    """
    global _model
    if _model is not None:
        return _model
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise SemanticUnavailable(_INSTALL_HINT) from exc
    try:
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
    """For each query, the ranked corpus entries above `threshold`.

    Returns one dict per query, in input order, each carrying its ranked
    `matches` as (index, score) pairs and the single `best` pair regardless
    of threshold. `best` is reported unthresholded on purpose: the caller
    decides what to do with a near miss, and a matcher that hides its own
    runner-up is how a threshold's cost becomes invisible -- which is the
    exact defect commons/eval/RESULTS.md records against the shipped
    coverage figure.
    """
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
