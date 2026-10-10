"""Explicit provider registration. Import strings from untrusted config are never executed."""
from __future__ import annotations

import re
import threading
from collections.abc import Callable

from commontrace.exceptions import CapabilityError, ConfigurationError


class ProviderRegistry:
    def __init__(self):
        self._factories: dict[str, Callable] = {}
        self._lock = threading.RLock()

    def register(self, name: str, factory: Callable) -> None:
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) or not callable(factory):
            raise ConfigurationError("providers require a lowercase name and callable")
        with self._lock:
            if name in self._factories:
                raise ConfigurationError("provider already registered")
            self._factories[name] = factory

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._factories))

    def create(self, name: str, *args, **kwargs):
        with self._lock:
            factory = self._factories.get(name)
        if factory is None:
            raise CapabilityError("requested provider is not registered")
        return factory(*args, **kwargs)


LLMS = ProviderRegistry()
LLM_CREDENTIALS: dict[str, bool] = {}
EMBEDDERS = ProviderRegistry()
VECTORS = ProviderRegistry()
RERANKERS = ProviderRegistry()
MMR_LAMBDA = 0.7


def register_reranker(name: str, factory: Callable) -> None:
    """Register trusted code: ``factory()`` returns ``reranker(task, ranked) -> ranked``.

    The callable has the shape `commontrace.retrieval.apply_reranker` accepts:
    ``apply_reranker(task, ranked, providers.reranker("mmr"))``.
    """
    RERANKERS.register(name, factory)


# Registered rerankers that take a model: ``cohere:rerank-v3.5`` resolves to the
# ``cohere`` factory called with ``"rerank-v3.5"``.
_MODEL_RERANKERS = frozenset({"cohere", "voyage", "jina", "llm"})
_RERANK_MODEL = re.compile(r"[A-Za-z0-9._/\-]{1,128}")


def reranker(name: str):
    """Resolve a reranker by name or ``provider:model`` tag; built-ins register on first use.

    Lesson rerankers (``mmr``) take ``(task, ranked)``. Text rerankers (the
    cross-encoders, ``cohere``, ``voyage``, ``jina``, ``llm``) also expose
    ``rerank(query, documents, top_n)`` for facts, recall and conversations.
    """
    register_builtin_rerankers()
    if not isinstance(name, str):
        raise ConfigurationError("a reranker is chosen by name")
    base, _sep, model = name.partition(":")
    if _sep:
        if base not in _MODEL_RERANKERS or not _RERANK_MODEL.fullmatch(model):
            raise ConfigurationError("only cohere, voyage, jina and llm take a :model suffix")
        return RERANKERS.create(base, model)
    return RERANKERS.create(name)


def reranker_names() -> tuple[str, ...]:
    register_builtin_rerankers()
    return RERANKERS.names()


def _terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2}


def mmr_reranker(lam: float = MMR_LAMBDA):
    """Maximal marginal relevance over lesson descriptions and matched terms.

    Dependency-free: relevance is the first-stage score scaled to [0, 1] and
    redundancy is the highest Jaccard overlap with an already selected lesson.
    Order is a pure function of the input, so ties keep first-stage order.
    """
    if not 0.0 <= lam <= 1.0:
        raise ConfigurationError("MMR lambda must be between 0 and 1")

    def rerank(task: str, ranked: list) -> list:
        if len(ranked) < 3:
            return list(ranked)
        top = max((r.score for r in ranked), default=0.0) or 1.0
        bags = [_terms(r.description) | {t.lower() for t in r.matched_terms} for r in ranked]
        chosen: list[int] = []
        remaining = list(range(len(ranked)))
        while remaining:
            def value(i: int) -> float:
                overlap = max((len(bags[i] & bags[j]) / (len(bags[i] | bags[j]) or 1) for j in chosen), default=0.0)
                return lam * (ranked[i].score / top) - (1.0 - lam) * overlap
            best = max(remaining, key=lambda i: (value(i), -i))
            chosen.append(best)
            remaining.remove(best)
        return [ranked[i] for i in chosen]

    return rerank


def cross_encoder_reranker(mode: str = "cross-encoder"):
    """Second-stage cross-encoder over each lesson's (or document's) full text (attention extra)."""
    from commontrace.reranking import LocalCrossEncoder

    return LocalCrossEncoder(mode)


def _hosted_reranker(provider: str):
    def factory(model: str | None = None):
        from commontrace.rerankers_hosted import HostedReranker

        return HostedReranker(provider, model)
    return factory


def _llm_reranker(model: str | None = None):
    from commontrace.rerankers_hosted import LLMReranker

    return LLMReranker(model)


def register_builtin_rerankers() -> None:
    with _RERANK_LOCK:
        if _RERANK_REGISTERED[0]:
            return
        RERANKERS.register("mmr", mmr_reranker)
        RERANKERS.register("cross-encoder", lambda: cross_encoder_reranker("cross-encoder"))
        RERANKERS.register("cross-encoder-fast", lambda: cross_encoder_reranker("cross-encoder-fast"))
        RERANKERS.register("bge-reranker-v2-m3", lambda: cross_encoder_reranker("bge-reranker-v2-m3"))
        RERANKERS.register("mxbai-rerank", lambda: cross_encoder_reranker("mxbai-rerank"))
        for provider in ("cohere", "voyage", "jina"):
            RERANKERS.register(provider, _hosted_reranker(provider))
        RERANKERS.register("llm", _llm_reranker)
        _RERANK_REGISTERED[0] = True


_RERANK_LOCK = threading.Lock()
_RERANK_REGISTERED = [False]


def register_llm(name: str, complete: Callable, *, requires_api_key: bool = False) -> None:
    """Register trusted application code implementing (Config, prompt) -> (text, usage)."""
    LLMS.register(name, lambda: complete)
    LLM_CREDENTIALS[name] = requires_api_key


def llm_caller(name: str, builtins: dict[str, Callable]) -> Callable:
    return builtins[name] if name in builtins else LLMS.create(name)


def register_local_backends() -> None:
    """Idempotent optional factories; imports/downloads occur only when selected."""
    with _LOCAL_LOCK:
        if _LOCAL_REGISTERED[0]:
            return
        def lance(**kwargs):
            from commontrace.vector_lance import LanceVectorIndex

            return LanceVectorIndex(**kwargs)
        def sqlite(**kwargs):
            from commontrace.vector_store import SQLiteVectorIndex

            return SQLiteVectorIndex.open(**kwargs)
        def postgres(**kwargs):
            from commontrace.vector_store import PostgresVectorIndex

            return PostgresVectorIndex.open(**kwargs)
        def sentence_transformers():
            from commontrace.semantic import load_model

            return load_model()
        VECTORS.register("lance", lance)
        VECTORS.register("sqlite", sqlite)
        VECTORS.register("postgres", postgres)
        EMBEDDERS.register("sentence-transformers", sentence_transformers)
        _LOCAL_REGISTERED[0] = True


_LOCAL_LOCK = threading.Lock()
_LOCAL_REGISTERED = [False]


class BackendFactory:
    """Server-selected stores; owner/dataset are supplied from authenticated policy.

    SQLite uses separate files for every owner/dataset. Postgres/Lance additionally
    enforce their immutable tenant/namespace in every backend operation.
    """
    def __init__(self, root: str):
        self.root = root
        register_local_backends()

    async def vector(self, provider: str, *, owner: str, dataset: str, model: str, dimension: int, dsn: str = ""):
        import hashlib
        import inspect
        import os

        from commontrace import paths
        from commontrace.async_workers import STORE_WORKERS
        from commontrace.vector_store import _Scope

        scope = _Scope(owner, dataset, model, dimension)
        args = {"tenant": scope.tenant, "namespace": scope.namespace, "model": model, "dimension": dimension}
        if provider == "sqlite":
            identity = hashlib.sha256(repr((owner, dataset)).encode()).hexdigest()
            filename = os.path.join(paths.memory_dir(self.root), "vectors", identity+".sqlite")
            paths.enforce_boundary(self.root, filename)
            args["path"] = paths.safe_prepare_output_path(filename, allow_unlink_leaf=False)
        elif provider == "postgres":
            if not dsn:
                raise ConfigurationError("Postgres vector selection requires a server-configured DSN")
            args["dsn"] = dsn
        elif provider == "lance":
            args["root"] = self.root
        else:
            raise CapabilityError("unsupported configured vector backend")
        value = await STORE_WORKERS.run(VECTORS.create, provider, **args)
        return await value if inspect.isawaitable(value) else value
