"""Authenticated tenant measurement enrollment and transactional one-use release."""
from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from commontrace import attested_keys as keys
from commontrace import frontier_protocol as fp
from commontrace import origin
from commontrace import transparency_log as merkle
from hub import transparency
from hub.db import session_scope
from hub.models import AttestationChallenge, AttestedMeasurement, TransparencyEntry


def install_services(app: Any, provider: Any, verifiers: Mapping[str, keys.Verifier], *,
                     allow_simulated: bool = False) -> None:
    """Trusted application composition boundary; never accepts request parameters."""
    if any(name != verifier.backend for name, verifier in verifiers.items()):
        raise ValueError("UniMem attestation verifier registry keys must match their backend")
    if not allow_simulated and any(verifier.simulated for verifier in verifiers.values()):
        raise PermissionError("UniMem production attestation cannot enroll simulated verifier keys")
    app.state.attested_key_services = (provider, dict(verifiers), bool(allow_simulated))


def configure(app: Any, filename: str) -> None:
    if not filename:
        return
    path = Path(filename)
    metadata = path.stat() if path.exists() else None
    if (not path.is_absolute() or path.is_symlink() or metadata is None or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022):
        raise PermissionError("UniMem attestation configuration must be a deployment-owned absolute regular file")
    with path.open("rb") as stream:
        raw = stream.read(1024*1024+1)
    if len(raw) > 1024*1024:
        raise ValueError("UniMem attestation configuration exceeds 1 MiB")
    document = json.loads(raw)
    if not isinstance(document, dict) or set(document)-{"maa", "nvidia", "tenant_keys", "kms_region"}:
        raise ValueError("UniMem unsupported attestation deployment configuration")
    import jwt

    from hub.kms import TenantKeyProvider

    verifiers: dict[str, keys.Verifier] = {}
    for backend, options in document.get("maa", {}).items():
        pinned = {jwk["kid"]: jwt.PyJWK.from_dict(jwk, algorithm="RS256").key for jwk in options["jwks"]["keys"]}
        verifiers[backend] = keys.AzureVerifier(backend, issuer=options["issuer"], keys=pinned,
                                               policy_hash=options["policy_hash"])
    gpu = document.get("nvidia")
    if gpu:
        cpu_verifier = verifiers.get(gpu["cpu_backend"])
        if not isinstance(cpu_verifier, keys.AzureVerifier):
            raise ValueError("UniMem NVIDIA GPU attestation requires a pinned MAA CPU verifier")
        verifiers["nvidia-gpu"] = keys.NvidiaVerifier(gpu["program"], gpu["binary_digest"], cpu_verifier)
    provider = TenantKeyProvider(document.get("tenant_keys", {}), region=document.get("kms_region"))
    install_services(app, provider, verifiers)


async def enrolled(session: AsyncSession, org: str, measurement: str, options: Mapping[str, str], *,
                   lock: bool = False) -> AttestedMeasurement:
    query = select(AttestedMeasurement).where(AttestedMeasurement.org_id == org,
                                             AttestedMeasurement.measurement == keys.digest(measurement))
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = (await session.execute(query)).scalar_one_or_none()
    if row is None or row.revoked or fp.digest(keys.profile(row.profile)) != measurement:
        raise PermissionError("UniMem measurement is unregistered, revoked or inconsistent")
    anchor = row.receipt
    entry = await session.get(TransparencyEntry, (org, anchor["leaf"]))
    identity = transparency.signer(org, **options)
    signed_head = anchor.get("head", {})
    head_body = signed_head.get("record", {})
    expected = {"event": "attested-measurement", "measurement": measurement,
                "profile": row.profile, "revoked": False}
    if (entry is None or entry.statement != anchor["statement"]
            or not origin.verify(entry.statement, {identity.id: identity})
            or entry.statement["record"]["statement"] != expected
            or entry.leaf_hash != merkle.leaf_hash(entry.statement)
            or not origin.verify(signed_head, {identity.id: identity})
            or head_body.get("log_id") != org or type(head_body.get("tree_size")) is not int
            or head_body["tree_size"] <= anchor["leaf"]
            or not merkle.verify_inclusion(entry.leaf_hash, anchor["leaf"], head_body["tree_size"],
                anchor.get("proof", []), head_body.get("root_hash", ""))):
        raise PermissionError("UniMem measurement has no authentic enrollment in this tenant's log")
    return row


async def _body(request: Request) -> dict[str, Any]:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw)+len(chunk) > 512*1024:
            raise ValueError("UniMem attestation request exceeds 512 KiB")
        raw.extend(chunk)
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise ValueError("UniMem attestation request must be an object")
    return body


def add_routes(app: Any, session_factory: async_sessionmaker[AsyncSession], *, authenticate: Any,
               require_scope: Any, config: Any) -> None:
    configure(app, config.attestation_config)
    options = {"secret": config.ledger_signing_key, "algorithm": config.transparency_algorithm}

    async def handle(request: Request) -> Response:
        principal, denied = await authenticate(request)
        if denied is not None:
            return cast(Response, denied)
        denied = require_scope(principal, "admin")
        if denied is not None:
            return cast(Response, denied)
        if not config.ledger_signing_key:
            return JSONResponse({"error": "attestation_signer_unconfigured"}, status_code=503)
        operation = request.path_params["operation"]
        try:
            body = await _body(request)
            allowed = {"measurements-register": {"profile", "revoke"}, "measurements-list": set(),
                       "challenge": {"measurement", "worker_key", "operation", "payload_digest", "participants"},
                       "key-release": {"request", "evidence"}}
            if operation not in allowed:
                return JSONResponse({"error": "unknown_attestation_operation"}, status_code=404)
            if set(body)-allowed[operation]:
                raise ValueError("UniMem unknown attestation fields; tenant identity comes from authentication")
            org = principal.org_id
            if operation == "measurements-list":
                async with session_scope(session_factory, org_id=org) as session:
                    rows = (await session.execute(select(AttestedMeasurement).where(
                        AttestedMeasurement.org_id == org).order_by(AttestedMeasurement.measurement)
                        .limit(1025))).scalars()
                    values = [{"measurement": row.measurement, "profile": row.profile,
                               "revoked": row.revoked, "anchor": row.receipt} for row in rows]
                    if len(values) > 1024:
                        raise ValueError("UniMem measurement catalog exceeds its export bound")
                    return JSONResponse({"measurements": values})
            if operation == "measurements-register":
                build = keys.profile(body["profile"])
                measurement = fp.digest(build)
                revoke = body.get("revoke", False)
                if type(revoke) is not bool:
                    raise ValueError("UniMem revocation requires a boolean")
                async with session_scope(session_factory, org_id=org) as session:
                    # Conflict insertion and row locking make concurrent registration
                    # and revocation monotone across all Hub processes.
                    await session.execute(insert(AttestedMeasurement).values(org_id=org, measurement=measurement,
                        profile=build, revoked=revoke, receipt={}).on_conflict_do_nothing())
                    row = (await session.execute(select(AttestedMeasurement).where(
                        AttestedMeasurement.org_id == org, AttestedMeasurement.measurement == measurement)
                        .with_for_update().execution_options(populate_existing=True))).scalar_one()
                    if not row.receipt or revoke and not row.revoked:
                        row.revoked = bool(row.revoked or revoke)
                        row.receipt = await transparency.append(session, org, {"event": "attested-measurement",
                            "measurement": measurement, "profile": build, "revoked": row.revoked}, **options)
                    return JSONResponse({"measurement": measurement, "revoked": row.revoked, "anchor": row.receipt})
            services = getattr(app.state, "attested_key_services", None)
            if services is None:
                return JSONResponse({"error": "attested_key_provider_unconfigured"}, status_code=503)
            provider, verifiers, simulated = services
            if operation == "challenge":
                value = keys.request(org, **body)
                async with session_scope(session_factory, org_id=org) as session:
                    row = await enrolled(session, org, value["measurement"], options, lock=True)
                    if row.profile["backend"] not in verifiers:
                        return JSONResponse({"error": "attestation_verifier_unconfigured"}, status_code=503)
                    now = datetime.now(timezone.utc)
                    await session.execute(delete(AttestationChallenge).where(
                        AttestationChallenge.org_id == org, AttestationChallenge.expires_at <= now))
                    count = await session.scalar(select(func.count()).select_from(AttestationChallenge).where(
                        AttestationChallenge.org_id == org))
                    if count is None or count >= 1024:
                        return JSONResponse({"error": "attestation_challenge_limit"}, status_code=429)
                    session.add(AttestationChallenge(org_id=org, nonce=value["nonce"], request=value,
                        expires_at=datetime.fromisoformat(fp.moment(value["expires_at"])), consumed=False))
                    return JSONResponse({"request": value, "anchor": row.receipt})
            value, evidence = body["request"], body["evidence"]
            keys.validate_request(value)
            if value["tenant"] != org:
                raise PermissionError("UniMem quote tenant differs from authenticated key owner")
            # Verify expensive hardware appraisal before acquiring the release lock.
            async with session_scope(session_factory, org_id=org) as session:
                enrollment = await enrolled(session, org, value["measurement"], options)
                build = dict(enrollment.profile)
            verifier = verifiers.get(build["backend"])
            if verifier is None:
                return JSONResponse({"error": "attestation_verifier_unconfigured"}, status_code=503)
            appraisal = await asyncio.to_thread(keys.verify_quote, value, evidence, build, verifier,
                                                allow_simulated=simulated)
            # Commit consumption before KMS is called. A crash burns this nonce;
            # retry needs fresh evidence, never a replayed unwrap.
            async with session_scope(session_factory, org_id=org) as session:
                await enrolled(session, org, value["measurement"], options, lock=True)
                challenge = (await session.execute(select(AttestationChallenge).where(
                    AttestationChallenge.org_id == org, AttestationChallenge.nonce == value["nonce"])
                    .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
                if challenge is None or challenge.consumed or challenge.request != value:
                    raise PermissionError("UniMem attestation nonce is missing, replayed or binds another request")
                keys.validate_request(value)
                challenge.consumed = True
            async with session_scope(session_factory, org_id=org) as session:
                # Revocation and actual unwrap serialize on the measurement row.
                await enrolled(session, org, value["measurement"], options, lock=True)
                keys.validate_request(value)
                key = await asyncio.to_thread(provider.unwrap, org)
                envelope = keys.seal_key(key, value)
                anchor = await transparency.append(session, org, {"event": "attested-key-release", **appraisal,
                    "envelope_digest": fp.digest(envelope)}, **options)
                return JSONResponse({"envelope": envelope, "attestation": appraisal, "anchor": anchor})
        except PermissionError:
            return JSONResponse({"error": "attestation_denied"}, status_code=403)
        except (KeyError, ValueError, TypeError, OverflowError, RecursionError):
            return JSONResponse({"error": "invalid_attestation_request"}, status_code=400)
        except (ImportError, RuntimeError):
            return JSONResponse({"error": "attestation_backend_unavailable"}, status_code=503)

    app.add_route("/v1/attestation/{operation}", handle, methods=["POST"])
