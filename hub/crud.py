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

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hub import audit
from hub.abuse import RateLimited, RateLimiter, suspicion_reason, validate_size
from hub.config import DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT, HubConfig
from hub.models import TEXT_SEARCH_CONFIG, Trace, TraceRelation, Vote
from hub.schema_validation import validate_trace

# Fallback `actor` for a call site that didn't supply one. Recorded
# verbatim rather than silently omitted: an audit row that can't name
# its actor should be visibly incomplete, not invisible.
AUDIT_ACTOR_UNKNOWN = "unknown"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def _votes_by_trace(session: AsyncSession, trace_ids: list[str]) -> dict[str, list[dict]]:
    """Batch-load votes for many traces in ONE query.

    Previously this was a per-trace query inside _to_wire, so a 50-result
    search issued 1 + 2*50 = 101 round trips. Safe for tenant isolation
    because `trace_ids` is always derived from an already-org-scoped query
    -- these helpers never widen the set of traces the caller can see, they
    only decorate rows that were already selected.
    """
    if not trace_ids:
        return {}
    rows = (await session.execute(select(Vote).where(Vote.trace_id.in_(trace_ids)))).scalars().all()
    out: dict[str, list[dict]] = {}
    for v in rows:
        out.setdefault(v.trace_id, []).append(
            {"vote_type": v.vote_type, "feedback_tag": v.feedback_tag, "feedback_text": v.feedback_text}
        )
    return out


async def _related_by_trace(session: AsyncSession, trace_ids: list[str]) -> dict[str, list[dict]]:
    """Batch-load relation edges for many traces in ONE query. See _votes_by_trace."""
    if not trace_ids:
        return {}
    rows = (
        await session.execute(select(TraceRelation).where(TraceRelation.trace_id.in_(trace_ids)))
    ).scalars().all()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r.trace_id, []).append(
            {"relationship": r.relationship_type, "trace_id": r.related_trace_id}
        )
    return out


def _to_wire(trace: Trace, votes: list[dict], related: list[dict]) -> dict:
    """Pure shaping -- no I/O. Callers batch-load `votes`/`related` first
    (see _votes_by_trace) rather than letting this function issue queries."""
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
        "votes": votes,
        "related": related,
        "outcome": dict(trace.outcome or {}),
    }


async def _hydrate(session: AsyncSession, traces: list[Trace]) -> list[dict]:
    """Wire-shape a list of already-org-scoped traces, batch-loading their
    votes and relations (2 queries total, regardless of list length)."""
    trace_ids = [t.id for t in traces]
    votes = await _votes_by_trace(session, trace_ids)
    related = await _related_by_trace(session, trace_ids)
    return [_to_wire(t, votes.get(t.id, []), related.get(t.id, [])) for t in traces]


async def _hydrate_one(session: AsyncSession, trace: Trace) -> dict:
    return (await _hydrate(session, [trace]))[0]


# --- The six Hub tools -------------------------------------------------


async def search_traces(
    session: AsyncSession,
    org_id: str,
    query: str = "",
    tags: list[str] | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    offset: int = 0,
) -> dict:
    """Returns {"traces": [...], "limit", "offset", "has_more"}.

    Two deliberate changes from the original implementation, both visible
    to callers:

    1. **Pagination.** This used to hard-cap at 50 results with no offset,
       so a client could never reach result 51 at all. `limit` is clamped
       to [1, MAX_SEARCH_LIMIT] and `has_more` tells the caller whether to
       page again (computed by fetching one extra row, not by a second
       COUNT query).

    2. **Matching is full-text, not substring.** The old
       `ILIKE '%query%'` could not use an index -- a leading wildcard
       defeats B-tree prefix matching -- so every search sequentially
       scanned the org's traces. It now matches against the
       `traces.search_vector` GIN index (hub/models.py).

       This changes results, not just speed, and the difference cuts both
       ways: "deploy" now also matches "deployed"/"deploying" (stemming),
       which substring matching missed; but "ploy" no longer matches
       "deploy", which substring matching caught. For a knowledge store
       queried in natural language that trade is the right one -- mid-word
       substring hits are mostly noise -- but it IS a behavior change, not
       a transparent optimization.

       Ranking follows from the same change: with a query, results come
       back by relevance (ts_rank) and then recency; with no query, purely
       by recency as before.
    """
    limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
    offset = max(0, int(offset))

    stmt = select(Trace).where(Trace.org_id == org_id, Trace.quarantined.is_(False))
    if query:
        # plainto_tsquery (not to_tsquery) because the input is arbitrary
        # user text: it tokenizes plain words and cannot raise a syntax
        # error on stray operators like '&' or '!'.
        tsquery = func.plainto_tsquery(TEXT_SEARCH_CONFIG, query)
        stmt = stmt.where(Trace.search_vector.op("@@")(tsquery))
        stmt = stmt.order_by(func.ts_rank(Trace.search_vector, tsquery).desc(), Trace.created_at.desc())
    else:
        stmt = stmt.order_by(Trace.created_at.desc())
    if tags:
        stmt = stmt.where(Trace.tags.overlap(tags))

    # Fetch one more than asked so has_more is exact without a COUNT(*).
    stmt = stmt.offset(offset).limit(limit + 1)
    rows = (await session.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    traces = list(rows[:limit])

    if traces:
        await session.execute(
            update(Trace)
            .where(Trace.id.in_([t.id for t in traces]))
            .values(retrievals=Trace.retrievals + 1)
        )
    return {
        "traces": await _hydrate(session, traces),
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
    }


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
    actor: str = AUDIT_ACTOR_UNKNOWN,
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
    # Bounded, content-free summary -- audit rows outlive an org purge, so
    # they must never carry the trace body. See hub/audit.py.
    await audit.record(
        session,
        actor=actor,
        action="contribute_trace",
        org_id=org_id,
        target_type="trace",
        target_id=trace.id,
        summary=(
            f"agent_type={agent_type or '?'} title_len={len(title)} "
            f"n_tags={len(tags)} quarantined={trace.quarantined}"
        ),
    )
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
    return await _hydrate_one(session, trace)


async def vote_trace(
    session: AsyncSession,
    org_id: str,
    trace_id: str,
    vote_type: str,
    feedback_tag: str = "",
    feedback_text: str = "",
    actor: str = AUDIT_ACTOR_UNKNOWN,
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
    await audit.record(
        session,
        actor=actor,
        action="vote_trace",
        org_id=org_id,
        target_type="trace",
        target_id=trace_id,
        summary=f"vote={vote_type} feedback_tag={feedback_tag or '-'} new_trust={trace.trust:.3f}",
    )
    return await _hydrate_one(session, trace)


async def amend_trace(
    session: AsyncSession,
    org_id: str,
    trace_id: str,
    *,
    title: str | None = None,
    context_text: str | None = None,
    solution_text: str | None = None,
    tags: list[str] | None = None,
    actor: str = AUDIT_ACTOR_UNKNOWN,
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
    changed = [
        name
        for name, value in (
            ("title", title), ("context_text", context_text),
            ("solution_text", solution_text), ("tags", tags),
        )
        if value is not None
    ]
    await audit.record(
        session,
        actor=actor,
        action="amend_trace",
        org_id=org_id,
        target_type="trace",
        target_id=amended.id,
        summary=f"supersedes={original.id} depth={amended.depth} changed={','.join(changed) or 'nothing'}",
    )
    return await _hydrate_one(session, amended)


async def list_tags(session: AsyncSession, org_id: str) -> list[str]:
    stmt = select(Trace.tags).where(Trace.org_id == org_id, Trace.quarantined.is_(False))
    rows = (await session.execute(stmt)).scalars().all()
    tag_set: set[str] = set()
    for tags in rows:
        tag_set.update(tags or [])
    return sorted(tag_set)
