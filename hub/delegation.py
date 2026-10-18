"""UniMem capability narrowing after Hub authentication, with shared replay state."""
from __future__ import annotations

import base64
import contextvars
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, delete, false, select, text, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse

from commontrace import capabilities as cap
from commontrace import capability_io as io
from commontrace import frontier_protocol as fp
from hub.db import session_scope

ROOT = contextvars.ContextVar("unimem_hub_capability_root", default="")


class Boundary:
    """Bound delegated contexts to one HTTP request, including deferred egress."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        current, sticky, root = cap.CURRENT.set(None), cap.STICKY.set(()), ROOT.set("")
        from hub import policies

        policy_actor = policies.ACTOR.set("service:internal")
        policy_snapshot = policies.SNAPSHOT.set(None)
        started = False
        async def guarded_send(message):
            nonlocal started
            authorization = scope.get("unimem.authorization")
            if authorization and message["type"] in {"http.response.start", "http.response.body"}:
                if cap.CURRENT.get() is None:
                    with io.activate(authorization):
                        io.current(authorization["root"])
                else:
                    io.current(authorization["root"])
            await send(message)
            started |= message["type"] == "http.response.start"
        try:
            await self.app(scope, receive, guarded_send)
        except PermissionError:
            if started:
                raise
            await JSONResponse({"error": "forbidden", "detail": "delegation expired or was revoked before delivery"},
                               status_code=403)(scope, receive, send)
        finally:
            policies.SNAPSHOT.reset(policy_snapshot)
            policies.ACTOR.reset(policy_actor)
            ROOT.reset(root)
            cap.STICKY.reset(sticky)
            cap.CURRENT.reset(current)


def install(app, config):
    # Policy identity must also be isolated when delegation is not configured.
    app.user_middleware[:] = [entry for entry in app.user_middleware if entry.cls is not Boundary]
    app.add_middleware(Boundary)


def _decode(encoded, bound):
    if not isinstance(encoded, str) or not encoded or len(encoded) > bound:
        raise ValueError("UniMem invalid capability header size")
    try:
        result = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True))
    except RecursionError:
        raise ValueError("UniMem capability header nesting exceeds its bound") from None
    if not isinstance(result, dict):
        raise ValueError("UniMem capability header must encode an object")
    return result


def _operation(request, raw, config):
    path, method = request.url.path, request.method
    if path == config.streamable_http_path and method == "POST":
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("UniMem delegated MCP requests must be single JSON-RPC objects")
        params = payload.get("params", {})
        name = params.get("name") if isinstance(params, dict) else None
        operation = {"search_traces": "read", "get_trace": "read", "contribute_trace": "write",
                     "amend_trace": "write", "delete_trace": "forget"}.get(name)
        if payload.get("method") in {"initialize", "notifications/initialized", "ping", "tools/list",
                                     "notifications/cancelled"}:
            operation = "read"
    else:
        operation = {("POST", "/api/v1/traces/search"): "read", ("POST", "/api/v1/traces"): "write",
                     ("POST", "/v1/memory/record-search"): "read", ("POST", "/v1/memory/record-put"): "write",
                     ("POST", "/v1/memory/record-forget"): "forget",
                     ("POST", "/api/v1/memory/records/search"): "read",
                     ("POST", "/api/v1/memory/records/put"): "write",
                     ("POST", "/api/v1/memory/records/forget"): "forget"}.get((method, path))
    if operation is None:
        raise PermissionError("UniMem this Hub operation does not support delegated capabilities")
    return operation


async def authorize(request, authenticated, config, factory):
    """Run only after existing primary authentication and request rate limits."""
    encoded = request.headers.get("x-unimem-capability", "")
    possession = request.headers.get("x-unimem-capability-proof", "")
    if not config.capability_directory:
        if encoded or possession:
            raise PermissionError("UniMem Hub capability directory is not configured")
        return
    directory = os.path.realpath(config.capability_directory)
    root = os.path.join(directory, str(authenticated.org_id))
    if os.path.dirname(os.path.realpath(root)) != directory:
        raise PermissionError("UniMem invalid authenticated tenant directory")
    ROOT.set(root)
    await drain(factory, authenticated.org_id, root)
    if not encoded and not possession:
        if io.required(root, authenticated.key_prefix):
            raise PermissionError("UniMem enrolled delegate must supply capability and possession headers")
        return
    token, proof = _decode(encoded, 96 * 1024), _decode(possession, 8192)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > config.max_request_body_bytes:
            raise ValueError("UniMem delegated request body exceeds configured bound")
        chunks.append(chunk)
    raw = b"".join(chunks)
    request._body = raw
    purpose = request.headers.get("x-unimem-purpose", "")
    target = request.scope.get("raw_path", request.url.path.encode()).decode("ascii")
    if request.scope.get("query_string"):
        target += "?" + request.scope["query_string"].decode("ascii")
    authorization = io.authenticate(root, token, proof,
        request_digest=cap.request_digest(request.method, target, raw, purpose=purpose),
        operation=_operation(request, raw, config), purpose=purpose,
        principal=authenticated.key_prefix, tenant_id=str(authenticated.org_id), consume=False)
    from hub.models import CapabilityNonce

    async with session_scope(factory, org_id=authenticated.org_id) as session:
        now, expiry = datetime.now(timezone.utc), cap.moment(authorization["request_expires_at"])
        if expiry <= now:
            raise PermissionError("UniMem possession proof expired before nonce consumption")
        await session.execute(delete(CapabilityNonce).where(CapabilityNonce.org_id == authenticated.org_id,
                                                           CapabilityNonce.expires_at <= now))
        consumed = await session.scalar(insert(CapabilityNonce).values(org_id=authenticated.org_id,
            nonce_id=authorization["nonce_id"], expires_at=expiry).on_conflict_do_nothing().returning(
                CapabilityNonce.nonce_id))
        if consumed is None:
            raise PermissionError("UniMem capability request nonce was already consumed")
    cap.CURRENT.set(authorization)
    request.scope["unimem.authorization"] = authorization


async def admit_request(request, authenticated, config, factory):
    try:
        await authorize(request, authenticated, config, factory)
        from hub import policies

        policies.ACTOR.set("key:"+authenticated.key_id if authenticated.key_id else authenticated.key_prefix)
    except PermissionError as exc:
        return JSONResponse({"error": "forbidden", "detail": str(exc)}, status_code=403)
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)
    except (OSError, RuntimeError, SQLAlchemyError):
        return JSONResponse({"error": "unavailable", "detail": "delegation metadata is unavailable"}, status_code=503)
    return None


def trace_predicate(*, operation="read"):
    from hub import policies
    from hub.models import Trace

    root = ROOT.get()
    snapshot = policies.SNAPSHOT.get()
    policy_clause = (policies.trace_predicate(snapshot[1], action=operation)
                     if snapshot and operation != "forget" else true())
    if not root:
        return policy_clause
    value = io.current(root)
    clauses = []
    if value:
        rules = value["verified"]["caveats"]
        if not cap.permits(rules, namespace="traces", operation=operation, memory_class="general",
                           purpose=value["purpose"], labels=[]):
            return false()
        clauses.append(Trace.scopes.contained_by(rules["labels"]))
    if operation != "forget" and io.enabled(root):
        blocked = []
        for identity in io._quarantine_snapshot(root):
            try:
                blocked.append(str(uuid.UUID(identity)))
            except ValueError:
                continue
        if blocked:
            clauses.append(Trace.id.not_in(blocked))
    return and_(policy_clause, *clauses)


def record_predicate(*, operation="read"):
    from hub.models import MemoryRecordProjection as Record

    root = ROOT.get()
    if not root:
        return true()
    value = io.current(root)
    clauses = []
    if value:
        from commontrace.record_visibility import scope_from_labels

        rules = value["verified"]["caveats"]
        if "records" not in rules["namespaces"] or operation not in rules["operations"]:
            return false()
        clauses.extend([Record.record["kind"].astext.in_(rules["classes"]),
                        Record.scope.op("<@")(scope_from_labels(rules["labels"]))])
    # Qualified runtime IDs are admitted by _cap_allowed_ids before sparse/ANN top-k.
    return and_(*clauses) if clauses else true()


async def queue(session, org_id, signed):
    if signed is None:
        return
    from hub.models import CapabilityAttestation

    await session.execute(insert(CapabilityAttestation).values(org_id=org_id, event_id=fp.digest(signed),
        body=signed, created_at=datetime.now(timezone.utc)).on_conflict_do_nothing())
    session.info["capability_outbox"] = (org_id, ROOT.get())


async def drain(factory, org_id, root):
    from commontrace import lineage
    from hub.models import CapabilityAttestation

    async with session_scope(factory, org_id=org_id) as session:
        # Shared DB fence preserves order across worker processes and filesystem drainer retries.
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:owner, 61924))"),
                              {"owner": "unimem-delegation:" + str(org_id)})
        rows = (await session.execute(select(CapabilityAttestation).where(
            CapabilityAttestation.org_id == org_id).order_by(CapabilityAttestation.created_at,
                                                           CapabilityAttestation.event_id).limit(10001))).scalars().all()
        fp.count(len(rows), 0, 10000, "UniMem Hub delegation outbox maintenance bound")
        for row in rows:
            lineage.commit_prepared(root, row.body)
            await session.delete(row)


async def after_commit(factory, org_id, root):
    try:
        await drain(factory, org_id, root)
    except (OSError, ValueError, PermissionError, RuntimeError, SQLAlchemyError):
        logging.getLogger(__name__).warning("UniMem committed Hub delegation metadata awaits recovery")


def idempotency(key):
    value = cap.CURRENT.get()
    if key is None or value is None:
        return key
    return "delegated-" + fp.digest([value["verified"]["chain_digest"], fp.text(key, "idempotency key", 128)])


async def stamp_trace(session, org_id, trace, *, parent=""):
    if cap.CURRENT.get() is None:
        return
    from commontrace import lineage

    root = ROOT.get()
    body = {"id": trace.id, "context": trace.context_text, "solution": trace.solution_text,
            "scopes": list(trace.scopes or []), "outcome": trace.outcome, "memory_type": "general"}
    effective = io.before_write(root, body, namespace="traces")
    receipt = fp.sign(root, body)
    profile = dict((trace.extensions or {}).get("profile", {}))
    profile.update(io.delegation(root, body, namespace="traces"))
    profile["origin"] = {"principal": effective[0], "authority": effective[1], "receipt": receipt}
    trace.extensions = {**(trace.extensions or {}), "profile": profile}
    await queue(session, org_id, lineage.prepare(root, body, receipt,
                                               inputs=[parent] if parent else [], operation="trace"))


async def stamp_record(session, org_id, record):
    if cap.CURRENT.get() is None:
        return
    from commontrace import lineage

    root = ROOT.get()
    body = io.runtime_descriptor(record, root=root)
    inputs = sorted({io.runtime_identity(root, source) for ids in record.relations.values() for source in ids})
    await queue(session, org_id, lineage.prepare(root, body, fp.sign(root, body),
                                               inputs=inputs, operation="runtime-record"))


def enrolled():
    return bool(ROOT.get()) and io.enabled(ROOT.get())


def readable_record(record):
    from hub import policies

    root = ROOT.get()
    return policies.record_permits(record) and (not root or not io.enabled(root) or io.runtime_permits(root, record))
