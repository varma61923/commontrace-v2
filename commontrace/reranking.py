"""Second-stage reranking for every retrieval path, behind one text contract.

A reranker is chosen by name through `commontrace.providers.reranker(name)`:

- ``cross-encoder``, ``cross-encoder-fast``, ``bge-reranker-v2-m3``,
  ``mxbai-rerank``: local cross-encoders (the attention extra), loaded lazily;
- ``cohere[:model]``, ``voyage[:model]``, ``jina[:model]``: hosted rerank APIs
  (`commontrace.rerankers_hosted`), keys from the environment or ``NAME_FILE``;
- ``llm[:model]``: a listwise reranker over the configured `commontrace.llm`
  provider, which only ever accepts a permutation of the ids it was shown.

Every text reranker implements ``rerank(query, documents, top_n=None)`` and
returns `Hit` rows, best first, with ties kept in input order. It is also
callable with the lesson contract ``(task, ranked) -> ranked``, so
`commontrace.retrieval.apply_reranker` accepts it unchanged.

`stage` is the seam multi-channel recall, fact search and conversation recall
share: it reranks the head of a first-stage ranking and blends each item's
reranked position with its fused position. A blend of 0 keeps the first-stage
order exactly, 1 takes the reranker's order, and anything between is a
weighted reciprocal-rank fusion of the two. A reranker that fails (no key, no
network, offline mode, a malformed reply) leaves the first-stage order intact
and says why; it never sinks the retrieval it was asked to improve.
"""
from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from commontrace.exceptions import CapabilityError, ConfigurationError

RRF_K = 60
DEFAULT_DEPTH = 30
MAX_DEPTH = 200
DEFAULT_BLEND = 0.5
MAX_QUERY_CHARS = 2_000
EXPLAIN_TOP = 10


class RerankError(RuntimeError):
    """A reranker answered with something that is not a valid reordering."""


@dataclass(frozen=True)
class Hit:
    index: int  # position in the documents the reranker was given
    score: float


def order_scores(scores: Sequence[float], top_n: int | None = None) -> list[Hit]:
    """Stable best-first order: equal scores keep their input order."""
    values = [float(s) for s in scores]
    if not all(math.isfinite(v) for v in values):
        raise RerankError("the reranker returned a non-finite score")
    ranked = sorted(range(len(values)), key=lambda i: (-values[i], i))
    if top_n is not None:
        ranked = ranked[:max(0, top_n)]
    return [Hit(i, values[i]) for i in ranked]


def validate_hits(rows: Sequence[tuple[object, object]], count: int, top_n: int | None, source: str) -> list[Hit]:
    """Check a provider's (index, score) rows and return them best first.

    Each index must be a distinct integer in range and each score finite; the
    number of rows must be exactly what was requested. Anything else raises,
    naming the provider, rather than guessing which document a score belongs to.
    """
    expected = count if top_n is None else min(count, max(0, top_n))
    if len(rows) != expected:
        raise RerankError(f"{source} returned {len(rows)} results for {count} documents (expected {expected})")
    seen: set[int] = set()
    hits: list[Hit] = []
    for index, score in rows:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < count or index in seen:
            raise RerankError(f"{source} returned an invalid or repeated document index: {index!r}")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)):
            raise RerankError(f"{source} returned a non-numeric relevance score for document {index}")
        seen.add(index)
        hits.append(Hit(index, float(score)))
    return sorted(hits, key=lambda h: (-h.score, h.index))


class TextReranker:
    """Base class: subclasses implement `score` (one score per document) or `rerank`."""

    name = "text"
    max_chars = 4_000

    def clip(self, documents: Sequence[str]) -> list[str]:
        return [str(d or " ")[:self.max_chars] for d in documents]

    def score(self, query: str, documents: list[str]) -> list[float]:
        raise NotImplementedError

    def rerank(self, query: str, documents: Sequence[str], top_n: int | None = None) -> list[Hit]:
        documents = self.clip(documents)
        if not documents:
            return []
        scores = self.score(str(query or "")[:MAX_QUERY_CHARS], documents)
        if len(scores) != len(documents):
            raise RerankError(f"{self.name} returned {len(scores)} scores for {len(documents)} documents")
        return order_scores(scores, top_n)

    def __call__(self, task: str, ranked: list) -> list:
        """The lesson contract: reorder `RankedLesson` rows by their full text.

        Lessons whose file cannot be read keep their first-stage order after
        the scored ones, exactly as the cross-encoder seam always has.
        """
        if len(ranked) < 2:
            return list(ranked)
        from commontrace import frontmatter, rerank_arm

        text_of = rerank_arm.texts([r.slug for r in ranked], {r.slug: r.path for r in ranked}, frontmatter.read)
        slugs = [r.slug for r in ranked if r.slug in text_of]
        hits = self.rerank(task, [text_of[s] for s in slugs])
        order = {slugs[h.index]: n for n, h in enumerate(hits)}
        return sorted(ranked, key=lambda r: (order.get(r.slug, len(order)), ranked.index(r)))


class LocalCrossEncoder(TextReranker):
    """A local sentence-transformers cross-encoder from `rerank_arm.MODELS`."""

    def __init__(self, mode: str = "cross-encoder"):
        from commontrace import rerank_arm

        if mode not in rerank_arm.MODELS:
            raise ConfigurationError("unknown cross-encoder mode")
        if not rerank_arm.available():
            raise CapabilityError("cross-encoder reranking needs the attention extra")
        self.mode = mode
        self.name = mode
        self.max_chars = rerank_arm.MAX_CHARS

    def score(self, query: str, documents: list[str]) -> list[float]:
        from commontrace import rerank_arm

        keys = [str(i) for i in range(len(documents))]
        page, _unused = rerank_arm.rerank(query, keys, dict(zip(keys, documents)), len(keys), mode=self.mode)
        by_key = {key: score for key, score in page}
        return [by_key[k] for k in keys]


def resolve(choice) -> TextReranker | None:
    """None for no reranker; a name resolves through `providers.reranker`."""
    if choice is None or choice == "" or choice == "none":
        return None
    if isinstance(choice, str):
        from commontrace import providers

        choice = providers.reranker(choice)
    if not callable(getattr(choice, "rerank", None)):
        raise ConfigurationError("this reranker reorders lessons only; choose a text reranker such as "
                                 "cross-encoder, bge-reranker-v2-m3, cohere, voyage, jina or llm")
    return choice


def load(choice) -> tuple[TextReranker | None, str]:
    """(reranker, "") or (None, why not) for a configured reranker.

    An unknown name or a lesson-only reranker is the caller's mistake and
    raises ValueError; a known reranker that cannot run here (the attention
    extra is missing) is reported, so the retrieval proceeds without it.
    """
    if choice is None or choice == "" or choice == "none":
        return None, ""
    if isinstance(choice, str):
        from commontrace import providers

        if choice.partition(":")[0] not in providers.reranker_names():
            raise ValueError(f"unknown reranker {choice!r}; choose from none, "
                             + ", ".join(n for n in providers.reranker_names() if n != "mmr"))
    try:
        return resolve(choice), ""
    except CapabilityError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    except (ConfigurationError, RerankError) as exc:
        raise ValueError(str(exc)) from None


def check_options(depth: int, blend: float) -> None:
    if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= MAX_DEPTH:
        raise ValueError(f"rerank_depth must be an integer in 1..{MAX_DEPTH}")
    if isinstance(blend, bool) or not isinstance(blend, (int, float)) or not 0.0 <= float(blend) <= 1.0:
        raise ValueError("rerank_blend must be a number between 0 and 1")


def stage(query: str, ids: Sequence[str], text_of: Mapping[str, str], reranker: TextReranker, *,
          depth: int = DEFAULT_DEPTH, blend: float = DEFAULT_BLEND,
          scores: dict | None = None) -> tuple[list[str], dict]:
    """Rerank the first `depth` of `ids` (first-stage order) and blend the two orders.

    Returns the new order of every id and a report for ``explain``: which
    reranker ran, its latency, how many items it saw, how many moved, and the
    top scores. On failure the order is unchanged and the report carries the error.
    A caller-supplied ``scores`` dict receives every reranked id's score.
    """
    check_options(depth, blend)
    blend = float(blend)
    ids = list(ids)
    head = [i for i in ids[:depth] if i in text_of]
    report: dict = {"reranker": getattr(reranker, "name", type(reranker).__name__), "depth": depth,
                    "blend": blend, "items": len(head), "latency_ms": 0.0, "moved": 0}
    if len(head) < 2:
        return ids, report
    started = time.perf_counter()
    try:
        hits = reranker.rerank(query, [text_of[i] for i in head], top_n=len(head))
    except Exception as exc:  # noqa: BLE001 - a failed reranker keeps the first stage, and says why
        report.update(latency_ms=round((time.perf_counter() - started) * 1000, 1), items=0,
                      error=f"{type(exc).__name__}: {exc}"[:300])
        return ids, report
    report["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    note = getattr(reranker, "last_fallback", "")
    if note:
        report["fallback"] = note
    position = {h.index: n for n, h in enumerate(hits)}
    fused = {i: n for n, i in enumerate(head)}

    def combined(n: int) -> float:
        reranked = position.get(n, len(hits) + n)
        return (1.0 - blend) / (RRF_K + n + 1) + blend / (RRF_K + reranked + 1)

    new_head = [head[n] for n in sorted(range(len(head)), key=lambda n: (-combined(n), n))]
    report["moved"] = sum(1 for n, i in enumerate(new_head) if fused[i] != n)
    report["top"] = [{"id": head[h.index], "score": round(h.score, 4)} for h in hits[:EXPLAIN_TOP]]
    if scores is not None:
        scores.update((head[h.index], h.score) for h in hits)
    chosen = set(new_head)
    return new_head + [i for i in ids if i not in chosen], report
