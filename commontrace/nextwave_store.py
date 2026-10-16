"""UniMem versioned metadata and deletable, bounded artifact content.

Signed journals retain commitments and scope labels, rather than private content.
Content is stored separately so erasure can remove every managed copy. This is
local storage plumbing, not an admission bypass: callers use `admit` before
exporting source material, and artifact reads enforce their inherited scopes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from commontrace import _jsonl, frontmatter, lesson_cache, lesson_io, memory_authority, memory_control, paths
from commontrace import frontier_protocol as fp

MAX_OBJECTS = 4096
MAX_BLOB = 256 * 1024
MAX_TOTAL = 64 * fp.MAX_BYTES


def operator() -> None:
    if memory_authority.WRITER.get()[1] != "operator":
        raise PermissionError("UniMem operation requires operator authority")


def kind_name(kind: str) -> str:
    if not isinstance(kind, str) or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", kind) is None:
        raise ValueError("invalid UniMem artifact kind")
    return kind


def journal_name(kind: str) -> str:
    return "unimem-" + kind_name(kind) + ".jsonl"


def events(root: str, kind: str) -> list[dict[str, Any]]:
    return fp.journal(root, journal_name(kind))


def latest(root: str, kind: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for event in events(root, kind):
        result[event["id"]] = event
    fp.count(len(result), 0, MAX_OBJECTS, "UniMem artifact count")
    return result


def event(root: str, kind: str, identity: str, value: dict[str, Any]) -> dict[str, Any]:
    fp.text(identity)
    if set(value) & {"type", "version", "id", "sequence", "previous", "recorded_at"}:
        raise ValueError("reserved UniMem event fields")
    return fp.append(root, journal_name(kind),
                     {"type": "unimem." + kind_name(kind), "version": 1, "id": identity, **value})


def _blob(root: str, kind: str, digest: str) -> str:
    if re.fullmatch(r"[a-f0-9]{64}", digest) is None:
        raise ValueError("invalid UniMem content digest")
    filename = os.path.join(paths.memory_dir(root), "unimem", kind_name(kind), digest + ".json")
    paths.enforce_boundary(root, filename)
    if os.path.islink(filename):
        raise ValueError("UniMem artifact must not be a symlink")
    return filename


def put(root: str, kind: str, identity: str, value: dict[str, Any], *, scopes: Sequence[str] = (),
        sources: Sequence[str] = (), immutable: bool = False) -> dict[str, Any]:
    data = fp.wire(value)
    if len(data) > MAX_BLOB:
        raise ValueError("UniMem artifact exceeds 256 KiB")
    fp.text(identity)
    fp.count(len(scopes), 0, 128)
    fp.count(len(sources), 0, 256)
    labels = sorted({fp.text(s, "scope", 256) for s in scopes})
    ids = sorted({fp.text(s) for s in sources})
    required = memory_control.REQUIRED_PRINCIPAL_SCOPE.get()
    if required and required not in labels:
        raise PermissionError("UniMem artifact cannot escape its authenticated agent scope")
    digest = hashlib.sha256(data).hexdigest()
    filename = _blob(root, kind, digest)
    with _jsonl.locked(fp.filename(root, journal_name(kind))):
        prior = latest(root, kind)
        old = prior.get(identity)
        if old and (old.get("scopes") != labels or old.get("sources") != ids):
            raise PermissionError("UniMem artifact identity cannot change its scope or sources")
        if old and old.get("status") == "erased":
            raise PermissionError("UniMem erased artifact identity cannot be reused")
        if old and old.get("digest") == digest:
            return dict(old)
        if immutable and old:
            raise ValueError("UniMem artifact is immutable")
        fp.count(len(prior) + (identity not in prior), 1, MAX_OBJECTS)
        directory = os.path.dirname(filename)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        entries = list(Path(directory).iterdir())
        fp.count(len(entries), 0, 8192, "UniMem artifact files")
        total = sum(p.stat().st_size for p in entries if p.is_file())
        if total + len(data) > MAX_TOTAL:
            raise ValueError("UniMem artifact content exceeds 64 MiB; archive a scoped namespace")
        if not os.path.exists(filename):
            def write(stream: Any) -> None:
                stream.write(data.decode("utf-8") + "\n")
            _jsonl._replace(filename, write)
        signed = event(root, kind, identity, {"digest": digest, "scopes": labels,
                                             "sources": ids, "status": "active"})
        return dict(signed["record"])


def visible(scopes: Sequence[str], context: Sequence[str]) -> bool:
    required = memory_control.REQUIRED_PRINCIPAL_SCOPE.get()
    return (not required or required in scopes) and set(scopes) <= set(context)


def get(root: str, kind: str, identity: str, *, context: Sequence[str] = (),
        owner: bool = False) -> dict[str, Any]:
    fp.text(identity)
    if owner:
        operator()
    row = latest(root, kind).get(identity)
    if row is None or row.get("status") != "active":
        raise ValueError("UniMem artifact is unavailable")
    if not owner and not visible(row["scopes"], context):
        raise PermissionError("UniMem artifact is outside the caller's scope")
    filename = _blob(root, kind, row["digest"])
    try:
        with open(filename, "rb") as stream:
            raw = stream.read(MAX_BLOB + 2)
    except FileNotFoundError:
        raise ValueError("UniMem artifact content was erased or archived") from None
    if len(raw) > MAX_BLOB + 1:
        raise ValueError("UniMem artifact exceeds its size limit")
    value = json.loads(raw)
    if not isinstance(value, dict) or fp.digest(value) != row["digest"]:
        raise ValueError("UniMem artifact content does not match its signed commitment")
    return value


def list_objects(root: str, kind: str, *, context: Sequence[str] = (), owner: bool = False) -> list[dict[str, Any]]:
    if owner:
        operator()
    result = []
    for identity, row in sorted(latest(root, kind).items()):
        if row.get("status") == "active" and (owner or visible(row["scopes"], context)):
            result.append(get(root, kind, identity, context=context, owner=owner))
    return result


def admit(root: str, identities: Sequence[str], *, context: Sequence[str] = (),
          action_class: str = "") -> dict[str, dict[str, Any]]:
    """Canonical fact admission, eligible lesson admission and IFC presentation.

    This helper deliberately does not turn an arbitrary artifact ID into a fact.
    Features must separately check their own artifact scope and source admission.
    """
    from commontrace import memory_ifc, search_recipes

    fp.count(len(identities), 0, 512)
    ids = sorted({fp.text(i) for i in identities})
    existing: set[str] = set()
    facts = search_recipes.admit(root, ids, context=list(context), action_class=action_class, existing=existing)
    result = {identity: memory_ifc.label(root, {"id": identity, "text": fact.statement,
        "revision": fact.revision, "scopes": fact.scopes}) for identity, fact in facts.items()}
    for identity in ids:
        if identity in existing:
            continue
        path = lesson_io.lesson_path(root, identity)
        if path is None:
            continue
        fm, body = frontmatter.read(path)
        if (set(fm.get("scopes", [])) <= set(context)
                and lesson_cache.fresh_eligible(path, fm, identity, body=body, root=root)
                and not memory_authority.lineage_blocked(root, identity)):
            result[identity] = memory_ifc.label(root, {"id": identity, "text": body,
                "revision": lesson_io.current_revision(path), "scopes": fm.get("scopes", [])})
    return result


def erase(root: str, identities: Sequence[str]) -> list[dict[str, Any]]:
    """Remove managed descendants' blobs; retain signed hash tombstones.

    Shared blobs are retained when an unrelated live artifact still references
    them. The caller handles legal holds, hazard checks and external replicas.
    """
    operator()
    fp.count(len(identities), 1, 2048)
    deleted = {fp.text(i) for i in identities}
    directory = Path(paths.memory_dir(root))
    files = sorted(directory.glob("unimem-*.jsonl"))
    fp.count(len(files), 0, 128)
    removed = []
    for filename in files:
        kind = filename.name[len("unimem-"):-len(".jsonl")]
        with _jsonl.locked(str(filename)):
            objects = latest(root, kind)
            affected = {identity for identity, row in objects.items()
                        if identity in deleted or set(row.get("sources", [])) & deleted}
            live_digests = {row.get("digest") for identity, row in objects.items()
                            if identity not in affected and row.get("status") == "active"}
            for identity in sorted(affected):
                row = objects[identity]
                if row.get("status") == "erased":
                    continue
                versions = {str(e["digest"]) for e in events(root, kind) if e["id"] == identity and e.get("digest")}
                for digest in versions - live_digests:
                    try:
                        os.unlink(_blob(root, kind, str(digest)))
                    except FileNotFoundError:
                        pass
                event(root, kind, identity, {"status": "erased", "digest": row.get("digest"),
                                            "scopes": row.get("scopes", []), "sources": row.get("sources", [])})
                removed.append({"kind": kind, "id": identity, "content_digests": sorted(versions)})
    return removed
