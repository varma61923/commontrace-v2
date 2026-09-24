"""The semantic retrieval arm, in-process, for a long-lived server.

`commontrace query` runs the semantic arm as a subprocess
(commontrace/reference/query.py), which loads a sentence-transformer model
on every call -- seconds each time. Fine for a person at a terminal; not for
an MCP server answering a `retrieve` on every agent turn, which is why that
surface stayed lexical even when a store had configured fusion, and agents
were given the weaker ranking. Measured on LoCoMo (benchmark/peers/),
lexical alone found 54% of the answering turns in its top 10; fused with
this arm, 63% (66.5% with the idf-v3 lexical arm).

This module runs the SAME ranking function the subprocess runs
(`query.rank`), holding the model and the parsed index in memory between
calls, and returns the slug list the CLI parses from that subprocess's
output (query_cmd._slugs_from_semantic_output over the same lines). So the
two surfaces cannot rank differently: there is one implementation, and this
only decides whether it is re-loaded.

The index is reloaded when index.npz changes (inode, mtime, size). The
model is loaded once, on first use, and never replaced: it is the one
trusted model name the reference script hardcodes.
"""

from __future__ import annotations

import importlib.util
import os
import threading

from commontrace import paths

_LOCK = threading.Lock()
_SCRIPT = None
_MODEL = None
_INDEX: dict[str, tuple[tuple, object]] = {}

QUERY_SCRIPT = os.path.join("memory", "attention", "query.py")


def available() -> bool:
    """Whether the optional attention extra is installed (numpy +
    sentence-transformers), without importing either."""
    return (
        importlib.util.find_spec("numpy") is not None
        and importlib.util.find_spec("sentence_transformers") is not None
    )


def _script(root: str):
    """The reference query module, loaded from the same file the CLI would run."""
    global _SCRIPT
    if _SCRIPT is not None:
        return _SCRIPT
    from commontrace.commands._shellout import find_reference_script

    path = find_reference_script(root, QUERY_SCRIPT)
    if path is None:
        return None
    spec = importlib.util.spec_from_file_location("commontrace_reference_query", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _SCRIPT = module
    return module


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
        global _MODEL
        if _MODEL is None:
            model = script.load_model()
            if isinstance(model, script.Ranked):
                return model.rc, [], model.stderr
            _MODEL = model
        result = script.rank(
            query, top_k, 4, agent_type,
            index_path=ipath, lessons_dir=paths.lessons_dir(root),
            index=index, model=_MODEL,
        )
    if result.rc != 0:
        return result.rc, [], result.stderr
    return 0, _slugs_from_semantic_output("\n".join(result.lines) + "\n"), result.stderr
