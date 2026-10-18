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
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from commontrace import _jsonl, frontmatter, lesson_cache, lesson_io, memory_authority, memory_control, paths
from commontrace import frontier_protocol as fp

MAX_OBJECTS = 4096
MAX_BLOB = 256 * 1024
MAX_TOTAL = 64 * fp.MAX_BYTES
_ERASURE_HOOKS: dict[str, Callable[[str, set[str]], None]] = {}


def register_erasure_hook(kind: str, hook: Callable[[str, set[str]], None]) -> None:
    """Install a process-local managed-cache invalidator; no dynamic imports."""
    _ERASURE_HOOKS[kind_name(kind)] = hook


def operator() -> None:
    from commontrace import capabilities

    if capabilities.CURRENT.get() is not None or memory_authority.bound_writer()[1] != "operator":
        raise PermissionError("UniMem operation requires an undelegated operator context")


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
    if re.fullmatch(r"tier-[a-f0-9]{32}", kind):
        from commontrace import tiering

        tiering.assert_read(root, kind)
    data = fp.wire(value)
    if len(data) > MAX_BLOB:
        raise ValueError("UniMem artifact exceeds 256 KiB")
    fp.text(identity)
    fp.count(len(scopes), 0, 128)
    fp.count(len(sources), 0, 256)
    labels = sorted({fp.text(s, "scope", 256) for s in scopes})
    ids = sorted({fp.text(s, "source", 256) for s in sources})
    from commontrace import policy_io

    policy_io.assert_write(root, {"id": identity, "kind": kind, "scopes": labels, "sources": ids})
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
    if owner:
        operator()
    return _get(root, kind, identity, context=context, owner=owner, stack=set(), cache={}, budget=[0])


def reference(kind: str, identity: str) -> str:
    return fp.text("artifact:" + kind_name(kind) + ":" + fp.text(identity), "UniMem source reference", 256)


def _get(root: str, kind: str, identity: str, *, context: Sequence[str], owner: bool,
         stack: set[tuple[str, str]], cache: dict[tuple[str, str], dict[str, Any]],
         budget: list[int]) -> dict[str, Any]:
    if re.fullmatch(r"tier-[a-f0-9]{32}", kind):
        from commontrace import tiering

        tiering.assert_read(root, kind)
    fp.text(identity)
    key = kind_name(kind), identity
    if key in stack:
        raise ValueError("UniMem artifact source graph contains a cycle")
    if key in cache:
        return cache[key]
    fp.count(len(stack), 0, 15, "UniMem artifact source depth")
    fp.count(len(cache) + len(stack), 0, 2047, "UniMem artifact source nodes")
    stack.add(key)
    row = latest(root, kind).get(identity)
    if row is None or row.get("status") != "active":
        raise ValueError("UniMem artifact is unavailable")
    from commontrace import policy_io

    if not policy_io.permits(root, {**row, "kind": kind}):
        raise PermissionError("UniMem active finite policy denies this artifact read")
    if memory_authority.lineage_blocked(root, identity):
        raise PermissionError("UniMem artifact was revoked by its canonical source authority")
    if not owner and not visible(row["scopes"], context):
        raise PermissionError("UniMem artifact is outside the caller's scope")
    sources = row.get("sources", [])
    canonical = [s for s in sources if not s.startswith("artifact:")]
    if canonical and set(admit(root, canonical, context=context)) != set(canonical):
        raise PermissionError("UniMem artifact has an unavailable or unauthorized source")
    for source in sources:
        if source.startswith("artifact:"):
            _, source_kind, source_id = source.split(":", 2)
            _get(root, source_kind, source_id, context=context, owner=False, stack=stack, cache=cache, budget=budget)
    filename = _blob(root, kind, row["digest"])
    try:
        with open(filename, "rb") as stream:
            raw = stream.read(MAX_BLOB + 2)
    except FileNotFoundError:
        raise ValueError("UniMem artifact content was erased or archived") from None
    if len(raw) > MAX_BLOB + 1:
        raise ValueError("UniMem artifact exceeds its size limit")
    budget[0] += len(raw)
    fp.count(budget[0], 0, MAX_TOTAL, "UniMem artifact source bytes")
    value = json.loads(raw)
    if not isinstance(value, dict) or fp.digest(value) != row["digest"]:
        raise ValueError("UniMem artifact content does not match its signed commitment")
    stack.remove(key)
    cache[key] = value
    return value


def list_objects(root: str, kind: str, *, context: Sequence[str] = (), owner: bool = False) -> list[dict[str, Any]]:
    if owner:
        operator()
    result = []
    for identity, row in sorted(latest(root, kind).items()):
        if row.get("status") == "active" and (owner or visible(row["scopes"], context)):
            result.append(get(root, kind, identity, context=context, owner=owner))
    return result


def traces(root: str) -> dict[str, dict[str, Any]]:
    """Load a bounded local trajectory namespace without following external paths."""
    from commontrace import trace_io

    directory = Path(paths.traces_dir(root))
    result = {}
    total = 0
    for index, filename in enumerate(directory.glob("*.md")):
        fp.count(index, 0, 4095, "UniMem source files; use a scoped trajectory namespace")
        if filename.name == "README.md":
            continue
        paths.enforce_boundary(root, str(filename))
        size = filename.stat().st_size
        if size > MAX_BLOB:
            raise ValueError("UniMem source trace exceeds 256 KiB")
        total += size
        fp.count(total, 0, 16 * fp.MAX_BYTES, "UniMem source bytes")
        trace, _ = trace_io.read(str(filename))
        if isinstance(trace.get("id"), str):
            result[trace["id"]] = trace
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
        if (memory_control.matches(fm.get("scopes", []), list(context))
                and lesson_cache.fresh_eligible(path, fm, identity, body=body, root=root)
                and not memory_authority.lineage_blocked(root, identity)):
            result[identity] = memory_ifc.label(root, {"id": identity, "text": body,
                "revision": lesson_io.current_revision(path), "scopes": fm.get("scopes", [])})
    runtime_ids = [identity for identity in ids if identity.startswith(("record:", "capture:"))]
    if runtime_ids and os.path.isfile(os.path.join(paths.memory_dir(root), "runtime.sqlite3")):
        from commontrace.memory_journal import Journal
        from commontrace.record_visibility import scope_from_labels

        journal = Journal(root)
        for identity in runtime_ids:
            row = journal.admit_reference(identity, scope=scope_from_labels(list(context)))
            if row is not None:
                result[identity] = memory_ifc.label(root, row)
    remaining = set(ids) - set(result) - existing - set(runtime_ids)
    if remaining:
        from commontrace import ttl

        for identity, trace in traces(root).items():
            if identity not in remaining:
                continue
            record = memory_authority.trace_record(trace)
            receipt = trace.get("extensions", {}).get("profile", {}).get("origin", {})
            if (memory_control.matches(trace.get("scopes", []), list(context)) and ttl.trace_is_live(trace)
                    and memory_authority.permits_record(root, receipt, record, action_class=action_class)):
                result[identity] = memory_ifc.label(root, {"id": identity, "text": trace.get("solution_text", ""),
                    "content_digest": fp.digest(record), "scopes": trace.get("scopes", [])})
    return result


def erase(root: str, identities: Sequence[str]) -> list[dict[str, Any]]:
    """Remove managed descendants' blobs; retain signed hash tombstones.

    Shared blobs are retained when an unrelated live artifact still references
    them. The caller handles legal holds, hazard checks and external replicas.
    """
    operator()
    return _erase_sources(root, identities)


def _erase_sources(root: str, identities: Sequence[str]) -> list[dict[str, Any]]:
    """Internal cascade after the canonical erasure boundary authorizes sources.

    Scoped agents may already erase their own canonical records. This helper
    must not turn that established operation into an operator-only operation;
    authorization belongs to its canonical caller, before source removal.
    """
    fp.count(len(identities), 1, 2048)
    deleted = {fp.text(i) for i in identities}
    directory = Path(paths.memory_dir(root))
    files = sorted(directory.glob("unimem-*.jsonl"))
    fp.count(len(files), 0, 128)
    snapshots = {filename: latest(root, filename.name[len("unimem-"):-len(".jsonl")]) for filename in files}
    for _ in range(16):
        affected_ids: set[str] = set()
        for path, objects in snapshots.items():
            kind = path.name[len("unimem-"):-len(".jsonl")]
            for identity, row in objects.items():
                qualified = reference(kind, identity)
                if identity in deleted or qualified in deleted or set(row.get("sources", [])) & deleted:
                    affected_ids.update((identity, qualified))
        if affected_ids <= deleted:
            break
        deleted.update(affected_ids)
        fp.count(len(deleted), 1, 2048, "UniMem erasure descendants")
    else:
        raise ValueError("UniMem erasure exceeds sixteen derivation levels")
    from commontrace import incident, safety

    incident.assert_unpinned(root, list(deleted))
    safety.assert_mutable(root, list(deleted))
    removed = []
    for filename in files:
        kind = filename.name[len("unimem-"):-len(".jsonl")]
        with _jsonl.locked(str(filename)):
            objects = latest(root, kind)
            affected = {identity for identity, row in objects.items()
                        if identity in deleted or reference(kind, identity) in deleted
                        or set(row.get("sources", [])) & deleted}
            live_digests = {row.get("digest") for identity, row in objects.items()
                            if identity not in affected and row.get("status") == "active"}
            if kind == "rl-captures":
                trace_ids = {i for i in affected if objects[i].get("status") != "erased"}
                # Revoke every trace before removing provenance edges, so
                # removing those edges cannot make a revoked derivative live.
                for identity in sorted(trace_ids):
                    memory_authority.record_forgetting(root, identity, forgotten=True)
                origins = os.path.join(paths.memory_dir(root), "origins.jsonl")
                with _jsonl.locked(origins):
                    _jsonl.write_rows(origins, [r for r in _jsonl.read_rows(origins)
                                              if r.get("record", {}).get("id") not in trace_ids])
            for identity in sorted(affected):
                row = objects[identity]
                if row.get("status") == "erased":
                    continue
                versions = {str(e["digest"]) for e in events(root, kind) if e["id"] == identity and e.get("digest")}
                cold_tiers: list[str] = []
                if re.fullmatch(r"tier-[a-f0-9]{32}", kind):
                    from commontrace import tiering

                    cold_tiers = tiering.purge_cold(root, kind, sorted(versions - live_digests))
                for digest in versions - live_digests:
                    try:
                        os.unlink(_blob(root, kind, str(digest)))
                    except FileNotFoundError:
                        pass
                if kind == "skill-exports":
                    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identity) is None or len(identity) > 64:
                        raise ValueError("invalid managed UniMem skill folder")
                    skills_dir = os.path.join(root, "skills")
                    for name in ("SKILL.md", "EVIDENCE.json"):
                        target = os.path.join(skills_dir, identity, name)
                        paths.enforce_boundary(skills_dir, target)
                        try:
                            os.unlink(target)
                        except FileNotFoundError:
                            pass
                if kind == "rl-captures":
                    target = os.path.join(root, row["filename"])
                    paths.enforce_boundary(paths.traces_dir(root), target)
                    try:
                        os.unlink(target)
                    except FileNotFoundError:
                        pass
                event(root, kind, identity, {"status": "erased", "digest": row.get("digest"),
                                            "scopes": row.get("scopes", []), "sources": row.get("sources", [])})
                removed.append({"kind": kind, "id": identity, "content_digests": sorted(versions),
                                **({"cold_tiers": cold_tiers} if cold_tiers else {})})
    affected_kinds = {item["kind"] for item in removed}
    for kind, hook in list(_ERASURE_HOOKS.items()):
        if kind in affected_kinds:
            hook(root, deleted)
    # Model KV is an acceleration copy of governed source memory. Evict it in
    # every registered pool and carry the tier receipt into the erasure report.
    from commontrace import kv_capsules

    for receipt in kv_capsules.evict_sources(root, sorted(deleted)):
        removed.append({"kind": "kv-capsules", "id": receipt["memory_id"],
                        "capsule_tiers": receipt["capsule_tiers"],
                        "removed_tiers": receipt["removed_tiers"],
                        "failed_tiers": receipt["failed_tiers"]})
    return removed
