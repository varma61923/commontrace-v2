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
when a memory's revision or embedding changes, with at most daily recency refreshes
for unchanged content. They are derived from memory
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
import struct
import threading
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from unimem import _jsonl, effect_field, paths

MAX_DIMS = 4096
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")
CANDIDATE_DAYS = 30  # a memory unseen for this long is no longer forecast
REFRESH_DAYS = 1  # refresh recency at most daily for an unchanged signature
MAX_CACHED_ROOTS = 128
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
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            raise ValueError("embedding values must be finite numbers")
        try:
            number = float(x)
        except OverflowError:
            raise ValueError("embedding values must be finite numbers") from None
        if not math.isfinite(number):
            raise ValueError("embedding values must be finite numbers")
        vector.append(round(number, 6))
    label = DEFAULT_MODEL if model is None else model
    if not isinstance(label, str) or not MODEL_RE.fullmatch(label):
        raise ValueError("embedding_model must be a short label such as 'siglip2-base' or 'bge-small-en-v1.5'")
    if not any(vector):
        raise ValueError("embedding must not be all zeros")
    return label, tuple(vector)


def _key(revision: str | None, emb: tuple[str, tuple[float, ...]] | None, text: str | None = None) -> str:
    vector = (
        "" if emb is None else emb[0] + ":" + hashlib.sha256(struct.pack(f"<{len(emb[1])}d", *emb[1])).hexdigest()[:16]
    )
    if not revision:
        revision = "text:" + hashlib.sha256((text or "")[: effect_field.MAX_TEXT_CHARS].encode("utf-8")).hexdigest()
    return f"{revision}|{vector}"


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


def enabled(root: str, salt: str | None = None) -> bool:
    """Should recall record signatures? `salt` saves a config read when the caller already has it."""
    if os.path.exists(os.path.join(_dir(root), RECORDING_NAME)):
        return True
    from unimem import allocation

    return allocation.field_mode(root, salt)


_INDEX: OrderedDict[str, tuple[tuple[int, ...], int, dict[str, Signature]]] = OrderedDict()
_INDEX_LOCK = threading.Lock()


def _after_fork() -> None:
    global _INDEX_LOCK
    _INDEX_LOCK = threading.Lock()
    _INDEX.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _parse(row: Mapping[str, Any]) -> Signature | None:
    try:
        memory, key = row["id"], row["key"]
        if not isinstance(memory, str) or not memory or not isinstance(key, str) or not key:
            return None
        raw_terms = row.get("terms") or {}
        if not isinstance(raw_terms, dict) or len(raw_terms) > effect_field.MAX_BAG_TERMS:
            return None
        if any(
            not isinstance(t, str) or type(c) is not int or not 1 <= c <= effect_field.MAX_TEXT_CHARS
            for t, c in raw_terms.items()
        ):
            return None
        emb = embedding(row.get("vector"), row.get("model"))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return Signature(
        memory,
        key,
        MappingProxyType(dict(raw_terms)),
        emb[0] if emb else None,
        emb[1] if emb else None,
        str(row.get("at") or ""),
    )


def load(root: str) -> dict[str, Signature]:
    """The latest signature of every memory. Appends are parsed incrementally."""
    path = signatures_path(root)
    with _INDEX_LOCK:
        return dict(_load(path))


def _load(path: str) -> dict[str, Signature]:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        _INDEX.pop(path, None)
        return {}
    cached = _INDEX.get(path)
    stamp = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    if cached is not None and cached[0][:2] == stamp[:2] and (cached[0] == stamp or cached[0][2] < st.st_size):
        offset, latest = cached[1], cached[2]
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
    _INDEX[path] = (stamp, offset, latest)
    _INDEX.move_to_end(path)
    while len(_INDEX) > MAX_CACHED_ROOTS:
        _INDEX.popitem(last=False)
    return latest


def observe(
    root: str,
    items: Iterable[tuple[str, str | None, str | None, tuple[str, tuple[float, ...]] | None]],
    *,
    durable: bool = False,
) -> int:
    """Record new/changed (memory, text, revision, embedding) signatures or refresh daily; returns how many."""
    observed_at = datetime.datetime.now(datetime.timezone.utc)
    now = observed_at.isoformat()
    refresh = (observed_at - datetime.timedelta(days=REFRESH_DAYS)).isoformat()
    rows = []
    for memory, text, revision, emb in items:
        key = _key(revision, emb, text)
        row: dict[str, Any] = {"id": memory, "key": key, "terms": effect_field.term_bag(text or ""), "at": now}
        if emb is not None:
            row["model"], row["vector"] = emb[0], list(emb[1])
        rows.append(row)
    if not rows:
        return 0
    path = signatures_path(root)
    written = 0
    with _jsonl.locked(path):
        _jsonl.discard_unterminated_tail(path)
        with _INDEX_LOCK:
            index = _load(path)
            latest = {row["id"]: index[row["id"]] for row in rows if row["id"] in index}
        for row in rows:
            previous = latest.get(row["id"])
            if previous is None and not row["terms"] and "vector" not in row:
                continue
            if previous is None or previous.key != row["key"] or previous.at < refresh:
                _jsonl.append_row(path, row, fsync=durable)
                latest[row["id"]] = Signature(row["id"], row["key"], row["terms"], at=now)
                written += 1
    return written


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
            with _INDEX_LOCK:
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
    path = signatures_path(root)
    with _jsonl.locked(path):
        signatures = load(root)
        expired = {memory for memory, sig in signatures.items() if memory not in measured | active and sig.at < cutoff}
        if expired:
            _jsonl.write_rows(path, [row for row in _jsonl.read_rows(path) if row.get("id") not in expired])
            with _INDEX_LOCK:
                _INDEX.pop(path, None)
            signatures = {memory: sig for memory, sig in signatures.items() if memory not in expired}
    for memory, sig in signatures.items():
        if memory not in bags and sig.terms:
            bags[memory] = dict(sig.terms)
        if sig.vector is not None and sig.model is not None:
            embeddings[memory] = (sig.model, sig.vector)
        if sig.at >= cutoff:
            recent.add(memory)
    return effect_field.Kernel.combined(bags, embeddings), sorted((active | recent) - measured)
