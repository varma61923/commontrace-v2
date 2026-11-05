"""Content signatures for any memory, so measured evidence transfers across agents, models and robots.

Lesson files have text, but most memories reach UniMem as an id, a revision and
content that passes through: candidates sent to the gateway, items behind
framework middleware, skills and keyframes behind a robot bridge. The effect
field needs a content representation to carry randomized evidence between
similar memories. A signature is that representation, without the content
itself:

- hashed term counts of the memory's text (`effect_field.term_bag`, at most 256
  terms);
- an optional embedding with its model label, from any encoder. Examples are a
  sentence encoder for an LLM tip, a vision-language encoder for a robot keyframe
  or skill, or a policy embedding for a motion primitive. Memories are compared
  only within the same model label, so a robot fleet and a chat agent can share
  one store without their spaces mixing.

Signatures are recorded at recall, only for randomized memories, only while a
``thompson-field`` schedule is in force or recording was enabled explicitly, and
only when a memory's revision or embedding changes. They are derived from memory
content, so they are erasable: `forget` removes them by rewriting the file, and
the canonical erasure cascade calls it. Hashed terms are not encryption: anyone
holding a vocabulary can test whether a word occurred.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import re
from array import array
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from unimem import _jsonl, effect_field, paths

MAX_DIMS = 4096
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")
CANDIDATE_DAYS = 30  # a memory unseen for this long is no longer forecast
SIGNATURES_NAME = "signatures.jsonl"
RECORDING_NAME = "record-signatures"
DEFAULT_MODEL = "embedding"


def _dir(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "field")


def signatures_path(root: str) -> str:
    return os.path.join(_dir(root), SIGNATURES_NAME)


@dataclass(frozen=True)
class Signature:
    memory: str
    key: str  # content revision plus embedding digest: a new key is a new signature
    terms: Mapping[str, int]
    model: str | None = None
    vector: tuple[float, ...] | None = None
    at: str = ""


def embedding(value: object, model: object = None) -> tuple[str, tuple[float, ...]] | None:
    """A validated (model, vector), or None when `value` is None."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= MAX_DIMS:
        raise ValueError(f"embedding must be a list of 1 to {MAX_DIMS} numbers")
    vector = []
    for x in value:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            raise ValueError("embedding values must be finite numbers")
        vector.append(round(float(x), 6))
    label = DEFAULT_MODEL if model is None else model
    if not isinstance(label, str) or not MODEL_RE.match(label):
        raise ValueError("embedding_model must be a short label such as 'siglip2-base' or 'bge-small-en-v1.5'")
    if not any(vector):
        raise ValueError("embedding must not be all zeros")
    return label, tuple(vector)


def _key(revision: str | None, emb: tuple[str, tuple[float, ...]] | None) -> str:
    vector = "" if emb is None else emb[0] + ":" + hashlib.sha256(array("d", emb[1]).tobytes()).hexdigest()[:16]
    return f"{revision or ''}|{vector}"


# -- recording -----------------------------------------------------------------------------------


def set_recording(root: str, on: bool) -> None:
    """Record signatures even without a ``thompson-field`` schedule (for example to warm a store)."""
    path = os.path.join(_dir(root), RECORDING_NAME)
    if on:
        os.makedirs(_dir(root), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("on\n")
    else:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def enabled(root: str) -> bool:
    if os.path.exists(os.path.join(_dir(root), RECORDING_NAME)):
        return True
    from unimem import allocation, holdout_io

    current = allocation.active(root, holdout_io.load_config(root).salt)
    return current is not None and current.policy.get("mode") == allocation.MODE_FIELD


_INDEX: dict[str, tuple[int, int, dict[str, Signature]]] = {}  # path -> (inode, parsed bytes, latest by memory)


def _parse(row: Mapping[str, Any]) -> Signature | None:
    try:
        memory, key = str(row["id"]), str(row["key"])
        terms = {str(t): int(c) for t, c in dict(row.get("terms") or {}).items() if int(c) > 0}
        vector = tuple(float(x) for x in row["vector"]) if row.get("vector") else None
    except (KeyError, TypeError, ValueError):
        return None
    return Signature(memory, key, terms, row.get("model") if vector else None, vector, str(row.get("at") or ""))


def load(root: str) -> dict[str, Signature]:
    """The latest signature of every memory. Appends are parsed incrementally."""
    path = signatures_path(root)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        _INDEX.pop(path, None)
        return {}
    cached = _INDEX.get(path)
    if cached is not None and cached[0] == st.st_ino and cached[1] <= st.st_size:
        offset, latest = cached[1], dict(cached[2])
    else:
        offset, latest = 0, {}
    if offset < st.st_size:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
        complete = data.rfind(b"\n") + 1
        for line in data[:complete].splitlines():
            try:
                raw = json.loads(line)
            except ValueError:
                continue
            sig = _parse(raw) if isinstance(raw, dict) else None
            if sig is not None:
                latest[sig.memory] = sig
        offset += complete
    _INDEX[path] = (st.st_ino, offset, latest)
    return dict(latest)


def observe(
    root: str,
    items: Iterable[tuple[str, str | None, str | None, tuple[str, tuple[float, ...]] | None]],
    *,
    durable: bool = False,
) -> int:
    """Record (memory, text, revision, embedding) signatures that are new or changed; returns how many."""
    known = load(root)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rows = []
    for memory, text, revision, emb in items:
        key = _key(revision, emb)
        if memory in known and known[memory].key == key:
            continue
        row: dict[str, Any] = {"id": memory, "key": key, "terms": effect_field.term_bag(text or ""), "at": now}
        if emb is not None:
            row["model"], row["vector"] = emb[0], list(emb[1])
        if row["terms"] or emb is not None:
            rows.append(row)
    if rows:
        path = signatures_path(root)
        with _jsonl.locked(path):
            for row in rows:
                _jsonl.append_row(path, row, fsync=durable)
    return len(rows)


def forget(root: str, memories: Iterable[str]) -> list[str]:
    """Remove every signature of `memories` from disk; returns the memories that had one."""
    targets = set(memories)
    path = signatures_path(root)
    if not targets or not os.path.exists(path):
        return []
    with _jsonl.locked(path):
        rows = _jsonl.read_rows(path)
        hit = sorted({str(r.get("id")) for r in rows if r.get("id") in targets})
        if hit:
            _jsonl.write_rows(path, [r for r in rows if r.get("id") not in targets])
            _INDEX.pop(path, None)
    return hit


# -- the kernel ----------------------------------------------------------------------------------


def kernel(
    root: str, texts: Mapping[str, str], measured: Iterable[str], active: Iterable[str]
) -> tuple[effect_field.Kernel, list[str]]:
    """A kernel over lesson `texts` and every recorded signature, and the unmeasured memories to forecast.

    A lesson file's text wins over a signature of the same id. Candidates are active
    lessons and memories whose signature was recorded in the last CANDIDATE_DAYS.
    """
    measured, active = set(measured), set(active)
    bags = {m: effect_field.term_bag(t) for m, t in texts.items() if m in measured or m in active}
    embeddings: dict[str, tuple[str, Sequence[float]]] = {}
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=CANDIDATE_DAYS)).isoformat()
    recent: set[str] = set()
    for memory, sig in load(root).items():
        if memory not in bags and sig.terms:
            bags[memory] = dict(sig.terms)
        if sig.vector is not None and sig.model is not None:
            embeddings[memory] = (sig.model, sig.vector)
        if sig.at >= cutoff:
            recent.add(memory)
    return effect_field.Kernel.combined(bags, embeddings), sorted((active | recent) - measured)
