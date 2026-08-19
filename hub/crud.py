"""Org-scoped data access layer for the six Hub MCP tools.

Tenant isolation is enforced *here*, at the query layer: every function that
reads or writes a Trace takes the caller's org_id as an explicit, required
argument and puts it in the SQL WHERE clause of the query that touches the
`traces` table. There is no function in this module that fetches trace rows
and then filters them in Python -- that is exactly the pattern that leaks
data the moment someone adds a new call site and forgets the filter.
hub/tests/test_tenant_isolation.py asserts this property from the outside.

Every one of these functions returns wire-shaped Trace dicts (matching
protocol/schemas/trace.schema.json) or None/[]; hub/server.py is the only
layer that talks MCP-protocol shapes, hub/schema_validation.py is the only
layer that validates against the schema files, and this module is the only
layer that talks SQL. Keeping those separate is what makes the isolation
property testable without spinning up a live MCP transport for every case.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hub.abuse import RateLimited, RateLimiter, suspicion_reason, validate_size
from hub.config import HubConfig
from hub.models import Trace, TraceRelation, Vote
from hub.schema_validation import validate_trace

_SEARCH_LIMIT = 50


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def _votes_for(session: AsyncSession, trace_id: str) -> list[dict]:
    rows = (await session.execute(select(Vote).where(Vote.trace_id == trace_id))).scalars().all()
    return [
        {"vote_type": v.vote_type, "feedback_tag": v.feedback_tag, "feedback_text": v.feedback_text}
        for v in rows
    ]


async def _related_for(session: AsyncSession, trace_id: str) -> list[dict]:
    rows = (
        await session.execute(select(TraceRelation).where(TraceRelation.trace_id == trace_id))
    ).scalars().all()
    return [{"relationship": r.relationship_type, "trace_id": r.related_trace_id} for r in rows]


async def _to_wire(session: AsyncSession, trace: Trace) -> dict:
    return {
        "id": trace.id,
        "title": trace.title,
        "context_text": trace.context_text,
        "solution_text": trace.solution_text,
        "tags": list(trace.tags or []),
        "agent_type": trace.agent_type,
        "profile": trace.profile,
        "extensions": dict(trace.extensions or {}),
        "watch_condition": trace.watch_condition,
        "review_after": trace.review_after,
        "supersedes_trace_id": trace.supersedes_trace_id or "",
        "contributor": trace.contributor,
        "created_at": _iso(trace.created_at),
        "trust": trace.trust,
        "retrievals": trace.retrievals,
        "depth": trace.depth,
        "votes": await _votes_for(session, trace.id),
        "related": await _related_for(session, trace.id),
        "outcome": dict(trace.outcome or {}),
    }


# --- The six Hub tools -------------------------------------------------


async def search_traces(
    session: AsyncSession, org_id: str, query: str = "", tags: list[str] | None = None
) -> list[dict]:
    stmt = select(Trace).where(Trace.org_id == org_id, Trace.quarantined.is_(False))
    if query:
        like = f"%{query}%"
        stmt = stmt.where(
            (Trace.title.ilike(like)) | (Trace.context_text.ilike(like)) | (Trace.solution_text.ilike(like))
        )
    if tags:
        stmt = stmt.where(Trace.tags.overlap(tags))
    stmt = stmt.order_by(Trace.created_at.desc()).limit(_SEARCH_LIMIT)

    traces = (await session.execute(stmt)).scalars().all()
    if traces:
        await session.execute(
            update(Trace)
            .where(Trace.id.in_([t.id for t in traces]))
            .values(retrievals=Trace.retrievals + 1)
        )
    return [await _to_wire(session, t) for t in traces]


async def contribute_trace(
    session: AsyncSession,
    org_id: str,
    config: HubConfig,
    rate_limiter: RateLimiter,
    *,
    title: str,
    context_text: str,
    solution_text: str,
    tags: list[str] | None = None,
    agent_type: str = "",
) -> dict:
    """Returns {"id": ..., "quarantined": bool, "quarantine_reason": str}.
    Raises TraceRejected (bad schema/oversized) or RateLimited (429-shaped)
    without storing anything."""
    tags = tags or []

    if not rate_limiter.allow(org_id):
        raise RateLimited(f"org {org_id} exceeded contribute_trace rate limit")

    candidate_id = str(uuid.uuid4())
    wire = {
        "id": candidate_id,
        "title": title,
        "context_text": context_text,
        "solution_text": solution_text,
        "tags": tags,
        "agent_type": agent_type,
    }
    validate_trace(wire)  # raises SchemaValidationError -> hard reject
    validate_size(wire, config)  # raises TraceRejected -> hard reject

    reason = suspicion_reason(wire, config)
    trace = Trace(
        id=candidate_id,
        org_id=org_id,
        title=title,
        context_text=context_text,
        solution_text=solution_text,
        tags=tags,
        agent_type=agent_type,
        quarantined=reason is not None,
        quarantine_reason=reason or "",
    )
    session.add(trace)
    await session.flush()
    return {"id": trace.id, "quarantined": trace.quarantined, "quarantine_reason": trace.quarantine_reason}


async def get_trace(session: AsyncSession, org_id: str, trace_id: str) -> dict | None:
    stmt = select(Trace).where(Trace.id == trace_id, Trace.org_id == org_id)
    trace = (await session.execute(stmt)).scalar_one_or_none()
    if trace is None:
        # Deliberately indistinguishable from "trace_id belongs to another
        # org": see hub/tests/test_tenant_isolation.py -- get_trace on a
        # known org_b id must 404, not 403, so org_a can never learn that
        # the id exists at all.
        return None
    await session.execute(update(Trace).where(Trace.id == trace_id).values(retrievals=Trace.retrievals + 1))
    return await _to_wire(session, trace)


async def vote_trace(
    session: AsyncSession,
    org_id: str,
    trace_id: str,
    vote_type: str,
    feedback_tag: str = "",
    feedback_text: str = "",
) -> dict | None:
    if vote_type not in ("up", "down"):
        raise ValueError(f"vote_type must be 'up' or 'down', got {vote_type!r}")

    stmt = select(Trace).where(Trace.id == trace_id, Trace.org_id == org_id)
    trace = (await session.execute(stmt)).scalar_one_or_none()
    if trace is None:
        return None

    existing = (
        await session.execute(select(Vote).where(Vote.trace_id == trace_id, Vote.org_id == org_id))
    ).scalar_one_or_none()
    if existing is not None:
        existing.vote_type = vote_type
        existing.feedback_tag = feedback_tag
        existing.feedback_text = feedback_text
    else:
        session.add(
            Vote(
                trace_id=trace_id,
                org_id=org_id,
                vote_type=vote_type,
                feedback_tag=feedback_tag,
                feedback_text=feedback_text,
            )
        )
    await session.flush()

    up = (
        await session.execute(
            select(Vote).where(Vote.trace_id == trace_id, Vote.vote_type == "up")
        )
    ).scalars().all()
    down = (
        await session.execute(
            select(Vote).where(Vote.trace_id == trace_id, Vote.vote_type == "down")
        )
    ).scalars().all()
    total = len(up) + len(down)
    trace.trust = (len(up) / total) if total else 0.5

    await session.flush()
    return await _to_wire(session, trace)


async def amend_trace(
    session: AsyncSession,
    org_id: str,
    trace_id: str,
    *,
    title: str | None = None,
    context_text: str | None = None,
    solution_text: str | None = None,
    tags: list[str] | None = None,
) -> dict | None:
    """Creates a new Trace that supersedes `trace_id`, rather than mutating
    history in place -- consistent with Trace.supersedes_trace_id /
    Trace.depth being an amendment *chain*, not an overwrite."""
    stmt = select(Trace).where(Trace.id == trace_id, Trace.org_id == org_id)
    original = (await session.execute(stmt)).scalar_one_or_none()
    if original is None:
        return None

    amended = Trace(
        org_id=org_id,
        title=title if title is not None else original.title,
        context_text=context_text if context_text is not None else original.context_text,
        solution_text=solution_text if solution_text is not None else original.solution_text,
        tags=tags if tags is not None else list(original.tags or []),
        agent_type=original.agent_type,
        profile=original.profile,
        extensions=dict(original.extensions or {}),
        supersedes_trace_id=original.id,
        depth=original.depth + 1,
    )
    session.add(amended)
    await session.flush()

    session.add(TraceRelation(trace_id=amended.id, related_trace_id=original.id, relationship_type="AMENDS"))
    session.add(
        TraceRelation(trace_id=original.id, related_trace_id=amended.id, relationship_type="SUPERSEDED_BY")
    )
    await session.flush()
    return await _to_wire(session, amended)


async def list_tags(session: AsyncSession, org_id: str) -> list[str]:
    stmt = select(Trace.tags).where(Trace.org_id == org_id, Trace.quarantined.is_(False))
    rows = (await session.execute(stmt)).scalars().all()
    tag_set: set[str] = set()
    for tags in rows:
        tag_set.update(tags or [])
    return sorted(tag_set)
