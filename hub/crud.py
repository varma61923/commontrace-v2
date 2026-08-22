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

import hashlib
import uuid
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from hub import audit, commons, plans
from hub.abuse import RateLimited, RateLimiter, TraceRejected, suspicion_reason, validate_size
from hub.config import DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT, HubConfig
from hub.models import TEXT_SEARCH_CONFIG, Organization, Trace, TraceRelation, UsageCounter, Vote
from hub.schema_validation import validate_trace

# Fallback `actor` for a call site that didn't supply one. Recorded
# verbatim rather than silently omitted: an audit row that can't name
# its actor should be visibly incomplete, not invisible.
AUDIT_ACTOR_UNKNOWN = "unknown"


class IdempotencyKeyConflict(ValueError):
    """A contribute_trace retry reused an idempotency_key with a different
    payload than the original call. Returning the ORIGINAL trace here would
    silently discard the caller's actual (different) request; raising lets
    the caller fix the bug (a key must identify one logical write) instead
    of one of the two payloads vanishing without a trace."""


def _contribute_request_hash(
    title: str, context_text: str, solution_text: str, tags: list[str], agent_type: str
) -> str:
    # Order-independent over tags (a client may reasonably reorder an
    # unordered set between retries) but otherwise exact -- this only needs
    # to distinguish "same logical request" from "different request", not
    # to be a general canonicalization.
    parts = [title, context_text, solution_text, agent_type, "\x1f".join(sorted(tags))]
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()


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
        # Whether this trace is in the cross-org commons. Surfaced so an org
        # can see its own sharing state (and so `commontrace commons
        # contribute` can skip what is already shared) without a second
        # round trip. Not a disclosure: on a commons result this is true by
        # definition, and on your own traces it is your own decision.
        "shared_with_commons": trace.shared_with_commons,
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


# --- Entitlements: the plan, enforced ----------------------------------
#
# The measurement half of the business model already existed
# (`commons-value`: what each org's shared knowledge delivered). This is the
# capture half. It lives in crud.py rather than in the MCP layer for the
# same reason tenant isolation does: a limit checked at the transport is a
# limit that a second call site forgets, and hub/manage.py is already a
# second call site.

METRIC_COMMONS_QUERIES = "commons_queries"


def billing_period(now: datetime | None = None) -> str:
    """The current billing period as 'YYYY-MM', in UTC.

    UTC and not local time: the Hub's replicas may sit in different zones,
    and a period boundary that moves between replicas would let an org get
    a second month's allowance by hitting the right instance.
    """
    now = now or datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


async def _delivered_hits(session: AsyncSession, org_id: str) -> int:
    """How many times this org's shared knowledge covered someone else's
    failure. This is what earns query credit, and it is deliberately not
    "traces shared": sharing is free and forgeable in bulk, while a hit
    requires a real match from a corpus that excludes the sharer's own
    rows. Seeded rows are excluded -- crediting the operator for priming
    its own commons would be circular (hub/plans.py)."""
    total = await session.scalar(
        select(func.coalesce(func.sum(Trace.commons_hits), 0)).where(
            Trace.org_id == org_id,
            Trace.commons_source != "seed",
        )
    )
    return int(total or 0)


async def _plan_for(session: AsyncSession, org_id: str) -> plans.Plan:
    org = await session.get(Organization, org_id)
    return plans.get(org.plan if org is not None else None)


async def _usage(session: AsyncSession, org_id: str, metric: str, period: str | None = None) -> int:
    n = await session.scalar(
        select(UsageCounter.n).where(
            UsageCounter.org_id == org_id,
            UsageCounter.period == (period or billing_period()),
            UsageCounter.metric == metric,
        )
    )
    return int(n or 0)


async def _meter(session: AsyncSession, org_id: str, metric: str) -> int:
    """Record one unit of usage atomically and return the new total.

    INSERT ... ON CONFLICT DO UPDATE SET n = n + 1, never SELECT-then-UPDATE.
    Two concurrent queries from one org would otherwise both read n and both
    write n+1, so every parallel call would be free -- the same lost-update
    class as the vote race, except this one loses revenue rather than a
    vote. RETURNING gives the post-increment value from the same statement,
    so there is no second read to race against either.
    """
    period = billing_period()
    stmt = (
        pg_insert(UsageCounter)
        .values(org_id=org_id, period=period, metric=metric, n=1)
        .on_conflict_do_update(
            constraint="uq_usage_org_period_metric",
            set_={"n": UsageCounter.n + 1, "updated_at": datetime.now(timezone.utc)},
        )
        .returning(UsageCounter.n)
    )
    return int((await session.execute(stmt)).scalar_one())


async def entitlements(session: AsyncSession, org_id: str) -> dict:
    """Everything an org is entitled to and has used this period.

    Read-only and side-effect free, so a client can render "you have 40 of
    45 queries left" without that query itself consuming one. A meter that
    charges you for checking the meter is the kind of detail that ends up
    in a support thread.
    """
    plan = await _plan_for(session, org_id)
    hits = await _delivered_hits(session, org_id)
    allowance = plans.query_allowance(plan, hits)
    used = await _usage(session, org_id, METRIC_COMMONS_QUERIES)
    traces = int(await session.scalar(
        select(func.count()).select_from(Trace).where(Trace.org_id == org_id)
    ) or 0)
    return {
        "plan": plan.name,
        "period": billing_period(),
        "commons_queries": {
            "used": used,
            "granted": plan.commons_queries_per_month,
            "earned": allowance - plan.commons_queries_per_month
                      if allowance != plans.UNLIMITED else 0,
            "allowance": allowance,
            "remaining": plans.UNLIMITED if allowance == plans.UNLIMITED
                         else max(0, allowance - used),
        },
        "traces": {"used": traces, "limit": plan.max_traces},
        "delivered_hits": hits,
    }


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
        # id.desc() as a tiebreaker: created_at alone is not unique enough
        # under concurrent inserts (or two rows sharing a timestamp) to
        # make OFFSET/LIMIT paging deterministic -- without a total order,
        # Postgres is free to break ties by physical row order, which is
        # not guaranteed stable across two separate queries.
        stmt = stmt.order_by(
            func.ts_rank(Trace.search_vector, tsquery).desc(), Trace.created_at.desc(), Trace.id.desc()
        )
    else:
        stmt = stmt.order_by(Trace.created_at.desc(), Trace.id.desc())
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
    idempotency_key: str | None = None,
) -> dict:
    """Returns {"id": ..., "quarantined": bool, "quarantine_reason": str}.
    Raises TraceRejected (bad schema/oversized) or RateLimited (429-shaped)
    without storing anything.

    `idempotency_key` makes retries safe: an MCP client that times out
    waiting for a response cannot tell "the write never happened" from "it
    happened but the response was lost", so without a key every retry
    creates a second trace (reproduced in
    hub/tests/test_concurrency_audit.py before this was added). Passing the
    same key on a retry returns the original result instead of duplicating
    it; passing the same key with a genuinely different payload raises
    IdempotencyKeyConflict rather than silently returning stale content.
    Omitting the key (the default) is unaffected -- NULL never conflicts
    with anything under the backing UNIQUE(org_id, idempotency_key).
    """
    tags = tags or []

    if idempotency_key is not None:
        existing = (
            await session.execute(
                select(Trace).where(Trace.org_id == org_id, Trace.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _idempotent_replay_or_conflict(
                existing, idempotency_key, title, context_text, solution_text, tags, agent_type
            )

    if not rate_limiter.allow(org_id):
        raise RateLimited(f"org {org_id} exceeded contribute_trace rate limit")

    # Checked after the idempotent-replay path above, deliberately: a
    # replay stores nothing, so refusing it at the storage limit would turn
    # a safe retry into a failure exactly when the org is at its cap.
    plan = await _plan_for(session, org_id)
    if plan.max_traces != plans.UNLIMITED:
        stored = int(await session.scalar(
            select(func.count()).select_from(Trace).where(Trace.org_id == org_id)
        ) or 0)
        if not plans.within(plan.max_traces, stored):
            raise plans.EntitlementExceeded(
                metric="traces", limit=plan.max_traces, used=stored, plan=plan.name,
                remedy="Purge traces you no longer need, or move to a plan with more storage.",
            )

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
        idempotency_key=idempotency_key,
        request_hash=(
            _contribute_request_hash(title, context_text, solution_text, tags, agent_type)
            if idempotency_key is not None
            else None
        ),
    )
    session.add(trace)
    try:
        await session.flush()
    except IntegrityError:
        # Lost the race: a concurrent call with the same (org_id,
        # idempotency_key) committed first. Roll back this attempt and
        # treat it exactly like we'd found the row up front.
        await session.rollback()
        existing = (
            await session.execute(
                select(Trace).where(Trace.org_id == org_id, Trace.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing is None:
            raise  # the constraint fired for some other reason; don't mask it
        return _idempotent_replay_or_conflict(
            existing, idempotency_key, title, context_text, solution_text, tags, agent_type
        )

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


def _idempotent_replay_or_conflict(
    existing: Trace,
    idempotency_key: str,
    title: str,
    context_text: str,
    solution_text: str,
    tags: list[str],
    agent_type: str,
) -> dict:
    incoming_hash = _contribute_request_hash(title, context_text, solution_text, tags, agent_type)
    if existing.request_hash != incoming_hash:
        raise IdempotencyKeyConflict(
            f"idempotency_key {idempotency_key!r} was already used for a different contribute_trace "
            "payload; reuse a key only to retry the exact same request"
        )
    return {
        "id": existing.id,
        "quarantined": existing.quarantined,
        "quarantine_reason": existing.quarantine_reason,
    }


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

    # A separate SELECT-existing-vote then INSERT-or-UPDATE is not atomic:
    # two concurrent first-time votes on the same trace can both see no
    # existing row, both attempt an INSERT, and the loser gets an
    # IntegrityError on uq_votes_trace_org that propagated all the way out
    # of this function uncaught (reproduced under 20-way asyncio.gather
    # concurrency in hub/tests/test_concurrency_audit.py). A single atomic
    # upsert closes the race at the database level instead of racing two
    # round trips against it.
    upsert = (
        pg_insert(Vote)
        .values(
            trace_id=trace_id,
            org_id=org_id,
            vote_type=vote_type,
            feedback_tag=feedback_tag,
            feedback_text=feedback_text,
        )
        .on_conflict_do_update(
            constraint="uq_votes_trace_org",
            set_={
                "vote_type": vote_type,
                "feedback_tag": feedback_tag,
                "feedback_text": feedback_text,
            },
        )
    )
    await session.execute(upsert)

    # COUNT, not SELECT: the previous form hydrated every up- and
    # down-vote row for the trace as ORM objects just to len() the lists.
    # A trace with thousands of votes made every single new vote cast pull
    # its entire voting history into memory for two integers.
    counts = (
        await session.execute(
            select(Vote.vote_type, func.count())
            .where(Vote.trace_id == trace_id)
            .group_by(Vote.vote_type)
        )
    ).all()
    tally = dict(counts)
    up_count, down_count = tally.get("up", 0), tally.get("down", 0)
    total = up_count + down_count
    new_trust = (up_count / total) if total else 0.5

    # A plain `trace.trust = new_trust` here is a real, silent lost-update
    # bug: SQLAlchemy's unit-of-work only emits an UPDATE when the new
    # value differs from the value `trace` held at load time (before this
    # function even reached the vote-row lock above). Under concurrent
    # votes, `trace` can be loaded while trust=1.0 (another vote's already-
    # committed value), and *this* call can independently compute
    # new_trust=1.0 too (its own vote happens to match) even though, by the
    # time it gets here, a third concurrent vote already committed
    # trust=0.0 in between -- "new == old" from this call's stale
    # perspective wrongly skips the write, leaving trust permanently
    # inconsistent with the vote that's actually on record (reproduced:
    # ~1 in 4 runs of 4-way concurrent voting in
    # hub/tests/test_concurrency_audit.py before this fix). An explicit,
    # unconditional UPDATE -- the same pattern already used for
    # `retrievals` below -- always writes the value this transaction just
    # computed from its own fresh, post-lock COUNT, regardless of what the
    # in-memory object happened to hold before.
    await session.execute(update(Trace).where(Trace.id == trace_id).values(trust=new_trust))
    set_committed_value(trace, "trust", new_trust)

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
    config: HubConfig,
    rate_limiter: RateLimiter,
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

    # amend_trace is a WRITE path and carries caller-supplied content, so it
    # gets the same three guards contribute_trace does. Without them it was
    # the way around all of them: unlimited writes, unvalidated payloads
    # (a title past the column width became a hard 500 rather than a clean
    # rejection), and spam that quarantine would have caught on the way in.
    if not rate_limiter.allow(org_id):
        raise RateLimited(f"org {org_id} exceeded write rate limit")

    resolved_title = title if title is not None else original.title
    resolved_context = context_text if context_text is not None else original.context_text
    resolved_solution = solution_text if solution_text is not None else original.solution_text
    resolved_tags = tags if tags is not None else list(original.tags or [])

    amended_id = str(uuid.uuid4())
    wire = {
        "id": amended_id,
        "title": resolved_title,
        "context_text": resolved_context,
        "solution_text": resolved_solution,
        "tags": resolved_tags,
        "agent_type": original.agent_type,
    }
    validate_trace(wire)
    validate_size(wire, config)
    reason = suspicion_reason(wire, config)

    amended = Trace(
        id=amended_id,
        org_id=org_id,
        title=resolved_title,
        context_text=resolved_context,
        solution_text=resolved_solution,
        tags=resolved_tags,
        agent_type=original.agent_type,
        profile=original.profile,
        extensions=dict(original.extensions or {}),
        # These four have no override parameter (a caller amending title
        # can't currently ask to change them), so -- like agent_type,
        # profile, and extensions above -- they must carry forward
        # unchanged. Without this they silently reset to their column
        # defaults ("" / {}) on every amendment: a trace's contributor
        # attribution and structured outcome data would vanish the first
        # time anyone tweaked its title, with no error and no audit trail
        # of the loss.
        watch_condition=original.watch_condition,
        review_after=original.review_after,
        contributor=original.contributor,
        outcome=dict(original.outcome or {}),
        supersedes_trace_id=original.id,
        depth=original.depth + 1,
        quarantined=reason is not None,
        quarantine_reason=reason or "",
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
        summary=(f"supersedes={original.id} depth={amended.depth} "
                 f"changed={','.join(changed) or 'nothing'} quarantined={reason is not None}"),
    )
    return await _hydrate_one(session, amended)


async def list_tags(session: AsyncSession, org_id: str) -> list[str]:
    stmt = select(Trace.tags).where(Trace.org_id == org_id, Trace.quarantined.is_(False))
    rows = (await session.execute(stmt)).scalars().all()
    tag_set: set[str] = set()
    for tags in rows:
        tag_set.update(tags or [])
    return sorted(tag_set)


# --- The cross-org commons (opt-in) ------------------------------------
#
# These three functions are the ONLY place in this module where a row can
# cross an org boundary, and they can only ever reach a trace whose owner
# explicitly put it there. Everything above stays unconditionally
# org-scoped; hub/tests/test_tenant_isolation.py passes unchanged. See
# hub/commons.py for why the exchange is signatures-in / consented-text-out.


async def share_trace(
    session: AsyncSession,
    org_id: str,
    trace_id: str,
    rationale: str = "",
    actor: str = AUDIT_ACTOR_UNKNOWN,
) -> dict | None:
    """Contribute one of your own traces to the cross-org commons.

    Org-scoped lookup, so an org can only ever share a trace it owns -- the
    same `Trace.org_id == org_id` guard as get_trace, for the same reason.
    Returns None (not a permission error) for a trace that isn't yours, so
    this cannot be used as an existence oracle for another org's ids.
    """
    stmt = select(Trace).where(Trace.id == trace_id, Trace.org_id == org_id)
    trace = (await session.execute(stmt)).scalar_one_or_none()
    if trace is None:
        return None

    # A quarantined trace is content the Hub already flagged as suspect.
    # Letting it into a corpus other orgs read would propagate exactly what
    # quarantine exists to contain.
    if trace.quarantined:
        raise TraceRejected(
            f"trace {trace_id} is quarantined ({trace.quarantine_reason or 'no reason recorded'}) "
            "and cannot be shared to the commons until an operator releases it"
        )

    trace.shared_with_commons = True
    trace.shared_at = datetime.now(timezone.utc)
    trace.shared_rationale = (rationale or "")[:500]
    trace.commons_signature = commons.signature_for(trace.title, trace.context_text, trace.tags)
    await session.flush()

    await audit.record(
        session,
        actor=actor,
        action="share_trace",
        org_id=org_id,
        target_type="trace",
        target_id=trace_id,
        # Content-free, like every other audit summary: records that the
        # decision was made and whether a rationale was given, not the text.
        summary=f"shared_to_commons rationale_len={len(rationale or '')}",
    )
    return {"id": trace.id, "shared_with_commons": True, "shared_at": _iso(trace.shared_at)}


async def unshare_trace(
    session: AsyncSession,
    org_id: str,
    trace_id: str,
    actor: str = AUDIT_ACTOR_UNKNOWN,
) -> dict | None:
    """Withdraw a trace from the commons. Clears the signature too, so it
    stops matching immediately rather than lingering in results."""
    stmt = select(Trace).where(Trace.id == trace_id, Trace.org_id == org_id)
    trace = (await session.execute(stmt)).scalar_one_or_none()
    if trace is None:
        return None

    trace.shared_with_commons = False
    trace.shared_at = None
    trace.shared_rationale = ""
    trace.commons_signature = None
    await session.flush()

    await audit.record(
        session,
        actor=actor,
        action="unshare_trace",
        org_id=org_id,
        target_type="trace",
        target_id=trace_id,
        summary="withdrawn_from_commons",
    )
    return {"id": trace.id, "shared_with_commons": False}


async def commons_overlap(
    session: AsyncSession,
    org_id: str,
    failures: object,
    threshold: float = commons.DEFAULT_COMMONS_THRESHOLD,
    include_matches: bool = True,
    agent_type: str = "",
) -> dict:
    """**The number the cross-org thesis lives or dies on** (STRATEGY.md §5):
    of the recurring failures this fleet keeps hitting, what fraction has
    some *other* fleet already solved?

    The caller sends MinHash signatures of its own failures -- computed
    locally, no failure text leaves the client. What comes back is drawn
    only from traces whose owners explicitly shared them.

    Two deliberate scoping decisions:

    1. **The caller's own traces are excluded from the corpus.** The
       question is what you would *gain* from everyone else, so counting
       your own contributions would inflate the headline number into
       something meaningless for exactly the decision it informs.
    2. **Quarantined traces are excluded**, same as every other read path.
    """
    submitted = commons.validate_submitted_failures(failures)
    threshold = max(0.0, min(float(threshold), 1.0))

    # Metered here, and only here: this is the one call whose value comes
    # from other orgs' contributions rather than the caller's own data.
    # Validation runs first so a malformed request is a 400 rather than a
    # silently consumed query -- charging for a call that returned an error
    # is the kind of thing customers notice and remember.
    #
    # An empty submission is not metered either: it compares nothing, so
    # billing it would be charging for a no-op.
    if submitted:
        plan = await _plan_for(session, org_id)
        if not plan.commons_access:
            raise plans.EntitlementExceeded(
                metric="commons_access", limit=0, used=0, plan=plan.name,
                remedy="The commons is not included in this plan.",
            )
        allowance = plans.query_allowance(plan, await _delivered_hits(session, org_id))
        if allowance != plans.UNLIMITED:
            used = await _usage(session, org_id, METRIC_COMMONS_QUERIES)
            if not plans.within(allowance, used):
                raise plans.EntitlementExceeded(
                    metric=METRIC_COMMONS_QUERIES, limit=allowance, used=used, plan=plan.name,
                    remedy=(
                        "Share traces to the commons: every time your knowledge covers "
                        f"another fleet's failure you earn {plans.QUERY_CREDIT_PER_HIT} "
                        "more queries this period. Or move to a larger plan."
                    ),
                )
        # Metered before the scan rather than after: a query that times out
        # or errors mid-scan still consumed the corpus read it asked for,
        # and "only charge on success" is an invitation to cancel every
        # expensive call just before it returns.
        await _meter(session, org_id, METRIC_COMMONS_QUERIES)

    where = [
        Trace.shared_with_commons.is_(True),
        Trace.quarantined.is_(False),
        Trace.commons_signature.isnot(None),
        Trace.org_id != org_id,
    ]
    # Optional semantic narrowing. Not an approximation: a support fleet's
    # failures genuinely should not be scored against CUDA substrate. It is
    # also the cheapest way to keep the scan small as the corpus grows,
    # because it runs in Postgres instead of Python.
    if agent_type:
        where.append(Trace.agent_type == agent_type)

    total_corpus = (
        await session.execute(select(func.count()).select_from(Trace).where(*where))
    ).scalar_one()

    # Bounded scan. See commons.MAX_COMMONS_CORPUS for why this exists and
    # what the real fix past it is. Ordered by recency so a truncated scan
    # is at least a *defined* subset rather than whatever the planner
    # returned first.
    rows = (
        await session.execute(
            select(Trace)
            .where(*where)
            .order_by(Trace.created_at.desc(), Trace.id.desc())
            .limit(commons.max_corpus_scan())
        )
    ).scalars().all()
    corpus_truncated = total_corpus > len(rows)

    best = commons.best_matches(submitted, [r.commons_signature or [] for r in rows])

    matches: list[dict] = []
    by_domain: dict[str, int] = {}
    n_covered = 0

    hit_ids: list[str] = []
    for (label, _sig), (idx, sim) in zip(submitted, best):
        if idx < 0 or sim < threshold:
            continue
        hit = rows[idx]
        n_covered += 1
        hit_ids.append(hit.id)
        key = hit.agent_type or "(unspecified)"
        by_domain[key] = by_domain.get(key, 0) + 1
        if include_matches:
            matches.append(
                {
                    "failure_label": label,
                    "similarity": round(sim, 4),
                    "agent_type": hit.agent_type,
                    "tags": list(hit.tags or []),
                    # The payoff. Safe to return in full: `hit` is only in
                    # the corpus because its owning org explicitly shared it.
                    "trace": _to_wire(hit, [], []),
                }
            )

    if hit_ids:
        # Atomic in-database increment, same pattern as the retrievals
        # counter: a read-modify-write through the ORM would lose counts
        # under concurrent queries, and this number is the basis for
        # contributor value (hub/models.py:Trace.commons_hits). Counted
        # once per covered failure, not once per query, so an org
        # re-running the same report does not inflate a contributor's
        # standing for free -- but a genuinely repeated need does register.
        #
        # `hit_ids` can repeat: two different submitted failures in the same
        # call can both best-match the same shared trace (a fleet hitting
        # one substrate failure across several tasks, submitted in one
        # batch). A flat `+1` under `Trace.id.in_(hit_ids)` credits that
        # trace once per QUERY regardless of duplicates in the list --
        # Postgres updates each matching row once per UPDATE statement, not
        # once per occurrence in the IN-list -- which silently under-counts
        # exactly the batch case this docstring says is supposed to count.
        # A CASE-weighted single statement stays one atomic UPDATE (still no
        # read-modify-write) while crediting each trace by how many
        # distinct failures in THIS call it covered.
        hit_counts = Counter(hit_ids)
        # WHEN clauses as (Trace.id == tid, count) tuples, not a
        # {tid: count} dict matched against value=Trace.id: the dict form
        # binds each key as a bare literal with no column to infer its type
        # from, and asyncpg then sends it as VARCHAR against a UUID column
        # ("operator does not exist: uuid = character varying"). Comparing
        # against the column directly (Trace.id == tid) reuses the same
        # UUID cast `.in_()` already gets right below it.
        increment = case(*((Trace.id == tid, cnt) for tid, cnt in hit_counts.items()), else_=0)
        await session.execute(
            update(Trace).where(Trace.id.in_(hit_counts)).values(commons_hits=Trace.commons_hits + increment)
        )

    matches.sort(key=lambda m: m["similarity"], reverse=True)
    n_failures = len(submitted)
    return {
        "n_failures": n_failures,
        "n_commons_traces": len(rows),
        "n_commons_traces_total": total_corpus,
        "corpus_truncated": corpus_truncated,
        "n_covered": n_covered,
        "covered_fraction": (n_covered / n_failures) if n_failures else 0.0,
        "threshold": threshold,
        "by_agent_type": dict(sorted(by_domain.items(), key=lambda kv: -kv[1])),
        "matches": matches,
        "note": _commons_note(n_failures, len(rows), corpus_truncated, total_corpus),
    }


# Measured, not estimated: against 46 held-out failures the corpus provably
# contains, described in on-call vocabulary rather than the corpus's own,
# the matcher found 5 -- with zero false positives across 22 deliberately
# absent failures (commons/eval/RESULTS.md). So the coverage figure this
# tool returns systematically UNDER-states real coverage, and saying so is
# not a disclaimer: a customer who reads the number as an estimate rather
# than a floor will conclude the commons is empty when it is not.
_FLOOR_CAVEAT = (
    "This figure is a FLOOR, not an estimate: matching is lexical, so a failure "
    "the commons does contain but your fleet words differently is counted as "
    "uncovered. Measured recall against known-present failures is roughly 1 in 9 "
    "(commons/eval/RESULTS.md). Matches are reliable; misses are not evidence of absence."
)


def _commons_note(
    n_failures: int, n_corpus: int, truncated: bool = False, total: int = 0
) -> str:
    """Say plainly when a number should not be leaned on. A coverage
    percentage over a handful of failures, or against an almost-empty
    commons, is noise -- and this number is exactly the kind that gets
    quoted once and repeated forever.

    Every branch that reports a real comparison also carries
    `_FLOOR_CAVEAT`, because the dominant error in this number is not
    sampling noise -- it is the matcher missing knowledge that is present.
    The two degenerate branches (nothing to compare against, nothing
    submitted) omit it: there is no measurement there to qualify.
    """
    if truncated:
        # Stated first and unambiguously: a truncated scan can only ever
        # under-count coverage, so the honest framing is a lower bound.
        return (
            f"Compared against the {n_corpus:,} most recent of {total:,} commons traces "
            f"(per-query scan limit). Real coverage is AT LEAST this figure -- treat it "
            "as a lower bound, and narrow with agent_type for a tighter answer. "
            + _FLOOR_CAVEAT
        )
    if n_corpus == 0:
        return (
            "No other org has contributed to the commons yet, so this measures nothing. "
            "Coverage is 0% by construction, not by finding."
        )
    if n_failures == 0:
        return (
            "No recurring failures submitted. Capture them with "
            "`commontrace capture --repeated-error` for this to have input."
        )
    if n_failures < 20:
        return (
            f"Only {n_failures} failures submitted -- treat this as directional. "
            f"MinHash adds roughly {100 / (commons.COMMONS_NUM_PERM ** 0.5):.0f}% "
            "standard error per comparison on top of small-sample noise. "
            + _FLOOR_CAVEAT
        )
    if n_corpus < 50:
        return (
            f"The commons holds only {n_corpus} shared traces from other orgs; "
            "coverage will grow with it. " + _FLOOR_CAVEAT
        )
    return _FLOOR_CAVEAT
