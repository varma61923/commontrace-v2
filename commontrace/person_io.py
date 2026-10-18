"""Owner-controlled passport exports, explicit issuer pins and review-only imports."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from commontrace import frontier_protocol as fp


def _store() -> Any:
    from commontrace import nextwave_store

    return nextwave_store


def _identity(root: str, *, create: bool = False) -> Any:
    from commontrace import credentials, memory_authority, origin

    private = memory_authority._key(root, algorithm="ed25519", create=create)
    if private is None:
        raise PermissionError("UniMem person issuer signing key is not configured")
    public = credentials.public_key(private)
    issuer = "urn:unimem:issuer:"+hashlib.sha256(public).hexdigest()
    return origin.Principal(issuer, "unimem-person-v1", "operator", private, "ed25519", public)


def identity(root: str) -> dict[str, Any]:
    _store().operator()
    principal = _identity(root, create=True)
    return {"issuer": principal.id, "public_key": principal.public_key.hex(),
            "key_id": "sha256:"+hashlib.sha256(principal.public_key).hexdigest(),
            "algorithm": "Ed25519", "trust": "pin this key out of band; embedded keys are never trusted"}


def trust(root: str, issuer: str, public_key: str) -> dict[str, Any]:
    from commontrace import transparency_log

    store = _store()
    store.operator()
    fp.text(issuer, "pinned issuer", 512)
    if not issuer.startswith(("https://", "urn:")):
        raise ValueError("UniMem issuer must be an absolute HTTPS or URN identifier")
    public = bytes.fromhex(public_key)
    if len(public) != 32 or public_key != public.hex():
        raise ValueError("UniMem a pinned Ed25519 issuer needs a canonical 32-byte public key")
    identity = fp.digest(issuer)
    from commontrace import _jsonl

    with _jsonl.locked(fp.filename(root, "person-trust.lock")):
        prior = store.latest(root, "person-trust").get(identity)
        if prior and prior["public_key"] != public_key:
            raise PermissionError("UniMem issuer pins are immutable; enroll a distinct key-bound issuer for rotation")
        if not prior:
            store.event(root, "person-trust", identity, {"issuer": issuer, "public_key": public_key})
            transparency_log.append(root, "person-issuer-trust", {"issuer_digest": identity,
                                                                 "key_digest": fp.digest(public_key)})
    return {"issuer": issuer, "public_key": public_key, "trust": "explicit operator pin"}


def _pin(root: str, issuer: str) -> bytes:
    from commontrace import memory_authority

    if memory_authority._key(root, algorithm="ed25519") is not None:
        own = _identity(root)
        if own.id == issuer:
            return bytes(own.public_key)
    row = _store().latest(root, "person-trust").get(fp.digest(issuer))
    if not row or row["issuer"] != issuer:
        raise PermissionError("UniMem person issuer has not been pinned by the receiving operator")
    return bytes.fromhex(row["public_key"])


def _fact(root: str, identity: str) -> Any:
    from commontrace import fact_store, hierarchical

    if fact_store.enabled(root):
        with fact_store.transaction(root) as canonical:
            return canonical.get(identity)
    return hierarchical.load_facts(root).get(identity)


def _snapshot(root: str, identity: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    from commontrace import capability_io, frontmatter, lesson_admission, lesson_io
    from commontrace.memory_journal import Journal
    from commontrace.record_visibility import scope_from_labels

    fact = _fact(root, identity)
    if fact is not None:
        return "semantic", fact.to_dict(), {"namespace": "facts", "confidence": fact.confidence,
            "created_at": fact.created_at, "version": fact.revision or "1", "labels": fact.scopes,
            "parents": fact.source_traces, "memory_class": fact.memory_type}
    path = lesson_io.lesson_path(root, identity)
    if path is not None:
        fm, text = frontmatter.read(path)
        content = {"frontmatter": lesson_admission._json_value(fm), "body": text}
        return "procedural", content, {"namespace": "lessons", "confidence": None,
            "created_at": _created(fm.get("created_at") or fm.get("created")),
            "version": lesson_io.current_revision(path), "labels": fm.get("scopes", []),
            "parents": fm.get("source_traces", []), "memory_class": "lesson"}
    if identity.startswith(("record:", "capture:")):
        journal = Journal(root)
        with journal.connect() as db:
            row = db.execute("SELECT id,kind FROM resource_refs WHERE ref=?", (identity,)).fetchone()
            if row is not None and row[1] == "record":
                value = db.execute("SELECT body FROM records WHERE id=?", (row[0],)).fetchone()
                if value is not None:
                    content = json.loads(value[0])
                    kind = content["kind"]
                    component = {"episode": "episodic", "profile": "identity", "lesson": "procedural",
                                 "directive": "procedural", "working": "working"}.get(kind, "semantic")
                    return component, content, {"namespace": "records", "confidence": content.get("confidence"),
                        "created_at": content.get("created_at") or fp.now(), "version": str(content["version"]),
                        "labels": capability_io.scope_labels(content.get("scope", {})),
                        "parents": content.get("relations", {}).get("derives", []), "memory_class": kind}
            if row is not None and row[1] == "capture":
                value = db.execute("SELECT session,event,body,scope FROM captures WHERE id=?", (row[0],)).fetchone()
                if value is not None:
                    actual = json.loads(value[3])
                    # Admission is checked by the caller under its actual context.
                    scope = scope_from_labels(capability_io.scope_labels(actual))
                    reference = journal.admit_reference(identity, scope=scope)
                    if reference is not None:
                        return "episodic", {"id": row[0], "session": value[0], "event": value[1],
                            "payload": json.loads(value[2]), "scope": actual}, {"namespace": "records",
                            "confidence": None, "created_at": fp.now(), "version": reference["revision"],
                            "labels": capability_io.scope_labels(actual), "parents": [], "memory_class": "capture"}
    from pathlib import Path

    from commontrace import paths, trace_io

    for index, filename in enumerate(Path(paths.traces_dir(root)).glob("*.md")):
        fp.count(index, 0, 4095, "passport trace namespace")
        paths.enforce_boundary(root, str(filename))
        if filename.name == "README.md":
            continue
        fp.count(filename.stat().st_size, 0, _store().MAX_BLOB, "passport trace bytes")
        trace, body = trace_io.read(str(filename))
        if trace.get("id") != identity:
            continue
        fm, _ = frontmatter.read(str(filename))
        portable = {"frontmatter": lesson_admission._json_value(fm), "body": body}
        return "episodic", portable, {"namespace": "traces", "confidence": None,
            "created_at": _created(trace.get("created_at")), "version": fp.digest(portable),
            "labels": trace.get("scopes", []), "parents": trace.get("source_traces", []), "memory_class": "trace",
            "timestamps": {name: trace.get(name) for name in ("valid_from", "valid_until", "expires_at")
                           if trace.get(name) is not None}}
    raise PermissionError("UniMem passport source is not a canonical admitted memory")


def _created(value: Any) -> str:
    if isinstance(value, str):
        try:
            return fp.moment(value)
        except ValueError:
            pass
    # This is the passport entry's creation time; unknown source timestamps
    # remain exactly preserved in content, without inventing a source date.
    return fp.now()


def _canonical_sources(root: str, identities: list[str], context: Sequence[str]) -> tuple[list[str], dict[str, str]]:
    """Enroll an explicitly requested native alias without changing its bytes."""
    from commontrace import capability_io, memory_authority, policy_io
    from commontrace.memory_journal import Journal
    from commontrace.record_visibility import scope_from_labels

    aliases = {}
    for identity in identities:
        if not identity.startswith(("record:", "capture:")) or _fact(root, identity) is not None:
            aliases[identity] = identity
            continue
        prefix, _, raw_id = identity.partition(":")
        journal = Journal(root)
        with journal.connect() as db:
            prior = db.execute("SELECT id,kind FROM resource_refs WHERE ref=?", (identity,)).fetchone()
        if prior is not None:
            aliases[identity] = identity
            continue
        capture = prefix == "capture"
        if capture:
            with journal.connect() as db:
                row = db.execute("SELECT session,event,body,scope FROM captures WHERE id=?", (raw_id,)).fetchone()
            if row is None:
                raise PermissionError("UniMem passport capture alias is unavailable")
            scope = json.loads(row[3])
            descriptor = capability_io.capture_descriptor(raw_id, row[0], row[1], json.loads(row[2]), scope)
            if not set(descriptor["scopes"]) <= set(context) or not policy_io.permits(root, descriptor):
                raise PermissionError("UniMem passport capture alias is outside the caller's admitted scope")
        else:
            record = journal.current(raw_id, scope=scope_from_labels(list(context)))
            if record is None:
                raise PermissionError("UniMem passport record alias is unavailable or outside the caller's scope")
        memory_authority._key(root, create=True)
        reference = capability_io.runtime_identity(root, raw_id, capture=capture)
        if memory_authority.lineage_blocked(root, reference):
            raise PermissionError("UniMem passport source alias was erased or revoked")
        with journal.connect(write=True) as db:
            db.execute("INSERT OR IGNORE INTO resource_refs VALUES(?,?,?)", (reference, raw_id, prefix))
        aliases[identity] = reference
    canonical = sorted(aliases.values())
    if len(set(canonical)) != len(canonical):
        raise ValueError("UniMem passport aliases name the same canonical source more than once")
    return canonical, aliases


def export(root: str, identities: Sequence[str], *, model_digest: str, source_agent: str = "local",
           context: Sequence[str] = (), grants: Mapping[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    from commontrace import (
        capabilities,
        capability_io,
        incident,
        lineage,
        origin,
        passport,
        policy_io,
        sentinel_io,
        transparency_log,
    )

    store = _store()
    store.operator()
    fp.count(len(identities), 1, passport.MAX_ENTRIES)
    ids = sorted({fp.text(value) for value in identities})
    if len(ids) != len(identities) or set(grants or {})-set(ids):
        raise ValueError("UniMem passport sources must be distinct and grants must name selected sources")
    passport.model_digest(model_digest)
    with incident.protect(root):
        ids, aliases = _canonical_sources(root, ids, context)
        grants = {aliases[key]: value for key, value in (grants or {}).items()}
        sentinel_io.assert_generalize(root, ids, context=context)
        policy_io.assert_export(root, ids, context=list(context))
        admitted = store.admit(root, ids, context=context)
        if set(admitted) != set(ids):
            raise PermissionError("UniMem passport contains erased, unapproved or out-of-scope sources")
        groups: dict[str, list[dict[str, Any]]] = {kind: [] for kind in passport.COMPONENTS}
        snapshots = {}
        for identity in ids:
            component, content, metadata = _snapshot(root, identity)
            snapshots[identity] = fp.digest(content)
            tokens = list((grants or {}).get(identity, []))
            fp.count(len(tokens), 0, 8)
            for token in tokens:
                verified = capability_io.check(root, token)
                rules = verified["caveats"]
                if not rules["purposes"] or not capabilities.permits(rules,
                        namespace=metadata["namespace"], operation="export",
                        memory_class=metadata["memory_class"], purpose=rules["purposes"][0], labels=metadata["labels"]):
                    raise PermissionError("UniMem segment capability does not authorize the exported source")
            timestamps = metadata.get("timestamps", {
                name: content.get(name) for name in ("valid_from", "valid_until", "expires_at")
                if content.get(name) is not None})
            from commontrace import purpose

            consent = purpose.scope(root, identity)
            groups[component].append({"parent_ids": sorted(set(metadata["parents"])),
                "created_at": metadata["created_at"], "version": metadata["version"], "content": content,
                "provenance": {"source_id": identity, "namespace": metadata["namespace"],
                               "lineage": lineage.record(root, identity)},
                "confidence": metadata["confidence"], "timestamps": timestamps, "consent_scope": consent,
                "labels": sorted(set(metadata["labels"])), "capability_tokens": tokens})
        manifest = passport.build(groups, created_at=fp.now(), source_agent={"name": source_agent,
            "model_digest": model_digest, "model_identity": "operator-declared digest; execution not attested",
            "runtime": "unimem"})
        # Recheck the actual canonical snapshots before binding and publishing.
        if set(store.admit(root, ids, context=context)) != set(ids) or any(
                fp.digest(_snapshot(root, identity)[1]) != digest for identity, digest in snapshots.items()):
            raise PermissionError("UniMem passport source changed during export")
        principal = _identity(root, create=True)
        signed = origin.bind(manifest, principal)
        bundle = {"passport": signed, "id": "sha256:"+fp.digest(manifest)}
        store.put(root, "passports", bundle["id"], bundle, scopes=context, sources=ids, immutable=True)
        anchor = transparency_log.append(root, "person-passport-export", {"passport_digest": fp.digest(bundle),
                                                                         "source_count": len(ids)})
    return {"bundle": bundle, "anchor": anchor, "public_key": principal.public_key.hex(), "source_aliases": aliases,
            "admission": "operator-authorized export; model execution and claim truth are not attested"}


def verify_passport(root: str, bundle: Mapping[str, Any]) -> dict[str, Any]:
    from commontrace import origin, passport

    fp.wire(bundle)
    if set(bundle) != {"passport", "id"} or not isinstance(bundle["passport"], dict):
        raise ValueError("UniMem expected a signed memory passport envelope")
    signed = bundle["passport"]
    issuer = fp.text(signed["principal"], "passport issuer", 512)
    principal = origin.Principal(issuer, "unimem-person-v1", "operator", b"", "ed25519", _pin(root, issuer))
    if not origin.verify(signed, {issuer: principal}):
        raise PermissionError("UniMem passport signature is invalid or its issuer is not pinned")
    manifest = passport.verify(signed["record"])
    if bundle["id"] != "sha256:"+fp.digest(manifest):
        raise PermissionError("UniMem passport manifest address does not match")
    return manifest


def import_passport(root: str, bundle: dict[str, Any], *, context: Sequence[str] = ()) -> dict[str, Any]:
    from commontrace import passport, transparency_log

    store = _store()
    store.operator()
    manifest = verify_passport(root, bundle)
    assessments = []
    for component in passport.COMPONENTS:
        for value in manifest["components"][component]:
            # Scanner findings are detached metadata; the original signed bytes
            # remain exact in the review artifact and are never executed/admitted.
            candidates = passport.stage(fp.wire(value["content"]).decode(),
                                        source="signed-passport:"+fp.digest(bundle["passport"]["principal"])[:16])
            assessments.append({"entry_id": value["id"], "verified_source_signature": True,
                                "status": "review", "screening": [candidate["screening"] for candidate in candidates]})
    labels = sorted(set(context) | {label for kind in passport.COMPONENTS
                                   for value in manifest["components"][kind] for label in value["labels"]})
    identity = "passport-import:"+fp.digest([bundle, labels])
    source_id = "passport-source:"+fp.digest([bundle, labels])
    store.put(root, "person-import-bundles", source_id, bundle, scopes=labels, immutable=True)
    reference = store.reference("person-import-bundles", source_id)
    value = {"bundle_reference": reference, "assessments": assessments, "status": "review", "admitted": False,
        "source_signature": "pinned key verified; imported assertions still require review",
        "capabilities": "source-realm tokens are preserved; the receiving realm must independently authorize them"}
    store.put(root, "person-imports", identity, value, scopes=labels, sources=[reference], immutable=True)
    transparency_log.append(root, "person-passport-import", {"import_digest": fp.digest(value),
                                                             "entry_count": manifest["entry_count"]})
    return {"id": identity, "bundle": bundle, **value}


def stage(root: str, text: str, source: str, *, context: Sequence[str] = ()) -> dict[str, Any]:
    from commontrace import passport, transparency_log

    store = _store()
    store.operator()
    candidates = passport.stage(text, source=source)
    identity = "person-stage:"+fp.digest([source, candidates, list(context)])
    value = {"candidates": candidates, "status": "review", "admitted": False}
    store.put(root, "person-staging", identity, value, scopes=context, immutable=True)
    transparency_log.append(root, "person-import-stage", {"candidate_digest": fp.digest(value),
                                                          "count": len(candidates), "automatic_admission": False})
    return {"id": identity, **value}


def configure_credentials(root: str, status_url: str) -> dict[str, Any]:
    from urllib.parse import urlsplit

    from commontrace import _jsonl, transparency_log

    store = _store()
    store.operator()
    fp.text(status_url, "credential status list URL", 512)
    parsed = urlsplit(status_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("UniMem credential status needs an operator-controlled HTTPS URL without credentials")
    principal = _identity(root, create=True)
    value = {"issuer": principal.id, "key_id": "sha256:"+hashlib.sha256(principal.public_key).hexdigest(),
             "public_key": principal.public_key.hex(), "status_url": status_url}
    with _jsonl.locked(fp.filename(root, "credential-settings.lock")):
        before = store.latest(root, "credential-settings").get("local")
        if before and any(before[key] != value[key] for key in value):
            raise PermissionError("UniMem credential issuer and status-list enrollment are immutable")
        if not before:
            store.event(root, "credential-settings", "local", value)
            transparency_log.append(root, "credential-issuer-enrollment", {"settings_digest": fp.digest(value)})
    return {**value, "status_publication": "publish the signed status output at this URL; no hosting is assumed"}


def _credential_settings(root: str) -> dict[str, Any]:
    settings = _store().latest(root, "credential-settings").get("local")
    if settings is None:
        raise PermissionError("UniMem credential issuer and status URL must be explicitly configured")
    principal = _identity(root)
    if principal.id != settings["issuer"] or principal.public_key.hex() != settings["public_key"]:
        raise PermissionError("UniMem credential private key differs from its pinned enrollment")
    return dict(settings)


def _epoch() -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(fp.now()).timestamp())


def issue(root: str, identity: str, holder_public: str, model_digest: str, *, context: Sequence[str] = (),
          subject: str | None = None, expires_seconds: int = 86400) -> dict[str, Any]:
    import hmac
    import uuid

    from commontrace import (
        _jsonl,
        credentials,
        incident,
        memory_authority,
        passport,
        policy_io,
        transparency_log,
    )

    store = _store()
    store.operator()
    settings = _credential_settings(root)
    model_digest = passport.model_digest(model_digest)
    holder = bytes.fromhex(holder_public)
    fp.count(expires_seconds, 60, 86400*365)
    if len(holder) != 32 or holder.hex() != holder_public:
        raise ValueError("UniMem holder key must be a canonical Ed25519 public key")
    with incident.protect(root), _jsonl.locked(fp.filename(root, "credential-issue.lock")):
        policy_io.assert_export(root, [identity], context=list(context))
        if identity not in store.admit(root, [identity], context=context, action_class="factual"):
            raise PermissionError("UniMem credential source is erased, unapproved or outside the owner's scope")
        fact = _fact(root, identity)
        if fact is None:
            raise ValueError("UniMem this credential profile issues one canonical atomic fact")
        registry = store.latest(root, "credential-index")
        fp.count(len(registry), 0, store.MAX_OBJECTS-1)
        index = len(registry)
        credential_id = "urn:uuid:"+str(uuid.uuid4())
        issued_at = _epoch()
        from commontrace import purpose, ttl

        consent_scope = purpose.scope(root, identity)
        expires_at = issued_at+expires_seconds
        if consent_scope.get("expires_at"):
            expires_at = min(expires_at, int(ttl.parse_expiry(consent_scope["expires_at"]).timestamp()))
        for deadline in (fact.expires_at, fact.valid_until):
            if deadline:
                expires_at = min(expires_at, int(ttl.parse_expiry(deadline).timestamp()))
        if expires_at <= issued_at:
            raise PermissionError("UniMem a credential cannot outlive its canonical source validity")
        claims = {"statement": fact.statement, "confidence": fact.confidence, "sourceId": fact.id,
            "provenance": {"originDigest": fp.digest(fact.origin),
                           "originAuthority": fact.origin.get("authority", "external"),
                           "classification": "issuer assertion of the admitted snapshot; truth not attested"},
            "labels": fact.scopes, "modelDigest": model_digest, "createdAt": fact.created_at,
            "memoryClass": fact.memory_type, "revision": fact.revision,
            "consent": consent_scope, "purposes": consent_scope["purposes"]}
        if subject is not None:
            key = memory_authority._key(root, create=True)
            if key is None:
                raise PermissionError("UniMem subject pseudonym key is unavailable")
            claims["subjectId"] = "urn:unimem:subject:"+hmac.new(key,
                b"unimem-credential-subject-v1\x00"+fp.text(subject, "credential subject", 1024).encode(),
                hashlib.sha256).hexdigest()
        # Reserve a never-reused status index before writing a token. An interrupted
        # reservation stays revoked until its issuance artifact and state commit.
        store.event(root, "credential-index", str(index), {"credential_id": credential_id,
            "source_id": identity, "expires_at_epoch": expires_at})
        token = credentials.issue(claims, issuer=settings["issuer"], kid=settings["key_id"],
            signing_key=_identity(root).key, holder_public=holder, credential_id=credential_id,
            status_url=settings["status_url"], status_index=index, issued_at=issued_at,
            expires_at=expires_at)
        artifact = {"credential_id": credential_id, "issued": token, "status_index": index,
                    "source_id": identity, "issued_at": fp.now()}
        store.put(root, "credential-issuances", credential_id, artifact,
                  scopes=context, sources=[identity], immutable=True)
        store.event(root, "credential-state", credential_id, {"state": "issued", "status_index": index})
        anchor = transparency_log.append(root, "credential-issued", {"credential_digest": fp.digest(token),
            "status_index": index, "source_digest": fp.digest(identity)})
    return {**artifact, "anchor": anchor, "holder_binding_required": True,
            "privacy": "keep the full issued SD-JWT private; present only selected disclosures"}


def revoke(root: str, credential_id: str, *, reason: str) -> dict[str, Any]:
    from commontrace import _jsonl, transparency_log

    store = _store()
    store.operator()
    fp.text(reason, "credential revocation rationale", 2000)
    with _jsonl.locked(fp.filename(root, "credential-issue.lock")):
        before = store.latest(root, "credential-state").get(credential_id)
        if before is None:
            raise ValueError("UniMem credential is not in this issuer's registry")
        if before["state"] != "revoked":
            store.event(root, "credential-state", credential_id, {"state": "revoked",
                "status_index": before["status_index"], "reason_digest": fp.digest(reason)})
            transparency_log.append(root, "credential-revoked", {"credential_digest": fp.digest(credential_id),
                                                                 "status_index": before["status_index"]})
    return {"credential_id": credential_id, "revoked": True, "status_index": before["status_index"]}


def _active_credentials(root: str, now: int) -> tuple[dict[str, Any], set[int]]:
    from commontrace import memory_authority, purpose

    store = _store()
    registry = store.latest(root, "credential-index")
    states = store.latest(root, "credential-state")
    artifacts = store.latest(root, "credential-issuances")
    active = set()
    for text_index, row in registry.items():
        identity = row["credential_id"]
        artifact = artifacts.get(identity, {})
        state = states.get(identity, {})
        if (state.get("state") == "issued" and state.get("status_index") == int(text_index)
                and artifact.get("status") == "active" and now < row["expires_at_epoch"]
                and not memory_authority.lineage_blocked(root, row["source_id"])
                and purpose.scope(root, row["source_id"])["status"] in {"given", "not-configured"}):
            active.add(int(text_index))
    return registry, active


def status(root: str, *, audience: str | None = None, nonce: str | None = None) -> dict[str, Any]:
    """Fresh revocation state; unused indices provide cover for issued credentials."""
    from commontrace import _jsonl, credentials, incident

    settings = _credential_settings(root)
    now = _epoch()
    with incident.protect(root), _jsonl.locked(fp.filename(root, "credential-issue.lock")):
        registry, active = _active_credentials(root, now)
        revoked = [int(index) for index in registry if int(index) not in active]
        token = credentials.status_list(revoked, issuer=settings["issuer"], kid=settings["key_id"],
            signing_key=_identity(root).key, status_url=settings["status_url"], issued_at=now, expires_at=now+60,
            audience=audience, nonce=nonce)
    return {"token": token, "issuer": settings["issuer"], "status_url": settings["status_url"],
            "public_key": settings["public_key"], "valid_seconds": 60,
            "scope": "current local issuer state; external copies need fresh status publication and downstream erasure"}


def _challenges(root: str) -> dict[str, Any]:
    from commontrace import memory_authority

    filename = fp.filename(root, "credential-challenges.json")
    try:
        with open(filename, "rb") as stream:
            raw = stream.read(fp.MAX_BYTES+1)
    except FileNotFoundError:
        return {}
    if len(raw) > fp.MAX_BYTES:
        raise PermissionError("UniMem verifier challenge state exceeds its bound")
    value = json.loads(raw)
    if not memory_authority.verify(root, value) or value["record"].get("type") != "unimem.credential-challenges.v1":
        raise PermissionError("UniMem verifier challenge state signature is invalid")
    challenges = value["record"]["challenges"]
    fp.count(len(challenges), 0, 1024)
    if not isinstance(challenges, dict):
        raise ValueError("UniMem invalid verifier challenge state")
    return dict(challenges)


def challenge(root: str, audience: str) -> dict[str, Any]:
    import secrets

    from commontrace import _jsonl, memory_authority

    fp.text(audience, "credential verifier audience", 512)
    filename = fp.filename(root, "credential-challenges.json")
    nonce = secrets.token_urlsafe(32)
    now = _epoch()
    with _jsonl.locked(filename):
        values = {key: value for key, value in _challenges(root).items() if value["expires"] > now}
        fp.count(len(values), 0, 1023)
        values[fp.digest(nonce)] = {"audience": audience, "issued": now, "expires": now+60,
                                  "requester": list(memory_authority.bound_writer())}
        _jsonl.write_json(filename, fp.sign(root, {"type": "unimem.credential-challenges.v1", "challenges": values}))
    return {"audience": audience, "nonce": nonce, "expires_in": 60,
            "verification": "single use challenge; credential verification is not a memory access grant"}


def verify_credential(root: str, presentation: str, audience: str, nonce: str, *,
                      status_token: str | None = None) -> dict[str, Any]:
    from commontrace import _jsonl, credentials, memory_authority, transparency_log

    fp.text(nonce, "credential verifier nonce", 256)
    fp.text(audience, "credential verifier audience", 512)
    header, unverified, _, _ = credentials._parts(presentation.split("~", 1)[0])
    issuer, kid = unverified["iss"], header["kid"]
    public = _pin(root, issuer)
    if kid != "sha256:"+hashlib.sha256(public).hexdigest():
        raise PermissionError("UniMem credential key id differs from the pinned issuer key")
    own = _store().latest(root, "credential-settings").get("local")
    local = own is not None and own["issuer"] == issuer
    now = _epoch()
    registry, active = _active_credentials(root, now) if local else ({}, set())
    if local:
        status_token = status(root, audience=audience, nonce=nonce)["token"]
        now = _epoch()
    if status_token is None:
        raise PermissionError("UniMem a foreign credential requires a fresh pinned issuer status list")
    keys = {(issuer, kid): public}
    challenge_issued: int
    def is_revoked(entry: dict[str, Any]) -> bool:
        if local:
            text_index = entry["statusListIndex"]
            row = registry.get(text_index)
            if row is None or row["credential_id"] != unverified["id"] or int(text_index) not in active:
                return True
        # The remote list is an explicit verifier input: no untrusted URL fetch.
        _, status_claims, _, _ = credentials._parts(status_token)
        if (now-status_claims["iat"] > 60 or status_claims["iat"] < challenge_issued
                or status_claims.get("aud") != audience or status_claims.get("nonce") != nonce):
            raise PermissionError("UniMem status is old or lacks the verifier's nonce-bound freshness quote")
        return credentials.revoked(status_token, entry, issuer_keys=keys, issuer=issuer, now=now)
    filename = fp.filename(root, "credential-challenges.json")
    with _jsonl.locked(filename):
        values = _challenges(root)
        record = values.get(fp.digest(nonce))
        if (record is None or record["expires"] <= now or record["audience"] != audience
                or record["requester"] != list(memory_authority.bound_writer())):
            raise PermissionError("UniMem credential challenge is expired, used or for another verifier")
        challenge_issued = fp.count(record["issued"], 0, 2**40)
        verified = credentials.verify(presentation, issuer_keys=keys, audience=audience, nonce=nonce, now=now,
                                      is_revoked=is_revoked)
        del values[fp.digest(nonce)]
        _jsonl.write_json(filename, fp.sign(root, {"type": "unimem.credential-challenges.v1", "challenges": values}))
        anchor = transparency_log.append(root, "credential-presentation-verified", {
            "credential_digest": fp.digest(verified["id"]), "nonce_digest": fp.digest(nonce),
            "disclosed_fields": sorted(verified["credentialSubject"])})
    return {"credential": verified, "anchor": anchor, "nonce_consumed": True,
            "scope": "issuer authenticity, disclosure binding and checked status; factual truth is not attested"}


def present(root: str, issued: str, disclose: list[str], holder_key_file: str,
            audience: str, nonce: str) -> dict[str, Any]:
    import os
    import stat

    from commontrace import credentials

    _store().operator()
    fd = os.open(holder_key_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or hasattr(os, "getuid") and info.st_uid != os.getuid()
                or os.name != "nt" and info.st_mode & 0o077):
            raise PermissionError("UniMem holder key requires a private owner-only regular file")
        raw = stream.read(66)
    if len(raw) == 32:
        key = raw
    elif len(raw.strip()) == 64:
        key = bytes.fromhex(raw.strip().decode("ascii"))
    else:
        raise ValueError("UniMem holder key must contain 32 raw bytes or 64 hexadecimal characters")
    return {"presentation": credentials.present(issued, disclose, holder_key=key,
            audience=audience, nonce=nonce, issued_at=_epoch()), "disclosed": sorted(disclose),
            "privacy": "only selected disclosures are sent; keep the issued token and holder key private"}


def holder_key(root: str, output: str) -> dict[str, Any]:
    import os
    import secrets

    from commontrace import credentials, paths

    _store().operator()
    key = secrets.token_bytes(32)
    public = credentials.public_key(key)
    filename = paths.safe_prepare_output_path(output)
    fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(key)
        stream.flush()
        os.fsync(stream.fileno())
    return {"holder_public": public.hex(), "key_file": os.path.abspath(filename),
            "privacy": "private holder key stays in this owner-only file; only the public key is shared"}
