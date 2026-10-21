"""Consent-scoped household memory and ephemeral bystander handling.

Identity is supplied by the robot perception stack through an injected resolver;
this module never derives identity from biometric data. Unknown identities and
no-memory regions fail closed before any spatial fact or journal write.
"""
from __future__ import annotations

import copy
from typing import Any, Callable

from unimem import _jsonl
from unimem import frontier_protocol as fp
from unimem import transparency_log as log

JOURNAL = "household-consent.jsonl"
GATES = "household-write-gates.jsonl"
ROOMS = "household-rooms.jsonl"
SCOPES = {"resident", "household", "guest", "bystander", "caregiver"}


def _actor(value: str) -> str:
    return fp.text(value, "household person id", 128)


def _valid_signed(root: str, name: str) -> list[dict[str, Any]]:
    count, _, _ = fp.event_snapshot(root, name, 0)
    return [row for index in range(count) if fp.verify(root, row := fp.signed_at(root, name, index))]


def configure_room(root: str, room_id: str, *, no_memory: bool, actor: str) -> dict[str, Any]:
    """Set a signed, monotone no-memory room policy; this interface never relaxes it."""
    room_id, actor = fp.text(room_id, "room id", 128), _actor(actor)
    if type(no_memory) is not bool:
        raise ValueError("no_memory must be a boolean")
    body = {"room_id": room_id, "no_memory": no_memory, "actor": actor}
    with _jsonl.locked(fp.filename(root, ROOMS)):
        rows = _valid_signed(root, ROOMS)
        prior = [r["record"] for r in rows if r["record"].get("room_id") == room_id]
        if prior and prior[-1]["no_memory"] and not no_memory:
            raise PermissionError("no-memory room policy cannot be relaxed through this interface")
        signed = fp.append(root, ROOMS, body)
    log.append(root, "household-room-policy", {"room_id": room_id, "no_memory": no_memory,
                                                "digest": fp.digest(signed)}, receipt=False)
    return copy.deepcopy(signed["record"])


def no_memory(root: str, room_id: str) -> bool:
    rows = _valid_signed(root, ROOMS)
    policies = [row["record"] for row in rows if row["record"].get("room_id") == room_id]
    return bool(policies and policies[-1]["no_memory"])


def consent(root: str, person_id: str, scope: str, *, granted: bool, grantor: str,
            proxy_for: str | None = None, proxy_authority: str | None = None) -> dict[str, Any]:
    """Record consent. Proxy grants require an explicit, independently named authority."""
    person_id, grantor = _actor(person_id), _actor(grantor)
    if scope not in SCOPES - {"bystander"} or type(granted) is not bool:
        raise ValueError("household consent requires a grantable scope and boolean decision")
    if proxy_for is not None:
        proxy_for = _actor(proxy_for)
        if proxy_for != person_id or grantor == person_id or not proxy_authority:
            raise PermissionError("proxy consent requires a distinct trusted proxy and authority reference")
        fp.text(proxy_authority, "proxy authority", 256)
    elif grantor != person_id:
        raise PermissionError("only the person or an explicitly authorized proxy may record consent")
    body = {"person_id": person_id, "scope": scope, "granted": granted, "grantor": grantor,
            "proxy_for": proxy_for, "proxy_authority": proxy_authority}
    signed = fp.append(root, JOURNAL, body)
    log.append(root, "household-consent", {"person_id": person_id, "scope": scope,
                                           "granted": granted, "proxy": proxy_for is not None,
                                           "digest": fp.digest(signed)}, receipt=False)
    return copy.deepcopy(signed["record"])


def scope_for(root: str, person_id: str) -> str | None:
    person_id = _actor(person_id)
    rows = [r["record"] for r in _valid_signed(root, JOURNAL)
            if r["record"].get("person_id") == person_id]
    if not rows or not rows[-1]["granted"]:
        return None
    scope = fp.text(rows[-1]["scope"], "household scope", 32)
    if scope not in SCOPES - {"bystander"}:
        raise ValueError("household consent journal contains an invalid scope")
    return scope


def observe(root: str, scene: dict[str, Any], *, room_id: str,
            identity_resolver: Callable[[dict[str, Any]], list[dict[str, Any]]],
            persist: Callable[[dict[str, Any], list[dict[str, str]], dict[str, Any]], Any]) -> dict[str, Any]:
    """Gate one scene before persistence using robot-stack identity signals.

    Resolver outputs are IDs and confidence/status only; face and voice embeddings
    are rejected. Any unknown or non-consenting person makes the complete scene
    ephemeral so a mixed scene cannot leak an incidental bystander.
    """
    room_id = fp.text(room_id, "room id", 128)
    if no_memory(root, room_id):
        return {"persisted": False, "reason": "no-memory-room"}
    people = identity_resolver(copy.deepcopy(scene))
    if not isinstance(people, list) or len(people) > 64:
        raise ValueError("robot identity resolver must return a bounded person list")
    scopes: list[dict[str, str]] = []
    for item in people:
        if not isinstance(item, dict) or set(item) - {"person_id", "status"}:
            raise ValueError("identity resolver may return only person_id and status; biometric data is forbidden")
        if item.get("status") != "recognized" or not item.get("person_id"):
            return {"persisted": False, "reason": "unknown-person-ephemeral"}
        person_id = _actor(item["person_id"])
        scope = scope_for(root, person_id)
        if scope is None:
            return {"persisted": False, "reason": "consent-required"}
        scopes.append({"person_id": person_id, "scope": scope})
    gate_body = {"room_id": room_id, "scene_digest": fp.digest(scene), "scopes": scopes}
    gate = fp.append(root, GATES, gate_body)
    result = persist(copy.deepcopy(scene), scopes, copy.deepcopy(gate))
    return {"persisted": True, "result": result, "scopes": scopes}


def assert_spatial_persistable(root: str, delta: dict[str, Any], context: dict[str, str]) -> None:
    room_id = delta.get("room_id") or context.get("room")
    if room_id and no_memory(root, room_id):
        raise PermissionError("no-memory room perception cannot be persisted")
    # Person observations must flow through observe(), which obtains identity from
    # the robot stack and proves consent before calling the persistence sink.
    if delta.get("contains_person") is True:
        gate = delta.get("household_gate")
        if (not isinstance(gate, dict) or not fp.verify(root, gate)
                or gate.get("record", {}).get("scene_digest") != fp.digest({k: v for k, v in delta.items()
                    if k not in {"household_gate", "contains_person", "room_id"}})):
            raise PermissionError("person observation must pass the household identity and consent gate")
        record = gate["record"]
        if room_id and record.get("room_id") != room_id:
            raise PermissionError("household write gate belongs to a different room")
        for entry in record.get("scopes", []):
            if scope_for(root, entry.get("person_id", "")) != entry.get("scope"):
                raise PermissionError("household consent changed after the scene was authorized")


def bridge_scene_handler(root: str, *, room_resolver: Callable[[dict[str, Any]], str],
                         identity_resolver: Callable[[dict[str, Any]], list[dict[str, Any]]],
                         persist: Callable[[dict[str, Any], list[dict[str, str]], dict[str, Any]], Any]
                         ) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Create the robot bridge's persistence callback from trusted stack adapters."""
    def handle(scene: dict[str, Any]) -> dict[str, Any]:
        room = room_resolver(copy.deepcopy(scene))
        return observe(root, scene, room_id=room, identity_resolver=identity_resolver, persist=persist)
    return handle
