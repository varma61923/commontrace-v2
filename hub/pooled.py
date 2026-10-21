"""UniMem authenticated aggregate contributions; no cross-tenant raw-data reads."""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from starlette.responses import JSONResponse

from hub import transparency
from hub.db import session_scope
from hub.models import PooledContribution
from unimem import frontier_protocol as fp
from unimem import pooled_evidence


def add_routes(app, session_factory, *, authenticate, require_scope, config):
    async def contribute(request):
        principal, denied = await authenticate(request)
        if denied is not None:
            return denied
        denied = require_scope(principal, "admin")
        if denied is not None:
            return denied
        if not config.ledger_signing_key:
            return JSONResponse({"error": "unimem_pool_signer_unconfigured"}, status_code=503)
        card = next((c for c in config.pool_contributors if c["tenant"] == principal.org_id), None)
        if card is None:
            return JSONResponse({"error": "unimem_pool_contributor_unregistered"}, status_code=403)
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 65536:
                    raise ValueError("UniMem contribution exceeds 64 KiB")
                raw.extend(chunk)
            body = json.loads(raw)
            if not isinstance(body, dict) or set(body) != {"signed"}:
                raise ValueError("UniMem contribution request requires a signed envelope")
            signed = body["signed"]
            data = pooled_evidence.verify_contribution(signed, card=card)
            async with session_scope(session_factory, org_id=principal.org_id) as session:
                # Pin ownership in the primary key; untrusted JSON never chooses org_id.
                identity = (principal.org_id, data["release_id"])
                existing = await session.get(PooledContribution, identity)
                if existing:
                    if existing.contribution != signed:
                        return JSONResponse({"error": "unimem_pool_release_conflict"}, status_code=409)
                    return JSONResponse({"accepted": True, "anchor": existing.anchor, "replayed": True})
                anchor = await transparency.append(session, principal.org_id,
                    {"event": "unimem-pooled-contribution-v1", "digest": fp.digest(signed),
                     "unit_hash": data["unit_hash"]}, secret=config.ledger_signing_key,
                    algorithm=config.transparency_algorithm)
                await session.execute(insert(PooledContribution).values(
                    org_id=principal.org_id, release_id=data["release_id"], unit_hash=data["unit_hash"],
                    contribution=signed, anchor=anchor).on_conflict_do_nothing())
                persisted = (await session.execute(select(PooledContribution).where(
                    PooledContribution.org_id == principal.org_id,
                    PooledContribution.release_id == data["release_id"]))).scalar_one()
                if persisted.contribution != signed:
                    raise ValueError("UniMem release identity conflicts with an existing contribution")
                return JSONResponse({"accepted": True, "anchor": persisted.anchor}, status_code=201)
        except PermissionError:
            return JSONResponse({"error": "unimem_pool_signature_denied"}, status_code=403)
        except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
            return JSONResponse({"error": "invalid_unimem_pool_contribution"}, status_code=400)

    app.add_route("/v1/evidence/pool/contributions", contribute, methods=["POST"])
