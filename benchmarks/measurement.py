"""Source-bound, model-free conversation benchmark measurement contracts.

Rank metrics describe candidate ordering before context packing. They do not
establish that a selected excerpt contains an answer or that a reader is correct.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from commontrace.conversation.store import Store, Turn

SCHEMA_VERSION = 1
_ADAPTER_NAMES = frozenset({
    "locomo_cases", "sample_cases", "longmemeval_cases", "beam_cases", "dolphin_cases", "_ids",
    "_payload_from_cases", "_cases_from_payload", "BEAM_MARK", "LOCOMO_CATEGORIES",
    "_load_cases",
})


def dataset_digest(path: str) -> str:
    """Hash dataset bytes, not mtime; directory datasets bind structured inputs."""
    digest = hashlib.sha256()
    if os.path.isfile(path):
        files = [("", path)]
    elif os.path.isdir(path):
        files = []
        for directory, dirs, names in os.walk(path):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(names):
                if name.endswith((".json", ".jsonl", ".yaml", ".yml", ".parquet")):
                    full = os.path.join(directory, name)
                    files.append((os.path.relpath(full, path).replace(os.sep, "/"), full))
        files.sort()
        if not files:
            raise ValueError("dataset directory contains no structured input files")
    else:
        raise FileNotFoundError(path)
    for relative, full in files:
        if relative:
            digest.update(relative.encode("utf-8") + b"\0")
        with open(full, "rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        if relative:
            digest.update(b"\0")
    return digest.hexdigest()


def adapter_digest(path: str) -> str:
    """Pin normalization and serialization semantics independently of retrieval."""
    with open(path, encoding="utf-8") as source:
        tree = ast.parse(source.read())
    selected: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _ADAPTER_NAMES:
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in _ADAPTER_NAMES for target in node.targets):
            selected.append(node)
    digest = hashlib.sha256(ast.dump(ast.Module(body=selected, type_ignores=[])).encode("utf-8"))
    with open(__file__, "rb") as source:
        digest.update(source.read())
    return digest.hexdigest()


def product_digest(root: str) -> str:
    """Bind run evidence to the complete Python product, not a mutable branch name."""
    digest = hashlib.sha256()
    files: list[str] = []
    base = os.path.join(root, "commontrace")
    for directory, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        files.extend(os.path.join(directory, name) for name in names if name.endswith(".py"))
    for path in sorted(files):
        digest.update(os.path.relpath(path, root).replace(os.sep, "/").encode("utf-8") + b"\0")
        with open(path, "rb") as source:
            digest.update(source.read())
        digest.update(b"\0")
    return digest.hexdigest()


def question_digest(question: Mapping[str, Any]) -> str:
    """Bind pairing to the actual question, answer, rubric and gold annotations."""
    def encode(value: object) -> object:
        if isinstance(value, (set, frozenset)):
            return sorted(value)
        raise TypeError(f"unsupported question field type: {type(value).__name__}")

    payload = json.dumps(dict(question), sort_keys=True, ensure_ascii=False, default=encode, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def gold_sessions(evidence: Iterable[str], explicit: Iterable[str],
                  sessions: Sequence[tuple[str, Any, Sequence[Mapping[str, Any]]]]) -> list[str]:
    """Resolve gold turn refs to sessions; unknown annotations remain misses."""
    by_ref: dict[str, str] = {}
    names = set()
    for session, _date, messages in sessions:
        names.add(session)
        for message in messages:
            ref = str(message["id"])
            if ref in by_ref and by_ref[ref] != session:
                raise ValueError("a gold turn reference maps to multiple sessions")
            by_ref[ref] = session
    out = set(explicit)
    for ref in set(evidence):
        if ref in by_ref:
            out.add(by_ref[ref])
            continue
        # Some datasets annotate a source session rather than an individual turn.
        fragments = {session for candidate, session in by_ref.items() if candidate.startswith(ref + "#")}
        if fragments:
            out.update(fragments)
        else:
            missing = "!unresolved-turn!" + hashlib.sha256(ref.encode("utf-8")).hexdigest()
            while missing in names or missing in out:
                missing += "!"
            out.add(missing)
    return sorted(out)


def gold_turns(evidence: Iterable[str],
               sessions: Sequence[tuple[str, Any, Sequence[Mapping[str, Any]]]]) -> set[str]:
    """Expand dataset session-level annotations; never drop an unresolved ref."""
    refs = {str(message["id"]) for _session, _date, messages in sessions for message in messages}
    out = set()
    for ref in set(evidence):
        if ref in refs:
            out.add(ref)
        else:
            fragments = {candidate for candidate in refs if candidate.startswith(ref + "#")}
            out.update(fragments or {ref})
    return out


def ranking_fields(ranked: Sequence[int], turns: Mapping[int, Turn],
                   evidence: Iterable[str], session_gold: Iterable[str]) -> dict[str, list[str]]:
    """Collapse split passages to unique raw turns, then to unique sessions."""
    refs: dict[str, None] = {}
    sessions: dict[str, None] = {}
    for identity in ranked:
        turn = turns.get(identity)
        if turn is None:
            raise ValueError("ranked turn is absent from the canonical store")
        if not turn.ref:
            raise ValueError("benchmark turn has no raw source reference")
        refs.setdefault(turn.ref, None)
        sessions.setdefault(turn.session, None)
    return {
        "ranked_turn_ids": list(refs), "ranked_session_ids": list(sessions),
        "gold_turn_ids": sorted(set(evidence)), "gold_session_ids": sorted(set(session_gold)),
    }


def canonical_digest(store: Store) -> str:
    """Bind all persisted raw-turn/session fields, including dates and speakers."""
    digest = hashlib.sha256()
    for sql in ("SELECT * FROM sessions ORDER BY seq,id", "SELECT * FROM turns ORDER BY id"):
        cursor = store.db.execute(sql)
        digest.update(sql.encode("utf-8") + b"\0")
        while rows := cursor.fetchmany(256):
            for row in rows:
                digest.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                digest.update(b"\0")
    return digest.hexdigest()


def shared_source_clusters(cases: Sequence[tuple[str, Any, Any, Sequence[Mapping[str, Any]]]]) -> dict[str, str]:
    """Keep shared gold-source histories together, without merging filler sessions.

    Some datasets reuse an answer session across separately stored questions.
    Conservatively joining their source components avoids treating those
    questions as independent; absence of annotations falls back to case identity.
    """
    parents = {space: space for space, _sessions, _now, _questions in cases}
    owners: dict[str, str] = {}

    def root(space: str) -> str:
        while parents[space] != space:
            parents[space] = parents[parents[space]]
            space = parents[space]
        return space

    for space, _sessions, _now, questions in cases:
        for question in questions:
            for session in sorted(question["sessions"]):
                if session in owners:
                    left, right = sorted((root(space), root(owners[session])))
                    parents[right] = left
                else:
                    owners[session] = space
    return {space: root(space) for space in sorted(parents)}
