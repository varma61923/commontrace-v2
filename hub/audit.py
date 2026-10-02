"""Writing audit-log entries (hub/models.py: AuditLogEntry)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from hub.models import AuditLogEntry

ACTOR_OPERATOR_CLI = "operator-cli"

_MAX_SUMMARY = 500


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
) -> None:
    session.add(
        AuditLogEntry(
            actor=actor[:128],
            action=action[:64],
            org_id=org_id,
            target_type=target_type[:32],
            target_id=str(target_id)[:64],
            summary=summary[:_MAX_SUMMARY],
        )
    )
