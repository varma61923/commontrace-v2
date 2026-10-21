"""Tenant-bound reviewed finite policies, independent of any filesystem replica.

The database and authenticated application context are trusted. External relation
services and their changing state are outside the exhaustive finite proof.
"""
from __future__ import annotations

import json
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, false, or_, select, true
from sqlalchemy.dialects.postgresql import insert
from starlette.responses import JSONResponse

from unimem import frontier_protocol as fp
from unimem import origin, policy_proof
from hub import transparency
from hub.db import session_scope
from hub.models import MemoryPolicyState, TransparencyEntry

ACTOR: ContextVar[str] = ContextVar("unimem_hub_policy_actor", default="service:internal")
SNAPSHOT: ContextVar[tuple[str, Any] | None] = ContextVar("unimem_hub_policy_snapshot", default=None)


async def active(session, org_id):
    row = await session.get(MemoryPolicyState, org_id)
    if row is None or row.generation == 0:
        SNAPSHOT.set((org_id, None))
        return None
    if (not isinstance(row.activation, dict) or not isinstance(row.policy, dict)
            or row.activation.get("record", {}).get("generation") != row.generation
            or row.activation["record"].get("policy_hash") != fp.digest(row.policy)):
        raise PermissionError("UniMem Hub active policy commitment differs from its canonical state")
    policy = policy_proof.normalize(row.policy)
    SNAPSHOT.set((org_id, policy))
    return policy


def permits(policy, record, *, action="read"):
    if policy is None:
        return True
    from unimem import capabilities, capability_io

    delegated = capabilities.CURRENT.get()
    purpose = "default"
    actor = ACTOR.get()
    if delegated is not None:
        value = capability_io.current(delegated["root"])
        actor, purpose = "agent:"+value["principal"], value["purpose"]
    labels = record.get("scopes", [])
    if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
        return False
    return policy_proof._allows(policy, actor, action,
        record.get("memory_type") or record.get("kind") or "general", tuple(sorted(set(labels))), purpose)


def trace_predicate(policy, *, action="read"):
    from hub.models import Trace

    if policy is None:
        return true()
    labels = [values for values in policy["domain"]["label_sets"]
              if permits(policy, {"memory_type": "trace", "scopes": values}, action=action)]
    return or_(*(and_(Trace.scopes.contains(values), Trace.scopes.contained_by(values))
                 for values in labels)) if labels else false()


def record_permits(record, *, action="read"):
    from unimem.capability_io import runtime_descriptor

    snapshot = SNAPSHOT.get()
    return snapshot is None or permits(snapshot[1], runtime_descriptor(record), action=action)


async def _locked(session, org_id):
    await session.execute(insert(MemoryPolicyState).values(org_id=org_id, generation=0).on_conflict_do_nothing())
    return (await session.execute(select(MemoryPolicyState).where(MemoryPolicyState.org_id == org_id)
                                  .with_for_update().execution_options(populate_existing=True))).scalar_one()


async def _verified(session, org_id, envelope, identity, event, field):
    fp.wire(envelope)
    signed, anchor = envelope[field], envelope["anchor"]
    body = signed.get("record", {})
    if (not origin.verify(signed, {identity.id: identity}) or signed.get("organization") != org_id
            or body.get("type") != event or body.get("version") != 1
            or not fp.moment(body["issued_at"]) <= fp.now() < fp.moment(body["expires_at"])):
        raise PermissionError("UniMem policy envelope is forged, expired or belongs to another tenant")
    entry = await session.get(TransparencyEntry, (org_id, anchor["leaf"]))
    if (entry is None or entry.statement != anchor["statement"]
            or entry.statement["record"]["statement"] != {"event": event, "digest": fp.digest(signed)}):
        raise PermissionError("UniMem policy envelope is not anchored in this tenant's transparency log")
    return body


def _times(seconds):
    fp.count(seconds, 60, 86400)
    issued = fp.now()
    return {"issued_at": issued,
            "expires_at": (datetime.fromisoformat(issued)+timedelta(seconds=seconds)).isoformat()}


def add_routes(app, session_factory, *, authenticate, require_scope, config):
    options = {"secret": config.ledger_signing_key, "algorithm": config.transparency_algorithm}

    async def handle(request):
        principal, denied = await authenticate(request)
        if denied is not None:
            return denied
        denied = require_scope(principal, "admin")
        if denied is not None:
            return denied
        if not config.ledger_signing_key:
            return JSONResponse({"error": "unimem_policy_signer_unconfigured"}, status_code=503)
        operation = request.path_params["operation"]
        if operation not in {"current", "diff", "certify", "review", "activate"}:
            return JSONResponse({"error": "unknown_policy_operation"}, status_code=404)
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw)+len(chunk) > 512*1024:
                    raise ValueError("UniMem policy request exceeds 512 KiB")
                raw.extend(chunk)
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("UniMem expected a policy request object")
            allowed = {"current": set(), "diff": {"policy", "format", "domain"},
                       "certify": {"policy", "format", "domain", "expires_seconds"},
                       "review": {"certificate", "reason", "expires_seconds"},
                       "activate": {"certificate", "review_receipt"}}[operation]
            if set(body)-allowed:
                raise ValueError("UniMem unknown policy request fields")
            identity = transparency.signer(principal.org_id, **options)
            async with session_scope(session_factory, org_id=principal.org_id) as session:
                state = await _locked(session, principal.org_id)
                before = await active(session, principal.org_id)
                if operation == "current":
                    return JSONResponse({"policy": before, "generation": state.generation})
                if operation in {"diff", "certify"}:
                    from unimem.policy_io import _candidate

                    proposed, analysis = _candidate(body["policy"], body.get("format", "unimem"), body.get("domain"))
                    if len(fp.wire(proposed)) > 128*1024:
                        raise ValueError("UniMem finite policy exceeds 128 KiB")
                    difference = policy_proof.difference(before or policy_proof.deny(proposed["domain"]), proposed)
                    if operation == "diff":
                        return JSONResponse({"difference": difference, "generation": state.generation,
                                             "cedar_analysis": analysis})
                    data = {"type": "unimem.policy.change", "version": 1, "policy": proposed,
                            "difference": difference, "generation": state.generation, "cedar_analysis": analysis,
                            **_times(body.get("expires_seconds", 3600))}
                    signed = origin.bind(data, identity)
                    anchor = await transparency.append(session, principal.org_id,
                        {"event": data["type"], "digest": fp.digest(signed)}, **options)
                    return JSONResponse({"certificate": signed, "anchor": anchor})
                certificate = body["certificate"]
                change = await _verified(session, principal.org_id, certificate, identity,
                                         "unimem.policy.change", "certificate")
                proposed = policy_proof.normalize(change["policy"])
                difference = policy_proof.difference(before or policy_proof.deny(proposed["domain"]), proposed)
                if state.generation != change["generation"] or difference != change["difference"]:
                    return JSONResponse({"error": "stale_policy_certificate"}, status_code=409)
                if operation == "review":
                    fp.text(body["reason"], "policy review rationale", 2000)
                    data = {"type": "unimem.policy.widening-review", "version": 1,
                        "certificate_digest": fp.digest(certificate["certificate"]),
                        "generation": state.generation, "old_hash": difference["old_hash"],
                        "new_hash": difference["new_hash"], "reviewer": ACTOR.get(),
                        "reason_digest": fp.digest(body["reason"]), **_times(body.get("expires_seconds", 3600))}
                    signed = origin.bind(data, identity)
                    anchor = await transparency.append(session, principal.org_id,
                        {"event": data["type"], "digest": fp.digest(signed)}, **options)
                    return JSONResponse({"receipt": signed, "anchor": anchor})
                review = body.get("review_receipt")
                if not difference["no_widening"]:
                    if not isinstance(review, dict):
                        raise PermissionError("UniMem widening requires a content-bound admin review receipt")
                    receipt = await _verified(session, principal.org_id, review, identity,
                                              "unimem.policy.widening-review", "receipt")
                    if (receipt["certificate_digest"] != fp.digest(certificate["certificate"])
                            or receipt["generation"] != state.generation
                            or any(receipt[key] != difference[key] for key in ("old_hash", "new_hash"))):
                        raise PermissionError("UniMem review receipt covers a different policy change")
                state.generation += 1
                state.policy = proposed
                state.activation = origin.bind({"type": "unimem.policy.activation", "version": 1,
                    "generation": state.generation, "policy_hash": fp.digest(proposed),
                    "certificate_digest": fp.digest(certificate["certificate"]),
                    "review_digest": fp.digest(review) if review else None, "issued_at": fp.now()}, identity)
                anchor = await transparency.append(session, principal.org_id,
                    {"event": "unimem.policy.activation", "digest": fp.digest(state.activation)}, **options)
                return JSONResponse({"activation": state.activation, "anchor": anchor})
        except PermissionError:
            return JSONResponse({"error": "unimem_policy_review_or_signature_denied"}, status_code=403)
        except ImportError:
            return JSONResponse({"error": "unimem_policy_cedar_extra_unavailable"}, status_code=503)
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, RecursionError):
            return JSONResponse({"error": "invalid_unimem_policy_request"}, status_code=400)

    app.add_route("/v1/policy/{operation}", handle, methods=["POST"])
