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
