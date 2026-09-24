"""The semantic retrieval arm, in-process, for a long-lived server.

`commontrace query` runs the semantic arm as a subprocess
(commontrace/reference/query.py), which loads a sentence-transformer model
on every call -- seconds each time. Fine for a person at a terminal; not for
an MCP server answering a `retrieve` on every agent turn, which is why that
surface stayed lexical even when a store had configured fusion, and agents
were given the weaker ranking. Measured on LoCoMo (benchmark/peers/),
lexical alone found 54% of the answering turns in its top 10; fused with
this arm, 64.5% (66% with the idf-v3 lexical arm).

This module runs the SAME ranking function the subprocess runs
(`query.rank`), holding the model and the parsed index in memory between
calls, and returns the slug list the CLI parses from that subprocess's
output (query_cmd._slugs_from_semantic_output over the same lines). So the
two surfaces cannot rank differently: there is one implementation, and this
only decides whether it is re-loaded.

The index is reloaded when index.npz changes (inode, mtime, size). The
model is loaded once, on first use, and never replaced: it is the one
trusted model name the reference script hardcodes.

THE INDEX KEEPS ITSELF CURRENT. Nothing used to rebuild index.npz: a lesson
approved after the last `commontrace index` made the index stale, and a stale
index sent every retrieval back to lexical-only -- silently degrading the
store that opted into fusion, and, worse, logging those occasions under the
lexical label, so an experiment's log mixed two eligibility rules and the
audit reported it as a changed treatment. Now a stale index is refreshed
before ranking (`ensure_fresh`): the builder re-embeds only lessons whose
text changed (build_index.build_or_update_index keys vectors by content
hash), using the model this process already holds. `commontrace query` does
the same through the index command, so both surfaces rank against the same,
current index.
"""

from __future__ import annotations

import importlib.util
import os
import threading

from commontrace import paths

_LOCK = threading.Lock()
_SCRIPT = None
_BUILDER = None
_MODEL = None
_INDEX: dict[str, tuple[tuple, object]] = {}

QUERY_SCRIPT = os.path.join("memory", "attention", "query.py")
BUILD_SCRIPT = os.path.join("memory", "attention", "build_index.py")


def available() -> bool:
    """Whether the optional attention extra is installed (numpy +
    sentence-transformers), without importing either."""
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
    """The reference query module, loaded from the same file the CLI would run."""
    global _SCRIPT
    if _SCRIPT is None:
        _SCRIPT = _load_reference(root, QUERY_SCRIPT, "commontrace_reference_query")
    return _SCRIPT


def _builder(root: str):
    """The reference index builder, loaded from the file `commontrace index` runs."""
    global _BUILDER
    if _BUILDER is None:
        _BUILDER = _load_reference(root, BUILD_SCRIPT, "commontrace_reference_build_index")
    return _BUILDER


def _model(script):
    """The trusted model, loaded once per process; a Ranked failure otherwise."""
    global _MODEL
    if _MODEL is None:
        model = script.load_model()
        if isinstance(model, script.Ranked):
            return model
        _MODEL = model
    return _MODEL


def ensure_fresh(root: str) -> str:
    """Refresh the semantic index if it is stale. Returns "" when it is
    usable afterwards, else why not.

    Staleness is decided by the same check `commontrace query` uses
    (query_cmd._index_is_unusable). Only lessons whose text changed are
    re-embedded.
    """
    from commontrace.commands.query_cmd import _index_is_unusable

    reason = _index_is_unusable(root)
    if not reason:
        return ""
    with _LOCK:
        script, builder = _script(root), _builder(root)
        if script is None or builder is None:
            return reason
        model = _model(script)
        if isinstance(model, script.Ranked):
            return "; ".join(model.stderr) or reason
        try:
            builder.build_or_update_index(
                paths.lessons_dir(root), index_path(root),
                model=model, log=lambda *_a, **_k: None,
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
    """(rc, slugs in rank order, warnings) -- what `commontrace query`'s
    semantic arm returns for the same store and arguments.

    rc != 0 means the arm could not run (no script, corrupt index, model
    unavailable); callers fall back to lexical exactly as the CLI does.
    """
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
        model = _model(script)
        if isinstance(model, script.Ranked):
            return model.rc, [], model.stderr
        result = script.rank(
            query, top_k, 4, agent_type,
            index_path=ipath, lessons_dir=paths.lessons_dir(root),
            index=index, model=model,
        )
    if result.rc != 0:
        return result.rc, [], result.stderr
    return 0, _slugs_from_semantic_output("\n".join(result.lines) + "\n"), result.stderr
