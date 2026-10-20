"""Purpose admission and operator-recorded consent, with bounded signed state.

DPV 2.1 terms describe purpose and processing. The receipt records an operator's
assertion of consent; it does not prove that a person consented or certify law.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import lru_cache
from typing import Any

from commontrace import _jsonl
from commontrace import frontier_protocol as fp

DPV = "https://w3id.org/dpv#"
PURPOSES = ("AccountManagement", "CustomerCare", "ImproveHealthcare", "PaymentManagement",
            "Personalisation", "ServicePersonalisation", "ServiceProvision")
PROCESSING = ("Collect", "Store", "Retrieve", "Use", "Infer", "Disclose", "Erase")
_REQUEST: ContextVar[str] = ContextVar("unimem_processing_purpose", default="")
_OWNER_VIEW: ContextVar[bool] = ContextVar("unimem_consent_owner_view", default=False)
TYPE = "unimem.consent-state.v1"


def enabled(root: str) -> bool:
    return os.path.isfile(fp.filename(root, "consent-state.json"))


def purposes(root: str) -> dict[str, Any]:
    return {"vocabulary": "https://w3id.org/dpv/2.1", "purposes": [DPV+name for name in PURPOSES],
            "processing": [DPV+name for name in PROCESSING],
            "profile": "bounded DPV subset; exact purposes, no implicit superclass grants"}


def _state(root: str) -> dict[str, Any]:
    path = fp.filename(root, "consent-state.json")
    return _load_state(root, path, fp._stamp(path), fp._pins(root))


@lru_cache(maxsize=8)
def _load_state(root: str, path: str, stamp: Any, pins: Any) -> dict[str, Any]:
    try:
        with open(path, "rb") as stream:
            raw = stream.read(fp.MAX_BYTES+1)
    except FileNotFoundError:
        return {"type": TYPE, "receipts": {}, "resources": {}}
    if len(raw) > fp.MAX_BYTES:
        raise PermissionError("UniMem consent state exceeds its bound")
    signed = json.loads(raw)
    if not fp.verify(root, signed) or signed.get("record", {}).get("type") != TYPE:
        raise PermissionError("UniMem consent state signature is invalid")
    record = signed["record"]
    fp.count(len(record["resources"]), 0, 4096)
    fp.count(len(record["receipts"]), 0, 1024)
    return dict(record)


def _save(root: str, state: dict[str, Any]) -> None:
    signed = fp.sign(root, state)
    fp.wire(signed)
    _jsonl.write_json(fp.filename(root, "consent-state.json"), signed)


def _subject(root: str, subject: str) -> str:
    from commontrace import memory_authority

    key = memory_authority._key(root, create=True)
    if key is None:
        raise PermissionError("UniMem subject pseudonym key is unavailable")
    return "urn:unimem:subject:"+hmac.new(key, b"unimem-consent-subject-v1\x00"+
        fp.text(subject, "subject", 1024).encode(), hashlib.sha256).hexdigest()


def current(root: str) -> str:
    from commontrace import capabilities, capability_io

    delegation = capability_io.current(root) if capabilities.CURRENT.get() is not None else None
    return str(delegation["purpose"]) if delegation else _REQUEST.get()


@contextmanager
def request(root: str, purpose: str | None = None) -> Iterator[None]:
    """Only a verified leaf or trusted operator application selects a purpose."""
    from commontrace import capabilities, memory_authority

    selected = current(root)
    if purpose is not None:
        fp.text(purpose, "processing purpose", 256)
        if capabilities.CURRENT.get() is not None:
            if purpose != selected:
                raise PermissionError("UniMem request purpose differs from its authenticated capability")
        elif memory_authority.bound_writer()[1] != "operator":
            raise PermissionError("UniMem agent purposes require an authenticated capability")
        selected = purpose
    token = _REQUEST.set(selected)
    try:
        yield
    finally:
        _REQUEST.reset(token)


@contextmanager
def _owner_view() -> Iterator[None]:
    from commontrace import nextwave_store

    nextwave_store.operator()
    token = _OWNER_VIEW.set(True)
    try:
        yield
    finally:
        _OWNER_VIEW.reset(token)


def _key(root: str, record: Mapping[str, Any]) -> str:
    identity = record.get("id", "")
    if not isinstance(identity, str):
        return ""
    if identity.startswith(("record:", "capture:", "artifact:")):
        return identity
    if (record.get("kind") and "payload" in record and "version" in record) or record.get("memory_type") == "capture":
        from commontrace import capability_io

        return capability_io.runtime_identity(root, identity, capture=record.get("memory_type") == "capture")
    return identity


def _bindings(root: str, record: Mapping[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    identities = {_key(root, record)}
    identities.update(record.get("source_traces", []))
    identities.update(record.get("sources", []))
    relations = record.get("relations", {})
    for name in ("derives", "caused_by"):
        for identity in relations.get(name, []):
            from commontrace import capability_io

            identities.add(capability_io.runtime_identity(root, identity, capture=name == "caused_by"))
    from commontrace import lineage

    for identity in list(identities):
        identities.update(lineage.ancestors(root, identity))
    found = {state["resources"][identity] for identity in identities if identity in state["resources"]}
    return [state["receipts"][identity] for identity in sorted(found)]


def live(receipt: Mapping[str, Any]) -> bool:
    return (receipt["status"] == "given"
            and fp.moment(receipt["issued_at"]) <= fp.now() < fp.moment(receipt["expires_at"]))


def permits(root: str, record: Mapping[str, Any], *, action: str = "read") -> bool:
    if _OWNER_VIEW.get():
        return True  # Disclosure to the undelegated owner, not an agent-use grant.
    if not enabled(root):
        return True
    receipts = _bindings(root, record, _state(root))
    if not receipts:
        return True
    operation = {"read": "Retrieve", "write": "Store", "export": "Disclose"}.get(action, "Use")
    selected = current(root)
    inferred = action == "write" and (bool(record.get("source_traces")) or bool(record.get("sources"))
                                     or bool(record.get("relations")))
    return all((not inferred or DPV+"Infer" in row["processing"]) and live(row)
               and selected in row["purposes"] and DPV+operation in row["processing"]
               for row in receipts)


def scope(root: str, identity: str) -> dict[str, Any]:
    state = _state(root)
    key = state["resources"].get(identity)
    if key is None:
        return {"status": "not-configured", "purposes": []}
    receipt = state["receipts"][key]
    return {"status": "given" if live(receipt) else "expired-or-withdrawn", "purposes": receipt["purposes"],
            "processing": receipt["processing"], "expires_at": receipt["expires_at"],
            "receipt": fp.sign(root, {key: value for key, value in receipt.items() if key != "vault_directory"})}


def consent(root: str, subject: str, identities: list[str], purposes: list[str], *, controller: str,
            expires_at: str, collection_method: str, context: Sequence[str] = (),
            processing: list[str] | None = None, inferred: list[str] | None = None,
            vault_directory: str | None = None) -> dict[str, Any]:
    from commontrace import incident, nextwave_store, person_io, transparency_log

    nextwave_store.operator()
    fp.count(len(identities), 1, 64)
    if len(set(identities)) != len(identities):
        raise ValueError("UniMem consent sources must be distinct")
    if not purposes or len(purposes) > 16 or len(set(purposes)) != len(purposes) or not set(purposes) <= {
            DPV+name for name in PURPOSES}:
        raise ValueError("UniMem consent requires explicit supported DPV purposes")
    operations = processing if processing is not None else [DPV+name for name in PROCESSING]
    if not operations or len(set(operations)) != len(operations) or not set(operations) <= {DPV+v for v in PROCESSING}:
        raise ValueError("UniMem consent processing must use the supported DPV subset")
    expires_at = fp.moment(expires_at)
    if expires_at <= fp.now():
        raise ValueError("UniMem consent expiry must be in the future")
    fp.text(controller, "data controller", 256)
    fp.text(collection_method, "consent collection method", 512)
    if not set(inferred or []) <= set(identities):
        raise ValueError("UniMem inferred entries must identify selected canonical sources")
    if inferred and DPV+"Infer" not in operations:
        raise PermissionError("UniMem inferred memories require consent for inference")
    filename = fp.filename(root, "consent-state.json")
    with incident.protect(root), _jsonl.locked(filename), _owner_view():
        canonical, aliases = person_io._canonical_sources(root, identities, context)
        if set(nextwave_store.admit(root, canonical, context=context)) != set(canonical):
            raise PermissionError("UniMem consent cannot enroll an unavailable or out-of-scope memory")
        state = deepcopy(_state(root))
        # One immutable binding prevents consent broadening or subject reassignment
        # by overwriting a receipt. Withdrawal/expiry never falls back to legacy.
        if set(canonical) & set(state["resources"]):
            raise PermissionError("UniMem consent-bound memory cannot be rebound; create a reviewed new memory")
        fp.count(len(state["resources"])+len(canonical), 1, 4096)
        fp.count(len(state["receipts"]), 0, 1023)
        snapshots = {identity: person_io._snapshot(root, identity) for identity in canonical}
        from commontrace import memory_authority

        receipt: dict[str, Any] = {"schema": "unimem.consent.v1", "vocabulary": "https://w3id.org/dpv/2.1",
            "recorded_by": list(memory_authority.bound_writer()),
            "subject": _subject(root, subject), "controller": controller, "purposes": sorted(purposes),
            "processing": sorted(operations), "collection_method": collection_method, "issued_at": fp.now(),
            "expires_at": expires_at, "status": "given",
            "assertion": "operator-recorded consent; not person authentication",
            "sources": {identity: {"digest": fp.digest(content), "namespace": meta["namespace"],
                "scopes": meta["labels"], "inferred": identity in {aliases[v] for v in inferred or []}}
                for identity, (_, content, meta) in snapshots.items()}}
        identity = fp.digest(receipt)
        receipt["id"] = identity
        receipt["vault_directory"] = os.path.realpath(vault_directory) if vault_directory else None
        if vault_directory:
            from commontrace.certified_erasure import SubjectVault

            vault = SubjectVault(root, vault_directory)
            # These envelopes give key destruction an actual encrypted scope.
            # Canonical plaintext and older plaintext backups remain excluded.
            receipt["envelopes"] = {}
            for source_id, (_, content, meta) in snapshots.items():
                capsule_id = identity+"-"+fp.digest(source_id)[:16]
                envelope = vault.seal("consent:"+identity, source_id, fp.wire(content).decode())
                nextwave_store.put(root, "consent-envelopes", capsule_id, {"envelope": envelope},
                    scopes=meta["labels"], sources=[source_id], immutable=True)
                receipt["envelopes"][source_id] = nextwave_store.reference("consent-envelopes", capsule_id)
        state["receipts"][identity] = receipt
        state["resources"].update({key: identity for key in canonical})
        _save(root, state)
        anchor = transparency_log.append(root, "consent-recorded", {"receipt_digest": fp.digest(receipt)})
        return {"receipt": fp.sign(root, receipt), "anchor": anchor}


def withdraw(root: str, receipt_id: str) -> dict[str, Any]:
    from commontrace import incident, nextwave_store, transparency_log

    nextwave_store.operator()
    filename = fp.filename(root, "consent-state.json")
    with incident.protect(root), _jsonl.locked(filename):
        state = deepcopy(_state(root))
        receipt = state["receipts"].get(receipt_id)
        if receipt is None:
            raise ValueError("UniMem unknown consent receipt")
        receipt["status"] = "withdrawn"
        _save(root, state)
        anchor = transparency_log.append(root, "consent-withdrawn", {"receipt_digest": fp.digest(receipt)})
        return {"receipt_id": receipt_id, "status": "withdrawn", "anchor": anchor}


def explain(root: str, subject: str, *, context: Sequence[str] = ()) -> dict[str, Any]:
    from commontrace import nextwave_store, person_io

    nextwave_store.operator()
    pseudonym = _subject(root, subject)
    items = []
    with _owner_view():
        state = _state(root)
        for receipt in state["receipts"].values():
            if receipt["subject"] != pseudonym:
                continue
            for identity, source in receipt["sources"].items():
                if identity not in nextwave_store.admit(root, [identity], context=context):
                    continue
                _, content, _ = person_io._snapshot(root, identity)
                items.append({"id": identity, "memory": content, "inferred": source["inferred"],
                    "purposes": receipt["purposes"], "processing": receipt["processing"],
                    "consent": {"id": receipt["id"], "status": receipt["status"], "live": live(receipt),
                                "expires_at": receipt["expires_at"]}})
    return {"subject": pseudonym, "memories": items, "coverage": "explicitly enrolled subject bindings only"}


def reap(root: str) -> dict[str, Any]:
    from commontrace import incident, nextwave_store, retention, subject_policy

    nextwave_store.operator()
    results = []
    with incident.protect(root):
        for receipt in _state(root)["receipts"].values():
            if not live(receipt):
                results.append(retention.expire_consent(root, receipt))
    return {"expired": results, "regulated_retention": subject_policy.reap(root),
            "read_filter": "expiry enforced independently of cleanup"}
