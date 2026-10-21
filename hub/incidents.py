"""Bounded tenant evidence snapshots under existing legal-hold enforcement."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy import DateTime, case, cast, select
from starlette.responses import HTMLResponse, JSONResponse

from unimem import frontier_protocol as fp
from unimem import incident, origin
from hub import retention, transparency
from hub.db import session_scope
from hub.models import AuditLogEntry, HoldoutObservation, IncidentSnapshot, MemoryRecordEvent

# Timestamps the Python writer validated as ISO 8601 with an explicit offset;
# only these are cast in SQL, so a stray value can never raise inside the query.
_ISO_WITH_OFFSET = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
EVIDENCE_ROWS = 1000


# Bulky columns are pinned by digest: the legal hold keeps the bytes in place,
# and the signed snapshot stays bounded however large individual rows are.
_DIGESTED = {"record", "summary"}


def _row(model, row) -> dict:
    result = {}
    for column in model.__table__.columns:
        if column.name == "org_id":
            continue
        value = getattr(row, column.name)
        if column.name in _DIGESTED:
            result[column.name + "_digest"] = fp.digest(value)
        else:
            result[column.name] = value.isoformat() if isinstance(value, datetime) else value
    return result


async def _evidence(session, org_id: str, when: datetime) -> tuple[dict, dict]:
    """The newest bounded evidence rows recorded at or before the incident's valid time.

    A large tenant never makes a freeze fail: each evidence type keeps its newest
    EVIDENCE_ROWS rows and says when older rows exist. The org-wide legal hold
    placed by the freeze preserves the complete tables for later export.
    """
    created = MemoryRecordEvent.record["created_at"].astext
    record_time = case((created.op("~")(_ISO_WITH_OFFSET), cast(created, DateTime(timezone=True))), else_=None)
    collected, window = {}, {}
    for label, model, moment in (("record_events", MemoryRecordEvent, record_time),
                                 ("holdouts", HoldoutObservation, HoldoutObservation.created_at),
                                 ("audit", AuditLogEntry, AuditLogEntry.created_at)):
        found = list((await session.execute(
            select(model).where(model.org_id == org_id, moment.is_not(None), moment <= when)
            .order_by(moment.desc()).limit(EVIDENCE_ROWS + 1))).scalars())
        collected[label] = [_row(model, row) for row in found[:EVIDENCE_ROWS]]
        window[label] = {"rows": len(collected[label]), "older_rows_omitted": len(found) > EVIDENCE_ROWS}
    return collected, window


async def freeze(session, org_id: str, identity: str, valid_time: str, *, actor: str, secret: str,
                 algorithm: str = "hmac-sha256") -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
        raise ValueError("invalid incident identifier")
    when = datetime.fromisoformat(fp.moment(valid_time))
    if await session.get(IncidentSnapshot, (org_id, identity)):
        raise ValueError("incident is already frozen")
    collected, window = await _evidence(session, org_id, when)
    payload = {"org_id": org_id, "id": identity, "valid_time": when.isoformat(),
               "known_at": fp.now(), "evidence": collected, "evidence_window": window,
               "control_mapping": incident.CONTROLS, "legal_note": "Verify against the Official Journal",
               "replay_limit": "Historical content is limited to recorded versions; no unlogged bytes inferred."}
    signed = origin.bind(payload, transparency.signer(org_id, secret, algorithm))
    fp.wire(signed)
    hold = await retention.place_hold(session, org_id, reason="Incident freeze: " + identity, placed_by=actor)
    anchor = await transparency.append(session, org_id,
                                       {"event": "incident-freeze", "id": identity, "digest": fp.digest(signed)},
                                       secret=secret, algorithm=algorithm)
    session.add(IncidentSnapshot(org_id=org_id, id=identity, valid_time=when,
                                known_at=datetime.now(timezone.utc), hold_id=hold.id, snapshot=signed, anchor=anchor))
    await session.flush()
    return {"snapshot": signed, "hold_id": hold.id, "anchor": anchor}


async def evidence_pack(session, snapshot, regime: str, *, secret: str, algorithm: str = "hmac-sha256") -> dict:
    if regime not in incident.CONTROLS:
        raise ValueError("unknown evidence regime")
    files = {"snapshot.json": snapshot.snapshot}
    manifest = origin.bind({"incident_id": snapshot.id, "org_id": snapshot.org_id,
        "files": {name: fp.digest(value) for name, value in files.items()},
        "control_ids": incident.CONTROLS[regime], "issued_at": fp.now(),
        "legal_note": "Verify against the Official Journal"},
        transparency.signer(snapshot.org_id, secret, algorithm))
    anchor = await transparency.append(session, snapshot.org_id,
        {"event": "incident-pack", "manifest_digest": fp.digest(manifest)}, secret=secret, algorithm=algorithm)
    return {"manifest": manifest, "files": files, "anchor": anchor, "hold_id": snapshot.hold_id}


def add_routes(app, session_factory, *, authenticate, require_scope, config):
    async def handle(request):
        authenticated, denied = await authenticate(request)
        if denied is not None:
            return denied
        denied = require_scope(authenticated, "admin" if request.method == "POST" else "read")
        if denied is not None:
            return denied
        if not config.ledger_signing_key:
            return JSONResponse({"error": "incident_signer_not_configured"}, status_code=503)
        try:
            async with session_scope(session_factory, org_id=authenticated.org_id) as session:
                identity = request.path_params.get("incident_id", "")
                if request.method != "POST" and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
                    raise ValueError("invalid incident identifier")
                if request.method == "POST":
                    data = await request.body()
                    if len(data) > 4096:
                        raise ValueError("incident request exceeds 4 KiB")
                    body = json.loads(data)
                    result = await freeze(session, authenticated.org_id, body["id"], body["valid_time"],
                                          actor=authenticated.key_prefix, secret=config.ledger_signing_key,
                                          algorithm=config.transparency_algorithm)
                    return JSONResponse(result, status_code=201)
                snapshot = await session.get(IncidentSnapshot, (authenticated.org_id, identity))
                if snapshot is None:
                    return JSONResponse({"error": "incident_not_found"}, status_code=404)
                regime = request.query_params.get("regime", "eu-ai-act")
                if regime not in incident.CONTROLS:
                    raise ValueError("unknown evidence regime")
                if request.url.path.endswith("/pack"):
                    return JSONResponse(await evidence_pack(session, snapshot, regime,
                        secret=config.ledger_signing_key, algorithm=config.transparency_algorithm))
                return JSONResponse({"snapshot": snapshot.snapshot, "anchor": snapshot.anchor,
                                     "control_ids": incident.CONTROLS[regime], "hold_id": snapshot.hold_id})
        except (ValueError, KeyError, TypeError) as exc:
            return JSONResponse({"error": "invalid_incident_request", "detail": str(exc)}, status_code=400)

    async def console(request):
        authenticated, denied = await authenticate(request)
        if denied is not None:
            return denied
        denied = require_scope(authenticated, "read")
        if denied is not None:
            return denied
        return HTMLResponse("<main><h1>Incident evidence</h1><p>Freeze a decision state with the authenticated "
                            "POST /v1/incidents endpoint. Export its signed evidence and legal hold with "
                            "GET /v1/incidents/{id}/pack. Verify legal mappings against the Official Journal."
                            "</p></main>")

    app.add_route("/v1/incidents", handle, methods=["POST"])
    for operation in ("timeline", "pack"):
        app.add_route("/v1/incidents/{incident_id}/" + operation, handle, methods=["GET"])
    app.add_route("/v1/incidents/console", console, methods=["GET"])
