"""Receive a system of record's webhook and turn it into recorded outcomes.

The order of operations is the security design, so it is stated once:

 1. Look the connector up by the id in the URL, UNSCOPED (the org is exactly what
    is not yet known). An unknown, disabled or unreadable connector answers the
    same 401 as a bad signature, after the same amount of HMAC work, so the route
    does not say which connector ids exist.
 2. Verify the vendor's signature over the raw body, in constant time, and refuse a
    stale signed timestamp. Nothing below runs for an unauthenticated request.
 3. Only now scope the session to the connector's org (row-level security applies
    to everything after this) and claim the delivery id with insert-or-ignore.
    Two simultaneous copies of one delivery: exactly one proceeds.
 4. Map the payload to signals and apply them -- or, in dry-run, only describe them.

A signal is applied through `crud.record_occasion_outcome`, which only ever fills
an UNRESOLVED observation, so an outcome already counted is never flipped by a
late or repeated event.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from hub import audit, auth, crud
from hub.connectors import PROVIDERS, base
from hub.db import session_scope
from hub.encryption import NULL_CIPHER, EncryptionError, EnvelopeCipher
from hub.models import Connector, ConnectorDelivery, PendingOutcome

logger = logging.getLogger("commontrace.hub.connectors")

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_NAME_MAX = 100
#: A push can carry thousands of commits; each signal is a query. Bounded so one
#: delivery cannot be made to do unbounded work.
MAX_SIGNALS = 200
#: How long the replay ledger is kept. Past this a replay of a vendor delivery with
#: no signed timestamp (GitHub) is no longer recognised, which is harmless to the
#: measurement: an outcome already counted is never flipped.
LEDGER_RETENTION_DAYS = 90


class ConnectorError(ValueError):
    """A connector cannot be created or changed as asked."""


@dataclass
class Result:
    status: int
    body: dict = field(default_factory=dict)


# --- Secrets ----------------------------------------------------------------------


def seal_secret(cipher: EnvelopeCipher, org_id: str, secret: str) -> str:
    """The vendor secret, encrypted and BOUND to its org: the org id is part of what
    is encrypted, so a row copied between orgs fails to open instead of verifying
    one tenant's deliveries with another's credential."""
    return cipher.encrypt(f"{org_id}:{secret}")


def open_secret(cipher: EnvelopeCipher, org_id: str, sealed: str) -> str:
    plain = cipher.decrypt(sealed)
    bound, sep, secret = plain.partition(":")
    if not sep or not hmac.compare_digest(bound.encode(), org_id.encode()):
        raise EncryptionError("a connector secret is not bound to the org that holds it")
    return secret


# --- Management --------------------------------------------------------------------


async def create_connector(
    session, org_id: str, provider: str, secret: str, *, cipher: EnvelopeCipher,
    name: str = "", config: dict | None = None, actor: str = audit.ACTOR_OPERATOR_CLI,
) -> Connector:
    """Register a connector. It starts in DRY-RUN: it verifies and parses and says
    what it would record, and records nothing, until `set_live`."""
    module = PROVIDERS.get(provider)
    if module is None:
        raise ConnectorError(f"unknown provider {provider!r}; known: {', '.join(sorted(PROVIDERS))}")
    if not cipher.enabled:
        raise ConnectorError(
            "a connector stores a vendor signing secret, which must be encrypted at rest. "
            "Set HUB_ENCRYPTION_KEY first (`python -m hub.manage generate-encryption-key`)."
        )
    if not isinstance(secret, str) or not secret.strip():
        raise ConnectorError("the vendor's signing secret is required")
    if len(name) > _NAME_MAX:
        raise ConnectorError(f"name is longer than {_NAME_MAX} characters")
    try:
        cleaned = module.validate_config(config or {})
    except base.ConfigError as exc:
        raise ConnectorError(str(exc)) from None
    connector = Connector(
        org_id=org_id, provider=provider, name=name, config=cleaned, dry_run=True, enabled=True,
        secret=seal_secret(cipher, org_id, secret),
    )
    session.add(connector)
    await session.flush()
    await audit.record(
        session, actor=actor, action="connector.create", org_id=org_id,
        target_type="connector", target_id=connector.id, summary=f"{provider} (dry-run)",
    )
    return connector


async def set_live(session, connector_id: str, live: bool, *, actor: str = audit.ACTOR_OPERATOR_CLI) -> Connector:
    connector = await _get(session, connector_id)
    connector.dry_run = not live
    await audit.record(
        session, actor=actor, action="connector.live" if live else "connector.dry_run",
        org_id=connector.org_id, target_type="connector", target_id=connector.id,
        summary=connector.provider,
    )
    return connector


async def set_enabled(session, connector_id: str, enabled: bool, *, actor: str = audit.ACTOR_OPERATOR_CLI) -> Connector:
    connector = await _get(session, connector_id)
    connector.enabled = enabled
    await audit.record(
        session, actor=actor, action="connector.enable" if enabled else "connector.disable",
        org_id=connector.org_id, target_type="connector", target_id=connector.id,
        summary=connector.provider,
    )
    return connector


async def _get(session, connector_id: str) -> Connector:
    if not _UUID.match(str(connector_id)):
        raise ConnectorError(f"no such connector: {connector_id}")
    connector = await session.get(Connector, connector_id)
    if connector is None:
        raise ConnectorError(f"no such connector: {connector_id}")
    return connector


# --- Ingest -----------------------------------------------------------------------

_INVALID = Result(401, {"error": "invalid signature"})


def _burn(secret: str, body: bytes) -> None:
    """Spend the HMAC work a real verification would, so an unknown connector id
    is not distinguishable from a bad signature by timing."""
    hmac.new(b"\0" + secret.encode(), body, hashlib.sha256).digest()


async def ingest(
    session_factory, connector_id: str, headers, body: bytes, *,
    cipher: EnvelopeCipher = NULL_CIPHER, now: datetime | None = None,
) -> Result:
    now = now or datetime.now(timezone.utc)

    snapshot = None
    if _UUID.match(str(connector_id)):
        async with session_scope(session_factory) as session:
            row = await session.get(Connector, connector_id)
            if row is not None:
                snapshot = {
                    "id": row.id, "org_id": row.org_id, "provider": row.provider,
                    "secret": row.secret, "config": dict(row.config or {}),
                    "dry_run": row.dry_run, "enabled": row.enabled,
                }
    module = PROVIDERS.get(snapshot["provider"]) if snapshot else None
    if snapshot is None or not snapshot["enabled"] or module is None:
        _burn("unknown", body)
        return _INVALID
    try:
        secret = open_secret(cipher, snapshot["org_id"], snapshot["secret"])
    except EncryptionError:
        # An operator problem (key rotated away, cipher off), not the caller's.
        logger.error("connector %s: secret cannot be opened", snapshot["id"])
        _burn("unknown", body)
        return _INVALID

    try:
        module.verify(headers, body, secret, now=now)
    except base.SignatureError as exc:
        logger.warning("connector %s: refused delivery: %s", snapshot["id"], exc)
        return _INVALID

    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return Result(400, {"error": "payload is not JSON"})
    if not isinstance(payload, dict):
        return Result(400, {"error": "payload is not a JSON object"})
    try:
        delivery = module.delivery_id(headers, payload)
    except base.SignatureError as exc:
        return Result(exc.status, {"error": str(exc)})

    token = auth.current_org_id.set(snapshot["org_id"])
    try:
        async with session_scope(session_factory) as session:
            return await _handle(session, module, snapshot, headers, payload, delivery, now)
    finally:
        auth.current_org_id.reset(token)


async def _handle(session, module, snapshot, headers, payload, delivery, now) -> Result:
    dry = snapshot["dry_run"]
    claimed = (await session.execute(
        pg_insert(ConnectorDelivery)
        .values(org_id=snapshot["org_id"], connector_id=snapshot["id"], delivery_id=delivery, dry_run=dry)
        .on_conflict_do_nothing(constraint="uq_connector_delivery")
        .returning(ConnectorDelivery.id)
    )).scalar_one_or_none()
    if claimed is None:
        return Result(200, {"status": "duplicate", "delivery": delivery})

    try:
        signals = module.signals(headers, payload, snapshot["config"])
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        # Authentic but not mappable: no usable ticket id, or a field of an
        # unexpected type. Acknowledge: a retry of the same bytes cannot succeed,
        # and an unhandled error here would make the vendor retry it for ever.
        note = f"unusable payload: {type(exc).__name__}: {exc}"[:500]
        logger.warning("connector %s: %s", snapshot["id"], note)
        await _finish(session, snapshot, claimed, note, now)
        return Result(200, {"status": "unusable", "detail": note})
    if len(signals) > MAX_SIGNALS:
        logger.warning("connector %s: %d signals in one delivery, keeping %d",
                       snapshot["id"], len(signals), MAX_SIGNALS)
        signals = signals[:MAX_SIGNALS]

    applied = []
    for sig in signals:
        applied.append(await _apply(session, snapshot, sig, now, dry))
    summary = "; ".join(a["action"] for a in applied) or "ignored: no outcome signal"
    await _finish(session, snapshot, claimed, ("dry-run: " if dry else "") + summary, now)
    if applied and not dry:
        await audit.record(
            session, actor=f"connector:{snapshot['provider']}:{snapshot['id'][:8]}",
            action="connector.outcome", org_id=snapshot["org_id"], target_type="connector",
            target_id=snapshot["id"], summary=summary[:500],
        )
    return Result(200, {
        "status": "dry-run" if dry else "ok", "delivery": delivery,
        "signals": [a for a in applied],
    })


async def _finish(session, snapshot, delivery_row_id, outcome: str, now) -> None:
    row = await session.get(ConnectorDelivery, delivery_row_id)
    row.outcome = outcome[:500]
    connector = await session.get(Connector, snapshot["id"])
    connector.last_event_at = now


async def _record(session, org_id: str, occasion_id: str, succeeded: bool, actor: str) -> int:
    result = await crud.record_occasion_outcome(session, org_id, occasion_id, succeeded, actor=actor)
    return int(result["observations_resolved"])


async def _apply(session, snapshot, sig: base.Signal, now: datetime, dry: bool) -> dict:
    org_id, cid = snapshot["org_id"], snapshot["id"]
    actor = f"connector:{snapshot['provider']}:{cid[:8]}"
    window = float(snapshot["config"].get("window_days", 0))
    info = {"kind": sig.kind, "occasion_id": sig.occasion_id, "event": sig.event_id}

    if sig.kind == base.CANDIDATE:
        if window <= 0:
            info["action"] = f"success {sig.occasion_id}"
            if not dry:
                info["resolved"] = await _record(session, org_id, sig.occasion_id, True, actor)
            return info
        mature_at = min(sig.at, now) + timedelta(days=window)
        info["action"] = f"candidate {sig.occasion_id} matures {mature_at:%Y-%m-%d}"
        if not dry:
            await session.execute(
                pg_insert(PendingOutcome)
                .values(org_id=org_id, connector_id=cid, occasion_id=sig.occasion_id,
                        ref=sig.ref, mature_at=mature_at)
                .on_conflict_do_nothing(constraint="uq_connector_pending_occasion")
            )
        return info

    if sig.kind in (base.SUCCESS, base.FAILURE):
        won = sig.kind == base.SUCCESS
        info["action"] = f"{'success' if won else 'failure'} {sig.occasion_id}"
        if not dry:
            await session.execute(delete(PendingOutcome).where(
                PendingOutcome.connector_id == cid, PendingOutcome.occasion_id == sig.occasion_id))
            info["resolved"] = await _record(session, org_id, sig.occasion_id, won, actor)
        return info

    # REVERSAL: undo a pending candidate, found by occasion or by vendor reference.
    query = select(PendingOutcome).where(PendingOutcome.connector_id == cid)
    if sig.occasion_id:
        query = query.where(PendingOutcome.occasion_id == sig.occasion_id)
    elif sig.ref:
        query = query.where(PendingOutcome.ref == sig.ref)
    else:
        info["action"] = "reversal ignored: names neither an occasion nor a reference"
        return info
    pending = (await session.execute(query.with_for_update())).scalars().first()
    if pending is None:
        info["action"] = f"reversal ignored: nothing pending ({sig.occasion_id or sig.ref[:12]}); " \
                         "already final or never seen"
        return info
    info["occasion_id"] = pending.occasion_id
    info["action"] = f"failure {pending.occasion_id} (reversed inside its window)"
    if not dry:
        occasion = pending.occasion_id
        await session.delete(pending)
        info["resolved"] = await _record(session, org_id, occasion, False, actor)
    return info


# --- Maturing ---------------------------------------------------------------------


async def finalize_matured(session_factory, *, now: datetime | None = None, limit: int = 500) -> int:
    """Record success for every candidate whose window has passed with no reversal.

    One transaction per row, scoped to that row's org, claimed with
    FOR UPDATE SKIP LOCKED: two sweepers divide the work rather than double-record,
    and one bad row cannot block the rest. Also trims the replay ledger past its
    retention. Returns how many were finalized.
    """
    now = now or datetime.now(timezone.utc)
    async with session_scope(session_factory) as session:
        await session.execute(delete(ConnectorDelivery).where(
            ConnectorDelivery.received_at < now - timedelta(days=LEDGER_RETENTION_DAYS)))
        due = (await session.execute(
            select(PendingOutcome.id, PendingOutcome.org_id)
            .where(PendingOutcome.mature_at <= now).order_by(PendingOutcome.mature_at).limit(limit)
        )).all()
    done = 0
    for pending_id, org_id in due:
        token = auth.current_org_id.set(org_id)
        try:
            async with session_scope(session_factory) as session:
                row = (await session.execute(
                    select(PendingOutcome).where(PendingOutcome.id == pending_id)
                    .with_for_update(skip_locked=True)
                )).scalar_one_or_none()
                if row is None:
                    continue
                connector = await session.get(Connector, row.connector_id)
                occasion = row.occasion_id
                await session.delete(row)
                if connector is not None and not connector.dry_run:
                    actor = f"connector:{connector.provider}:{connector.id[:8]}"
                    await _record(session, org_id, occasion, True, actor)
                    await audit.record(
                        session, actor=actor, action="connector.outcome", org_id=org_id,
                        target_type="connector", target_id=connector.id,
                        summary=f"success {occasion} (window passed with no reversal)",
                    )
                done += 1
        except Exception:  # noqa: BLE001 - one bad row must not stop the sweep
            logger.exception("connector finalize failed for pending outcome %s", pending_id)
        finally:
            auth.current_org_id.reset(token)
    return done
