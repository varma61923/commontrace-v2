"""The semantic retrieval arm, in-process, for a long-lived server."""

from __future__ import annotations

import importlib.util
import os
import threading

from commontrace import paths

_LOCK = threading.Lock()
_SCRIPT = None
_BUILDER = None
_MODELS: dict[str, object] = {}
_INDEX: dict[str, tuple[tuple, object]] = {}

QUERY_SCRIPT = os.path.join("memory", "attention", "query.py")
BUILD_SCRIPT = os.path.join("memory", "attention", "build_index.py")


def available() -> bool:
    return (
        importlib.util.find_spec("numpy") is not None
        and importlib.util.find_spec("sentence_transformers") is not None
    )


def _load_reference(root: str, relative: str, name: str):
    from commontrace.commands._shellout import find_reference_script

    path = find_reference_script(root, relative)
    if path is None:
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script(root: str):
    global _SCRIPT
    if _SCRIPT is None:
        _SCRIPT = _load_reference(root, QUERY_SCRIPT, "commontrace_reference_query")
    return _SCRIPT


def _builder(root: str):
    global _BUILDER
    if _BUILDER is None:
        _BUILDER = _load_reference(root, BUILD_SCRIPT, "commontrace_reference_build_index")
    return _BUILDER


def _model(script, name: str):
    if name not in _MODELS:
        model = script.load_model(name)
        if isinstance(model, script.Ranked):
            return model
        _MODELS[name] = model
    return _MODELS[name]


def stored_model(root: str) -> str | None:
    try:
        import numpy as np

        with np.load(index_path(root), allow_pickle=False) as data:
            return str(data["model_name"])
    except Exception:  # noqa: BLE001 - no readable index names no model
        return None


def index_model(root: str) -> str | None:
    cached = _INDEX.get(index_path(root))
    return str(cached[1][0]) if cached is not None else None


def ensure_fresh(root: str) -> str:
    from commontrace.commands.query_cmd import _index_is_unusable

    reason = _index_is_unusable(root)
    if not reason:
        return ""
    with _LOCK:
        script, builder = _script(root), _builder(root)
        if script is None or builder is None:
            return reason
        from commontrace import retrieval_io

        name = builder.index_model(index_path(root), retrieval_io.logged_embedding_model(root))
        model = _model(script, name)
        if isinstance(model, script.Ranked):
            return "; ".join(model.stderr) or reason
        try:
            builder.build_or_update_index(
                paths.lessons_dir(root), index_path(root),
                model_name=name, model=model, log=lambda *_a, **_k: None,
            )
        except Exception as exc:  # noqa: BLE001 - a failed refresh falls back, never crashes retrieval
            return f"{reason}; refreshing it failed: {type(exc).__name__}: {exc}"
    return _index_is_unusable(root)


def _identity(path: str) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def index_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "attention", "index.npz")


def ranked_slugs(
    root: str, query: str, top_k: int, agent_type: str | None = None,
) -> tuple[int, list[str], list[str]]:
    from commontrace.commands.query_cmd import _slugs_from_semantic_output

    with _LOCK:
        script = _script(root)
        if script is None:
            return 1, [], ["the semantic query script is not installed"]
        ipath = index_path(root)
        identity = _identity(ipath)
        cached = _INDEX.get(ipath)
        if cached is not None and identity is not None and cached[0] == identity:
            index = cached[1]
        else:
            index = script.load_index(ipath)
            if isinstance(index, script.Ranked):
                _INDEX.pop(ipath, None)
                return index.rc, [], index.stderr
            _INDEX[ipath] = (identity, index)
        model = _model(script, index[0]) if index[0] in script.TRUSTED_MODELS else None
        if model is not None and isinstance(model, script.Ranked):
            return model.rc, [], model.stderr
        from commontrace import lesson_cache

        try:
            index_ns = os.stat(ipath).st_mtime_ns
            mtimes = [
                (p, lesson_cache.mtime_seconds(ns)) for p, ns, _size in lesson_cache.listing(root)
                if ns > index_ns - 1_000_000
            ]
        except OSError:
            mtimes = None
        result = script.rank(
            query, top_k, 4, agent_type,
            index_path=ipath, lessons_dir=paths.lessons_dir(root),
            index=index, model=model, lesson_mtimes=mtimes,
        )
    if result.rc != 0:
        return result.rc, [], result.stderr
    return 0, _slugs_from_semantic_output("\n".join(result.lines) + "\n"), result.stderr
