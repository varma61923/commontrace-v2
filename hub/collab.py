"""Collaboration on a trace, for a customer's own team: comments,
assignment, and a notification inbox (audit §8.1: "No reviewer queue,
comments, assignments, notification inbox, ownership").

Distinct from hub/manage.py's Knowledge Base review queue (kb-review,
approve-submission, reject-submission), which is an OPERATOR surface --
cross-tenant, staff-only. This is the surface a customer's own team uses
on their own traces, and it did not exist until hub/models.py:User gave a
request an actual PERSON to attribute a comment to, be assigned to, or
notify.

Every function here takes an `AuthenticatedUser` (hub/auth.py), never just
an org_id, for the reason hub/auth.py:get_current_user exists: a shared
workload API key cannot author a remark or be the one someone assigns
work to. hub/server.py's tool wrappers are the only caller that resolves
one from request context.

`target_type` is a real column (Comment/Assignment/Notification all carry
it) but only `"trace"` is validated and reachable through the MCP tool
surface today -- kept generic so a second target kind doesn't need a
schema change, not because more than one is supported yet.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hub import audit
from hub.models import Assignment, Comment, Notification, Trace, User

TARGET_TRACE = "trace"

_MAX_COMMENT_CHARS = 4000


class CollabError(ValueError):
    """A well-formed request this module refuses on its own terms (an
    empty body, a disabled assignee) -- reported as `invalid_request` by
    hub/server.py, the same way crud.py's own input errors are, never as
    an internal_error."""


class CollabNotFound(CollabError):
    """The trace, user, or notification named does not exist in this org
    -- reported as `not_found`, matching get_trace's own convention for an
    id that is simply wrong (including one that belongs to another org)."""


async def _get_trace_or_raise(session: AsyncSession, org_id: str, trace_id: str) -> Trace:
    trace = await session.get(Trace, trace_id)
    if trace is None or trace.org_id != org_id:
        raise CollabNotFound(f"no trace with id {trace_id}")
    return trace


async def add_comment(
    session: AsyncSession, org_id: str, author, trace_id: str, body: str,
) -> dict:
    """`author` is an `AuthenticatedUser` (hub/auth.py:get_current_user)."""
    body = body.strip()
    if not body:
        raise CollabError("comment body must not be empty")
    if len(body) > _MAX_COMMENT_CHARS:
        raise CollabError(f"comment body must be at most {_MAX_COMMENT_CHARS} characters")

    await _get_trace_or_raise(session, org_id, trace_id)

    comment = Comment(
        org_id=org_id, target_type=TARGET_TRACE, target_id=trace_id,
        author_user_id=author.id, body=body,
    )
    session.add(comment)
    await session.flush()

    # Tell the assignee, if any -- not the comment's own author, who
    # obviously already knows what they just wrote.
    assignment = (
        await session.execute(
            select(Assignment).where(
                Assignment.org_id == org_id,
                Assignment.target_type == TARGET_TRACE,
                Assignment.target_id == trace_id,
            )
        )
    ).scalar_one_or_none()
    if assignment is not None and assignment.assignee_user_id != author.id:
        session.add(Notification(
            org_id=org_id, user_id=assignment.assignee_user_id,
            kind="comment", target_type=TARGET_TRACE, target_id=trace_id,
            summary=f"{author.email} commented on a trace assigned to you",
        ))

    await audit.record(
        session, actor=f"user:{author.id}", action="comment.add",
        org_id=org_id, target_type=TARGET_TRACE, target_id=trace_id,
        summary=f"{len(body)} chars",
    )
    return {
        "id": comment.id, "trace_id": trace_id, "author_email": author.email,
        "body": comment.body, "created_at": comment.created_at.isoformat(),
    }


async def list_comments(session: AsyncSession, org_id: str, trace_id: str) -> list[dict]:
    await _get_trace_or_raise(session, org_id, trace_id)
    rows = (
        await session.execute(
            select(Comment, User.email)
            .join(User, User.id == Comment.author_user_id)
            .where(
                Comment.org_id == org_id,
                Comment.target_type == TARGET_TRACE,
                Comment.target_id == trace_id,
            )
            .order_by(Comment.created_at)
        )
    ).all()
    return [
        {
            "id": comment.id, "author_email": email, "body": comment.body,
            "created_at": comment.created_at.isoformat(),
        }
        for comment, email in rows
    ]


async def assign(
    session: AsyncSession, org_id: str, assigner, trace_id: str, assignee_user_id: str,
) -> dict:
    """`assigner` is an `AuthenticatedUser`. Re-assigning a trace that
    already has an assignee overwrites it -- see Assignment's own
    docstring for why this is mutable current state, not an append-only
    log."""
    await _get_trace_or_raise(session, org_id, trace_id)

    assignee = await session.get(User, assignee_user_id)
    if assignee is None or assignee.org_id != org_id:
        raise CollabNotFound(f"no user with id {assignee_user_id} in this organization")
    if assignee.disabled_at is not None:
        raise CollabError(f"{assignee_user_id} is disabled and cannot be assigned work")

    existing = (
        await session.execute(
            select(Assignment).where(
                Assignment.org_id == org_id,
                Assignment.target_type == TARGET_TRACE,
                Assignment.target_id == trace_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.assignee_user_id = assignee_user_id
        existing.assigned_by_user_id = assigner.id
        existing.created_at = datetime.now(timezone.utc)
    else:
        session.add(Assignment(
            org_id=org_id, target_type=TARGET_TRACE, target_id=trace_id,
            assignee_user_id=assignee_user_id, assigned_by_user_id=assigner.id,
        ))

    if assignee_user_id != assigner.id:
        session.add(Notification(
            org_id=org_id, user_id=assignee_user_id,
            kind="assigned", target_type=TARGET_TRACE, target_id=trace_id,
            summary=f"{assigner.email} assigned you a trace",
        ))

    await audit.record(
        session, actor=f"user:{assigner.id}", action="assignment.set",
        org_id=org_id, target_type=TARGET_TRACE, target_id=trace_id,
        summary=f"assignee={assignee.email}",
    )
    return {"trace_id": trace_id, "assignee_email": assignee.email}


async def unassign(session: AsyncSession, org_id: str, actor, trace_id: str) -> bool:
    await _get_trace_or_raise(session, org_id, trace_id)
    existing = (
        await session.execute(
            select(Assignment).where(
                Assignment.org_id == org_id,
                Assignment.target_type == TARGET_TRACE,
                Assignment.target_id == trace_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        return False
    await session.delete(existing)
    await audit.record(
        session, actor=f"user:{actor.id}", action="assignment.clear",
        org_id=org_id, target_type=TARGET_TRACE, target_id=trace_id,
    )
    return True


async def list_my_notifications(
    session: AsyncSession, org_id: str, person, *, unread_only: bool = False,
) -> list[dict]:
    stmt = select(Notification).where(
        Notification.org_id == org_id, Notification.user_id == person.id,
    )
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    stmt = stmt.order_by(Notification.created_at.desc())
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": n.id, "kind": n.kind, "target_type": n.target_type,
            "target_id": n.target_id, "summary": n.summary,
            "created_at": n.created_at.isoformat(),
            "read_at": n.read_at.isoformat() if n.read_at else None,
        }
        for n in rows
    ]


async def mark_notification_read(
    session: AsyncSession, org_id: str, person, notification_id: str,
) -> bool:
    notification = await session.get(Notification, notification_id)
    # Scoped to the caller's OWN id, not just their org: another person in
    # the same org must not be able to mark -- or even discover the
    # existence of -- someone else's inbox entry by guessing an id.
    if (
        notification is None
        or notification.org_id != org_id
        or notification.user_id != person.id
    ):
        return False
    if notification.read_at is None:
        notification.read_at = datetime.now(timezone.utc)
    return True
