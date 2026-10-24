"""Writing audit-log entries (hub/models.py: AuditLogEntry)."""

from __future__ import annotations

import hashlib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from hub.models import AuditLogEntry

ACTOR_OPERATOR_CLI = "operator-cli"

_MAX_SUMMARY = 500


def safe_summary(action: str, summary: str) -> str:
    """Webhook URLs may be bearer credentials, including in historical rows."""
    if action in {"webhook.add", "webhook.disable"}:
        return "Webhook destination redacted"
    return summary


def actor_for_api_key(key_prefix: str) -> str:
    """Build an `actor` string from an API key's non-secret prefix."""
    return f"api-key:{key_prefix}"


async def record(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    org_id: str | None = None,
    target_type: str = "",
    target_id: str = "",
    summary: str = "",
) -> AuditLogEntry:
    summary = safe_summary(action, summary)
    entry = AuditLogEntry(
        actor=actor[:128],
        action=action[:64],
        org_id=org_id,
        target_type=target_type[:32],
        target_id=str(target_id)[:64],
        summary=summary[:_MAX_SUMMARY],
    )
    info = session.info if isinstance(session.info, dict) else {}
    key = info.get("transparency_key")
    if org_id and isinstance(key, str) and key:
        from hub import transparency

        previous = await session.scalar(text("SELECT current_setting('app.org_id', true)"))
        if previous and previous != str(org_id):
            raise PermissionError("audit organization does not match the bound tenant")
        # Signup and operator sessions may be unscoped. Transparency tables
        # require a tenant even there. A savepoint restores SET LOCAL on error
        # and keeps the audit row atomic with its signed transparency entry.
        async with session.begin_nested():
            await session.execute(text("SELECT set_config('app.org_id', :org, true)"), {"org": str(org_id)})
            session.add(entry)
            await transparency.append(session, org_id,
                {"actor": actor[:128], "action": action[:64], "target_type": target_type[:32],
                 "target_id": str(target_id)[:64], "summary_hash": hashlib.sha256(summary.encode()).hexdigest()},
                secret=key, algorithm=info.get("transparency_algorithm", "hmac-sha256"))
            await session.execute(text("SELECT set_config('app.org_id', :org, true)"), {"org": previous or ""})
    else:
        session.add(entry)
    return entry


async def prune_audit_logs(
    session: AsyncSession,
    *,
    older_than_days: int = 90,
    org_id: str | None = None,
) -> int:
    """Retention sweep: delete audit logs older than the retention threshold."""
    import datetime

    from sqlalchemy import delete

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=older_than_days)
    stmt = delete(AuditLogEntry).where(AuditLogEntry.created_at < cutoff)
    if org_id:
        stmt = stmt.where(AuditLogEntry.org_id == org_id)
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


async def list_audit_entries(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    action: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[AuditLogEntry]:
    """Retrieve audit log entries filtered by org or action with pagination."""
    from sqlalchemy import select

    query = select(AuditLogEntry)
    if org_id:
        query = query.where(AuditLogEntry.org_id == org_id)
    if action:
        query = query.where(AuditLogEntry.action == action)
    query = query.order_by(AuditLogEntry.created_at.desc()).offset(offset).limit(limit)
    rows = await session.execute(query)
    return list(rows.scalars())


class audit_context:
    """Async context manager that records an audit entry on completion with timing."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        actor: str,
        action: str,
        org_id: str | None = None,
        target_type: str = "",
        target_id: str = "",
    ) -> None:
        self.session = session
        self.actor = actor
        self.action = action
        self.org_id = org_id
        self.target_type = target_type
        self.target_id = target_id
        self.summary = ""

    async def __aenter__(self) -> audit_context:
        import time
        self.start_time = time.monotonic()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        import time
        duration_ms = round((time.monotonic() - self.start_time) * 1000)
        status = "failed" if exc_type else "ok"
        full_summary = f"[{status}] {self.summary or self.action} ({duration_ms}ms)"
        await record(
            self.session,
            actor=self.actor,
            action=self.action,
            org_id=self.org_id,
            target_type=self.target_type,
            target_id=self.target_id,
            summary=full_summary,
        )

