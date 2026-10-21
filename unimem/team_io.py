"""UniMem opt-in space policy, read snapshots and durably fenced team writes.

A single inter-process lock orders policy/lease changes before canonical writer
transactions. Read versions are checked inside those transactions. The lock is
not distributed: a space must have a single shared local store and lock domain.
Raw file edits by the store operator are outside this concurrency protocol.
"""
from __future__ import annotations

import contextlib
import functools
import hmac
import json
import os
import secrets
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any

from unimem import _jsonl, memory_authority, paths
from unimem import frontier_protocol as fp
from unimem import team_isolation as pure

_ERASURE: ContextVar[tuple[str, frozenset[str]] | None] = ContextVar("unimem_team_erasure", default=None)


def _path(root: str, name: str = "team-isolation.json") -> str:
    filename = os.path.join(paths.memory_dir(root), name)
    paths.enforce_boundary(root, filename)
    if os.path.islink(filename):
        raise ValueError("UniMem team paths cannot be symbolic links")
    return filename


def config(root: str) -> dict[str, Any]:
    try:
        with open(_path(root), "rb") as stream:
            raw = stream.read(128 * 1024 + 1)
    except FileNotFoundError:
        return {}
    if len(raw) > 128 * 1024:
        raise ValueError("UniMem team policy exceeds its bound")
    value = json.loads(raw)
    if (not fp.verify(root, value) or value["record"].get("type") != "unimem.team-policy"
            or value["record"].get("version") != 1):
        raise PermissionError("UniMem team policy signature is invalid")
    return dict(value["record"]["spaces"])


def isolation(root: str, *, space: str = "default", level: str | None = None,
              scopes: Sequence[str] = ()) -> dict[str, Any]:
    fp.text(space, "space", 128)
    if level is None:
        return {"space": space, **config(root).get(space, {"level": "none", "scopes": []})}
    from unimem import nextwave_store as store

    store.operator()
    if level not in pure.LEVELS:
        raise ValueError("UniMem isolation must be none, snapshot, serializable or sequenced")
    fp.count(len(scopes), 0, 64)
    labels = sorted({fp.text(label, "scope", 256) for label in scopes})
    with _jsonl.locked(_path(root, ".team-isolation")):
        spaces = config(root)
        if space not in spaces and len(spaces) >= 64:
            raise ValueError("UniMem at most 64 team spaces are supported")
        spaces[space] = {"level": level, "scopes": labels}
        _jsonl.write_json(_path(root), fp.sign(root, {"type": "unimem.team-policy", "version": 1, "spaces": spaces}))
    return {"space": space, **spaces[space]}


def _space(root: str, space: str, context: Sequence[str]) -> dict[str, Any]:
    spaces = config(root)
    if spaces and space not in spaces:
        raise PermissionError("UniMem unknown team space")
    settings = {"space": space, **spaces.get(space, {"level": "none", "scopes": []})}
    if not set(settings["scopes"]) <= set(context):
        raise PermissionError("UniMem team space is outside the caller's scope")
    return settings


@contextlib.contextmanager
def request(*, read_set: Any = None, space: str = "default", context: Sequence[str] = (),
            work_item: str = "", fencing_token: int | None = None,
            lease_id: str = "", merge: bool = False) -> Iterator[None]:
    fp.text(space, "space", 128)
    if type(merge) is not bool:
        raise ValueError("UniMem merge flag must be boolean")
    if not isinstance(context, (list, tuple)):
        raise ValueError("UniMem team context must be a list of scope labels")
    fp.count(len(context), 0, 128)
    labels = [fp.text(label, "scope", 256) for label in context]
    if fencing_token is not None:
        fp.count(fencing_token, 1, 2**53 - 1, "fencing token")
    if work_item:
        fp.text(work_item, "work item", 128)
    if lease_id:
        fp.text(lease_id, "lease ID", 128)
    token = pure.REQUEST.set({"read_set": None if read_set is None else pure.read_set(read_set),
                             "space": space, "context": labels, "work_item": work_item,
                             "fencing_token": fencing_token, "lease_id": lease_id, "merge": merge})
    try:
        yield
    finally:
        pure.REQUEST.reset(token)


def _leases(root: str) -> sqlite3.Connection:
    filename = _path(root, "team-leases.sqlite3")
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    connection = sqlite3.connect(filename, timeout=30, isolation_level=None)
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("CREATE TABLE IF NOT EXISTS leases (space TEXT, item TEXT, principal TEXT NOT NULL, "
                       "lease_id TEXT NOT NULL, fence INTEGER NOT NULL, expires TEXT NOT NULL, "
                       "PRIMARY KEY(space,item))")
    return connection


def lease(root: str, *, work_item: str, space: str = "default", context: Sequence[str] = (),
          seconds: int = 60, release: bool = False, lease_id: str = "",
          fencing_token: int | None = None) -> dict[str, Any]:
    fp.text(work_item, "work item", 128)
    fp.count(seconds, 1, 3600, "lease seconds")
    if type(release) is not bool:
        raise ValueError("UniMem lease release must be boolean")
    principal = memory_authority.bound_writer()[0]
    with _jsonl.locked(_path(root, ".team-isolation")):
        _space(root, space, context)
        with contextlib.closing(_leases(root)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT principal,lease_id,fence,expires FROM leases WHERE space=? AND item=?",
                                     (space, work_item)).fetchone()
            stamp = datetime.now(timezone.utc)
            if release:
                if (row is None or row[0] != principal or row[2] != fencing_token
                        or not hmac.compare_digest(row[1], lease_id)):
                    raise pure.StaleGeneration(reason="stale_fencing_token")
                connection.execute("UPDATE leases SET expires=? WHERE space=? AND item=?",
                                   (stamp.isoformat(), space, work_item))
                connection.commit()
                return {"released": True, "fencing_token": row[2]}
            if row and datetime.fromisoformat(row[3]) > stamp:
                raise pure.StaleGeneration(reason="work_item_leased")
            if row is None and connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0] >= 10000:
                raise ValueError("UniMem lease work-item bound reached")
            fence = row[2] + 1 if row else 1
            fp.count(fence, 1, 2**53 - 1, "fencing token")
            identity = secrets.token_urlsafe(24)
            expires = (stamp + timedelta(seconds=seconds)).isoformat()
            connection.execute("INSERT INTO leases VALUES(?,?,?,?,?,?) ON CONFLICT(space,item) DO UPDATE SET "
                               "principal=excluded.principal,lease_id=excluded.lease_id,fence=excluded.fence,"
                               "expires=excluded.expires", (space, work_item, principal, identity, fence, expires))
            connection.commit()
            return {"space": space, "work_item": work_item, "lease_id": identity,
                    "fencing_token": fence, "expires_at": expires, "principal": principal}


def _fence(root: str, settings: dict[str, Any], value: dict[str, Any]) -> None:
    if settings["level"] != "sequenced":
        return
    if not value["work_item"]:
        raise pure.StaleGeneration(reason="lease_required")
    with contextlib.closing(_leases(root)) as connection:
        row = connection.execute("SELECT principal,lease_id,fence,expires FROM leases WHERE space=? AND item=?",
                                 (value["space"], value["work_item"])).fetchone()
    if (row is None or row[0] != memory_authority.bound_writer()[0]
            or row[2] != value["fencing_token"] or not hmac.compare_digest(row[1], value["lease_id"])
            or datetime.fromisoformat(row[3]) <= datetime.now(timezone.utc)):
        raise pure.StaleGeneration(reason="stale_fencing_token")


def _visible(root: str, facts: Mapping[str, Any], context: Sequence[str],
             identities: Sequence[str]) -> dict[str, dict[str, Any]]:
    from unimem.fact_evidence import EvidenceResolver
    from unimem.search_recipes import _eligible_fact

    resolver = EvidenceResolver(root, facts)
    result = {}
    eligible: Callable[..., bool] = _eligible_fact
    for identity in identities:
        fact = facts.get(identity)
        if fact is not None and eligible(root, fact, datetime.now(timezone.utc), None, list(context), "", resolver):
            result[identity] = fact.to_dict()
    return result


def fetch(root: str, *, identities: Sequence[str], space: str = "default",
          context: Sequence[str] = ()) -> dict[str, Any]:
    from unimem import fact_store, hierarchical

    if not isinstance(identities, (list, tuple)):
        raise ValueError("UniMem team identities must be a list of fact IDs")
    fp.count(len(identities), 0, pure.MAX_READS)
    ids = [fp.text(identity, "fact ID", 256) for identity in identities]
    with _jsonl.locked(_path(root, ".team-isolation")):
        settings = _space(root, space, context)
        if fact_store.enabled(root):
            with fact_store.transaction(root) as facts:
                rows = _visible(root, facts, context, ids)
        else:
            with _jsonl.locked(hierarchical._facts_file(root)):
                facts = hierarchical.load_facts(root)
                rows = _visible(root, facts, context, ids)
    if any(i not in rows or not set(settings["scopes"]) <= set(rows[i].get("scopes", [])) for i in ids):
        raise PermissionError("UniMem fact is unavailable in this team space")
    return {"space": space, "level": settings["level"], "facts": [rows[i] for i in ids],
            "read_set": [{"id": i, "transaction_time": pure.transaction_time(rows[i])} for i in ids]}


@contextlib.contextmanager
def _checked(root: str, facts: Any, settings: dict[str, Any], value: dict[str, Any]) -> Iterator[None]:
    from unimem import memory_control

    declared = value["read_set"]
    if settings["level"] != "none" and declared is None:
        raise pure.StaleGeneration(reason="read_set_required")
    declared = declared or {}
    # Canonical versions are inspected only under the canonical writer lock.
    # Validate visibility BEFORE computing or returning stale fact bodies.
    admitted = _visible(root, facts, value["context"], list(declared))
    current = {i: pure.transaction_time(row) for i, row in admitted.items()}
    changed = pure.stale(declared, current)
    if settings["level"] in ("serializable", "sequenced") and changed:
        successors = []
        # Superseded sources are not themselves admitted. Follow at most one
        # canonical supersession edge, and admit the target independently.
        for identity in changed:
            if identity in admitted:
                successors.append(identity)
            old = facts.get(identity)
            if old is not None and set(old.scopes) <= set(value["context"]):
                target = getattr(old, "superseded_by", "")
                if target:
                    successors.append(target)
        details = list(_visible(root, facts, value["context"], successors[:pure.MAX_READS]).values())
        raise pure.StaleGeneration(details)

    _fence(root, settings, value)
    value["level"] = settings["level"]
    value["read_version"] = fp.digest(sorted(declared.items()))
    value["validated_at"] = fp.now()
    fp.count(len(facts), 0, 10000, "team fact count")
    before = {i: pure.transaction_time(f.to_dict()) for i, f in facts.items()}
    prior_scopes = {i: list(f.scopes) for i, f in facts.items()}
    yield
    changed_ids = [i for i, f in facts.items() if before.get(i) != pure.transaction_time(f.to_dict())]
    changed_ids += [i for i in before if i not in facts]
    if any(i in facts and not memory_control.matches(facts[i].scopes, value["context"])
           for i in changed_ids):
        raise PermissionError("UniMem team write is outside the caller's scope")
    if any(not memory_control.matches(prior_scopes[i], value["context"])
           for i in changed_ids if i in prior_scopes):
        raise PermissionError("UniMem team write cannot alter an unavailable source")
    if any(i in facts and not set(settings["scopes"]) <= set(facts[i].scopes) for i in changed_ids):
        raise PermissionError("UniMem team write cannot escape its space")
    if settings["level"] == "snapshot" and any(
            i in before and (i not in declared or i in changed) for i in changed_ids):
        raise pure.StaleGeneration(reason="snapshot_write_conflict")
    # Lease expiry during generation/commit fails too; lease acquisition is
    # serialized on this same lock, and cannot advance the fence underneath us.
    _fence(root, settings, value)


def transactions(factory: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(factory)
    @contextlib.contextmanager
    def wrapped(root: str, **kwargs: Any) -> Iterator[Any]:
        if not kwargs.get("write", factory.__name__ == "_mutate_legacy_facts"):
            with factory(root, **kwargs) as facts:
                yield facts
            return
        from unimem import incident

        # Privacy erasure holds the incident/legal-hold fence before touching
        # canonical facts. All writers use that same order to avoid a writer
        # waiting on incident metadata while a purge waits on the team fence.
        with incident.protect(root), _jsonl.locked(_path(root, ".team-isolation")):
            if not os.path.isfile(_path(root)):
                with factory(root, **kwargs) as facts:
                    yield facts
                return
            erasure = _ERASURE.get()
            if erasure and erasure[0] == os.path.realpath(root):
                with factory(root, **kwargs) as facts:
                    before = {i: pure.transaction_time(f.to_dict()) for i, f in facts.items()}
                    yield facts
                    if (any(i not in before or before[i] != pure.transaction_time(f.to_dict())
                            for i, f in facts.items()) or not (set(before) - set(facts)) <= erasure[1]):
                        raise PermissionError("UniMem privacy purge may only remove its authorized facts")
                return
            value = pure.REQUEST.get()
            if value is None:
                if all(settings["level"] == "none" for settings in config(root).values()):
                    with factory(root, **kwargs) as facts:
                        yield facts
                    return
                # An active isolation policy cannot be bypassed by omitting
                # the declared space. Returning every space to none restores
                # the legacy trusted local writer API.
                raise pure.StaleGeneration(reason="team_request_required")
            settings = _space(root, value["space"], value["context"])
            with factory(root, **kwargs) as facts:
                with _checked(root, facts, settings, value):
                    yield facts
    return wrapped


def contradictions(root: str | None, facts: Any, index: Any, statement: str,
                   scopes: list[str]) -> list[Any]:
    """Inspect contradiction slots before any near/semantic dedup decision."""
    if root is None or pure.REQUEST.get() is None:
        return []
    from unimem import hierarchical as h

    probe = h.AtomicFact(id="team-probe-" + secrets.token_hex(12), statement=statement,
                         category="general", scopes=scopes, confidence=.8, confirmations=1,
                         valid_from=fp.now(), valid_until=None)
    found: list[Any] = []
    h._detect_conflicts(facts, index, probe, root, found, may_supersede=False)
    value = pure.REQUEST.get()
    if value and value["merge"]:
        from unimem import fact_conflicts

        declared = value["read_set"]
        if not declared:
            raise ValueError("UniMem semantic merge requires a fresh nonempty read_set")
        present = {conflict.older for conflict in found}
        for identity in declared:
            source = facts.get(identity)
            if source is None or not set(source.scopes) <= set(scopes):
                raise PermissionError("UniMem merge cannot reduce its source scopes")
            if source.statement != statement and identity not in present:
                found.append(fact_conflicts.Conflict(identity, probe.id, "team-merge", source.statement,
                             statement, fp.now(), "flagged", "Semantic merge proposal requires review"))
    return found


def resolved_conflicts(found: list[Any], identity: str) -> list[Any]:
    from dataclasses import replace

    return [replace(conflict, newer=identity, action="flagged") for conflict in found
            if conflict.older != identity]


def gateway_request(root: str, body: dict[str, Any], context: Sequence[str]) -> Any:
    fields = {"read_set", "team_space", "work_item", "fencing_token", "lease_id", "merge"}
    if not fields.intersection(body) and not os.path.isfile(_path(root)):
        return contextlib.nullcontext()
    return request(read_set=body.get("read_set"), space=body.get("team_space", "default"),
                   context=context, work_item=body.get("work_item", ""),
                   fencing_token=body.get("fencing_token"), lease_id=body.get("lease_id", ""),
                   merge=body.get("merge", False))


@contextlib.contextmanager
def erasure(root: str, identities: Sequence[str]) -> Iterator[None]:
    """Only an undelegated owner may bypass stale generation for byte erasure.

    The canonical forgetting authority must already be committed for every ID.
    This context cannot modify or add facts, or remove an unrelated fact.
    """
    if not os.path.isfile(_path(root)):
        yield
        return
    from unimem import nextwave_store as store

    store.operator()
    fp.count(len(identities), 1, 1024, "team privacy purge size")
    if not all(memory_authority.lineage_blocked(root, identity, canonical_only=True) for identity in identities):
        raise PermissionError("UniMem privacy purge requires committed canonical forgetting authority")
    token = _ERASURE.set((os.path.realpath(root), frozenset(identities)))
    try:
        yield
    finally:
        _ERASURE.reset(token)
