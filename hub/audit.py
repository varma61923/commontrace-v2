"""Writing audit-log entries (hub/models.py: AuditLogEntry).

Two rules callers must follow, because an audit trail that leaks is worse
than no audit trail:

  1. Never pass a secret. `actor` for an API-key-authenticated action is
     "api-key:<key_prefix>" -- the short non-secret prefix hub/auth.py
     stores alongside the argon2 hash, never the raw key.
  2. Never pass full trace bodies. `summary` is a short, bounded
     description ("title: 42 chars, 3 tags"), not the content itself.
     Audit rows survive an org purge on purpose (see AuditLogEntry's
     docstring); copying customer content into them would defeat that
     purge.

`record()` adds to the caller's session without committing, so an audit row
lands in the same transaction as the action it describes: if the action
rolls back, so does its audit entry, and there is no window where the log
claims something happened that didn't.
"""

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
