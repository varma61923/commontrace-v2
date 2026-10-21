"""Tenant-scoped transactional Merkle history with operator-pinned signers.

Appends lock only the organization's own head row (never the organizations
row, whose FOR UPDATE lock would block every foreign-key insert for the
tenant), then extend a stored Merkle frontier: O(log n) work and constant
reads per append, however long the history grows. The new leaf's inclusion
proof comes from the same frontier. Proof and consistency routes recompute
the tree from the leaf-hash column and check it against the signed head, so
tampering with stored hashes is detected when evidence is exported.

HMAC deployments need a shared verifier secret; Ed25519 supports public
verification. Pin or witness heads to detect split views across observers.
"""
from __future__ import annotations

import hashlib
import hmac

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hub.models import TransparencyEntry, TransparencyHead
from unimem import frontier_protocol as fp
from unimem import origin
from unimem import transparency_log as merkle

# Proof and consistency exports hold one 64-character hash per leaf in memory.
MAX_PROOF_LEAVES = merkle.MAX_TREE_LEAVES


class TransparencyIntegrityError(ValueError):
    """Stored history no longer matches its signed head: a server-side fault, not a bad request."""


def signer(org_id: str, secret: str, algorithm: str = "hmac-sha256") -> origin.Principal:
    if len(secret.encode()) < 32:
        raise ValueError("Hub transparency needs a configured signing key of at least 32 bytes")
    if algorithm not in ("hmac-sha256", "ed25519"):
        raise ValueError("unsupported transparency signature algorithm")
    key = hmac.new(secret.encode(), b"unimem-transparency-v1\0" + org_id.encode(), hashlib.sha256).digest()
    public = b""
    if algorithm == "ed25519":
        public = origin.ed25519_public_bytes(key)
    return origin.Principal("hub-transparency", org_id, "operator", key, algorithm, public)


def _signed_head(org_id: str, size: int, root_hash: str, identity: origin.Principal) -> dict:
    return origin.bind({"log_id": org_id, "tree_size": size, "root_hash": root_hash, "created_at": fp.now()},
                       identity)


def _check(clock: TransparencyHead, identity: origin.Principal) -> None:
    body = clock.signed_head.get("record", {}) if isinstance(clock.signed_head, dict) else {}
    if (not origin.verify(clock.signed_head, {identity.id: identity}) or body.get("tree_size") != clock.tree_size
            or body.get("root_hash") != clock.root_hash):
        raise TransparencyIntegrityError("transparency checkpoint mismatch")


async def hashes(session: AsyncSession, org_id: str, *, limit: int = MAX_PROOF_LEAVES) -> list[str]:
    """Every leaf hash in order (the leaf-hash column only, never the statements)."""
    result = await session.execute(
        select(TransparencyEntry.leaf_index, TransparencyEntry.leaf_hash)
        .where(TransparencyEntry.org_id == org_id).order_by(TransparencyEntry.leaf_index).limit(limit + 1))
    rows = result.all()
    if len(rows) > limit:
        raise ValueError(f"transparency history exceeds {limit} leaves; export proofs offline")
    if any(index != i for i, (index, _) in enumerate(rows)):
        raise TransparencyIntegrityError("transparency leaf indexes are not contiguous")
    return [leaf for _, leaf in rows]


async def _locked_head(session: AsyncSession, org_id: str, identity: origin.Principal) -> TransparencyHead:
    await session.execute(pg_insert(TransparencyHead).values(
        org_id=org_id, tree_size=0, root_hash=merkle.EMPTY, frontier=[],
        signed_head=_signed_head(org_id, 0, merkle.EMPTY, identity)).on_conflict_do_nothing(
        index_elements=[TransparencyHead.org_id]))
    clock = (await session.execute(
        select(TransparencyHead).where(TransparencyHead.org_id == org_id).with_for_update()
        .execution_options(populate_existing=True))).scalar_one()
    _check(clock, identity)
    frontier = clock.frontier
    if frontier is None:
        # Heads written before the frontier column: rebuild it once from the leaf hashes.
        frontier = []
        for leaf in await hashes(session, org_id, limit=max(MAX_PROOF_LEAVES, clock.tree_size)):
            frontier = merkle.frontier_append(frontier, leaf)
    if (not isinstance(frontier, list) or merkle.frontier_size(frontier) != clock.tree_size
            or merkle.frontier_root(frontier) != clock.root_hash):
        raise TransparencyIntegrityError("transparency frontier does not match the signed head")
    clock.frontier = frontier
    return clock


async def append(session: AsyncSession, org_id: str, statement: dict, *, secret: str,
                 algorithm: str = "hmac-sha256") -> dict:
    identity = signer(org_id, secret, algorithm)
    clock = await _locked_head(session, org_id, identity)
    signed = origin.bind({"org_id": org_id, "created_at": fp.now(), "statement": statement}, identity)
    leaf = merkle.leaf_hash(signed)
    index = clock.tree_size
    frontier, proof = merkle.frontier_append_with_proof(clock.frontier, leaf)
    root_hash = merkle.frontier_root(frontier)
    session.add(TransparencyEntry(org_id=org_id, leaf_index=index, leaf_hash=leaf, statement=signed))
    checkpoint = _signed_head(org_id, index + 1, root_hash, identity)
    clock.tree_size, clock.root_hash, clock.signed_head, clock.frontier = index + 1, root_hash, checkpoint, frontier
    await session.flush()
    return {"leaf": index, "statement": signed, "head": checkpoint, "proof": proof}


async def head(session: AsyncSession, org_id: str, *, secret: str, algorithm: str) -> dict:
    principal = signer(org_id, secret, algorithm)
    clock = await session.get(TransparencyHead, org_id)
    if clock is None:
        return _signed_head(org_id, 0, merkle.EMPTY, principal)
    _check(clock, principal)
    return dict(clock.signed_head)


async def verified_hashes(session: AsyncSession, org_id: str, *, secret: str, algorithm: str) -> tuple[dict, list[str]]:
    """The signed head and the leaf hashes it commits to, recomputed and compared."""
    checkpoint = await head(session, org_id, secret=secret, algorithm=algorithm)
    leaves = await hashes(session, org_id)
    body = checkpoint["record"]
    if len(leaves) != body["tree_size"] or merkle.tree_hash(leaves) != body["root_hash"]:
        raise TransparencyIntegrityError("transparency leaf history does not match the signed head")
    return checkpoint, leaves


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
        options = {"secret": config.ledger_signing_key, "algorithm": config.transparency_algorithm}
        try:
            async with session_scope(session_factory, org_id=org_id) as session:
                if request.url.path.endswith("/head"):
                    return JSONResponse(await head(session, org_id, **options))
                checkpoint, leaves = await verified_hashes(session, org_id, **options)
                if request.url.path.endswith("/proof"):
                    leaf = fp.count(int(request.query_params.get("leaf", "-1")), 0, len(leaves) - 1, "leaf")
                    entry = await session.get(TransparencyEntry, (org_id, leaf))
                    principal = signer(org_id, **options)
                    if (entry is None or merkle.leaf_hash(entry.statement) != leaves[leaf]
                            or not origin.verify(entry.statement, {principal.id: principal})):
                        raise TransparencyIntegrityError("transparency statement does not match its leaf")
                    return JSONResponse({"leaf": leaf, "statement": entry.statement, "head": checkpoint,
                                         "proof": merkle.inclusion(leaves, leaf)})
                start = fp.count(int(request.query_params.get("from", "-1")), 0, len(leaves), "from")
                end = fp.count(int(request.query_params.get("to", str(len(leaves)))), start, len(leaves), "to")
                return JSONResponse({"from": start, "to": end, "old_root": merkle.tree_hash(leaves[:start]),
                                     "root_hash": merkle.tree_hash(leaves[:end]),
                                     "proof": merkle.consistency(leaves[:end], start)})
        except TransparencyIntegrityError as exc:
            return JSONResponse({"error": "transparency_history_unverifiable", "detail": str(exc)}, status_code=503)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": "invalid_log_request", "detail": str(exc)}, status_code=400)

    for operation in ("head", "proof", "consistency"):
        app.add_route("/v1/log/" + operation, handle, methods=["GET"])
