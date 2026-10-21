"""Signed op-based site mesh using an observed-remove set and multi-value registers."""
from __future__ import annotations

import base64
import json
from collections import defaultdict
from typing import Any, cast

from unimem import frontier_protocol as fp

ROBOTS = "mesh-robots.jsonl"
REVOKED = "mesh-revocations.jsonl"
OPS = "mesh-ops.jsonl"
TAINTS = "mesh-taints.jsonl"


def _ed25519() -> Any:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise RuntimeError("install unimem[crdt] for signed robot mesh operations") from None
    return Ed25519PublicKey


def register_robot(root: str, robot_id: str, public_key: str) -> dict[str, Any]:
    from unimem import nextwave_store

    nextwave_store.operator()
    robot_id = fp.text(robot_id, "robot id", 128)
    try:
        key = bytes.fromhex(public_key)
    except ValueError:
        raise ValueError("robot Ed25519 key must be hex") from None
    if len(key) != 32:
        raise ValueError("robot Ed25519 public key must be 32 bytes")
    _ed25519().from_public_bytes(key)
    body = {"robot_id": robot_id, "public_key": public_key, "key_digest": fp.digest(public_key)}
    prior = [row for row in fp.journal(root, ROBOTS) if row["robot_id"] == robot_id]
    if prior and prior[-1] != body:
        raise ValueError("robot key registration is immutable; rotate under a new robot identity")
    if prior:
        return prior[-1]
    return cast(dict[str, Any], fp.append(root, ROBOTS, body)["record"])


def revoke_robot(root: str, robot_id: str, *, reason: str) -> dict[str, Any]:
    from unimem import nextwave_store

    nextwave_store.operator()
    robot_id, reason = fp.text(robot_id, "robot id", 128), fp.text(reason, "revocation reason", 256)
    if not any(row["robot_id"] == robot_id for row in fp.journal(root, ROBOTS)):
        raise KeyError("cannot revoke an unregistered mesh robot")
    if any(row["robot_id"] == robot_id for row in fp.journal(root, REVOKED)):
        return next(row for row in fp.journal(root, REVOKED) if row["robot_id"] == robot_id)
    return cast(dict[str, Any], fp.append(root, REVOKED,
                                          {"robot_id": robot_id, "reason": reason, "at": fp.now()})["record"])


def sign_operation(robot_id: str, private_key: str, *, counter: int, operation: str,
                    entity: str, value: Any = None, field: str = "value",
                    parents: list[str] | None = None, targets: list[str] | None = None,
                    safety: bool = False) -> dict[str, Any]:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        raise RuntimeError("install unimem[crdt] for signed robot mesh operations") from None
    try:
        secret = bytes.fromhex(private_key)
    except ValueError:
        raise ValueError("robot Ed25519 private key must be hex") from None
    if len(secret) != 32:
        raise ValueError("robot Ed25519 private key must be 32 bytes")
    if type(safety) is not bool:
        raise ValueError("mesh safety marker must be boolean")
    if operation not in {"add", "remove", "put"}:
        raise ValueError("mesh operation must be add, remove or put")
    body = {"robot_id": fp.text(robot_id, "robot id", 128),
            "counter": fp.count(counter, 1, 2**63-1), "operation": operation,
            "entity": fp.text(entity, "site entity", 128), "field": fp.text(field, "register field", 128),
            "parents": sorted(set(fp.text(item, "parent operation", 64) for item in (parents or []))),
            "safety": bool(safety)}
    if operation in {"add", "put"}:
        fp.wire(value)
        body["value"] = value
    else:
        body["targets"] = sorted(set(fp.text(item, "OR-set tag", 64) for item in (targets or [])))
        if not body["targets"]:
            raise ValueError("observed-remove requires explicit add tags")
    operation_id = fp.digest(body)
    signed_body = {**body, "id": operation_id}
    signature = Ed25519PrivateKey.from_private_bytes(secret).sign(fp.wire(signed_body))
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    public = Ed25519PrivateKey.from_private_bytes(secret).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {"body": signed_body, "signature": base64.b64encode(signature).decode(),
            "public_key_digest": fp.digest(public.hex())}


def _robot_keys(root: str) -> dict[str, str]:
    return {row["robot_id"]: row["public_key"] for row in fp.journal(root, ROBOTS)}


def _verify(root: str, signed: dict[str, Any]) -> dict[str, Any]:
    body = signed.get("body")
    if not isinstance(body, dict) or set(signed) != {"body", "signature", "public_key_digest"}:
        raise ValueError("malformed signed mesh operation")
    robot = body.get("robot_id")
    keys = _robot_keys(root)
    if robot not in keys or fp.digest(keys[robot]) != signed["public_key_digest"]:
        fp.append(root, TAINTS, {"robot_id": str(robot), "operation_id": body.get("id", ""),
                                 "reason": "unknown or mismatched robot key"})
        raise PermissionError("mesh operation key is unknown; operation was tainted")
    if any(row["robot_id"] == robot for row in fp.journal(root, REVOKED)):
        fp.append(root, TAINTS, {"robot_id": robot, "operation_id": body.get("id", ""),
                                 "reason": "revoked robot operation"})
        raise PermissionError("revoked robot operation was rejected and tainted")
    try:
        signature = base64.b64decode(signed["signature"], validate=True)
        _ed25519().from_public_bytes(bytes.fromhex(keys[robot])).verify(signature, fp.wire(body))
    except Exception:
        fp.append(root, TAINTS, {"robot_id": robot, "operation_id": body.get("id", ""),
                                 "reason": "invalid operation signature"})
        raise PermissionError("forged mesh operation was rejected and tainted") from None
    expected = fp.digest({key: value for key, value in body.items() if key != "id"})
    if body.get("id") != expected:
        raise ValueError("mesh operation digest does not match its signed body")
    return body


def apply(root: str, signed_ops: list[dict[str, Any]]) -> dict[str, Any]:
    fp.count(len(signed_ops), 1, 100_000)
    accepted = []
    prior = {row["id"] for row in fp.journal(root, OPS)}
    for signed in signed_ops:
        body = _verify(root, signed)
        if body["id"] in prior:
            continue
        row = fp.append(root, OPS, {**body, "signature": signed["signature"],
                                    "public_key_digest": signed["public_key_digest"]})["record"]
        prior.add(body["id"])
        accepted.append(row["id"])
    return {"accepted": accepted, "count": len(accepted)}


def merge_views(operations: list[dict[str, Any]], *, revoked: set[str] | None = None) -> dict[str, Any]:
    """Pure deterministic merge: conflicts are retained, never last-writer-wins."""
    revoked = revoked or set()
    live = [dict(op) for op in operations if op["robot_id"] not in revoked]
    adds = {op["id"]: op for op in live if op["operation"] == "add"}
    removed = {tag for op in live if op["operation"] == "remove" for tag in op["targets"]}
    entities: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for tag, op in adds.items():
        if tag not in removed or op.get("safety"):
            entities[op["entity"]].append({"tag": tag, "value": op.get("value"), "safety": op.get("safety", False),
                                           "robot_id": op["robot_id"], "counter": op["counter"]})
    registers: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for op in live:
        if op["operation"] == "put":
            registers[(op["entity"], op["field"])].append(op)
    conflicts = []
    register_values = {}
    op_by_id = {row["id"]: row for row in live}

    def ancestors(identity: str) -> set[str]:
        result, pending = set(), list(op_by_id.get(identity, {}).get("parents", []))
        while pending:
            parent = pending.pop()
            if parent in result:
                continue
            result.add(parent)
            pending.extend(op_by_id.get(parent, {}).get("parents", []))
        return result

    ancestor_cache = {row["id"]: ancestors(row["id"]) for row in live}
    for (entity, field), rows in sorted(registers.items()):
        values = {fp.digest(row["value"]) for row in rows}
        if len(values) <= 1:
            register_values[f"{entity}/{field}"] = rows[0]["value"]
            continue
        maximal = []
        for row in rows:
            superseded = any(row["id"] in ancestor_cache[other["id"]] for other in rows if other is not row)
            if not superseded:
                maximal.append(row)
        maximal_values = {fp.digest(row["value"]) for row in maximal}
        if len(maximal_values) == 1:
            register_values[f"{entity}/{field}"] = maximal[0]["value"]
        else:
            conflicts.append({"entity": entity, "field": field,
                              "values": sorted(((row["robot_id"], row["value"]) for row in maximal),
                                               key=lambda item: (item[0], fp.digest(item[1])))})
    return {"entities": {key: sorted(value, key=lambda item: item["tag"])
                         for key, value in sorted(entities.items())},
            "registers": register_values, "contradictions": conflicts,
            "active_operations": len(live), "kind": "observed-remove set and multi-value registers"}


def merge(root: str) -> dict[str, Any]:
    revoked = {row["robot_id"] for row in fp.journal(root, REVOKED)}
    ops = fp.journal(root, OPS)
    tainted = {row.get("operation_id") for row in fp.journal(root, TAINTS)}
    for row in ops:
        if row["robot_id"] in revoked and row["id"] not in tainted:
            fp.append(root, TAINTS, {"robot_id": row["robot_id"], "operation_id": row["id"],
                                     "reason": "historical operation invalidated by robot revocation"})
    # Revalidate source signatures and remove every op from newly revoked peers.
    accepted = [row for row in ops if row["robot_id"] not in revoked]
    try:
        from importlib import import_module

        automerge = import_module("automerge.core")
    except ImportError:
        view = merge_views(accepted, revoked=revoked)
        view["automerge_used"] = False
    else:
        ROOT, Document, ObjType, ScalarType = (automerge.ROOT, automerge.Document,
                                               automerge.ObjType, automerge.ScalarType)
        # Each robot's signed operation stream is a list in an Automerge
        # document. The CRDT library unions offline changes; our reducer then
        # enforces UniMem's safety and semantic-conflict policies.
        by_robot: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in accepted:
            by_robot[row["robot_id"]].append({"body": {key: value for key, value in row.items()
                if key not in {"signature", "public_key_digest", "sequence", "previous", "recorded_at"}},
                "signature": row["signature"],
                "public_key_digest": row["public_key_digest"]})
        documents = []
        for robot_id, rows in sorted(by_robot.items()):
            document = Document()
            with document.transaction() as transaction:
                sequence = transaction.put_object(ROOT, "operations", ObjType.List)
                for index, row in enumerate(sorted(rows, key=lambda item: item["body"]["id"])):
                    transaction.insert(sequence, index, ScalarType.Str, fp.wire(row).decode())
            documents.append(document)
        if not documents:
            view = merge_views([], revoked=revoked)
        else:
            merged = documents[0]
            for document in documents[1:]:
                merged.merge(document)
            sequence_entry = merged.get(ROOT, "operations")
            if sequence_entry is None:
                raise ValueError("Automerge merged operation document lost its sequence")
            sequence_id = sequence_entry[1]
            signed_ops = []
            for item in merged.values(sequence_id):
                scalar = item[0]
                if not isinstance(scalar, tuple) or len(scalar) != 2 or not isinstance(scalar[1], str):
                    raise ValueError("Automerge operation entry is not canonical JSON text")
                signed_ops.append(json.loads(scalar[1]))
            verified = [_verify(root, signed) for signed in signed_ops]
            view = merge_views(verified, revoked=revoked)
        view["automerge_used"] = True
    if view["contradictions"]:
        fp.append(root, "mesh-contradictions.jsonl", {"contradictions": view["contradictions"], "at": fp.now()})
    return view
