"""Non-malleable derivation attestations, inspired by in-toto (USENIX 2019).

Trust is the minimum of authenticated inputs. A summary cannot turn untrusted
data into an instruction. Operator-managed origin identities are the Sybil
boundary; distinct accounts alone are not proof of independent organizations.
This authenticates derivation claims, not model execution or semantic truth.
"""
from __future__ import annotations

import copy
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from commontrace import _jsonl
from commontrace import frontier_protocol as fp

LEVELS = {"unknown": -1, "external": 0, "tool": 1, "agent": 1, "user": 2, "operator": 3, "reviewed": 4}
JOURNAL = "lineage.jsonl"


@dataclass
class _Lineage:
    count: int = 0
    tip: str = ""
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    up: dict[str, set[str]] = field(default_factory=dict)
    down: dict[str, set[str]] = field(default_factory=dict)
    taints: list[dict[str, Any]] = field(default_factory=list)


_STATES: OrderedDict[str, _Lineage] = OrderedDict()
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_STATES.clear)


def _snapshot(root: str) -> _Lineage:
    with fp._GUARD:
        return _snapshot_locked(root)


def _snapshot_locked(root: str) -> _Lineage:
    root = os.path.realpath(root)
    count, tip = fp.checkpoint(root, JOURNAL)
    cached = _STATES.get(root)
    if cached is not None and cached.count == count and cached.tip == tip:
        return cached
    if cached is None or count < cached.count or cached.count and fp.digest(
            fp.signed_at(root, JOURNAL, cached.count - 1)) != cached.tip:
        cached = _Lineage()
    count, tip, events = fp.event_snapshot(root, JOURNAL, cached.count)
    for row in events:
        identity = row["id"]
        if row["kind"] == "attestation":
            cached.objects[identity] = row
            for parent in row["inputs"]:
                cached.up.setdefault(identity, set()).add(parent["id"])
                cached.down.setdefault(parent["id"], set()).add(identity)
        elif row["kind"] in ("review", "corroboration") and identity in cached.objects \
                and row.get("content_hash") == cached.objects[identity]["content_hash"]:
            cached.objects[identity]["integrity"] = "reviewed"
        elif row["kind"] == "taint":
            cached.taints.append(row)
    cached.count, cached.tip = count, tip
    _STATES[root] = cached
    _STATES.move_to_end(root)
    while len(_STATES) > 8 or sum(state.count * 2048 for state in _STATES.values()) > 64 * fp.MAX_BYTES:
        _STATES.popitem(last=False)
    return cached


def records(root: str) -> dict[str, dict[str, Any]]:
    return copy.deepcopy(_snapshot(root).objects)


def record(root: str, identity: str) -> dict[str, Any]:
    return copy.deepcopy(_snapshot(root).objects.get(identity, {}))


def attest(root: str, record: dict[str, Any], receipt: dict[str, Any], *, inputs: list[str],
           operation: str = "derive", model_id: str = "unspecified", prompt_hash: str = "") -> dict[str, Any]:
    with _jsonl.locked(fp.filename(root, JOURNAL)):
        return _attest(root, record, receipt, inputs=inputs, operation=operation,
                       model_id=model_id, prompt_hash=prompt_hash)


def _attest(root: str, record: dict[str, Any], receipt: dict[str, Any], *, inputs: list[str],
            operation: str, model_id: str, prompt_hash: str) -> dict[str, Any]:
    from commontrace import memory_authority

    if not memory_authority.verify(root, receipt, record):
        raise PermissionError("derivation requires an authenticated content receipt")
    identity = fp.text(record.get("id", ""))
    fp.count(len(inputs), 0, 256, "input count")
    inputs = sorted({fp.text(item) for item in inputs})
    if identity in inputs:
        raise ValueError("self derivation is forbidden")
    prior = _snapshot(root).objects
    if any(identity in ancestors(root, parent) for parent in inputs):
        raise ValueError("derivation cycle")
    before = prior.get(identity)
    parents = [{"id": item, "content_hash": prior.get(item, {}).get("content_hash", "unknown")}
               for item in inputs]
    ranks = [LEVELS.get(receipt.get("authority", "unknown"), -1)]
    ranks.extend(LEVELS[prior.get(item, {}).get("integrity", "unknown")] for item in inputs)
    if before:
        ranks.append(LEVELS[before["integrity"]])
    rank = min(ranks)
    level = next(key for key in ("unknown", "external", "agent", "user", "operator") if LEVELS[key] == rank)
    roots = sorted({source for item in inputs for source in prior.get(item, {}).get("root_origins", [])})
    if not inputs:
        roots = [fp.text(receipt["principal"])]
    statement = {"kind": "attestation", "id": identity, "inputs": parents, "operation": fp.text(operation),
                 "model_id": fp.text(model_id), "prompt_hash": prompt_hash, "created_at": fp.now(),
                 "actor": receipt["principal"],
                 "content_hash": fp.digest({k: v for k, v in record.items() if k != "id"}),
                 "integrity": level, "root_origins": roots}
    scopes = record.get("scopes", [])
    if not isinstance(scopes, list) or len(scopes) > 128:
        raise ValueError("derivation scopes must be a bounded list")
    inherited_scopes = [prior[item]["confidentiality"] for item in inputs if item in prior
                        and isinstance(prior[item].get("confidentiality"), list)]
    if operation == "lesson":
        memory_authority._check_source_scopes(scopes, inherited_scopes)
    statement["confidentiality"] = sorted({fp.text(scope, "scope", 256) for scope in scopes}
                                          | {scope for labels in inherited_scopes for scope in labels}) or "public"
    statement["lineage_digest"] = fp.digest(statement)
    from commontrace import interference

    context = interference.active_context(root)
    if context is not None:
        statement["experiment"], statement["arm"] = context
        statement["lineage_digest"] = fp.digest({k: v for k, v in statement.items() if k != "lineage_digest"})
    fp.append(root, JOURNAL, statement)
    return statement


def walk(root: str, identity: str, *, down: bool = False) -> list[str]:
    fp.text(identity)
    state = _snapshot(root)
    edges = state.down if down else state.up
    seen = {identity}
    pending = [identity]
    for _ in range(2000):
        if not pending:
            return sorted(seen - {identity})
        item = pending.pop()
        for neighbor in edges.get(item, set()) - seen:
            seen.add(neighbor)
            pending.append(neighbor)
    raise ValueError("lineage exceeds 2000 nodes; use a scoped store")


def ancestors(root: str, identity: str) -> list[str]:
    return walk(root, identity)


def descendants(root: str, identity: str) -> list[str]:
    return walk(root, identity, down=True)


def taint(root: str, identity: str, reason: str) -> dict[str, Any]:
    fp.text(identity)
    for event in reversed(_snapshot(root).taints):
        if event["id"] == identity and event["kind"] == "taint" and event["reason"] == reason:
            return dict(event)
    signed = fp.append(root, JOURNAL, {"kind": "taint", "id": identity, "reason": fp.text(reason, maximum=2048)})
    from commontrace import transparency_log

    transparency_log.append(root, "lineage-taint", {"id": identity, "reason": reason})
    return signed


def invalidate(root: str, fact_id: str) -> dict[str, Any]:
    return taint(root, fact_id, "superseded: review all descendants")


def labels(root: str, identity: str) -> dict[str, Any]:
    state = _snapshot(root)
    row = state.objects.get(identity, {})
    family = set(ancestors(root, identity)) | {identity}
    taints = [event for event in state.taints if event["id"] in family]
    level = row.get("integrity", "unknown")
    return {"integrity": "external" if taints else level, "lineage_digest": row.get("lineage_digest", ""),
            "review_required": bool(taints), "taints": [{"id": r["id"], "reason": r["reason"]} for r in taints]}


def explain(root: str, identity: str) -> dict[str, Any]:
    return {"id": identity, **labels(root, identity), "ancestors": ancestors(root, identity),
            "descendants": descendants(root, identity)}


def corroborate(root: str, identity: str, supporters: list[str], *, k: int = 3,
                minimum_weight: float = 0.5) -> dict[str, Any]:
    """Raise integrity only with disjoint authenticated roots and causal reputation.

    Reputation weights are read from the causal gate, never supplied by a model.
    Unknown-reputation origins do not count toward a quorum.
    """
    from commontrace import causal_gate

    fp.count(k, 2, 32, "quorum")
    fp.number(minimum_weight, 0, 1)
    fp.count(len(supporters), 0, 256)
    items = records(root)
    target = items.get(identity)
    if not target:
        raise ValueError("unknown attested object")
    rows = causal_gate.current_rows(root)
    model = causal_gate.fit(rows, causal_gate.sources_for(root, {r.lesson for r in rows}))
    seen: set[str] = set()
    accepted: list[str] = []
    for supporter in sorted(set(supporters)):
        row = items.get(supporter, {})
        roots = set(row.get("root_origins", []))
        # Corroborations attest to identical content, not merely similar text.
        weights = [model.sources[source].p_helps for source in roots if source in model.sources]
        if (row.get("content_hash") == target["content_hash"] and roots and not roots & seen
                and len(weights) == len(roots) and min(weights) >= minimum_weight):
            seen.update(roots)
            accepted.append(supporter)
    if len(accepted) < k:
        raise ValueError("independent-origin reputation quorum not met")
    return fp.append(root, JOURNAL, {"kind": "corroboration", "id": identity, "supporters": accepted,
                                    "root_origins": sorted(seen), "integrity": "reviewed",
                                    "content_hash": target["content_hash"]})


def review(root: str, identity: str, receipt: dict[str, Any]) -> None:
    """Called by the admission issuer after it commits a content-bound review."""
    from commontrace import frontmatter, lesson_admission, lesson_io

    path = lesson_io.lesson_path(root, identity)
    if path is None:
        raise ValueError("review requires an authoritative lesson")
    fm, body = frontmatter.read(path)
    if fm.get("approval_receipt") != receipt or not lesson_admission.eligible(root, path, fm, body):
        raise PermissionError("a current content-bound lesson-admission receipt is required")
    row = records(root).get(identity)
    if row:
        fp.append(root, JOURNAL, {"kind": "review", "id": identity, "receipt": receipt,
                                 "content_hash": row["content_hash"]})
