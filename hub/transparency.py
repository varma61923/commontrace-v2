"""Tenant-scoped transactional Merkle history with operator-pinned signers.

All appends lock the owning organization before allocating the next index.
HMAC deployments need a shared verifier secret; Ed25519 supports public
verification. Pin or witness heads to detect split views across observers.
"""
from __future__ import annotations

import hashlib
import hmac

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from commontrace import frontier_protocol as fp
from commontrace import origin
from commontrace import transparency_log as merkle
from hub.models import Organization, TransparencyEntry, TransparencyHead


def signer(org_id: str, secret: str, algorithm: str = "hmac-sha256") -> origin.Principal:
    if len(secret.encode()) < 32:
        raise ValueError("Hub transparency needs a configured signing key of at least 32 bytes")
    if algorithm not in ("hmac-sha256", "ed25519"):
        raise ValueError("unsupported transparency signature algorithm")
    key = hmac.new(secret.encode(), b"commontrace-transparency-v1\0" + org_id.encode(), hashlib.sha256).digest()
    public = b""
    if algorithm == "ed25519":
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        public = Ed25519PrivateKey.from_private_bytes(key).public_key().public_bytes_raw()
    return origin.Principal("hub-transparency", org_id, "operator", key, algorithm, public)


async def hashes(session: AsyncSession, org_id: str, principal: origin.Principal) -> list[str]:
    result = await session.execute(select(TransparencyEntry).where(
        TransparencyEntry.org_id == org_id).order_by(TransparencyEntry.leaf_index).limit(fp.MAX_ROWS + 1))
    entries = list(result.scalars())
    if len(entries) > fp.MAX_ROWS:
        raise ValueError("transparency journal exceeds read bound; rotate it")
    if any(entry.leaf_index != i or entry.leaf_hash != merkle.leaf_hash(entry.statement)
           or not origin.verify(entry.statement, {principal.id: principal})
           for i, entry in enumerate(entries)):
        raise ValueError("transparency leaf history is corrupt")
    return [entry.leaf_hash for entry in entries]


async def append(session: AsyncSession, org_id: str, statement: dict, *, secret: str,
                 algorithm: str = "hmac-sha256") -> dict:
    identity = signer(org_id, secret, algorithm)
    await session.execute(select(Organization.id).where(Organization.id == org_id).with_for_update())
    leaves = await hashes(session, org_id, identity)
    clock = await session.get(TransparencyHead, org_id)
    if clock and (clock.tree_size != len(leaves) or clock.root_hash != merkle.tree_hash(leaves)
                  or not origin.verify(clock.signed_head, {identity.id: identity})):
        raise ValueError("transparency checkpoint mismatch")
    fp.count(len(leaves), 0, fp.MAX_ROWS - 1)
    signed = origin.bind({"org_id": org_id, "created_at": fp.now(), "statement": statement}, identity)
    leaf = merkle.leaf_hash(signed)
    session.add(TransparencyEntry(org_id=org_id, leaf_index=len(leaves), leaf_hash=leaf, statement=signed))
    leaves.append(leaf)
    root_hash = merkle.tree_hash(leaves)
    checkpoint = origin.bind({"log_id": org_id, "tree_size": len(leaves), "root_hash": root_hash,
                              "created_at": fp.now()}, identity)
    if clock is None:
        session.add(TransparencyHead(org_id=org_id, tree_size=len(leaves), root_hash=root_hash,
                                     signed_head=checkpoint))
    else:
        clock.tree_size, clock.root_hash, clock.signed_head = len(leaves), root_hash, checkpoint
    await session.flush()
    return {"leaf": len(leaves) - 1, "statement": signed, "head": checkpoint,
            "proof": merkle.inclusion(leaves, len(leaves) - 1)}


async def head(session: AsyncSession, org_id: str, *, secret: str, algorithm: str) -> dict:
    clock = await session.get(TransparencyHead, org_id)
    if clock:
        principal = signer(org_id, secret, algorithm)
        leaves = await hashes(session, org_id, principal)
        if (clock.tree_size != len(leaves) or clock.root_hash != merkle.tree_hash(leaves)
                or not origin.verify(clock.signed_head, {principal.id: principal})):
            raise ValueError("transparency checkpoint mismatch")
        return dict(clock.signed_head)
    return origin.bind({"log_id": org_id, "tree_size": 0, "root_hash": merkle.EMPTY,
                        "created_at": fp.now()}, signer(org_id, secret, algorithm))


def add_routes(app, session_factory, *, authenticate, require_scope, config):
    from starlette.responses import JSONResponse

    from hub.db import session_scope

    async def handle(request):
        authenticated, denied = await authenticate(request)
        if denied is not None:
            return denied
        denied = require_scope(authenticated, "read")
        if denied is not None:
            return denied
        if not config.ledger_signing_key:
            return JSONResponse({"error": "transparency_signer_not_configured"}, status_code=503)
        org_id = authenticated.org_id
        try:
            async with session_scope(session_factory, org_id=org_id) as session:
                checkpoint = await head(session, org_id, secret=config.ledger_signing_key,
                                        algorithm=config.transparency_algorithm)
                if request.url.path.endswith("/head"):
                    return JSONResponse(checkpoint)
                leaves = await hashes(session, org_id, signer(org_id, config.ledger_signing_key,
                                                             config.transparency_algorithm))
                if request.url.path.endswith("/proof"):
                    leaf = fp.count(int(request.query_params.get("leaf", "-1")), 0, len(leaves) - 1)
                    entry = await session.get(TransparencyEntry, (org_id, leaf))
                    return JSONResponse({"leaf": leaf, "statement": entry.statement, "head": checkpoint,
                                         "proof": merkle.inclusion(leaves, leaf)})
                start = fp.count(int(request.query_params.get("from", "-1")), 0, len(leaves))
                end = fp.count(int(request.query_params.get("to", str(len(leaves)))), start, len(leaves))
                return JSONResponse({"from": start, "to": end, "old_root": merkle.tree_hash(leaves[:start]),
                                     "root_hash": merkle.tree_hash(leaves[:end]),
                                     "proof": merkle.consistency(leaves[:end], start)})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": "invalid_log_request", "detail": str(exc)}, status_code=400)

    for operation in ("head", "proof", "consistency"):
        app.add_route("/v1/log/" + operation, handle, methods=["GET"])
