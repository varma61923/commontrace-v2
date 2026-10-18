"""UniMem pinned agent identities, replay protection and delegation lifecycle.

Keys and tenancy are operator configuration. Tokens only narrow that authority.
Signed metadata retains commitments; handoff content remains deletable storage.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

from commontrace import _jsonl, lineage, memory_authority, paths
from commontrace import capabilities as cap
from commontrace import frontier_protocol as fp
from commontrace import nextwave_store as store

CURRENT, STICKY = cap.CURRENT, cap.STICKY


def _config_path(root: str) -> str:
    filename = os.path.join(paths.memory_dir(root), "capabilities.json")
    paths.enforce_boundary(root, filename)
    return filename


def enabled(root: str) -> bool:
    return os.path.isfile(os.path.join(paths.memory_dir(root), "capabilities.json"))


def _root_key(root: str, *, create: bool = False) -> bytes:
    key = memory_authority._key(root, create=create)
    if key is None:
        raise PermissionError("UniMem local delegation origin key is unavailable")
    return hmac.new(key, b"unimem.capability.issuer-key.v1", hashlib.sha256).digest()


def tenant(root: str) -> str:
    return hmac.new(_root_key(root), b"unimem.capability.tenant.v1", hashlib.sha256).hexdigest()


def _config(root: str) -> dict[str, Any]:
    filename = _config_path(root)
    if not os.path.lexists(filename):
        return {"agents": {}, "issuers": {}}
    if os.path.islink(filename):
        raise ValueError("UniMem delegation configuration cannot be a symlink")
    with open(filename, "rb") as stream:
        raw = stream.read(1_048_577)
    if len(raw) > 1_048_576:
        raise ValueError("UniMem delegation configuration exceeds its bound")
    signed = json.loads(raw)
    body = signed["record"]
    expected = hmac.new(_root_key(root), b"unimem.capability.config.v1\x00" + fp.wire(body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signed["signature"]):
        raise PermissionError("UniMem delegation configuration signature rejected")
    return dict(body)


def _save(root: str, value: dict[str, Any]) -> None:
    data = b"unimem.capability.config.v1\x00" + fp.wire(value)
    signed = {"record": value, "signature": hmac.new(_root_key(root, create=True), data, hashlib.sha256).hexdigest()}
    _jsonl.write_json(_config_path(root), signed)


def _private(filename: str) -> bytes:
    if os.path.islink(filename):
        raise PermissionError("UniMem agent key cannot be a symlink")
    fd = os.open(filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        stat = os.fstat(stream.fileno())
        key = stream.read(33)
    if stat.st_mode & 0o077 or len(key) != 32:
        raise PermissionError("UniMem agent key must contain 32 bytes and have private file permissions")
    return key


def register_agent(root: str, identity: str, principal: str, *, algorithm: str = "hmac-sha256",
                   secret_file: str = "", public_key: str = "", clearance: Sequence[str] = (),
                   authority: str = "agent", require_capability: bool = True) -> dict[str, Any]:
    store.operator()
    identity, principal = cap._label(identity), cap._label(principal)
    if (algorithm not in cap.ALGORITHMS or authority not in memory_authority.TRUST
            or type(require_capability) is not bool):
        raise ValueError("UniMem invalid agent algorithm, role or enrollment")
    labels = sorted({cap._label(label) for label in clearance})
    fp.count(len(labels), 0, 128)
    with _jsonl.locked(_config_path(root)):
        config = _config(root)
        if identity in config["agents"] or any(row["principal"] == principal for row in config["agents"].values()):
            raise ValueError("UniMem agent key IDs and primary principals are immutable and unique")
        fp.count(len(config["agents"]), 0, 255)
        _root_key(root, create=True)
        filename = ""
        if algorithm == "hmac-sha256":
            if public_key:
                raise ValueError("UniMem HMAC enrollment uses a private key file")
            filename = os.path.join(paths.memory_dir(root), ".capability-" + fp.digest(identity) + ".key")
            key = _private(secret_file) if secret_file else secrets.token_bytes(32)
            fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
            commitment = hashlib.sha256(key).hexdigest()
        else:
            public = bytes.fromhex(public_key)
            if len(public) != 32 or secret_file:
                raise ValueError("UniMem Ed25519 enrollment pins only an external holder's public key")
            commitment = hashlib.sha256(public).hexdigest()
        registration = {"id": identity, "principal": principal, "algorithm": algorithm,
                        "secret_file": filename, "public_key": public_key, "key_commitment": commitment,
                        "clearance": labels, "authority": authority, "require_capability": require_capability}
        config["agents"][identity] = registration
        _save(root, config)
    return {key: item for key, item in registration.items() if key != "public_key"}


def agent(root: str, identity: str) -> dict[str, Any]:
    row = _config(root)["agents"].get(identity)
    if row is None:
        raise PermissionError("UniMem unknown pinned agent key")
    return dict(row)


def required(root: str, principal: str) -> bool:
    if not enabled(root):
        return False
    return any(row["principal"] == principal and row["require_capability"] for row in _config(root)["agents"].values())


def _verification_key(root: str, identity: str) -> tuple[dict[str, Any], bytes]:
    row = agent(root, identity)
    key = _private(row["secret_file"]) if row["algorithm"] == "hmac-sha256" else bytes.fromhex(row["public_key"])
    if hashlib.sha256(key).hexdigest() != row["key_commitment"]:
        raise PermissionError("UniMem holder key differs from its immutable registration")
    return row, key


def pin_issuer(root: str, issuer: str, public_key: str, caveats: Mapping[str, Any]) -> dict[str, Any]:
    store.operator()
    issuer = cap._label(issuer)
    if issuer == "local" or len(bytes.fromhex(public_key)) != 32:
        raise ValueError("UniMem external issuers require a distinct name and pinned Ed25519 public key")
    with _jsonl.locked(_config_path(root)):
        config = _config(root)
        if issuer in config["issuers"]:
            raise ValueError("UniMem issuer pins are immutable")
        value = {"algorithm": "ed25519", "public_key": public_key, "caveats": cap.policy(caveats)}
        config["issuers"][issuer] = value
        _root_key(root, create=True)
        _save(root, config)
    return value


def revocations(root: str) -> set[str]:
    return {row["id"] for row in store.events(root, "capability-revocations")}


def check(root: str, token: Mapping[str, Any], *, tenant_id: str = "") -> dict[str, Any]:
    links = cap._token(token)
    issuer = links[0]["record"].get("issuer")
    if issuer == "local":
        algorithm, key, maximum = "hmac-sha256", _root_key(root), None
    else:
        pin = _config(root)["issuers"].get(issuer)
        if pin is None:
            raise PermissionError("UniMem external issuer has no trusted pin")
        algorithm, key, maximum = pin["algorithm"], bytes.fromhex(pin["public_key"]), pin["caveats"]
    def holder(identity: str) -> bytes:
        registration, key = _verification_key(root, identity)
        if registration["algorithm"] != algorithm:
            raise PermissionError("UniMem holder algorithm differs from this chain's issuer")
        return key
    verified = cap.verify(token, issuer=issuer, tenant=tenant_id or tenant(root), algorithm=algorithm, key=key,
                          holder_key=holder, now=fp.now(), revoked=sorted(revocations(root)))
    if maximum is not None:
        cap.subset(maximum, links[0]["record"]["caveats"])
    for node in links:
        policy = node["record"]["caveats"]
        registration, _ = _verification_key(root, policy["audience"])
        if registration["algorithm"] != algorithm or not set(policy["labels"]) <= set(registration["clearance"]):
            raise PermissionError("UniMem capability exceeds the holder's pinned clearance")
    return verified


def grant(root: str, caveats: Mapping[str, Any], *, tenant_id: str = "") -> dict[str, Any]:
    store.operator()
    token = cap.grant(caveats, issuer="local", tenant=tenant_id or tenant(root), nonce=secrets.token_hex(16),
                      now=fp.now(), algorithm="hmac-sha256", key=_root_key(root))
    return {"token": token, **check(root, token, tenant_id=tenant_id)}


def attenuate(root: str, token: Mapping[str, Any], caveats: Mapping[str, Any], *, secret_file: str) -> dict[str, Any]:
    checked = check(root, token)
    registration = agent(root, checked["caveats"]["audience"])
    if registration["algorithm"] != "hmac-sha256":
        raise ValueError("UniMem external Ed25519 holders attenuate with capabilities.attenuate offline")
    result = cap.attenuate(token, caveats, key=_private(secret_file))
    return {"token": result, **check(root, result)}


def consume_nonce(root: str, identity: str, expires_at: str) -> None:
    filename = os.path.join(paths.memory_dir(root), ".capability-nonces.sqlite3")
    paths.enforce_boundary(root, filename)
    if os.path.islink(filename) or os.path.exists(filename) and os.path.getsize(filename) > 32 * fp.MAX_BYTES:
        raise ValueError("UniMem nonce store is linked or exceeds its maintenance bound")
    now, expiry = cap.moment(fp.now()).timestamp(), cap.moment(expires_at).timestamp()
    if not now < expiry <= now + 30:
        raise PermissionError("UniMem request proof expired before nonce consumption")
    db = sqlite3.connect(filename, timeout=2, isolation_level=None)
    try:
        os.chmod(filename, 0o600)
        db.execute("CREATE TABLE IF NOT EXISTS nonces(id TEXT PRIMARY KEY,expires REAL NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM nonces WHERE expires<=?", (now,))
        if db.execute("SELECT count(*) FROM nonces").fetchone()[0] >= 10000:
            raise RuntimeError("UniMem live nonce bound exceeded")
        db.execute("INSERT INTO nonces VALUES(?,?)", (fp.text(identity), expiry))
        db.commit()
    except sqlite3.IntegrityError:
        raise PermissionError("UniMem capability request nonce was already consumed") from None
    finally:
        db.close()


def authenticate(root: str, token: Mapping[str, Any], proof: Mapping[str, Any], *, request_digest: str,
                 operation: str, purpose: str, principal: str = "", tenant_id: str = "",
                 consume: bool = True) -> dict[str, Any]:
    verified = check(root, token, tenant_id=tenant_id)
    policy = verified["caveats"]
    if operation not in policy["operations"] or purpose not in policy["purposes"]:
        raise PermissionError("UniMem capability cannot authorize this operation or purpose")
    registration, key = _verification_key(root, policy["audience"])
    if principal and principal != registration["principal"]:
        raise PermissionError("UniMem primary authenticated principal differs from capability holder")
    nonce = cap.verify_proof(token, verified, proof, key=key, algorithm=registration["algorithm"],
                            request_digest=request_digest, now=fp.now())
    nonce_id = fp.digest([verified["tenant"], policy["audience"], nonce])
    if consume:
        consume_nonce(root, nonce_id, proof["record"]["expires_at"])
    return {"root": os.path.realpath(root), "token": json.loads(fp.wire(dict(token))), "verified": verified,
            "proof": json.loads(fp.wire(dict(proof))), "principal": registration["principal"],
            "authority": registration["authority"], "registration": fp.digest(registration),
            "key_files": sorted({agent(root, node["record"]["caveats"]["audience"])["secret_file"]
                                 for node in token["links"]} - {""}),
            "request_digest": request_digest, "operation": operation, "purpose": purpose, "nonce_id": nonce_id,
            "request_expires_at": proof["record"]["expires_at"]}


@contextmanager
def activate(authorization: dict[str, Any]) -> Iterator[None]:
    if CURRENT.get() is not None:
        raise PermissionError("UniMem delegated contexts cannot be replaced or nested")
    token = CURRENT.set(authorization)
    try:
        yield
    finally:
        CURRENT.reset(token)


def _request_epoch(root: str, value: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(fp._stamp(filename) for filename in [
        _config_path(root), os.path.join(paths.memory_dir(root), ".origin-key"),
        fp.filename(root, store.journal_name("capability-revocations")), *value["key_files"]])


def current(root: str) -> dict[str, Any] | None:
    value = CURRENT.get()
    if value is None:
        return None
    if value["root"] != os.path.realpath(root):
        raise PermissionError("UniMem capability cannot cross tenant roots")
    if cap.moment(value["request_expires_at"]) <= cap.moment(fp.now()):
        raise PermissionError("UniMem request proof expired before delivery")
    if cap.moment(value["verified"]["caveats"]["expires_at"]) <= cap.moment(fp.now()):
        raise PermissionError("UniMem leaf capability expired before delivery")
    epoch = _request_epoch(root, value)
    if value.get("_epoch") != epoch:
        verified = check(root, value["token"], tenant_id=value["verified"]["tenant"])
        registration, key = _verification_key(root, verified["caveats"]["audience"])
        if fp.digest(registration) != value["registration"]:
            raise PermissionError("UniMem holder registration changed during request")
        cap.verify_proof(value["token"], verified, value["proof"], key=key, algorithm=registration["algorithm"],
                         request_digest=value["request_digest"], now=fp.now())
        if epoch != _request_epoch(root, value):
            raise PermissionError("UniMem issuer or holder key changed during verification; retry")
        value["verified"], value["_epoch"] = verified, epoch
    return value


def record_policy(record: Mapping[str, Any], *, namespace: str = "") -> tuple[str, str, list[str]]:
    kind = memory_authority.record_class(dict(record))
    namespace = namespace or {"fact": "facts", "trace": "traces", "lesson": "lessons"}.get(kind, kind)
    labels = record.get("scopes", [])
    if not isinstance(labels, list):
        raise ValueError("UniMem delegated scopes must be a list")
    return namespace, fp.text(record.get("memory_type", record.get("kind", "general"))), labels


def permits(root: str, record: Mapping[str, Any], *, namespace: str = "", operation: str = "read") -> bool:
    value = current(root)
    if value is None:
        return True
    namespace, kind, labels = record_policy(record, namespace=namespace)
    return cap.permits(value["verified"]["caveats"], namespace=namespace, operation=operation, memory_class=kind,
                       purpose=value["purpose"], labels=labels)


def before_write(root: str, record: Mapping[str, Any], *, namespace: str = "") -> tuple[str, str] | None:
    value = current(root)
    principal = value["principal"] if value else memory_authority.WRITER.get()[0]
    inherited = sticky_labels(root, principal) if enabled(root) else list(STICKY.get())
    if not set(inherited) <= set(record.get("scopes", [])):
        raise PermissionError("UniMem write must inherit active handoff labels")
    if value is None:
        return None
    if value["operation"] != "write" or not permits(root, record, namespace=namespace, operation="write"):
        raise PermissionError("UniMem write exceeds its leaf capability")
    rank = min(value["verified"]["caveats"]["ceiling"], memory_authority.TRUST[value["authority"]],
               memory_authority.TRUST[memory_authority.WRITER.get()[1]])
    authority = next(name for name in ("external", "agent", "user", "operator") if memory_authority.TRUST[name] == rank)
    return principal, authority


def delegation(root: str, record: Mapping[str, Any], *, namespace: str = "",
               operational: bool = False) -> dict[str, Any]:
    value = current(root)
    if value is None:
        return {}
    if not operational:
        before_write(root, record, namespace=namespace)
    return {"delegation": {"chain_digest": value["verified"]["chain_digest"], "chain": value["token"],
                           "chain_ids": value["verified"]["chain_ids"], "purpose": value["purpose"]}}


def stamp(root: str, record: Mapping[str, Any], *, namespace: str = "") -> None:
    value = current(root)
    if value is None:
        return
    before_write(root, record, namespace=namespace)
    content_hash = fp.digest({key: item for key, item in record.items() if key != "id"})
    identity = fp.digest([record["id"], content_hash, value["verified"]["chain_digest"]])
    if identity not in store.latest(root, "capability-writes"):
        store.event(root, "capability-writes", identity, {"target": fp.text(record["id"]), "content_hash": content_hash,
            "chain": value["token"], "chain_ids": value["verified"]["chain_ids"],
            "scopes": list(record.get("scopes", [])), "purpose": value["purpose"], "at": fp.now()})


_QUARANTINES: OrderedDict[str, dict[str, Any]] = OrderedDict()
_QUARANTINE_LOCK = threading.RLock()
def _after_fork() -> None:
    global _QUARANTINE_LOCK
    _QUARANTINES.clear()
    _QUARANTINE_LOCK = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _quarantine_epoch(root: str, key_files: Sequence[str]) -> tuple[Any, ...]:
    names = ["capabilities.json", ".origin-key", ".origin-ed25519-key", "origin-signing.json", "lineage.jsonl",
             store.journal_name("capability-writes"), store.journal_name("capability-revocations"),
             store.journal_name("capability-impacts")]
    return tuple(fp._stamp(filename) for filename in
                 [*(os.path.join(paths.memory_dir(root), name) for name in names), *key_files])


def _quarantine_snapshot(root: str) -> frozenset[str]:
    root = os.path.realpath(root)
    now = cap.moment(fp.now())
    with _QUARANTINE_LOCK:
        previous = _QUARANTINES.get(root)
        if (previous and now < previous["deadline"]
                and previous["epoch"] == _quarantine_epoch(root, previous["key_files"])):
            _QUARANTINES.move_to_end(root)
            return frozenset(previous["blocked"])
        config = _config(root)
        key_files = sorted({row["secret_file"] for row in config["agents"].values()} - {""})
        before = _quarantine_epoch(root, key_files)
        revoked = revocations(root)
        objects = lineage.records(root) if revoked else {}
        fp.count(len(objects), 0, 10000, "UniMem scoped lineage namespace")
        writes: dict[str, list[dict[str, Any]]] = {}
        deadline = now + timedelta(minutes=5)
        if revoked:
            for row in store.events(root, "capability-writes"):
                if row["content_hash"] == objects.get(row["target"], {}).get("content_hash"):
                    writes.setdefault(row["target"], []).append(row)
        blocked = set()
        for target, rows in writes.items():
            if not any(set(row["chain_ids"]) & revoked for row in rows):
                continue
            alternate = False
            for row in rows:
                if not set(row["chain_ids"]) & revoked:
                    try:
                        checked = check(root, row["chain"], tenant_id=row["chain"]["links"][0]["record"]["tenant"])
                        deadline = min(deadline, cap.moment(checked["caveats"]["expires_at"]))
                        alternate = True
                        break
                    except (PermissionError, ValueError):
                        continue
            if not alternate:
                blocked.add(target)
        children: dict[str, set[str]] = {}
        for identity, record in objects.items():
            for parent in record.get("inputs", []):
                children.setdefault(parent["id"], set()).add(identity)
        if revoked:
            for impact in store.events(root, "capability-impacts"):
                children.setdefault(impact["source"], set()).add(impact["target"])
        pending = [(identity, 0) for identity in blocked]
        while pending:
            identity, depth = pending.pop()
            fp.count(len(blocked), 0, 2000, "UniMem delegated quarantine namespace")
            descendants = children.get(identity, set()) - blocked
            if descendants and depth >= 16:
                raise ValueError("UniMem delegation dependency depth exceeds scoped bound")
            blocked.update(descendants)
            pending.extend((child, depth + 1) for child in descendants)
        if before != _quarantine_epoch(root, key_files):
            raise PermissionError("UniMem delegation lineage changed during quarantine computation; retry")
        snapshot = {"epoch": before, "key_files": key_files, "deadline": deadline, "blocked": frozenset(blocked)}
        _QUARANTINES[root] = snapshot
        _QUARANTINES.move_to_end(root)
        while len(_QUARANTINES) > 8:
            _QUARANTINES.popitem(last=False)
        return frozenset(blocked)


def quarantined(root: str, identity: str) -> bool:
    return enabled(root) and identity in _quarantine_snapshot(root)


def revoke(root: str, chain_id: str, *, reason: str, context: Sequence[str] = ()) -> dict[str, Any]:
    store.operator()
    if len(chain_id) != 64 or any(char not in "0123456789abcdef" for char in chain_id):
        raise ValueError("UniMem revocation requires a chain-node SHA-256 digest")
    fp.text(reason, "revocation reason", 2048)
    if chain_id in revocations(root):
        return {"revoked": chain_id, "already_revoked": True}
    candidates = sorted({row["target"] for row in store.events(root, "capability-writes")
                         if chain_id in row["chain_ids"]})
    visible = {}
    for start in range(0, len(candidates), 512):
        visible.update(store.admit(root, candidates[start:start + 512], context=context))
    from commontrace import contagion

    impacts, truncated = [], len(visible) > 128
    for identity in sorted(visible)[:128]:
        try:
            graph = contagion.radius(root, identity, context=context)
            truncated |= graph["truncated"]
            for node in graph["nodes"]:
                if node["kind"] == "output":
                    # Output names remain qualified: an agent cannot nominate an unrelated canonical ID.
                    impacts.append({"source": identity, "target": node["id"]})
                    fp.count(len(impacts), 0, 2000, "UniMem instrumented revocation outputs")
        except (ValueError, PermissionError, OSError):
            truncated = True
    for impact in impacts:
        store.event(root, "capability-impacts", fp.digest(impact), impact)
    store.event(root, "capability-revocations", chain_id, {"at": fp.now(), "reason_digest": fp.digest(reason),
                                                         "impact_truncated": truncated})
    from commontrace import change_stream

    blocked = [identity for identity in visible if quarantined(root, identity)]
    if change_stream.enabled(root):
        for identity in blocked:
            change_stream.publish(root, "quarantine", identity, scopes=visible[identity]["scopes"],
                                  namespace=visible[identity].get("namespace", "traces" if
                                      "content_digest" in visible[identity] else "facts"),
                                  event_id=fp.digest(["capability-quarantine", chain_id, identity]))
    return {"revoked": chain_id, "quarantined_previously_admitted": sorted(blocked),
            "observed_output_count": len(impacts), "impact_truncated": truncated,
            "classification": "chain revocation plus bounded observed dependencies; uninstrumented reads not covered"}


def sticky_labels(root: str, principal: str) -> list[str]:
    labels = set(STICKY.get())
    for row in store.events(root, "handoff-redemptions"):
        if row["recipient_principal"] == principal and cap.moment(row["expires_at"]) > cap.moment(fp.now()):
            labels.update(row["labels"])
    return sorted(labels)


def inherit_labels(root: str, labels: Sequence[str]) -> list[str]:
    value = current(root)
    principal = value["principal"] if value else memory_authority.WRITER.get()[0]
    return sorted(set(labels) | set(sticky_labels(root, principal)))


def handoff(root: str, identities: Sequence[str], recipient: str, *, context: Sequence[str] = (),
            seconds: int = 300) -> dict[str, Any]:
    from commontrace import hierarchical, memory_guard, memory_ifc, policy_io, safety, sentinel_io

    sentinel_io.assert_generalize(root, identities, context=context)

    value = current(root)
    if value is None:
        store.operator()
    elif value["operation"] != "export":
        raise PermissionError("UniMem delegated handoff requires an export request")
    fp.count(len(identities), 1, 32)
    fp.count(seconds, 1, 300)
    registration = agent(root, recipient)
    _verification_key(root, recipient)
    with policy_io.authorization("export"):
        admitted = store.admit(root, identities, context=context)
    protected = safety.protected(root)
    facts = hierarchical.load_facts(root)
    records, commitments, policies = [], {}, {}
    for identity, record in sorted(admitted.items()):
        if not set(record["scopes"]) <= set(registration["clearance"]):
            continue
        namespace = record.get("namespace") or ("facts" if identity in facts else
                                                "traces" if "content_digest" in record else "lessons")
        canonical = memory_authority.fact_record(facts[identity]) if identity in facts else {
            "id": identity, "scopes": record["scopes"], "memory_type": record.get("memory_class", "general")}
        if not permits(root, canonical, namespace=namespace, operation="export"):
            continue
        _, memory_class, labels = record_policy(canonical, namespace=namespace)
        text = record["text"]
        if identity not in protected:
            text = memory_guard.redact_pii(memory_guard.scrub_text(text))[0]
        memory = memory_ifc.label(root, {**{key: item for key, item in record.items() if key != "quoted_text"},
                                        "text": text})
        if memory_ifc.egress_match(root, tenant(root), text)["alert"]:
            raise PermissionError("UniMem handoff contains a tenant canary")
        records.append(memory)
        commitments[identity] = fp.digest(record)
        policies[identity] = {"namespace": namespace, "memory_class": memory_class, "labels": labels}
    if not records:
        raise PermissionError("UniMem handoff has no sources admitted to both sender and recipient")
    labels = sorted({label for record in records for label in record["scopes"]})
    identity = secrets.token_hex(16)
    expires_at = (cap.moment(fp.now()) + timedelta(seconds=seconds)).isoformat()
    store.put(root, "handoff-bundles", identity, {"memories": records}, scopes=labels,
              sources=sorted(commitments), immutable=True)
    claim = {"type": "unimem.handoff.v1", "id": identity, "recipient": recipient,
             "recipient_registration": fp.digest(registration), "labels": labels,
             "commitments": commitments, "source_policies": policies, "expires_at": expires_at}
    return {"claim": fp.sign(root, claim)}


def receive(root: str, signed: dict[str, Any], *, context: Sequence[str] = ()) -> dict[str, Any]:
    value = current(root)
    if value is None or value["operation"] != "read":
        raise PermissionError("UniMem handoff redemption requires recipient proof of possession")
    if not fp.verify(root, signed):
        raise PermissionError("UniMem handoff signature rejected")
    claim = signed["record"]
    recipient = value["verified"]["caveats"]["audience"]
    if (claim.get("type") != "unimem.handoff.v1" or claim.get("recipient") != recipient
            or claim.get("recipient_registration") != fp.digest(agent(root, recipient))
            or cap.moment(claim["expires_at"]) <= cap.moment(fp.now())):
        raise PermissionError("UniMem handoff recipient binding or expiry rejected")
    if not set(claim["labels"]) <= set(context):
        raise PermissionError("UniMem handoff is outside recipient context")
    admitted = store.admit(root, list(claim["commitments"]), context=context)
    if {identity: fp.digest(record) for identity, record in admitted.items()} != claim["commitments"]:
        raise PermissionError("UniMem handoff sources changed or became unavailable")
    for rule in claim["source_policies"].values():
        if not cap.permits(value["verified"]["caveats"], purpose=value["purpose"], operation="read", **rule):
            raise PermissionError("UniMem recipient leaf capability excludes a handoff source")
    with _jsonl.locked(fp.filename(root, store.journal_name("handoff-redemptions"))):
        if claim["id"] in store.latest(root, "handoff-redemptions"):
            raise PermissionError("UniMem handoff claim was already redeemed")
        payload = store.get(root, "handoff-bundles", claim["id"], context=context)
        store.event(root, "handoff-redemptions", claim["id"], {
            "recipient_principal": value["principal"], "labels": claim["labels"], "expires_at": claim["expires_at"]})
    return {"memories": payload["memories"], "sticky_labels": claim["labels"], "expires_at": claim["expires_at"]}


def agents_input_filter(root: str, signed: dict[str, Any], *, context: Sequence[str] = ()) -> Any:
    """OpenAI Agents SDK HandoffInputData callback; the SDK remains optional.

    Field names pinned to SDK commit 9abc9182d06dbd28330af888306031269d57a175.
    Replaces the incoming histories with admitted quoted data messages.
    """
    from dataclasses import replace

    def input_filter(data: Any) -> Any:
        bundle = receive(root, signed, context=context)
        messages = tuple({"role": "user", "content": memory.get("quoted_text", memory["text"])}
                         for memory in bundle["memories"])
        return replace(data, input_history=messages, pre_handoff_items=(), new_items=())
    return input_filter


def a2a_extension(signed: dict[str, Any]) -> dict[str, Any]:
    """Namespaced UniMem extension payload; agent transports negotiate support."""
    return {"uri": "urn:unimem:handoff:v1", "required": True, "params": {"claim": signed}}


@contextmanager
def fact_view(root: str, facts: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Admit before dedup/conflict indexing; merge detached authorized changes only."""
    from copy import deepcopy

    value = current(root)
    if value is None:
        yield facts
        return
    fp.count(len(facts), 0, 10000, "UniMem scoped canonical fact namespace")
    working = {identity: deepcopy(fact) for identity, fact in facts.items()
               if permits(root, memory_authority.fact_record(fact), namespace="facts")
               and not memory_authority.lineage_blocked(root, identity)}
    before = {identity: fact.to_dict() for identity, fact in working.items()}
    yield working
    changed = [fact for identity, fact in working.items() if before.get(identity) != fact.to_dict()]
    removed = set(before) - set(working)
    for identity in removed:
        assert_forget(root, memory_authority.fact_record(facts[identity]), namespace="facts")
    for fact in changed:
        if fact.id in facts and fact.id not in before:
            raise PermissionError("UniMem delegated write cannot replace an invisible canonical identity")
        before_write(root, memory_authority.fact_record(fact), namespace="facts")
        for source in fact.source_traces:
            if memory_authority.lineage_blocked(root, source):
                raise PermissionError("UniMem delegated write depends on a revoked source")
    for fact in changed:
        from commontrace import hierarchical

        fact.origin = memory_authority.bind(root, memory_authority.fact_record(fact), sources=fact.source_traces)
        fact.revision = hierarchical._compute_revision(fact.to_dict())
    for identity in removed:
        del facts[identity]
    facts.update({fact.id: fact for fact in changed})


def assert_forget(root: str, record: Mapping[str, Any], *, namespace: str) -> None:
    value = current(root)
    if value is not None and (value["operation"] != "forget" or
                             not permits(root, record, namespace=namespace, operation="forget")):
        raise PermissionError("UniMem delegated erasure exceeds its leaf capability")


def scope_labels(scope: Mapping[str, Any]) -> list[str]:
    labels = list(scope.get("labels", []))
    labels.extend(str(name) + ":" + str(value) for name, value in scope.items() if name != "labels")
    fp.count(len(labels), 0, 128)
    return sorted({cap._label(label) for label in labels})


def runtime_identity(root: str, identity: str, *, capture: bool = False) -> str:
    kind = "capture" if capture else "record"
    return kind + ":" + hmac.new(_root_key(root), ("unimem." + kind + ".identity.v1\x00" +
                                                fp.text(identity)).encode(), hashlib.sha256).hexdigest()


def runtime_descriptor(record: Any, *, root: str = "") -> dict[str, Any]:
    body = {**record.to_dict(), "scopes": scope_labels(record.scope), "memory_type": record.kind}
    if root:
        body["id"] = runtime_identity(root, record.id)
    return body


def runtime_permits(root: str, record: Any, *, operation: str = "read") -> bool:
    return bool((operation != "read" or not memory_authority.lineage_blocked(root, runtime_identity(root, record.id)))
                and permits(root, runtime_descriptor(record), namespace="records", operation=operation))


def runtime_prepare(root: str, record: Any) -> Any:
    from dataclasses import replace

    from commontrace import hierarchical, lesson_io
    from commontrace.record_visibility import scope_from_labels

    value = current(root)
    if value and (record.id in hierarchical.load_facts(root) or lesson_io.lesson_path(root, record.id) is not None):
        raise PermissionError("UniMem delegated runtime IDs cannot shadow canonical fact or lesson identities")
    labels = inherit_labels(root, scope_labels(record.scope))
    if set(labels) != set(scope_labels(record.scope)):
        record = replace(record, scope=scope_from_labels(labels))
    if value is None:
        return record
    effective = before_write(root, runtime_descriptor(record), namespace="records")
    if effective is None:
        raise PermissionError("UniMem delegated runtime write lacks authenticated authority")
    if memory_authority.TRUST[effective[1]] < memory_authority.TRUST["user"] and (
            record.kind in {"lesson", "directive"} or record.kind == "profile" and record.payload.get("critical")):
        record = replace(record, status="quarantined")
    return replace(record, provenance={**record.provenance, "principal": effective[0], "authority": effective[1],
        "delegation": {"chain_digest": value["verified"]["chain_digest"], "chain_ids": value["verified"]["chain_ids"],
                       "purpose": value["purpose"]}})


def runtime_attest(root: str, record: Any) -> dict[str, Any] | None:
    if current(root) is None:
        return None
    body = runtime_descriptor(record, root=root)
    inputs = sorted(
        {runtime_identity(root, identity) for identity in record.relations.get("derives", [])}
        | {runtime_identity(root, identity, capture=True) for identity in record.relations.get("caused_by", [])}
    )
    return lineage.prepare(root, body, fp.sign(root, body), inputs=inputs, operation="runtime-record")


def capture_descriptor(identity: str, session: str, event: str, payload: Mapping[str, Any],
                       scope: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": identity, "session": session, "event": event, "payload": dict(payload),
            "scopes": scope_labels(scope), "memory_type": "capture"}


def capture_attest(root: str, identity: str, session: str, event: str, payload: Mapping[str, Any],
                   scope: Mapping[str, Any]) -> dict[str, Any] | None:
    if current(root) is None:
        return None
    body = capture_descriptor(runtime_identity(root, identity, capture=True), session, event, payload, scope)
    before_write(root, body, namespace="records")
    return lineage.prepare(root, body, fp.sign(root, body), inputs=[], operation="runtime-record")
