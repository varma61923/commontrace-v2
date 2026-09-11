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

import asyncio
import dataclasses
import hashlib
import json
import math
import secrets
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, Float, and_, case, delete, distinct, func, literal, or_, select, union, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from commontrace import distill, experiment, integrity, revision, value
from hub import audit, commons, outcomes, plans
from hub import search as hub_search
from hub.abuse import (
    RateLimited,
    RateLimiter,
    TraceRejected,
    reject_unstorable_text,
    suspicion_reason,
    validate_size,
)
from hub.billing import StripeError, StripeSettings, cancel_subscription
from hub.config import DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT, MAX_SEARCH_OFFSET, HubConfig
from hub.models import (
    MAX_FEEDBACK_TEXT_CHARS,
    VALID_FEEDBACK_TAGS,
    VALID_VOTE_TYPES,
    HoldoutObservation,
    KnowledgeBaseSubmission,
    Organization,
    Trace,
    TraceRelation,
    UsageCounter,
    Vote,
)
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
    title: str,
    context_text: str,
    solution_text: str,
    tags: list[str],
    agent_type: str,
    outcome: dict | None = None,
    profile: str = "",
) -> str:
    # Order-independent over tags (a client may reasonably reorder an
    # unordered set between retries) but otherwise exact -- this only needs
    # to distinguish "same logical request" from "different request", not
    # to be a general canonicalization.
    #
    # `outcome` and `profile` are new, optional trailing arguments rather
    # than inserted among the others: this function is also called by
    # submit_kb_entry, which has no concept of either and never passes
    # them, so its hash -- and every already-stored request_hash for an
    # existing KB submission -- stays byte-identical to before. Each is
    # appended only when truthy, for the same reason: a contribute_trace
    # call that never passes `profile` (every caller before this one did)
    # must keep hashing to exactly what it did before, or an idempotency
    # key stored under the old formula would misread a legitimate retry
    # made mid-deploy as IdempotencyKeyConflict. Sorted-key JSON, not a
    # delimiter join like the string fields: outcome is a dict, not a
    # string, and json.dumps(..., sort_keys=True) is a canonical,
    # order-independent serialization of it for free.
    parts = [title, context_text, solution_text, agent_type, "\x1f".join(sorted(tags))]
    if outcome:
        parts.append(json.dumps(outcome, sort_keys=True, ensure_ascii=False))
    if profile:
        parts.append(profile)
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()


def _amend_request_hash(
    trace_id: str,
    title: str | None,
    context_text: str | None,
    solution_text: str | None,
    tags: list[str] | None,
    outcome: dict | None = None,
) -> str:
    """Distinguishes "the same amend_trace call, retried" from "a different
    one that happens to reuse an idempotency_key" -- including `trace_id`
    (reusing a key against a different original is a different request, not
    a retry) and each field's None-ness (None means "carry the original
    forward unchanged", which is not the same request as an explicit
    override that happens to match the original's current value).

    `outcome` is the caller's raw argument (validated, but before it is
    merged into the original's outcome dict below) -- None here means "the
    caller did not attempt to set an outcome," which is a different request
    than one that explicitly attaches `{}`, exactly the same None-vs-value
    distinction the other fields already make. Hashing the merged RESULT
    instead would be wrong: two calls with different `outcome` arguments
    could merge to the same final dict (the second's keys already matching
    the first's committed values), and would then be wrongly treated as the
    same request on retry.

    JSON, not hand-rolled delimiters like _contribute_request_hash's: this
    hash must distinguish None from "" and from every other string a field
    could contain, and a delimiter chosen to never collide with a caller's
    title/context/solution text is not a bet worth taking when json.dumps
    already escapes unambiguously for free.
    """
    payload = [
        trace_id, title, context_text, solution_text,
        sorted(tags) if tags is not None else None,
        outcome,
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clamp_int(value: object, lo: int, hi: int, default: int) -> int:
    """Coerce a caller-supplied pagination-style parameter to an int and
    clamp it to [lo, hi]; fall back to `default` if it cannot be
    interpreted as one at all.

    `int(x)` raises ValueError for a non-numeric string and TypeError for
    None -- both already routine, expected input errors -- but also
    OverflowError for a float infinity (`int(float("inf"))`), which is
    easy to miss because it is not the exception either of the other two
    cases trains you to expect. hub/commons.py's own
    _coerce_signature has a standing comment naming exactly this failure
    mode ("surfacing as an unhandled 500"); this function had the same gap
    at a different call site, reproduced live before this fix:
    search_traces(limit=float("inf")) crashed with an uncaught
    OverflowError, which matches none of hub/server.py:_error_response's
    branches.

    Every caller of this function already treats an out-of-range but
    well-formed limit/offset as something to silently clamp rather than
    reject (e.g. search_traces's own docstring: "limit is clamped to [1,
    MAX_SEARCH_LIMIT]") -- a malformed one gets the same tolerant
    treatment here, rather than a new, stricter failure mode this function
    did not previously have.
    """
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError, OverflowError):
        return default


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


# search_traces' `brief` mode truncates to this many characters per text
# field, cut at the nearest preceding whitespace so a preview never ends
# mid-word. Not operator-configurable: it is a wire-shaping constant, not a
# deployment policy like max_text_chars (HubConfig) is -- there is no
# version of "too short to be useful" or "too long to save anything" that
# varies legitimately by deployment the way a storage/abuse limit does.
BRIEF_PREVIEW_CHARS = 240


def _preview(text: str, limit: int = BRIEF_PREVIEW_CHARS) -> str:
    """First `limit` characters of `text`, or `text` unchanged if it
    already fits. Cuts at the last whitespace inside the limit rather than
    mid-word, and only when that does not throw away more than half the
    budget -- a preview that is merely short must never be confused with
    one that was cut, so a truncated preview always ends in `…`."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _to_wire(trace: Trace, votes: list[dict], related: list[dict], *, brief: bool = False) -> dict:
    """Pure shaping -- no I/O. Callers batch-load `votes`/`related` first
    (see _votes_by_trace) rather than letting this function issue queries.

    `brief=True` (search_traces only -- see its own docstring) previews
    `context_text`/`solution_text` instead of returning them whole. Those
    two fields are individually bounded by `HubConfig.max_text_chars`
    (20,000 by default) EACH, so a full page of results (`MAX_SEARCH_LIMIT`
    = 200) can legitimately run to millions of characters -- enough to
    blow a calling agent's own context budget, not just run up its bill.

    Brief mode also drops the operational/bookkeeping fields below when
    they hold their default (empty/false/zero) value -- measured on a real
    5-result page where none of them were populated, these ~14 fields cost
    roughly as many tokens as `brief`'s own truncation saved, which meant
    "browse many with brief, then get_trace the one you want" cost MORE
    than a single non-brief call, not less. A field that actually holds
    something (a real `agent_id`, a non-zero `trust`, an `outcome`) is
    still shipped in brief mode -- only the empty defaults are omitted, and
    only when brief; full mode is unchanged so an existing caller reading
    any of these off a normal result keeps working exactly as before.
    """
    out = {
        "id": trace.id,
        "title": trace.title,
        "context_text": _preview(trace.context_text) if brief else trace.context_text,
        "solution_text": _preview(trace.solution_text) if brief else trace.solution_text,
        "tags": list(trace.tags or []),
        "agent_type": trace.agent_type,
        "created_at": _iso(trace.created_at),
        "trust": trace.trust,
        # Whether this trace is quarantined. Surfaced (unlike the
        # models.py column comment's original framing of these as
        # "governance fields, not part of the wire object") because a caller
        # that reaches a quarantined trace of its OWN -- via get_trace/
        # vote_trace by id, which do not filter quarantine the way
        # search_traces/list_tags do -- otherwise gets the full body back
        # with no indication it is excluded from search and pending review.
        # Safe to expose on every call site: get_trace/vote_trace are
        # org-scoped to the trace's owner, and the one cross-org call site
        # (commons_overlap/commons_search's `_to_commons_wire(hit)`) only
        # ever reaches rows already filtered to `quarantined.is_(False)`,
        # so this is always False there. Kept unconditional (not folded
        # into `optional` below) because its own absence must never be
        # mistaken for "not quarantined" -- the one field on this object
        # where silence is not a safe default to imply.
        "quarantined": trace.quarantined,
    }
    optional = {
        "agent_id": trace.agent_id,
        "profile": trace.profile,
        "extensions": dict(trace.extensions or {}),
        "watch_condition": trace.watch_condition,
        "review_after": trace.review_after,
        "supersedes_trace_id": trace.supersedes_trace_id or "",
        # The forward half of the same chain -- see hub/models.py's
        # docstring on these two columns. Empty string / None, matching
        # supersedes_trace_id's own convention, when this trace is still
        # the current head. get_trace is unaffected by superseded_at (it
        # fetches by id regardless -- see search_traces's docstring for
        # why the exclusion belongs there, not here): a caller who already
        # has the id of a superseded trace can still retrieve it and see
        # what replaced it.
        "superseded_by_trace_id": trace.superseded_by_trace_id or "",
        "superseded_at": trace.superseded_at.isoformat() if trace.superseded_at else "",
        "contributor": trace.contributor,
        "retrievals": trace.retrievals,
        "depth": trace.depth,
        "votes": votes,
        "related": related,
        "outcome": dict(trace.outcome or {}),
        # Whether this trace is in the CommonTrace Knowledge Base. For a
        # customer's own trace this is always False: only the operator-run
        # `hub/manage.py:commons_seed` ever sets it, and it sets it on rows
        # under the operator's own org_id, never a customer's. Kept on the
        # wire rather than dropped so that stays visibly, checkably true
        # rather than merely asserted -- a customer can see for themselves
        # that nothing of theirs is flagged.
        "shared_with_commons": trace.shared_with_commons,
        "quarantine_reason": trace.quarantine_reason,
    }
    if brief:
        # A caller checking this key programmatically never has to guess
        # whether a short-but-complete field and a truncated one look the
        # same -- they don't rely on noticing the trailing "…" either.
        out["brief"] = True
        out.update({k: v for k, v in optional.items() if v})
    else:
        out.update(optional)
    return out


def commons_visible() -> list:
    """The four conditions a row must meet to be part of the CommonTrace
    Knowledge Base, as SQLAlchemy filter clauses.

    Expressed once, here, because there are three read paths that must
    apply it identically -- `commons_overlap`, `commons_search`, and
    `vote_trace`'s Knowledge Base branch -- and the cost of one of them
    drifting is not a wrong number but a boundary violation: `commons_source
    == "seed"` is the single line that makes "no customer's trace is ever
    visible to another customer" a property of the queries rather than a
    policy (see hub/commons.py's module docstring). Three hand-maintained
    copies of a security filter is two too many; a fourth read path added
    later gets this one by construction.

    `commons_retracted_at IS NULL` is the newest of the four: an operator
    who pulls an entry (`hub/manage.py kb-retract`) expects it to stop being
    served, and "stop being served" has to mean all three paths, including
    the one that would otherwise let orgs keep voting on withdrawn content.

    Deliberately NOT included: `Trace.org_id != org_id` and
    `Trace.commons_signature IS NOT NULL`. Neither is a visibility rule --
    the first is the two matching tools declining to score a caller against
    itself, the second is a matcher precondition -- and folding them in
    here would silently break `vote_trace`, which correctly applies
    neither.

    `superseded_at IS NULL` is the newest of the five, for the same reason
    search_traces gained it (hub/models.py:Trace.superseded_at's
    docstring): a trace an org later amends stops being the org's own
    current answer, and amend_trace does not carry `shared_with_commons`
    forward onto the new row -- re-sharing a correction is a separate,
    explicit decision this function does not make for the org. Left
    unfiltered, the stale, superseded original would keep being served to
    every OTHER org from the Knowledge Base indefinitely, which is a worse
    version of the bug this predicate fixes for per-org search: there it
    misled the org that wrote it, here it would mislead every org that
    didn't.
    """
    return [
        Trace.shared_with_commons.is_(True),
        Trace.commons_source == "seed",
        Trace.quarantined.is_(False),
        Trace.commons_retracted_at.is_(None),
        Trace.superseded_at.is_(None),
    ]


def standing_of(trace: Trace, now: datetime | None = None) -> str:
    """This entry's standing (hub/commons.py:entry_standing) from the
    denormalized columns, with no additional query."""
    return commons.entry_standing(
        trust=trace.trust,
        votes=trace.commons_votes,
        review_after=trace.commons_review_after,
        now=now,
    )


def _to_commons_wire(trace: Trace, now: datetime | None = None) -> dict:
    """Projection for a Knowledge Base match (commons_overlap/
    commons_search) -- deliberately narrower than _to_wire, which is used
    everywhere a caller is looking at its OWN trace.

    Every row this ever runs against is operator-curated
    (`commons_source == "seed"`), so there is no customer disclosure
    question here the way there would be for a customer-contributed
    row -- but the projection stays narrow anyway, because the extra
    columns are simply not what a lookup needs. `contributor`,
    `extensions`/`outcome`, and `watch_condition`/`review_after`/
    `retrievals`/`depth`/`supersedes_trace_id` are operational bookkeeping
    fields with no meaning on curated content. What answers "does this
    solve my failure" is title/context/solution/tags/agent_type, plus
    `trust` so the requester can judge how reliable the match is.

    `standing` and `vote_count` join `trust` for a reason worth stating: a
    bare trust score is uninterpretable to the agent receiving it. 0.0 from
    one downvote and 0.0 from twelve are the same number and completely
    different facts, and an autonomous caller deciding whether to apply a
    suggested fix needs the difference. `standing` is that judgement made
    once, in one place (hub/commons.py:entry_standing), rather than
    re-derived by every client from a float and a hope.

    `vote_count`, not `votes`: `_to_wire`'s `votes` is the list of vote
    RECORDS -- who voted, with what free-text feedback -- and is one of the
    fields this projection exists to withhold. Two keys named the same
    thing on two projections of the same object, one an int and one a list
    of dicts, is how a caller ends up shipping the wrong one."""
    return {
        "id": trace.id,
        "title": trace.title,
        "context_text": trace.context_text,
        "solution_text": trace.solution_text,
        "tags": list(trace.tags or []),
        "agent_type": trace.agent_type,
        "trust": trace.trust,
        "vote_count": trace.commons_votes,
        "standing": standing_of(trace, now),
        "created_at": _iso(trace.created_at),
    }


async def _hydrate(session: AsyncSession, traces: list[Trace], *, brief: bool = False) -> list[dict]:
    """Wire-shape a list of already-org-scoped traces, batch-loading their
    votes and relations (2 queries total, regardless of list length)."""
    trace_ids = [t.id for t in traces]
    votes = await _votes_by_trace(session, trace_ids)
    related = await _related_by_trace(session, trace_ids)
    return [_to_wire(t, votes.get(t.id, []), related.get(t.id, []), brief=brief) for t in traces]


async def _hydrate_one(session: AsyncSession, trace: Trace) -> dict:
    return (await _hydrate(session, [trace]))[0]


# --- Entitlements: the plan, enforced ----------------------------------
#
# `hub/manage.py:kb_stats` is the measurement half (is the Knowledge Base
# actually earning its query traffic). This is the enforcement half: refuse
# a request once an org's plan-defined allowance for a metered resource is
# spent. It lives in crud.py rather than in the MCP layer for the same
# reason tenant isolation does: a limit checked at the transport is a limit
# that a second call site forgets, and hub/manage.py is already a second
# call site.

METRIC_COMMONS_QUERIES = "commons_queries"

# --- Retrieval health ---------------------------------------------------
#
# These three are NOT billing metrics. Nothing enforces a limit against
# them and no plan mentions them; they ride on UsageCounter because it is
# already an atomic (org, period, metric, n) counter that cascades on org
# deletion, and a second table with those exact properties would be a copy.
#
# They exist because the defect `hub/search.py` documents was invisible for
# the entire life of the deployment that had it. `Trace.retrievals` counts
# rows RETURNED, so a search matching nothing incremented nothing; from the
# operator's side a broken retrieval tier and a customer who simply has not
# stored much yet produce identical telemetry. An operator can now read the
# difference (`hub/crud.py:search_health`, `python -m hub.manage retrieval`)
# on real fleets rather than on the synthetic corpus in
# `hub/bench_retrieval.py`.
#
# WHAT IS DELIBERATELY NOT STORED: the query text, the lexemes, and which
# traces came back. Three integers per org per month answer the question
# "is retrieval finding anything", and a query log -- which is a log of what
# a customer's agents were struggling with, in their own words -- answers it
# no better while creating exactly the retention liability DATA_RETENTION.md
# exists to avoid.
METRIC_SEARCHES = "searches"
# Zero results for a query that DID reduce to at least one searchable term.
# The one that means retrieval found nothing.
METRIC_SEARCHES_EMPTY = "searches_empty"
# Zero results because the query reduced to no lexemes at all -- empty, or
# nothing but stopwords. Counted separately because it is a malformed
# request, not a retrieval miss, and folding the two together would let a
# client that sends junk queries mask (or manufacture) a retrieval problem.
METRIC_SEARCHES_NO_TERMS = "searches_no_terms"


def billing_period(now: datetime | None = None) -> str:
    """The current billing period as 'YYYY-MM', in UTC.

    UTC and not local time: the Hub's replicas may sit in different zones,
    and a period boundary that moves between replicas would let an org get
    a second month's allowance by hitting the right instance.
    """
    now = now or datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


async def _plan_for(session: AsyncSession, org_id: str) -> plans.Plan:
    org = await session.get(Organization, org_id)
    return plans.get(org.plan if org is not None else None)


async def _plan_and_bonus_for(session: AsyncSession, org_id: str) -> tuple[plans.Plan, int]:
    """Like `_plan_for`, plus the org's earned `bonus_commons_queries`
    (hub/plans.py:query_allowance's second argument) -- split out rather
    than folded into `_plan_for` because most callers (contribute_trace,
    amend_trace, agent counting) never need the bonus and would otherwise
    read a column they discard."""
    org = await session.get(Organization, org_id)
    if org is None:
        return plans.get(None), 0
    return plans.get(org.plan), org.bonus_commons_queries


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


async def _reserve_trace_slot(session: AsyncSession, org_id: str, plan: plans.Plan) -> None:
    """Enforce plan.max_traces for a caller about to insert a new Trace row.

    Shared by contribute_trace and amend_trace: amend_trace does not mutate
    the original row, it INSERTs a new one into the supersession chain (see
    its docstring), so it consumes a storage slot exactly like
    contribute_trace does and must be checked the same way -- otherwise an
    org already at its cap could grow storage without bound simply by
    amending instead of contributing.

    Reads Organization.trace_count -- a maintained counter (see its column
    comment in hub/models.py), not a `count(*)` over the org's traces. That
    used to be a real per-write index scan whose cost grew with the org's
    ENTIRE trace history; this is a single-row read of an already-current
    value, and stays O(1) forever regardless of how large the org's corpus
    gets. contribute_trace/amend_trace increment it, unconditionally,
    right after the insert this call is guarding actually succeeds --
    NOT here, and not gated on plan: an unlimited-plan org still needs an
    accurate count in case it is ever downgraded to a bounded one later.

    SELECT ... FOR UPDATE on the org's own row, same as before: count-then-
    insert is a TOCTOU race under concurrent callers for the SAME org
    without it, and a different org's row lock never blocks this one.
    """
    if plan.max_traces == plans.UNLIMITED:
        return
    stored = int(await session.scalar(
        select(Organization.trace_count).where(Organization.id == org_id).with_for_update()
    ) or 0)
    if not plans.within(plan.max_traces, stored):
        raise plans.EntitlementExceeded(
            metric="traces", limit=plan.max_traces, used=stored, plan=plan.name,
            remedy="Purge traces you no longer need, or move to a plan with more storage.",
        )


async def _adjust_trace_count(session: AsyncSession, org_id: str, delta: int) -> None:
    """Atomically add `delta` (positive on insert, negative on delete) to
    Organization.trace_count. A plain `UPDATE ... SET trace_count =
    trace_count + $delta` rather than a read-modify-write: two concurrent
    calls for the same org (a write and a delete racing each other, or two
    concurrent deletes) both need to land, not have the second silently
    overwrite the first's effect the way separate read-then-write steps
    would -- the same lost-update hazard `_meter`'s docstring explains for
    UsageCounter. GREATEST(0, ...) is a defensive floor, not an expected
    path: it exists so a bug elsewhere in this mechanism degrades to an
    inaccurately-low (but never negative, never crash-on-underflow) count
    rather than corrupting the column into something plans.within() would
    choke on -- it must never be relied upon to paper over a real
    increment/decrement site being missed.
    """
    if delta == 0:
        return
    await session.execute(
        update(Organization)
        .where(Organization.id == org_id)
        .values(trace_count=func.greatest(0, Organization.trace_count + delta))
    )


def _active_agent_cutoff(now: datetime | None = None) -> datetime:
    """Start of the trailing window an agent must have written in to count."""
    return (now or datetime.now(timezone.utc)) - timedelta(days=plans.ACTIVE_AGENT_WINDOW_DAYS)


async def agents_under_management(session: AsyncSession, org_id: str) -> dict:
    """How many distinct agents this org actually runs -- the expansion
    variable STRATEGY.md §12.6 concludes the business should be measured on.

    Returns the count plus the evidence for reading it honestly. `active`
    is a FLOOR whenever `unattributed_traces` is non-zero: those traces
    came from clients that sent no agent_id, and an unknown number of real
    agents hides behind the single sentinel they collapse into. Callers
    render `is_floor` rather than deciding on their own whether to trust
    the number -- the same treatment the commons coverage percentage gets,
    and for the same reason: a floor quoted as a total is how a measurement
    turns into a claim nobody can defend.

    Scoped by org_id in the WHERE clause like every other read path here
    (hub/tests/test_tenant_isolation.py).
    """
    cutoff = _active_agent_cutoff()
    named = int(await session.scalar(
        select(func.count(distinct(Trace.agent_id))).where(
            Trace.org_id == org_id,
            Trace.created_at >= cutoff,
            Trace.agent_id != "",
        )
    ) or 0)
    unattributed_traces = int(await session.scalar(
        select(func.count()).select_from(Trace).where(
            Trace.org_id == org_id,
            Trace.created_at >= cutoff,
            Trace.agent_id == "",
        )
    ) or 0)
    return {
        "active": named + (1 if unattributed_traces else 0),
        "named": named,
        "unattributed_agent_id": plans.UNATTRIBUTED_AGENT_ID,
        "unattributed_traces": unattributed_traces,
        "is_floor": unattributed_traces > 0,
        "window_days": plans.ACTIVE_AGENT_WINDOW_DAYS,
    }


async def _reserve_agent_slot(
    session: AsyncSession, org_id: str, plan: plans.Plan, agent_id: str
) -> None:
    """Enforce plan.max_agents -- but only against a NEW agent.

    The rule this implements, and the reason it is not simply
    "count >= limit -> refuse":

        An org at its cap must keep serving the fleet it already has.

    Refusing writes from agents that are already active would convert a
    commercial limit into a production outage for a paying customer, in a
    system they are running live traffic through. So the cap blocks
    EXPANSION -- registering an agent the org was not already running --
    and never blocks OPERATION. hub/tests/test_agents.py pins this.

    Two other refusals are deliberately absent:

    * An unattributed write (no agent_id) is never refused. It cannot be
      attributed to a *new* agent because it cannot be attributed at all,
      and rejecting it would break every client written before agent
      identity existed for a metering concern its author never saw.
    * Nothing here is refused for an org over its cap because the LIMIT
      was lowered (a downgrade). Those agents are already active; the same
      "never break a running fleet" rule applies, and the overage shows up
      in `manage usage` for a human to act on rather than as writes
      failing in production.

    Concurrency: the fast path -- an agent that is already active -- takes
    no lock at all, which matters because that is nearly every call once a
    fleet is running. Only a genuinely new agent pays for the
    SELECT ... FOR UPDATE on the org row, and it re-checks *after*
    acquiring it, so two concurrent registrations of the same new agent
    cannot both pass the count. Same lock and same reasoning as
    _reserve_trace_slot; a different org's row lock never blocks this one.
    """
    if plan.max_agents == plans.UNLIMITED or not agent_id:
        return

    cutoff = _active_agent_cutoff()

    async def _already_active() -> bool:
        return await session.scalar(
            select(Trace.id).where(
                Trace.org_id == org_id,
                Trace.created_at >= cutoff,
                Trace.agent_id == agent_id,
            ).limit(1)
        ) is not None

    if await _already_active():
        return

    await session.execute(
        select(Organization.id).where(Organization.id == org_id).with_for_update()
    )
    # Re-check under the lock: another transaction may have registered this
    # very agent between the unlocked check above and the lock being granted.
    if await _already_active():
        return

    active = (await agents_under_management(session, org_id))["active"]
    if not plans.within(plan.max_agents, active):
        raise plans.EntitlementExceeded(
            metric="agents", limit=plan.max_agents, used=active, plan=plan.name,
            remedy=(
                f"'{agent_id}' would be a new agent. Agents already active in the last "
                f"{plans.ACTIVE_AGENT_WINDOW_DAYS} days keep working; retire one, or move "
                "to a plan with more agents."
            ),
        )


async def entitlements(session: AsyncSession, org_id: str) -> dict:
    """Everything an org is entitled to and has used this period.

    Read-only and side-effect free, so a client can render "you have 40 of
    45 queries left" without that query itself consuming one. A meter that
    charges you for checking the meter is the kind of detail that ends up
    in a support thread.
    """
    plan, bonus = await _plan_and_bonus_for(session, org_id)
    allowance = plans.query_allowance(plan, bonus)
    used = await _usage(session, org_id, METRIC_COMMONS_QUERIES)
    traces = int(await session.scalar(
        select(func.count()).select_from(Trace).where(Trace.org_id == org_id)
    ) or 0)
    return {
        "plan": plan.name,
        "period": billing_period(),
        "commons_queries": {
            "used": used,
            "allowance": allowance,
            "remaining": plans.UNLIMITED if allowance == plans.UNLIMITED
                         else max(0, allowance - used),
            # What of `allowance` came from accepted Knowledge Base
            # submissions rather than the plan's flat grant -- broken out
            # so a client can render "why is my allowance higher than my
            # plan" without re-deriving it from kb_submissions history.
            "bonus_from_accepted_submissions": bonus,
        },
        "traces": {"used": traces, "limit": plan.max_traces},
        "agents": {**(await agents_under_management(session, org_id)), "limit": plan.max_agents},
    }


# --- The six Hub tools -------------------------------------------------


async def search_traces(
    session: AsyncSession,
    org_id: str,
    query: str = "",
    tags: list[str] | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    offset: int = 0,
    brief: bool = False,
) -> dict:
    """Returns {"traces": [...], "limit", "offset", "has_more", "terms"}.

    Four deliberate properties, all visible to callers:

    1. **`brief` trades content for headroom.** `context_text`/
       `solution_text` are each independently bounded by
       `HubConfig.max_text_chars` (20,000 by default), so a full page at
       `MAX_SEARCH_LIMIT` (200) can legitimately run to millions of
       characters -- enough to blow a calling agent's own context budget,
       not just its bill. `brief=True` previews both fields instead
       (`BRIEF_PREVIEW_CHARS`, marked with a trailing "…" when actually
       cut, plus `"brief": true` on every row so a caller never has to
       infer it from the ellipsis) while leaving id/title/tags/agent_type/
       score untouched -- enough to judge relevance and decide which
       result to fetch in full via `get_trace`. Off by default: an
       existing caller reading `context_text`/`solution_text` straight off
       a search result keeps working exactly as before.

    2. **Pagination.** This used to hard-cap at 50 results with no offset,
       so a client could never reach result 51 at all. `limit` is clamped
       to [1, MAX_SEARCH_LIMIT] and `has_more` tells the caller whether to
       page again (computed by fetching one extra row, not by a second
       COUNT query).

    3. **Matching is full-text, not substring.** The old
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

    4. **Query terms are OR-ed, not AND-ed** (`hub/search.py`). This is the
       correction of a defect that made the product's core loop return
       nothing at all for the query shape it exists to serve.

       `plainto_tsquery` ANDs: a natural-language task description became
       `'custom' & 'charg' & 'twice' & 'one' & 'order' & ...`, and a trace
       had to contain every one. `hub/bench_retrieval.py` measured the
       consequence on the shipped substrate corpus against held-out
       paraphrased probes: **0.0% recall@1, 100% zero-result**, where the
       local file tier scored 84.8% recall@1 on the same 46 records.

       That failure was invisible from inside the system. An unmatched
       search returns `{"traces": []}` with HTTP 200; the agent correctly
       concludes there is no relevant prior experience and proceeds; and
       `retrievals` -- the only retrieval telemetry there was -- counts rows
       RETURNED, so a query that matched nothing incremented nothing and
       left no record of having been asked. `SearchStat` (below) exists so
       that is no longer true of any deployment.

       Ranking is what makes relaxation safe rather than a firehose:
       `ts_rank` sums the weights of the query lexemes each row matched, so
       a trace matching five of eight terms outranks one matching one, and
       the ordering does a threshold's job without a threshold's failure
       mode of discarding the answer at a cutoff (STRATEGY.md §12.7).

    `terms` is the lexeme list the query reduced to. It is returned on
    every text search, and it is the difference between "your corpus has no
    answer" and "you asked for nothing searchable" -- a query of nothing but
    stopwords produces `[]` and matches no rows, which without this field
    is indistinguishable from an empty corpus.
    """
    limit = _clamp_int(limit, 1, MAX_SEARCH_LIMIT, DEFAULT_SEARCH_LIMIT)
    offset = _clamp_int(offset, 0, MAX_SEARCH_OFFSET, 0)
    brief = bool(brief)
    if query:
        reject_unstorable_text(query, "query")
    # search_traces has no schema validation ahead of it the way the
    # write paths do (validate_trace), so this is the only place that
    # ever checks tags is actually a LIST before it reaches the query
    # below, which binds it as a VARCHAR[] array. Reproduced live:
    # search_traces(tags="abc") passed a bare string straight through --
    # `for tag in tags` iterates the string's own characters rather than
    # raising, so nothing here caught it either -- and crashed at the SQL
    # layer with an uncaught ProgrammingError ("operator does not exist:
    # character varying[] && character varying"), not a ValueError.
    if tags is not None and not isinstance(tags, list):
        raise ValueError(f"tags must be a list of strings, got {type(tags).__name__}")
    for tag in tags or []:
        reject_unstorable_text(tag, "tag")

    # Trace.superseded_at.is_(None): the bi-temporal fix (hub/models.py's
    # docstring on the column has the full story). Before this predicate
    # existed, amend_trace's own docstring correctly described the chain
    # as "supersedes, does not mutate" -- but nothing here acted on that:
    # a search could return the STALE original, occasionally in place of
    # its correction, whenever the old wording happened to rank higher.
    # Reproduced against a live Hub before this predicate existed: amend a
    # trace, search the old wording, get the old wording back. A
    # superseded trace is still reachable directly via get_trace (Zep's
    # "invalidated, never deleted" -- see the model docstring) and via
    # `superseded_by_trace_id` from whichever row now supersedes it; it is
    # simply no longer offered as a live search RESULT, which is the one
    # place staleness actually reaches an agent's context.
    stmt = select(Trace).where(
        Trace.org_id == org_id, Trace.quarantined.is_(False), Trace.superseded_at.is_(None)
    )
    chosen = hub_search.ChosenTerms((), (), ())
    # A trace whose OWN recorded outcome says the attempt failed
    # (`outcome.resolved: false` -- an agent's self-logged, unresolved
    # occasion, not a curated solution) sorts after every other trace at
    # the same relevance, rather than competing with them on text match
    # alone. A hand-written lesson and a fleet's own escalated retry of the
    # same query otherwise rank on equal footing, and text relevance alone
    # cannot tell them apart: both describe the same failure in the same
    # words. This is a ranking floor, not a filter -- a failed attempt is
    # still findable, just never placed ahead of a better-standing result
    # for the same query.
    failed_outcome = case((Trace.outcome["resolved"].astext == "false", 1), else_=0)
    if query:
        frequencies = (
            await session.execute(hub_search.term_frequency_stmt(org_id, query))
        ).all()
        chosen = hub_search.choose_terms([(row.lexeme, row.df) for row in frequencies])
        if chosen.used:
            tsquery = hub_search.tsquery_for(chosen.used)
            stmt = stmt.where(Trace.search_vector.op("@@")(tsquery))
            # created_at ASCENDING inside a relevance tie, not descending.
            # Exact ts_rank ties are the signature of near-duplicate text,
            # and a fleet produces those constantly: it resolves an
            # occasion using a lesson, then contributes a trace describing
            # what happened, which says the same thing in the same words.
            # Ordering those newest-first returns the fleet's own most
            # recent RE-TELLING of a lesson and pushes the original off the
            # page entirely once enough occasions have accumulated -- the
            # worse result of the two to hand an agent, and the reason the
            # near-duplicate clustering below could not hold a stable
            # randomization unit (a page made only of re-tellings has no
            # fixed member to anchor to, so the unit drifted with the page:
            # measured at 8 units for one lesson over 12 occasions).
            # Oldest-first inside a tie returns the original and keeps it
            # on the page. Recency still orders the no-query browse path
            # below, which is what that path is for.
            #
            # id.desc() as a final tiebreaker: created_at alone is not
            # unique enough under concurrent inserts (or two rows sharing a
            # timestamp) to make OFFSET/LIMIT paging deterministic --
            # without a total order, Postgres is free to break ties by
            # physical row order, which is not guaranteed stable across two
            # separate queries.
            stmt = stmt.order_by(
                failed_outcome.asc(),
                hub_search.relevance(Trace.search_vector, tsquery).desc(),
                Trace.created_at.asc(),
                Trace.id.desc(),
            )
    else:
        stmt = stmt.order_by(failed_outcome.asc(), Trace.created_at.desc(), Trace.id.desc())
    if tags:
        stmt = stmt.where(Trace.tags.overlap(tags))

    if query and not chosen.used:
        # A query was asked and nothing survived to match on -- either it
        # reduced to no lexemes at all (stopwords), or every lexeme was too
        # common to discriminate. The search is not run.
        #
        # Not running it is the point. With no `search_vector` predicate
        # this statement is "every trace in the org", so falling through
        # would answer a failed search with the whole corpus. And returning
        # the rows that share one corpus-wide word would not be a weak
        # answer either -- for an agent, which injects whatever it is
        # given, it is context poisoning with this product's name on it.
        # `terms` and `terms_ignored` say which of the two happened, so an
        # empty result is never read as an empty corpus.
        rows: list[Trace] = []
        has_more = False
    else:
        # Fetch one more than asked so has_more is exact without a COUNT(*).
        stmt = stmt.offset(offset).limit(limit + 1)
        rows = list((await session.execute(stmt)).scalars().all())
        has_more = len(rows) > limit
    traces = list(rows[:limit])

    if query:
        # Recorded on the FIRST page only. Paging through a result set is
        # one act of retrieval by the agent, and counting each page as its
        # own search would make an org that pages deeply look like it
        # searches more successfully than one that does not.
        if offset == 0:
            await _record_search(session, org_id, terms=list(chosen.all_terms), results=len(traces))

    if traces:
        await session.execute(
            update(Trace)
            .where(Trace.org_id == org_id, Trace.id.in_([t.id for t in traces]))
            .values(retrievals=Trace.retrievals + 1)
        )
    return {
        "traces": await _hydrate(session, traces, brief=brief),
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "terms": list(chosen.all_terms),
        "terms_ignored": list(chosen.ignored),
    }


async def _record_search(session: AsyncSession, org_id: str, *, terms: list[str], results: int) -> None:
    """One text search, aggregated into this month's counters.

    Unmetered in the billing sense -- see the METRIC_SEARCHES comment. The
    increments go through `_meter`'s atomic upsert for the same reason the
    billing ones do: two concurrent searches from one fleet must not lose
    an increment, and a fleet is many agents by definition.
    """
    await _meter(session, org_id, METRIC_SEARCHES)
    if results:
        return
    await _meter(session, org_id, METRIC_SEARCHES_EMPTY if terms else METRIC_SEARCHES_NO_TERMS)


async def search_health(
    session: AsyncSession, org_id: str, period: str | None = None
) -> dict:
    """How often this org's searches come back with nothing, this month.

    The live version of `hub/bench_retrieval.py`. The benchmark answers the
    question on a 46-record synthetic corpus with probes the same author
    wrote; this answers it on the fleet's own corpus with the fleet's own
    queries, which is the only version that settles STRATEGY.md §13.2's
    link 2 for a real customer.

    `miss_rate` divides by searches that HAD searchable terms, not by all
    searches: a client sending empty queries would otherwise drive the rate
    up without anything being wrong with retrieval, and an operator chasing
    that number would be chasing the wrong system.

    A high miss rate is not by itself a defect -- an org three days into a
    pilot has an almost empty corpus and should miss most of the time. It
    is a defect when it stays high while the corpus grows, which is why
    `traces` is reported alongside it rather than left to be looked up.
    """
    period = period or billing_period()
    searches = await _usage(session, org_id, METRIC_SEARCHES, period)
    empty = await _usage(session, org_id, METRIC_SEARCHES_EMPTY, period)
    no_terms = await _usage(session, org_id, METRIC_SEARCHES_NO_TERMS, period)
    searchable = searches - no_terms
    stored = await session.scalar(
        select(func.count(Trace.id)).where(Trace.org_id == org_id, Trace.quarantined.is_(False))
    )
    return {
        "org_id": org_id,
        "period": period,
        "searches": searches,
        "searches_with_terms": searchable,
        "empty": empty,
        "no_terms": no_terms,
        "miss_rate": (empty / searchable) if searchable else None,
        "traces": int(stored or 0),
    }


async def _possible_duplicates(
    session: AsyncSession,
    org_id: str,
    trace_id: str,
    title: str,
    context_text: str,
    solution_text: str,
    tags: list[str],
    agent_type: str,
) -> list[str]:
    """Ids of other LIVE traces in this org that look like the same thing
    as the one just contributed -- so the caller can `amend_trace` instead
    of leaving two disagreeing answers to the same problem both live.

    Graphiti (getzep/graphiti, cloned to scratch for this pass under
    Apache-2.0) resolves this with an LLM call per extracted fact: every
    new edge is checked against related existing edges for semantic
    contradiction, and a contradicted edge is invalidated automatically
    (graphiti_core/utils/maintenance/edge_operations.py:resolve_edge_
    contradictions). That is the wrong shape to adopt here -- adapting the
    idea, not the code: `commontrace/distill.py`'s own docstring already
    made this call deliberately for the identical tradeoff ("a headless
    CLI command has no standing agent session to call out to, and baking
    in a hardcoded model call/API-key dependency here would be a much
    bigger, riskier addition than this pass is scoped for"), and this is
    the Hub's write path, hit far more often than a batch CLI command.
    Automatic invalidation is also a stronger claim than this module wants
    to make unilaterally: `amend_trace` already exists as the explicit,
    human/agent-decided path for "this replaces that", and this function
    does not call it -- it only surfaces candidates.

    So: zero-LLM, using the same word-overlap Jaccard clustering
    `_cluster_representatives` above already reuses from distill.py for
    an unrelated problem (collapsing near-duplicate randomization units),
    rather than a third implementation of "these are probably the same
    thing". And bounded, per this module's own established discipline
    against scanning the tenant on a write (hub/crud.py's module
    docstring; see also `_reserve_trace_slot`): candidates come from ONE
    indexed query (`Trace.tags.overlap`, backed by `ix_traces_tags_gin`,
    the same idiom `search_traces` already uses), capped at 25 rows, and
    skipped entirely when the new trace has no tags to narrow on --
    exactly the shape `_cluster_representatives` documents as bounded to
    "one search page, not the cluster's true membership". A trace that
    happens not to cluster with anything in this bounded, recent sample
    is simply not flagged; a full-corpus guarantee is not the point, an
    agent about to contribute a near-duplicate of something recent is.
    """
    if not tags:
        return []
    stmt = (
        select(Trace.id, Trace.title, Trace.context_text, Trace.solution_text, Trace.tags, Trace.agent_type)
        .where(
            Trace.org_id == org_id,
            Trace.id != trace_id,
            Trace.superseded_at.is_(None),
            Trace.quarantined.is_(False),
            Trace.tags.overlap(tags),
        )
        .order_by(Trace.created_at.desc())
        .limit(25)
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return []
    candidates = [
        distill.TraceCandidate(
            id=trace_id, path="", title=title, context_text=context_text,
            solution_text=solution_text, tags=tags, agent_type=agent_type,
        )
    ] + [
        distill.TraceCandidate(
            id=row.id, path="", title=row.title, context_text=row.context_text,
            solution_text=row.solution_text, tags=list(row.tags), agent_type=row.agent_type,
        )
        for row in rows
    ]
    for cluster in distill.find_clusters(candidates, existing_lessons_source_traces=[]):
        cluster_ids = {c.id for c in cluster.traces}
        if trace_id in cluster_ids:
            return sorted(cluster_ids - {trace_id})
    return []


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
    agent_id: str = "",
    profile: str = "",
    outcome: dict | None = None,
    actor: str = AUDIT_ACTOR_UNKNOWN,
    idempotency_key: str | None = None,
) -> dict:
    """Returns {"id": ..., "quarantined": bool, "quarantine_reason": str}.
    Raises TraceRejected (bad schema/oversized) or RateLimited (429-shaped)
    without storing anything.

    `outcome` is this incident's eventual disposition -- `resolved`,
    `escalated`, `repeated_error`, `frustration_signal` (booleans),
    `tokens_used`/`llm_calls` (non-negative numbers), and `baseline` (a
    flag marking a trace captured before lessons were being injected) --
    see hub/outcomes.py's `fleet_outcomes`, which is the entire reason this
    parameter exists: before it, nothing in this Hub could ever accept
    outcome data at all, so `fleet_outcomes` could only ever report "not
    enough recorded outcomes" for every real customer regardless of how
    their fleet performed. Optional, and validated the same way a
    malformed title is: an unknown key or a wrong-typed value is rejected
    rather than silently stored and silently skewing a number someone will
    eventually quote. Often not known yet at contribution time -- a task's
    resolution frequently isn't decided until after the incident is first
    reported -- so `amend_trace` accepts the same parameter to attach or
    update it once the outcome is known, without needing a second write
    path.

    `profile` names a domain-specific extension profile a trace belongs to
    (protocol/schemas/trace.schema.json, e.g. "code-review" for the
    Alpha/A/B/Omega/Lambda pipeline SKILL.md ships) -- optional, and the
    local `commontrace capture --profile` client-side flag has populated it
    on-disk since the schema was written. This was the only one of the
    Trace columns supporting it (Trace.profile/extensions/watch_condition/
    review_after) with a real write path anywhere in this codebase and
    still had no way to reach the Hub: `amend_trace` already carries all
    four forward unchanged on every amendment (see its own docstring), but
    contribute_trace -- the only place a NEW trace is created -- accepted
    none of them, so a customer's `--profile` value was silently dropped
    the moment `commontrace sync --push-traces` sent that trace onward.
    `extensions`/`watch_condition`/`review_after` stay contribute-time
    defaults for now: nothing anywhere actually sets them yet, unlike
    `profile`, and `extensions` in particular is an open `additionalProperties:
    true` object -- accepting arbitrary customer-shaped JSON into it needs
    its own validation pass (a NUL byte or lone surrogate nested inside a
    JSONB value fails at INSERT exactly like reject_unstorable_text exists
    to prevent for a flat string column) rather than reusing this fix.

    `idempotency_key` makes retries safe: an MCP client that times out
    waiting for a response cannot tell "the write never happened" from "it
    happened but the response was lost", so without a key every retry
    creates a second trace (reproduced in
    hub/tests/test_concurrency_audit.py before this was added). Passing the
    same key on a retry returns the original result instead of duplicating
    it; passing the same key with a genuinely different payload (this now
    includes `outcome`) raises IdempotencyKeyConflict rather than silently
    returning stale content. Omitting the key (the default) is unaffected
    -- NULL never conflicts with anything under the backing
    UNIQUE(org_id, idempotency_key).

    `possible_duplicates` in the return value: ids of other live traces in
    this org that a bounded, zero-LLM heuristic (`_possible_duplicates`
    above) thinks may be the same thing as this one -- informational only,
    never blocking and never auto-amending. `[]` on an idempotent replay,
    matching that path's "store nothing new" contract.
    """
    tags = tags or []

    # reject_unstorable_text BEFORE the len() check, not after: len() itself
    # raises an uncaught TypeError for a non-string value (an int, a list,
    # None passed explicitly), and reject_unstorable_text's own isinstance
    # check is what turns that into a clean ValueError instead. Reproduced
    # live: contribute_trace(idempotency_key=123) used to crash with
    # "TypeError: object of type 'int' has no len()" from THIS len() call,
    # never reaching the validator at all.
    if idempotency_key is not None:
        reject_unstorable_text(idempotency_key, "idempotency_key")
    # Trace.idempotency_key is String(128) -- checked here rather than left
    # to the INSERT below to enforce it: a too-long value raised
    # asyncpg.StringDataRightTruncation (a DataError), which is not an
    # IntegrityError and isn't caught by the IntegrityError handler further
    # down, so it reached the caller as an opaque HTTP 500 instead of a
    # clean rejection of a malformed request.
    if idempotency_key is not None and len(idempotency_key) > 128:
        raise TraceRejected(f"idempotency_key exceeds 128 chars ({len(idempotency_key)})")

    # Missed by the original reject_unstorable_text sweep: agent_id never
    # passes through validate_size (it isn't in contribute_trace's wire
    # dict, only agent_type is), and it is queried on directly by
    # _reserve_agent_slot below -- BEFORE validate_size even runs on the
    # other fields -- so an embedded NUL or lone surrogate here reached
    # Postgres from inside that SELECT, not the final INSERT. Reproduced
    # against a live Postgres: both raised uncaught DBAPIErrors
    # (CharacterNotInRepertoireError / DataError) from _reserve_agent_slot's
    # own query, the same 500-instead-of-400 failure mode this function's
    # other fields already guard against. Ahead of the len() check below
    # for the same reason as idempotency_key above.
    reject_unstorable_text(agent_id, "agent_id")
    # Trace.agent_id is String(128). Checked here for the same reason
    # idempotency_key is above: left to the INSERT, an over-long value
    # raises asyncpg.StringDataRightTruncation (a DataError, not an
    # IntegrityError), which no handler below catches, so a malformed
    # request surfaces as an opaque HTTP 500 instead of a clean rejection.
    if len(agent_id) > 128:
        raise TraceRejected(f"agent_id exceeds 128 chars ({len(agent_id)})")

    # Validated (and canonicalized -- None becomes {}) before the
    # idempotent-replay check below, so a retry's hash and the freshly
    # stored trace's hash are computed from the exact same shape either
    # way, and a malformed outcome is rejected up front rather than after
    # paying for a rate-limit check and a slot reservation first.
    outcome = outcomes.validate_outcome(outcome)

    if idempotency_key is not None:
        existing = (
            await session.execute(
                select(Trace).where(Trace.org_id == org_id, Trace.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _idempotent_replay_or_conflict(
                existing, idempotency_key, title, context_text, solution_text, tags, agent_type, outcome,
                profile,
            )

    allowed, retry_after = await rate_limiter.check(org_id)
    if not allowed:
        raise RateLimited(
            f"org {org_id} exceeded contribute_trace rate limit", retry_after=retry_after
        )

    # Checked after the idempotent-replay path above, deliberately: a
    # replay stores nothing, so refusing it at the storage limit would turn
    # a safe retry into a failure exactly when the org is at its cap.
    plan = await _plan_for(session, org_id)
    await _reserve_trace_slot(session, org_id, plan)
    # After the storage check and after the idempotent-replay path, for the
    # same reason: a replay registers no new agent, so refusing it at the
    # agent cap would turn a safe retry into a failure.
    await _reserve_agent_slot(session, org_id, plan, agent_id)

    candidate_id = str(uuid.uuid4())
    wire = {
        "id": candidate_id,
        "title": title,
        "context_text": context_text,
        "solution_text": solution_text,
        "tags": tags,
        "agent_type": agent_type,
        "profile": profile,
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
        agent_id=agent_id,
        profile=profile,
        outcome=outcome,
        quarantined=reason is not None,
        quarantine_reason=reason or "",
        idempotency_key=idempotency_key,
        request_hash=(
            _contribute_request_hash(title, context_text, solution_text, tags, agent_type, outcome, profile)
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
            existing, idempotency_key, title, context_text, solution_text, tags, agent_type, outcome,
            profile,
        )

    # Only reached on a genuine new row -- never for the idempotent-replay
    # return above, which stores nothing new and must not double-count.
    await _adjust_trace_count(session, org_id, +1)

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
    possible_duplicates = await _possible_duplicates(
        session, org_id, trace.id, title, context_text, solution_text, tags, agent_type,
    )
    return {
        "id": trace.id,
        "quarantined": trace.quarantined,
        "quarantine_reason": trace.quarantine_reason,
        "possible_duplicates": possible_duplicates,
    }


def _idempotent_replay_or_conflict(
    existing: Trace,
    idempotency_key: str,
    title: str,
    context_text: str,
    solution_text: str,
    tags: list[str],
    agent_type: str,
    outcome: dict | None = None,
    profile: str = "",
) -> dict:
    incoming_hash = _contribute_request_hash(
        title, context_text, solution_text, tags, agent_type, outcome, profile
    )
    if existing.request_hash != incoming_hash:
        raise IdempotencyKeyConflict(
            f"idempotency_key {idempotency_key!r} was already used for a different contribute_trace "
            "payload; reuse a key only to retry the exact same request"
        )
    return {
        "id": existing.id,
        "quarantined": existing.quarantined,
        "quarantine_reason": existing.quarantine_reason,
        # A replay stores nothing new, so there is nothing new to check for
        # duplicates of -- see contribute_trace's docstring on this key.
        "possible_duplicates": [],
    }


async def _amend_idempotent_replay_or_conflict(
    session: AsyncSession,
    existing: Trace,
    idempotency_key: str,
    trace_id: str,
    title: str | None,
    context_text: str | None,
    solution_text: str | None,
    tags: list[str] | None,
    outcome: dict | None = None,
) -> dict:
    incoming_hash = _amend_request_hash(trace_id, title, context_text, solution_text, tags, outcome)
    if existing.request_hash != incoming_hash:
        raise IdempotencyKeyConflict(
            f"idempotency_key {idempotency_key!r} was already used for a different amend_trace "
            "call; reuse a key only to retry the exact same request"
        )
    # Unlike contribute_trace's replay (a fixed {id, quarantined,
    # quarantine_reason} shape), amend_trace's normal success return is the
    # full hydrated trace -- a replay must match that shape too, or a
    # retrying client sees a different response shape than the original
    # call got.
    return await _hydrate_one(session, existing)


def _is_uuid(value: str) -> bool:
    """Whether `value` is acceptable to bind against a UUID column.

    Every function below takes a caller-supplied trace_id and compares it
    directly against `Trace.id` (a UUID column) in a WHERE clause. asyncpg
    validates the bind parameter against the column's real type -- a
    non-UUID string raises asyncpg.DataError, which SQLAlchemy wraps as
    DBAPIError, neither of which is an IntegrityError or any of the other
    exception types hub/server.py's _error_response maps to a clean 4xx.
    Unhandled, that reached callers as an opaque HTTP 500 instead of the
    same "not found" a well-formed-but-nonexistent id already produces.
    Checking here lets a malformed id take the identical not-found path
    (see get_trace's own docstring on why 404, never 403, for a foreign
    org's id -- the same "reveal nothing extra" reasoning applies to a
    malformed id revealing nothing about whether IT exists either).
    """
    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


async def get_trace(session: AsyncSession, org_id: str, trace_id: str) -> dict | None:
    if not _is_uuid(trace_id):
        return None
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
    if vote_type not in VALID_VOTE_TYPES:
        raise ValueError(f"vote_type must be 'up' or 'down', got {vote_type!r}")
    # Validated here, at the application layer, rather than left to the DB's
    # own CHECK constraints (hub/models.py:Vote.__table_args__): a
    # constraint violation surfaces as an uncaught IntegrityError, which
    # falls through to _error_response's generic 500 rather than the clean
    # 400 a malformed request should get. feedback_text has no DB-level cap
    # at all otherwise -- unbounded Text, and every vote writes an
    # AuditLogEntry, so an unbounded field is a cheap storage/audit-log
    # flooding vector.
    if feedback_tag not in VALID_FEEDBACK_TAGS:
        raise ValueError(f"feedback_tag must be one of {VALID_FEEDBACK_TAGS!r}, got {feedback_tag!r}")
    # reject_unstorable_text before the len() check -- see contribute_trace's
    # identical fix for why: len() itself raises an uncaught TypeError for a
    # non-string value. Reproduced live: vote_trace(feedback_text=123) used
    # to crash with "TypeError: object of type 'int' has no len()" from the
    # len() call below, never reaching the isinstance check that would turn
    # it into a clean ValueError.
    reject_unstorable_text(feedback_text, "feedback_text")
    if len(feedback_text) > MAX_FEEDBACK_TEXT_CHARS:
        raise ValueError(
            f"feedback_text exceeds {MAX_FEEDBACK_TEXT_CHARS} chars ({len(feedback_text)})"
        )
    if not _is_uuid(trace_id):
        return None

    # An org may vote on its own trace, or on a CommonTrace Knowledge Base
    # entry (operator-curated, `commons_source == "seed"`) -- previously
    # this was scoped to `Trace.org_id == org_id` only, which made "vote"
    # mean "the owner rates its own submission": trust could only ever be
    # 0.0/0.5/1.0 from a single self-interested party, never a real signal,
    # even though a Knowledge Base entry's `trust` is surfaced to every org
    # it matches for (commons_overlap/commons_search's projection,
    # hub/crud.py:_to_commons_wire). This is customer-to-operator-content
    # feedback -- rating a Stack-Overflow-style answer -- never
    # customer-to-customer: gated on the same `commons_visible()` boundary
    # the two Knowledge Base queries enforce (shared AND seed-sourced AND
    # not quarantined AND not retracted -- not merely "any trace, any org",
    # which would let an org vote on private traces it has no business
    # seeing at all).
    # SELECT ... FOR UPDATE on the trace's own row: the tally computed
    # below (COUNT grouped by vote_type, several lines down) and the
    # UPDATE that writes trust/commons_votes from it are NOT atomic with
    # each other, so two orgs voting on the same trace at nearly the same
    # time can each compute their tally from a snapshot that does not yet
    # include the other's just-upserted vote -- both COUNTs run under
    # READ COMMITTED before either has committed, so neither sees the
    # other's row yet. Whichever UPDATE commits second then overwrites
    # trust/commons_votes with ITS stale total, silently discarding the
    # first vote's contribution to both numbers even though the Vote row
    # itself is durably on record (hub/commons.py:entry_standing's
    # disputed/established classification reads exactly this
    # trust/commons_votes pair, so this was a real, silent scoring
    # corruption, not just an internal accounting nit). Locking the trace
    # row up front -- the same `SELECT ... FOR UPDATE` pattern
    # _reserve_trace_slot/_reserve_agent_slot already use for other
    # count-then-write races in this file -- serializes concurrent voters
    # on the SAME trace so the second one's COUNT runs only after the
    # first has committed, and therefore sees it. A different trace's row
    # lock never blocks this one.
    stmt = select(Trace).where(
        Trace.id == trace_id,
        or_(Trace.org_id == org_id, and_(*commons_visible())),
    ).with_for_update()
    trace = (await session.execute(stmt)).scalar_one_or_none()
    if trace is None:
        return None
    is_owner = trace.org_id == org_id

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
    #
    # `commons_votes` rides along in the same statement, from the same
    # tally, for the same reason: it is the denominator `trust` throws
    # away, and hub/commons.py:entry_standing cannot tell a disputed entry
    # from a single disgruntled voter without it. Written as an assignment
    # rather than `commons_votes + 1` because this path UPSERTs -- an org
    # changing its existing vote must leave the total where it was, and
    # `total` above is already the authoritative post-write count.
    await session.execute(
        update(Trace).where(Trace.id == trace_id).values(trust=new_trust, commons_votes=total)
    )
    set_committed_value(trace, "trust", new_trust)
    set_committed_value(trace, "commons_votes", total)

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
    if is_owner:
        return await _hydrate_one(session, trace)
    # A vote on a Knowledge Base entry gets the same narrow projection
    # commons_overlap/commons_search return (_to_commons_wire) -- voting on
    # an entry is not an invitation to see operator-internal metadata
    # (contributor, extensions, outcome, ...), only to confirm the vote
    # registered and see the entry's current trust score.
    return _to_commons_wire(trace)


async def amendment_chain(session: AsyncSession, trace_id: str) -> set[str]:
    """Every trace id in `trace_id`'s amendment lineage: itself, every
    trace it (transitively) supersedes, and every trace that (transitively)
    supersedes it.

    amend_trace creates a NEW row that carries most of the original's
    content forward unchanged (see amend_trace below) -- title/context/
    solution_text can be identical or near-identical across the whole
    chain. A delete scoped to a single id in the middle of that chain
    leaves the same content sitting in its neighbors, which is exactly the
    gap "delete this trace" is supposed to close. Shared by
    hub/manage.py:purge_trace and delete_trace below -- both walk the same
    lineage, one at operator-CLI trust, one self-service and org-scoped.

    Expressed as a single recursive CTE rather than a Python-side BFS that
    issued one round trip per chain level: `commontrace/hub_client.py`'s
    documented curation pattern is repeated `amend_trace` calls on the same
    trace ("each attaching whatever became known since"), and nothing caps
    how deep that chain gets -- delete_trace below is a customer-reachable
    MCP tool, so an org whose chain grew into the thousands would otherwise
    hold this transaction (and its pooled connection) open for thousands of
    sequential queries. Walking both link directions from one seed row in
    one query costs the same either way. UNION (not UNION ALL) also gives
    the recursion its own cycle guard for free: Postgres stops expanding a
    branch once it re-derives a row already in the working table, so a
    malformed chain cannot recurse forever.
    """
    # Two separate single-direction recursive CTEs, unioned, rather than one
    # bidirectional CTE: Postgres requires a recursive term to reference its
    # own CTE exactly once, so a single `chain` walking both
    # `supersedes_trace_id` outward (to ancestors) and inward (to
    # descendants) in one recursive term is rejected outright
    # (`InvalidRecursionError`) -- verified against a real Postgres, not
    # just a compiled-SQL guess. Each CTE below references itself once, so
    # both are legal, and the pair still costs exactly one round trip.
    # Typed against Trace.id's own column type: an untyped literal binds as
    # VARCHAR, and Postgres has no `uuid = character varying` operator, so
    # the very first join above would fail outright rather than just being
    # slow -- caught by running this against a real Postgres, not merely a
    # compiled-SQL check.
    seed = literal(trace_id, type_=Trace.id.type)
    ancestors = select(seed.label("id")).cte(name="ancestors", recursive=True)
    ancestors = ancestors.union(
        select(Trace.supersedes_trace_id.label("id"))
        .join(ancestors, Trace.id == ancestors.c.id)
        .where(Trace.supersedes_trace_id.isnot(None))
    )
    # Seeded from the WHOLE `ancestors` chain, not just `seed` alone: a
    # `supersedes_trace_id` column is a single FK per row (at most one
    # parent), so this relation is a forest, but any node can have several
    # CHILDREN -- a fork, exactly what amend_trace's own docstring documents
    # as a real, reachable case (an unkeyed retry creates a second trace
    # superseding the same original instead of extending the chain). A
    # descendants walk seeded only from `seed` finds seed's own descendants
    # but never a sibling that forked off an ANCESTOR of seed rather than
    # off seed itself. Seeding from every id already known to be in the
    # ancestor chain means the first recursive step finds every direct
    # child of every one of those ids -- forks included -- and every
    # further step finds that fork's own descendants the same way,
    # recovering the whole connected subtree exactly as the BFS this
    # replaced did (reproduced missing a fork against a live Postgres
    # before this fix; see test_amendment_chain_includes_a_fork_off_an_ancestor).
    descendants = select(ancestors.c.id.label("id")).cte(name="descendants", recursive=True)
    descendants = descendants.union(
        select(Trace.id.label("id")).join(descendants, Trace.supersedes_trace_id == descendants.c.id)
    )
    rows = (
        await session.execute(union(select(ancestors.c.id), select(descendants.c.id)))
    ).scalars().all()
    return set(rows)


async def delete_trace(session: AsyncSession, org_id: str, trace_id: str, actor: str = AUDIT_ACTOR_UNKNOWN) -> bool:
    """Self-service deletion: an org permanently deletes one of its own
    traces via its own API key, plus every trace in its amendment chain
    (amendment_chain above) -- the same completeness guarantee
    hub/manage.py:purge_trace gives an operator, now reachable without
    operator/DB-access trust.

    Org-scoped and 404-shaped like every other read path here: a foreign
    org's trace id (or a malformed one) returns False rather than raising,
    so a caller cannot use this to learn whether an id exists in another
    org. The chain-membership filter additionally requires
    `Trace.org_id == org_id` on every id actually deleted -- amend_trace
    can never produce a chain spanning two orgs, so this is defense in
    depth, not a case that should ever trigger, exactly like
    `commons_source == "seed"` is for the Knowledge Base boundary.

    Irreversible, immediately, no grace period -- unlike whole-account
    deletion below. Deleting traces one at a time is not categorically
    riskier than what a compromised key can already do with amend_trace
    (overwrite every trace's content); a confirmation delay would protect
    against a different threat (a client bug or a compromised key wiping
    EVERYTHING in one call) that only the whole-account path actually poses.
    """
    if not _is_uuid(trace_id):
        return False
    trace = await session.get(Trace, trace_id)
    if trace is None or trace.org_id != org_id:
        return False

    chain_ids = await amendment_chain(session, trace_id)
    # amendment_chain itself is not org-scoped (it has no org_id to scope
    # by -- see its own docstring), so re-verify ownership of every id in
    # the chain here before deleting anything keyed by it. amend_trace can
    # never actually produce a chain spanning two orgs, so this should
    # never narrow chain_ids at all -- but without it, a TraceRelation
    # delete below would have no equivalent guard to the org_id filter the
    # original code already gave the Trace delete, and would remove
    # another org's relation-graph edges even on the day that assumption
    # stops holding.
    own_chain_ids = {
        row[0]
        for row in (
            await session.execute(select(Trace.id).where(Trace.id.in_(chain_ids), Trace.org_id == org_id))
        ).all()
    }
    await session.execute(
        delete(TraceRelation).where(
            TraceRelation.related_trace_id.in_(own_chain_ids),
        )
    )
    await session.execute(delete(Trace).where(Trace.id.in_(own_chain_ids)))
    # own_chain_ids, not chain_ids -- decrement by exactly what was
    # actually deleted above, which is the same defense-in-depth
    # own_chain_ids exists for in the first place (see this function's
    # docstring): a chain that somehow included a foreign id must not
    # decrement this org's count for a row that was never deleted (or
    # never even belonged to it).
    await _adjust_trace_count(session, org_id, -len(own_chain_ids))
    await audit.record(
        session, actor=actor, action="delete_trace", org_id=org_id,
        target_type="trace", target_id=trace_id,
        summary=f"irreversible n_amendment_chain={len(chain_ids)}",
    )
    return True


# --- Self-service account deletion --------------------------------------
#
# A single-call `delete_org` would let one compromised API key wipe an
# org's ENTIRE history irreversibly, with no window for anyone to notice.
# request_org_deletion/confirm_org_deletion split that into two
# differently-named calls with a mandatory minimum delay between them
# (DELETION_GRACE_SECONDS) -- long enough for an operator watching the
# audit log (every request is recorded) to revoke a compromised key via
# `hub.manage revoke-key` before the second call can succeed. See
# hub/models.py:Organization's comment on the columns this uses.

DELETION_GRACE_SECONDS = 300
DELETION_TOKEN_TTL_HOURS = 24


class DeletionNotReady(ValueError):
    """Raised when confirm_org_deletion is called before the grace period
    has elapsed, with no matching request, or past the token's expiry."""


class SubscriptionCancellationFailed(RuntimeError):
    """Raised when confirm_org_deletion (or hub/manage.py:purge_org)
    could not cancel an org's live Stripe subscription. The org is NOT
    deleted when this is raised -- see billing.cancel_subscription's
    docstring for why leaving a subscription active with no org row left
    to reconcile it against is worse than a deletion that must be
    retried."""


def _hash_deletion_token(raw_token: str) -> str:
    # A 256-bit random token has no meaningful offline-brute-force surface
    # for a slow KDF to defend against (unlike a human-memorable password),
    # so a fast hash is the right tool here -- same reasoning as
    # _contribute_request_hash elsewhere in this module, not the argon2id
    # hub/auth.py uses for API keys.
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


async def request_org_deletion(session: AsyncSession, org_id: str, actor: str = AUDIT_ACTOR_UNKNOWN) -> dict:
    """Start the two-step self-service account deletion. Deletes nothing.

    Returns {"confirmation_token", "confirm_not_before", "expires_at"}. The
    raw token is returned exactly once, here -- only its hash is stored,
    same as an API key. A second call before this one is confirmed replaces
    the pending request outright (a fresh token, a fresh grace-period
    clock), so an org is never locked into an old token it lost track of.
    """
    org = await session.get(Organization, org_id)
    if org is None:
        raise ValueError(f"no such organization: {org_id}")

    raw_token = "ctd_" + uuid.uuid4().hex + uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    org.deletion_token_hash = _hash_deletion_token(raw_token)
    org.deletion_requested_at = now
    org.deletion_expires_at = now + timedelta(hours=DELETION_TOKEN_TTL_HOURS)

    await audit.record(
        session, actor=actor, action="request_org_deletion", org_id=org_id,
        target_type="org", target_id=org_id,
        summary=f"confirm_not_before={(now + timedelta(seconds=DELETION_GRACE_SECONDS)).isoformat()}",
    )
    return {
        "confirmation_token": raw_token,
        "confirm_not_before": _iso(now + timedelta(seconds=DELETION_GRACE_SECONDS)),
        "expires_at": _iso(org.deletion_expires_at),
    }


async def cancel_org_deletion(session: AsyncSession, org_id: str, actor: str = AUDIT_ACTOR_UNKNOWN) -> bool:
    """Cancel a pending deletion request. Needs no token: this is a safety
    action, not a destructive one, so any of the org's own valid API keys
    may call it -- the asymmetry with confirm (which DOES need the token)
    is deliberate. Returns False if there was no pending request."""
    org = await session.get(Organization, org_id)
    if org is None or org.deletion_token_hash is None:
        return False
    org.deletion_token_hash = None
    org.deletion_requested_at = None
    org.deletion_expires_at = None
    await audit.record(
        session, actor=actor, action="cancel_org_deletion", org_id=org_id,
        target_type="org", target_id=org_id, summary="pending deletion request cancelled",
    )
    return True


async def confirm_org_deletion(
    session: AsyncSession, org_id: str, token: str, actor: str = AUDIT_ACTOR_UNKNOWN,
    stripe: StripeSettings = StripeSettings(),
) -> bool:
    """The second call: permanently deletes the org and everything scoped
    to it (api_keys, traces, votes, kb_submissions -- all FK
    ondelete=CASCADE, hub/models.py). Irreversible.

    Raises DeletionNotReady (never a bare ValueError, so a client can
    distinguish "not yet" from "malformed request") for: no pending
    request, a token that does not match, a call before
    DELETION_GRACE_SECONDS has elapsed since the request, or a token past
    its DELETION_TOKEN_TTL_HOURS expiry -- the last two are exactly the
    window the two-call design exists to create.

    If this org has a live Stripe subscription (`stripe_subscription_id`),
    it is cancelled FIRST, before anything is deleted. An org row deleted
    out from under an active subscription would keep charging that
    customer's card every billing cycle with no CommonTrace account left
    to ever notice -- charged and gone is a strictly worse failure than a
    deletion that has to be retried, so `SubscriptionCancellationFailed`
    is raised (and nothing is deleted) rather than deleting anyway on a
    Stripe error. `stripe` defaults to an unconfigured `StripeSettings()`
    for every caller (almost all of hub/tests/) that has no Stripe
    settings to pass and no subscription that could exist to cancel.
    """
    org = await session.get(Organization, org_id)
    if org is None:
        raise ValueError(f"no such organization: {org_id}")
    if org.deletion_token_hash is None or org.deletion_requested_at is None:
        raise DeletionNotReady("no pending deletion request for this organization")

    now = datetime.now(timezone.utc)
    if org.deletion_expires_at is not None and now > org.deletion_expires_at:
        raise DeletionNotReady("the confirmation token has expired; call request_account_deletion again")
    if now < org.deletion_requested_at + timedelta(seconds=DELETION_GRACE_SECONDS):
        wait = (org.deletion_requested_at + timedelta(seconds=DELETION_GRACE_SECONDS) - now).total_seconds()
        raise DeletionNotReady(f"too soon: wait {int(wait)} more second(s) before confirming")
    if not secrets.compare_digest(_hash_deletion_token(token), org.deletion_token_hash):
        raise DeletionNotReady("confirmation token does not match the pending request")

    if org.stripe_subscription_id:
        try:
            await cancel_subscription(stripe, subscription_id=org.stripe_subscription_id)
        except StripeError as exc:
            raise SubscriptionCancellationFailed(
                f"could not cancel the active Stripe subscription for org {org_id}; "
                f"account deletion was NOT performed: {exc}"
            ) from exc

    trace_ids = (await session.execute(select(Trace.id).where(Trace.org_id == org_id))).scalars().all()
    if trace_ids:
        await session.execute(delete(TraceRelation).where(TraceRelation.related_trace_id.in_(trace_ids)))
    org_name = org.name
    had_subscription = bool(org.stripe_subscription_id)
    await session.delete(org)
    await audit.record(
        session, actor=actor, action="confirm_org_deletion", org_id=org_id,
        target_type="org", target_id=org_id,
        summary=f"name={org_name!r} n_traces={len(trace_ids)} "
                f"stripe_subscription_cancelled={had_subscription} irreversible",
    )
    return True


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
    outcome: dict | None = None,
    actor: str = AUDIT_ACTOR_UNKNOWN,
    idempotency_key: str | None = None,
) -> dict | None:
    """Creates a new Trace that supersedes `trace_id`, rather than mutating
    history in place -- consistent with Trace.supersedes_trace_id /
    Trace.depth being an amendment *chain*, not an overwrite.

    `outcome`, unlike title/context_text/solution_text/tags, is MERGED into
    the original's outcome rather than replaced when provided: an
    incident's resolution is frequently not known at contribution time (see
    contribute_trace's docstring), so the normal shape of use is
    contribute_trace with no outcome, then one or more amend_trace calls
    each attaching whatever became known since -- `{"resolved": true}` now,
    `{"tokens_used": 800}` from a later call, without one clobbering the
    other. Replace-semantics would make that pattern actively hostile: a
    second amend_trace call attaching `tokens_used` would silently erase
    the `resolved` flag the first one set. `None` (the default) leaves the
    original's outcome untouched, exactly like every other field here.

    `idempotency_key` makes retries safe, for the same reason and the same
    way contribute_trace's does (see its docstring): an MCP client that
    times out waiting for a response cannot tell "the amendment never
    happened" from "it happened but the response was lost". Without a key,
    a retry called amend_trace(trace_id=X, ...) again against the same,
    still-unmutated original and created a SECOND trace superseding X --
    forking the supersession chain instead of extending it, rather than
    creating a duplicate sibling the way an unkeyed contribute_trace retry
    does (reproduced against a live Postgres before this was added: two
    "identical" retries left two rows both pointing at the same
    supersedes_trace_id). Passing the same key on a retry returns the
    original amendment instead of forking it; passing the same key with a
    genuinely different request (a different trace_id, or different field
    overrides, including a different `outcome`) raises
    IdempotencyKeyConflict rather than silently returning the wrong trace.
    """
    if not _is_uuid(trace_id):
        return None

    # reject_unstorable_text before the len() check -- see contribute_trace's
    # identical fix for why: len() itself raises an uncaught TypeError for a
    # non-string value, never reaching the isinstance check that would turn
    # it into a clean ValueError.
    if idempotency_key is not None:
        reject_unstorable_text(idempotency_key, "idempotency_key")
    if idempotency_key is not None and len(idempotency_key) > 128:
        raise TraceRejected(f"idempotency_key exceeds 128 chars ({len(idempotency_key)})")
    # Validated (and canonicalized -- None becomes {}) up front, same as
    # contribute_trace, so the idempotent-replay hash below and the
    # eventually-stored hash are computed from the same shape either way.
    outcome = outcomes.validate_outcome(outcome)

    stmt = select(Trace).where(Trace.id == trace_id, Trace.org_id == org_id)
    original = (await session.execute(stmt)).scalar_one_or_none()
    if original is None:
        return None

    if idempotency_key is not None:
        existing = (
            await session.execute(
                select(Trace).where(Trace.org_id == org_id, Trace.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return await _amend_idempotent_replay_or_conflict(
                session, existing, idempotency_key, trace_id, title, context_text, solution_text, tags, outcome
            )

    # amend_trace is a WRITE path and carries caller-supplied content, so it
    # gets the same four guards contribute_trace does. Without them it was
    # the way around all of them: unlimited writes, unbounded storage growth
    # past plan.max_traces (amend_trace INSERTs a new row -- see this
    # function's docstring -- so it consumes a storage slot exactly like
    # contribute_trace and must be capped the same way), unvalidated
    # payloads (a title past the column width became a hard 500 rather than
    # a clean rejection), and spam that quarantine would have caught on the
    # way in.
    allowed, retry_after = await rate_limiter.check(org_id)
    if not allowed:
        raise RateLimited(f"org {org_id} exceeded write rate limit", retry_after=retry_after)

    plan = await _plan_for(session, org_id)
    await _reserve_trace_slot(session, org_id, plan)

    resolved_title = title if title is not None else original.title
    resolved_context = context_text if context_text is not None else original.context_text
    resolved_solution = solution_text if solution_text is not None else original.solution_text
    resolved_tags = tags if tags is not None else list(original.tags or [])
    # MERGED, not replaced -- see amend_trace's docstring: `outcome` is
    # `{}` (a no-op update()) when the caller did not attempt to set one,
    # so this line is exactly "carry the original forward unchanged" in
    # that case, and "carry forward, then layer in whatever this call
    # newly knows" otherwise.
    resolved_outcome = dict(original.outcome or {})
    resolved_outcome.update(outcome)

    amended_id = str(uuid.uuid4())
    wire = {
        "id": amended_id,
        "title": resolved_title,
        "context_text": resolved_context,
        "solution_text": resolved_solution,
        "tags": resolved_tags,
        "agent_type": original.agent_type,
        "profile": original.profile,
    }
    validate_trace(wire)
    validate_size(wire, config)
    reason = suspicion_reason(wire, config)
    # Quarantine is inherited, never re-decided from scratch by the
    # heuristic alone: without this, a quarantined trace stays quarantined
    # only until whoever quarantined it (spammer or not) makes a small edit
    # that happens not to trip suspicion_reason on the new text -- amending
    # would otherwise be an unsupervised way around a state that is supposed
    # to require an operator's release_quarantine (hub/manage.py) to lift.
    # A trace that was NOT quarantined can still become quarantined by this
    # amendment's own content, same as contribute_trace.
    quarantined = original.quarantined or reason is not None
    quarantine_reason = (
        original.quarantine_reason if original.quarantined and original.quarantine_reason
        else (reason or "")
    )

    amended = Trace(
        id=amended_id,
        org_id=org_id,
        title=resolved_title,
        context_text=resolved_context,
        solution_text=resolved_solution,
        tags=resolved_tags,
        agent_type=original.agent_type,
        # No override parameter, so it must carry forward unchanged like
        # agent_type/profile/extensions/watch_condition/review_after/
        # contributor below -- without this, amend_trace (which INSERTs a
        # new row rather than mutating the original) silently reset every
        # amended trace's agent_id to "" the moment anyone amended it,
        # collapsing that agent back into the 'unattributed' bucket
        # capture_cmd.py's own agent_id docs warn about, and quietly
        # undercounting agents_under_management/plan.max_agents for any
        # org whose agents get amended traces (the common case: an
        # amendment is how an outcome gets attached after the fact).
        agent_id=original.agent_id,
        profile=original.profile,
        extensions=dict(original.extensions or {}),
        # These three have no override parameter (a caller amending title
        # can't currently ask to change them), so -- like agent_type,
        # agent_id, and profile/extensions above -- they must carry forward
        # unchanged. Without this they silently reset to their column
        # defaults ("" / {}) on every amendment: a trace's contributor
        # attribution would vanish the first time anyone tweaked its
        # title, with no error and no audit trail of the loss. `outcome`
        # DOES have an override parameter -- see resolved_outcome above --
        # so it is merged rather than blindly carried forward.
        watch_condition=original.watch_condition,
        review_after=original.review_after,
        contributor=original.contributor,
        outcome=resolved_outcome,
        supersedes_trace_id=original.id,
        depth=original.depth + 1,
        quarantined=quarantined,
        quarantine_reason=quarantine_reason,
        idempotency_key=idempotency_key,
        request_hash=(
            _amend_request_hash(trace_id, title, context_text, solution_text, tags, outcome)
            if idempotency_key is not None
            else None
        ),
    )
    # Bi-temporal supersession (hub/models.py:Trace.superseded_at, adapted
    # from Zep/Graphiti's bi-temporal fact model): the row being amended
    # records its own invalidation, set in the SAME flush as the amending
    # INSERT so the two are atomic -- a reader can never observe a new
    # head with no superseded original, or vice versa. `original` is
    # already the loaded, session-attached row fetched above; mutating its
    # attributes and letting flush() emit the UPDATE is the ordinary
    # SQLAlchemy unit-of-work pattern, not a second explicit statement.
    # Skipped entirely on the idempotent-replay paths above (both return
    # before reaching here), which is correct: a replay observes the
    # amendment that already happened rather than re-performing it.
    #
    # `original` is not required to be the current head of its lineage --
    # amend_trace intentionally accepts a stale (already-superseded)
    # trace_id too, and this is how the supersession graph FORKS (see this
    # function's docstring on idempotency_key, and
    # test_manage.py:test_amendment_chain_includes_a_fork_off_an_ancestor):
    # two independent amend_trace calls against the same still-unmutated
    # original are both accepted, producing two children of one parent.
    # superseded_by_trace_id is a single scalar column, so on a fork it can
    # only end up naming ONE child -- whichever amend_trace call reaches
    # this line last, silently overwriting the other's pointer -- while
    # supersedes_trace_id (the backward pointer, set below on `amended`)
    # stays accurate for every child regardless of forking, since each
    # child gets its own row. This is an accepted limitation of the
    # forward pointer as a search/commons-visibility convenience, not a
    # correctness bug: superseded_at is set correctly on `original` either
    # way (excluding it from search_traces/commons_visible()), and both
    # forked children still get superseded_at IS NULL and stay live and
    # independently searchable, which is what actually matters for this
    # column's purpose. crud.amendment_chain, not this scalar, is the
    # authoritative source when a fork's complete set of children matters
    # (e.g. hub/manage.py:purge_trace's delete of a whole lineage).
    original.superseded_at = datetime.now(timezone.utc)
    original.superseded_by_trace_id = amended_id

    session.add(amended)
    try:
        await session.flush()
    except IntegrityError:
        # Lost the race: a concurrent retry with the same (org_id,
        # idempotency_key) committed first. Roll back this attempt and
        # treat it exactly like we'd found the row up front -- same
        # handling as contribute_trace's identical race.
        await session.rollback()
        existing = (
            await session.execute(
                select(Trace).where(Trace.org_id == org_id, Trace.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing is None:
            raise  # the constraint fired for some other reason; don't mask it
        return await _amend_idempotent_replay_or_conflict(
            session, existing, idempotency_key, trace_id, title, context_text, solution_text, tags, outcome
        )

    # Only reached on a genuine new row -- see contribute_trace's identical
    # comment. amend_trace INSERTs rather than mutating (this function's
    # own docstring), so this is a real new storage slot exactly like
    # contribute_trace's, not a no-op that would double-count on replay.
    await _adjust_trace_count(session, org_id, +1)

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
                 f"changed={','.join(changed) or 'nothing'} quarantined={quarantined}"),
    )
    return await _hydrate_one(session, amended)


async def list_tags(session: AsyncSession, org_id: str) -> list[str]:
    """Every distinct tag used across this org's non-quarantined traces,
    computed in the database (SELECT DISTINCT unnest(tags)) rather than by
    pulling every trace's full tags array into Python and deduplicating
    there -- an org with a large trace store previously materialized its
    entire tags column into memory (and paid the network transfer for
    every duplicate) just to answer "what are the distinct tags", every
    single call."""
    stmt = (
        select(func.unnest(Trace.tags))
        .distinct()
        .where(Trace.org_id == org_id, Trace.quarantined.is_(False))
    )
    tags = (await session.execute(stmt)).scalars().all()
    return sorted(tags)


# --- Randomized holdout: the only causal instrument here ----------------
#
# `fleet_outcomes` below compares a fleet against its own past, which
# cannot separate this product's contribution from anything else that
# changed in the same window. This compares two arms of the same fleet in
# the SAME window, differing only by whether the memory was injected. That
# is what makes "what else changed that quarter?" answerable, and the
# answer "nothing, by construction".
#
# STRATEGY.md §11.3 names causally-measured memory as the entire moat, and
# §13.2 calls running it "the cheapest falsifier in the document" and says
# to run it first. Both were true of commontrace/experiment.py, which
# works against a local file store -- and nothing in the Hub could do it,
# so the falsifier could not be run on the surface where paying customers
# actually are. These three functions are that surface.
#
# The assignment function itself is IMPORTED from commontrace.experiment,
# for the third time in this codebase and the third variation on one
# reason: a near-copy that drifted would not fail loudly. Here the
# specific failure is that a fleet running both the local CLI and the Hub
# would randomize the same lesson two different ways and silently compare
# two mixtures, biasing every effect estimate toward zero.


class ExperimentNotRunning(ValueError):
    """holdout_assign was called for an org with no experiment configured.

    A clean error rather than a silent "inject everything": a client that
    believes it is running an experiment and is actually not would produce
    an all-injected dataset that looks like an underpowered result instead
    of like a misconfiguration.
    """


MAX_OCCASION_ID_CHARS = 128
MAX_TRACES_PER_ASSIGN = 100


async def holdout_assign(
    session: AsyncSession,
    org_id: str,
    trace_ids: list[str],
    occasion_id: str,
    actor: str = AUDIT_ACTOR_UNKNOWN,
    pinned: list[str] | None = None,
) -> dict:
    """For each trace eligible on this occasion, decide inject or withhold,
    record the decision, and return it.

    `pinned` is the caller's own working-set block: the trace ids that are
    already in this session's system prompt (`working_set` returns exactly
    this list in `entries[].trace_id`). They are excluded from the
    randomization entirely -- reported as `inject`, with NO observation
    recorded -- and passing them is what keeps the experiment honest.

    The note below says injecting a withheld trace "biases the measured
    effect toward zero". A pinned trace does precisely that, structurally,
    without the agent doing anything wrong: `working_set` puts it in the
    system prompt for the whole session, so when this function later draws
    it into the WITHHELD arm for a search result, the occasion is treated
    anyway -- the trace is still sitting in the prompt -- and is recorded as
    a control. `working_set`'s own contract says a trace is "either being
    randomized or it has graduated, never both"; nothing enforced that,
    because only the caller knows what it actually pasted. This parameter is
    how it says so. Omitting it preserves the previous behaviour exactly.

    Idempotent by construction, and it has to be: an agent that times out
    and retries must get the SAME arms back, or the retry would move an
    occasion between arms and corrupt the comparison. Assignment is a pure
    hash of (salt, trace_id, occasion_id), so a retry recomputes the same
    answer, and the unique constraint makes the recorded row a no-op rather
    than a duplicate that would double that occasion's weight.

    Traces are filtered to the caller's own org before anything is
    recorded -- an id belonging to another org is silently absent from the
    result rather than reported, matching get_trace's not-found behaviour.
    """
    if not isinstance(trace_ids, (list, tuple)):
        raise ValueError("trace_ids must be a list")
    if len(trace_ids) > MAX_TRACES_PER_ASSIGN:
        raise ValueError(
            f"too many traces in one assignment ({len(trace_ids)}); "
            f"the maximum is {MAX_TRACES_PER_ASSIGN}"
        )
    occasion_id = str(occasion_id or "").strip()
    if not occasion_id:
        raise ValueError("occasion_id is required: it is the key the outcome is reported against")
    if len(occasion_id) > MAX_OCCASION_ID_CHARS:
        raise ValueError(f"occasion_id exceeds {MAX_OCCASION_ID_CHARS} chars")
    reject_unstorable_text(occasion_id, "occasion_id")

    org = await session.get(Organization, org_id)
    if org is None or org.holdout_rate <= 0 or not org.holdout_salt:
        raise ExperimentNotRunning(
            "No holdout experiment is running for this organization. An operator "
            "starts one with `python -m hub.manage start-experiment <org_id> [rate]`."
        )

    # The trace CONTENT comes back with the id, not just the id. `amend_trace`
    # rewrites title/context/solution in place, so recording only the id
    # records a pointer to something that can change underneath the
    # experiment -- and the effect would then describe two treatments pooled.
    # Read at decision time, because what the trace said LATER is not what
    # this occasion was treated with.
    valid = list(
        (
            await session.execute(
                select(
                    Trace.id, Trace.title, Trace.context_text, Trace.solution_text, Trace.tags
                ).where(
                    Trace.org_id == org_id,
                    Trace.id.in_([t for t in trace_ids if _is_uuid(str(t))]),
                )
            )
        ).all()
    )

    # Already in the caller's system prompt, so there is no arm to assign:
    # a "withheld" verdict here would be recorded as a control while the
    # trace remains visible to the agent for the whole session. Skipped
    # before any decision is taken, so no observation row is written.
    pinned_ids = {str(p) for p in (pinned or [])}
    decisions = []
    already_pinned = []
    for row in valid:
        if row.id in pinned_ids:
            already_pinned.append(row.id)
            continue
        withheld = experiment.is_held_out(
            row.id, occasion_id, rate=org.holdout_rate, salt=org.holdout_salt
        )
        decisions.append({
            "trace_id": row.id,
            "injected": not withheld,
            "trace_revision": revision.revision_of_trace(
                row.title or "", row.context_text or "", row.solution_text or "",
                list(row.tags or []),
            ),
        })

    if decisions:
        # ON CONFLICT DO NOTHING, not DO UPDATE: the existing row is by
        # definition the same arm (assignment is deterministic), and
        # overwriting it would reset created_at and lose the original
        # decision's timestamp for no gain.
        await session.execute(
            pg_insert(HoldoutObservation)
            .values(
                [
                    {
                        "id": str(uuid.uuid4()),
                        "org_id": org_id,
                        "trace_id": d["trace_id"],
                        "occasion_id": occasion_id,
                        "injected": d["injected"],
                        "salt": org.holdout_salt,
                        "trace_revision": d["trace_revision"],
                    }
                    for d in decisions
                ]
            )
            .on_conflict_do_nothing(constraint="uq_holdout_org_salt_trace_occasion")
        )
        await session.flush()

    return {
        "occasion_id": occasion_id,
        "holdout_rate": org.holdout_rate,
        # Pinned traces are reported as inject because that is the truth --
        # they are in the prompt already -- but they are deliberately absent
        # from the recorded observations, so they contribute to neither arm.
        "inject": [d["trace_id"] for d in decisions if d["injected"]] + already_pinned,
        "withhold": [d["trace_id"] for d in decisions if not d["injected"]],
        "pinned": already_pinned,
        "note": (
            "Withheld traces must NOT be used on this occasion. Injecting one anyway "
            "moves it into the treated arm without the record saying so, which does "
            "not fail loudly -- it biases the measured effect toward zero. Report the "
            "result with record_occasion_outcome(occasion_id, succeeded)."
            + (
                f" {len(already_pinned)} trace(s) you reported as already pinned were "
                "left out of the randomization entirely: a trace in the system prompt "
                "cannot serve as its own control."
                if already_pinned else ""
            )
        ),
    }


def _cluster_representatives(traces: list[dict]) -> dict[str, str]:
    """id -> the id its whole near-duplicate cluster is randomized under.

    Without this, a fleet's own habit of contributing a trace of what
    happened after each occasion -- the exact pattern `commontrace capture`
    encourages on the local tier -- creates one new, independent trace id
    per occasion that is a near-duplicate of whatever lesson it resolved.
    `holdout_assign` treats every id it is given as its own randomization
    unit, so those duplicates each accumulate their own handful of
    injections instead of one lesson's injections accumulating on one id --
    which is exactly the shape that keeps a real, large effect UNDERPOWERED
    forever. `commontrace/distill.py` exists to solve the identical problem
    for the local tier's own lesson corpus; this reuses its clustering
    rather than inventing a second implementation of "these are probably
    the same thing".

    A cluster's representative is its OLDEST member (`created_at`, ties
    broken by id), preferring one with no recorded FAILED outcome
    (`outcome.resolved is not False`) when the cluster has any: an
    unresolved, escalated occasion log should not become the one id the
    whole cluster's observations -- and the causal effect a customer
    eventually reads -- are attributed to, when a better-standing member of
    the same cluster is available. Ids that cluster with nothing map to
    themselves, so a genuinely distinct trace is completely unaffected.

    OLDEST, specifically, and not `distill.representative`'s medoid, for a
    reason that is invisible until this runs against a real corpus: this
    function only ever sees ONE SEARCH PAGE, not the cluster's true
    membership, and the medoid is a function of which members happen to
    share that page. As a fleet logs more occasions, the page composition
    shifts, the textual centre of it shifts with it, and the "same" lesson
    is randomized under a different id from one occasion to the next --
    which re-creates, one level up, exactly the fragmentation this
    clustering exists to remove. Measured over the audit's own dynamics
    (top-5 page, corpus growing by one occasion log at a time): the medoid
    rule produced 17 distinct randomization units for a single lesson; the
    oldest-member rule produces 1.

    The oldest member is also the semantically right anchor rather than
    merely a stable one: a lesson is contributed before any occasion that
    used it, so the original is the oldest member of its own duplicate
    family, and it is what a customer reading `causal_effects` should see
    named as the memory under test.

    Clusters on whatever text `traces` already carries, which may be
    brief-truncated (`_to_wire(..., brief=True)`); a degraded grouping on
    truncated text still only ever falls back to today's per-id behavior,
    never to a wrong answer, so this is an acceptable degradation rather
    than a correctness risk.
    """
    candidates = [
        distill.TraceCandidate(
            id=t["id"], path="", title=t.get("title", ""),
            context_text=t.get("context_text", ""), solution_text=t.get("solution_text", ""),
            tags=list(t.get("tags") or []), agent_type=t.get("agent_type", ""),
        )
        for t in traces if isinstance(t, dict) and t.get("id")
    ]
    if len(candidates) < 2:
        return {c.id: c.id for c in candidates}
    by_id = {
        t["id"]: t for t in traces if isinstance(t, dict) and t.get("id")
    }

    def _age_key(trace_id: str) -> tuple[str, str]:
        # `created_at` is an ISO-8601 UTC string (`_iso`), so lexicographic
        # order IS chronological order. Id breaks ties, and stands in
        # entirely for a row with no timestamp, so the choice stays
        # deterministic either way.
        return (str(by_id.get(trace_id, {}).get("created_at") or ""), trace_id)

    clusters = distill.find_clusters(candidates, existing_lessons_source_traces=[])
    representative_of: dict[str, str] = {c.id: c.id for c in candidates}
    for cluster in clusters:
        preferred = [
            c for c in cluster.traces
            if (by_id.get(c.id, {}).get("outcome") or {}).get("resolved") is not False
        ]
        pool = preferred or cluster.traces
        rep_id = min((c.id for c in pool), key=_age_key)
        for c in cluster.traces:
            representative_of[c.id] = rep_id
    return representative_of


async def holdout_for_results(
    session: AsyncSession,
    org_id: str,
    traces: list[dict],
    occasion_id: str,
    actor: str = AUDIT_ACTOR_UNKNOWN,
    pinned: list[str] | None = None,
) -> dict:
    """Assign holdout arms for whatever a search just returned.

    `pinned` -- the trace ids already in the caller's system prompt, as
    returned by `working_set` -- are dropped BEFORE clustering, not after.
    Dropping them afterwards would not be enough: `_cluster_representatives`
    can elect a pinned trace as the representative of a near-duplicate
    cluster, and the whole cluster would then inherit that trace's arm. See
    `holdout_assign` for why a pinned trace has no arm to inherit.

    The friction this removes is the reason it exists. The local tier makes
    running an experiment a single flag (`commontrace query --experiment`):
    retrieval itself withholds and logs, so a fleet opts in without
    rewriting an agent's loop. On the Hub the same experiment needed two
    extra explicit calls wrapped around every retrieval, which is a
    rewrite -- and STRATEGY.md §13.2 calls running this the cheapest
    falsifier available, so friction here is not a UX detail, it is the
    thing that decides whether the falsifier ever gets run on a real fleet.

    Returns {} when no experiment is configured, so a caller that always
    passes `occasion_id` sees no change until an operator starts one. That
    is deliberately quieter than `holdout_assign`, which raises
    ExperimentNotRunning: a caller of THAT tool has explicitly asked to
    run an experiment and should be told it is off, while a caller of
    search_traces has only asked to search.

    Near-duplicate results (see `_cluster_representatives`) are assigned
    ONE holdout decision, under whichever member best represents the
    cluster -- but every id in `traces` still gets a `withhold`/`inject`
    verdict of its own in the response, so this is invisible at the wire
    level: a caller checking `holdout.withhold` for a specific id it
    fetched sees exactly the same shape as before, just backed by an
    experiment that is no longer fragmenting its own statistical power
    across duplicates the caller never asked to see as separate memories.
    """
    org = await session.get(Organization, org_id)
    if org is None or org.holdout_rate <= 0 or not org.holdout_salt:
        return {}
    pinned_ids = {str(p) for p in (pinned or [])}
    measurable = [
        t for t in traces
        if isinstance(t, dict) and t.get("id") and t["id"] not in pinned_ids
    ]
    ids = [t["id"] for t in measurable]
    if not ids:
        return {}
    representative_of = _cluster_representatives(measurable)
    assign_ids = list({representative_of.get(i, i) for i in ids})
    assignment = await holdout_assign(session, org_id, assign_ids, occasion_id, actor=actor)
    rep_withheld = set(assignment["withhold"])
    return {
        "occasion_id": assignment["occasion_id"],
        "withhold": [i for i in ids if representative_of.get(i, i) in rep_withheld],
        "note": assignment["note"],
    }


async def record_occasion_outcome(
    session: AsyncSession,
    org_id: str,
    occasion_id: str,
    succeeded: bool,
    actor: str = AUDIT_ACTOR_UNKNOWN,
) -> dict:
    """Close the loop: how did this occasion go?

    Updates every observation recorded for the occasion, in both arms at
    once, which is the point -- an outcome belongs to the TASK, not to any
    one memory that was or was not injected into it.
    """
    occasion_id = str(occasion_id or "").strip()
    if not occasion_id:
        raise ValueError("occasion_id is required")
    reject_unstorable_text(occasion_id, "occasion_id")
    if not isinstance(succeeded, bool):
        raise ValueError("succeeded must be a boolean")

    result = await session.execute(
        update(HoldoutObservation)
        .where(
            HoldoutObservation.org_id == org_id,
            HoldoutObservation.occasion_id == occasion_id,
            # Only unresolved rows. A second report for the same occasion
            # is ignored rather than allowed to flip an outcome already
            # counted -- otherwise a retry loop could walk a result back
            # and forth and the analysis would depend on which call landed
            # last.
            HoldoutObservation.succeeded.is_(None),
        )
        .values(succeeded=succeeded, resolved_at=datetime.now(timezone.utc))
    )
    await session.flush()
    return {"occasion_id": occasion_id, "observations_resolved": result.rowcount or 0}


def _integrity_wire(report: integrity.IntegrityReport) -> dict:
    """The validity report, as JSON an agent can act on.

    Findings that passed are included, not filtered to the failures. A
    caller has no way to distinguish "checked, clean" from "not checked"
    when only problems are reported, and those two mean opposite things
    about how much to trust the number underneath.
    """
    return {
        "verdict": report.verdict,
        "unit": report.unit,
        "effects_readable": report.readable,
        "n_assignments": report.n_assignments,
        "n_resolved": report.n_resolved,
        "findings": [
            {"check": f.check, "severity": f.severity, "headline": f.headline,
             "detail": f.detail, "numbers": f.numbers}
            for f in report.findings
        ],
        "projections": [
            {"trace_id": p.lesson, "n_injected": p.n_injected, "n_withheld": p.n_withheld,
             "needed_per_arm": p.needed_per_arm, "binding_arm": p.binding_arm,
             "still_needed": p.still_needed, "per_day": p.per_day,
             "days_remaining": p.days_remaining,
             "eta": p.eta.isoformat() if p.eta else None, "advice": p.advice}
            for p in report.projections
        ],
        "not_checkable": (
            "Whether an agent used a lesson it was told to withhold. That leaves no "
            "trace in the record and biases the effect toward zero -- it is honoured "
            "by the client or not at all."
        ),
    }


async def causal_effects(session: AsyncSession, org_id: str, alpha: float = 0.05) -> dict:
    """Per-trace causal effect estimates from the running experiment.

    Analysis is `commontrace.experiment.analyze` unchanged: per-lesson
    two-proportion tests, Benjamini-Hochberg across the lessons that met the
    per-arm floor, a minimum detectable effect on every inconclusive one, and
    an explicit UNDERPOWERED verdict so "cannot answer yet" never reads as
    "no effect".

    That last guarantee is stronger than it used to be, and it is why a Hub
    customer may see more UNDERPOWERED rows than before. Clearing the
    per-arm floor is a condition for running the test, not evidence the test
    could see anything: at 10 observations per arm the minimum detectable
    effect is over 60 percentage points. A null from a design that could not
    have detected an effect worth acting on is now reported as UNDERPOWERED
    rather than as NO_MEASURABLE_EFFECT, which is what it is
    (commontrace/experiment.py:DEFAULT_PRACTICAL_EFFECT). The projections
    beside it say how far each trace is from an answer.
    """
    org = await session.get(Organization, org_id)
    # A Core column-select, not `select(HoldoutObservation)`: the latter
    # hydrates a full mapped ORM entity per row (identity map, instrumented
    # attributes) for every one of what can be hundreds of thousands of rows
    # under a long-running experiment, and every field it hydrates beyond the
    # seven read below is wasted work. Measured on a live Postgres at 200k
    # rows: ORM instantiation alone (sqlalchemy.orm.loading) accounted for
    # roughly 60% of a 12-SECOND call -- a customer-facing MCP tool
    # (fleet_outcomes/value_delivered) that a fleet doing ordinary retrieval
    # volume reaches within months, not an edge case. Selecting only the
    # columns this function actually reads returns lightweight Row tuples
    # instead, with no change to what is computed: same rows, same fields,
    # same downstream Assignment objects.
    rows = (
        await session.execute(
            select(
                HoldoutObservation.trace_id,
                HoldoutObservation.occasion_id,
                HoldoutObservation.injected,
                HoldoutObservation.succeeded,
                HoldoutObservation.salt,
                HoldoutObservation.created_at,
                # Paired with created_at above, this is follow-up time: how
                # long each occasion was watched before its outcome arrived.
                # It is what lets the attrition audit tell an occasion nobody
                # has reported YET from one nobody ever will, instead of
                # counting a fast-concluding treated arm as missing data
                # (commontrace/survival.py).
                HoldoutObservation.resolved_at,
                HoldoutObservation.trace_revision,
            ).where(
                HoldoutObservation.org_id == org_id,
                # Scoped to the CURRENT experiment. Observations from an
                # earlier salt were drawn from a different randomization
                # and pooling them would mix two experiments into one
                # comparison -- the exact failure Organization.holdout_salt
                # exists to make detectable.
                HoldoutObservation.salt == (org.holdout_salt if org else ""),
                #
                # NOTE what is NOT filtered here any more. This used to carry
                # `succeeded.isnot(None)`, which is correct for the estimate
                # and is exactly where the estimate stopped being auditable:
                # an unresolved observation is an occasion that was assigned
                # an arm and then never reported, and dropping those in the
                # QUERY meant nothing downstream could see how many there
                # were or which arm they came from.
                #
                # Excluding them is unbiased only if both arms lose them at
                # the same rate, and the withheld arm -- by construction, the
                # one working without its memory -- is the arm more likely to
                # run long, escalate, or be abandoned before anyone reports.
                # They are fetched now and separated below: `analyze` still
                # sees only the resolved ones, and `integrity.audit` sees all
                # of them, which is the only way that check can exist.
            )
        )
    ).all()  # plain Row tuples (named attribute access below), not `.scalars()`
    # -- there is no single-entity column to scalar-ize; this selects eight.

    assignments = [
        integrity.Assignment(
            lesson=r.trace_id,
            occasion_id=r.occasion_id,
            injected=r.injected,
            rate=(org.holdout_rate if org else experiment.DEFAULT_HOLDOUT_RATE),
            salt=r.salt,
            succeeded=r.succeeded,
            at=r.created_at,
            resolved_at=r.resolved_at,
            revision=r.trace_revision,
        )
        for r in rows
    ]
    # unit="trace": the Hub randomizes traces, not lessons. Without this the
    # customer console tells a Hub customer that a `lesson` was edited, which
    # is the other tier's vocabulary and sends them looking for an object they
    # do not have.
    report = integrity.audit(assignments, unit=integrity.UNIT_TRACE)
    # Built from the same rows, for the same reason the audit is: a caller
    # that adds these effects together needs to know whether doing so counts
    # any occasion twice (commontrace/value.py:OccasionOverlap).
    _overlap = value.overlap_from_assignments(assignments)
    # The policy-level comparison on unique occasions, from the same rows.
    # On this Hub one occasion routinely receives several traces
    # (holdout_assign takes a list), so the per-trace effects below cannot be
    # added -- this is the aggregate that can be.
    _policy = value.policy_effect(assignments)

    unique, _ = integrity.normalize(assignments)
    observations = [
        experiment.HoldoutObservation(
            lesson_slug=r.lesson, occasion_id=r.occasion_id, injected=r.injected,
            succeeded=bool(r.succeeded),
        )
        for r in unique if r.succeeded is not None
    ]
    effects = experiment.analyze(observations, alpha=alpha)

    # WHEN each estimate stopped being updated. `analyze` pools a trace's
    # entire history with no notion of time, so an effect established in a
    # fleet's first month and never observed since carries exactly the same
    # authority here as one measured yesterday. That is correct for an audit
    # table -- the estimate IS what those occasions showed -- and it is not
    # sufficient for `working_set`, which pins the winners into every future
    # session's prompt on the strength of it. Dated here so the promotion
    # decision can see the age of the evidence it is acting on.
    #
    # Read from the RESOLVED observations only, and from `unique` rather than
    # the raw rows: those are the occasions the estimate was actually
    # computed from, so they are what "last measured" can honestly mean.
    last_measured: dict[str, datetime] = {}
    for r in unique:
        if r.succeeded is None or r.at is None:
            continue
        if r.lesson not in last_measured or r.at > last_measured[r.lesson]:
            last_measured[r.lesson] = r.at

    titles = dict(
        (
            await session.execute(
                select(Trace.id, Trace.title).where(
                    Trace.org_id == org_id, Trace.id.in_([e.lesson_slug for e in effects])
                )
            )
        ).all()
    )
    return {
        "experiment_running": bool(org and org.holdout_rate > 0 and org.holdout_salt),
        "holdout_rate": org.holdout_rate if org else 0.0,
        "n_observations": len(observations),
        "n_occasions": len({o.occasion_id for o in observations}),
        # First key a reader meets after the counts, and first for the same
        # reason the CLI prints it above the table: a caller that reads
        # `effects` without reading this can quote a number that a named,
        # identified mechanism is biasing.
        "integrity": _integrity_wire(report),
        # Which traces were injected on the same occasions as which others.
        # Computed here because this is where the raw assignments already
        # are -- `value_delivered` needs it to know whether the per-trace
        # contributions may be ADDED (one occasion that received two traces
        # would otherwise be counted, and billed, twice) and re-querying
        # hundreds of thousands of rows for it would be the expensive half
        # of this call run a second time. Pairs only, not occasion ids: the
        # question is which traces collide, and shipping the ids would put
        # an unbounded list on the wire to answer a bounded question.
        "co_injection": {
            "pairs": [sorted(pair) for pair in sorted(
                _overlap.shared_pairs, key=lambda p: sorted(p)
            )],
            "unique_injected_occasions": _overlap.unique_injected_occasions,
        },
        # The whole-policy comparison on UNIQUE occasions: occasions that got
        # any trace against occasions that got none. This is the aggregate
        # that stays valid when the per-trace sum does not -- which on this
        # Hub is the normal case, because holdout_assign takes a LIST of
        # traces for one occasion, so traces routinely share occasions.
        "policy_effect": {
            "readable": _policy.readable,
            "reason": _policy.reason,
            "n_treated": _policy.n_treated,
            "n_control": _policy.n_control,
            "rate_treated": _policy.rate_treated,
            "rate_control": _policy.rate_control,
            "effect": _policy.effect,
            "ci_95": [_policy.ci_low, _policy.ci_high],
            "p_value": _policy.p_value,
            "significant": _policy.significant,
            "occasions_improved": _policy.occasions_improved,
            "note": (
                "Occasions that received ANY memory against occasions that received "
                "none. Attributes nothing to an individual memory -- that is what the "
                "per-trace effects above are for -- but counts every occasion exactly "
                "once, which is what makes it addable when those are not."
            ),
        },
        "effects": [
            {
                "trace_id": e.lesson_slug,
                "title": titles.get(e.lesson_slug, "(deleted trace)"),
                "n_injected": e.n_injected,
                "n_withheld": e.n_withheld,
                "rate_injected": e.rate_injected,
                "rate_withheld": e.rate_withheld,
                "effect": e.effect,
                "ci_95": [e.ci_low, e.ci_high],
                "p_value": e.p_value,
                "significant": e.significant,
                "min_detectable_effect": e.min_detectable_effect,
                "verdict": e.verdict,
                "note": e.note,
                # Empty string, not null, matching the wire convention the
                # trace fields already use for "there is no such value".
                "last_measured_at": (
                    last_measured[e.lesson_slug].isoformat()
                    if e.lesson_slug in last_measured else ""
                ),
            }
            for e in effects
        ],
        "note": (
            "CAUSAL, unlike the before/after comparison alongside it: the two arms "
            "are the same fleet in the same window, differing only by whether the "
            "memory was injected. That is what makes this survive 'what else changed?'."
            + ("" if report.readable else
               " READ `integrity` FIRST: the sample these effects were computed on is "
               "compromised, so they are not estimates of the causal effect.")
        ),
    }


async def value_delivered(
    session: AsyncSession,
    org_id: str,
    value_per_occasion: float | None = None,
    rate_tiers: list[dict] | None = None,
    signing_key: str = "",
) -> dict:
    """What this fleet's memory was worth, causally, in its own units.

    STRATEGY.md 11.5 names the pricing hypothesis this product rests on --
    price against measured effect per fleet, not seats or trace volume,
    because measured effect is the only quantity here that is causal and the
    only one that scales with the customer's own benefit. It also says the
    mechanism ships and the number stays a business decision.

    Half of that was true. The effect size shipped; nothing turned it into a
    quantity a price could attach to, and this file -- the surface customers
    pay on -- computed no value at all. The one estimator that existed
    (`commontrace impact`) is correlational by its own admission. So the
    product had a causal instrument and a commercial number that were not
    connected to each other, and the commercial one was the confounded one.

    Reuses `causal_effects` rather than re-querying, so the value figure and
    the effect table can never disagree, and so the validity audit that
    governs the effects governs the value too: a COMPROMISED experiment
    yields no figure at all (`commontrace/value.py`).

    `value_per_occasion` is the caller's. This function returns a COUNT of
    occasions; currency enters only if the caller supplies a rate, and no
    price is stored anywhere.

    `rate_tiers` is the same statement made properly. A flat rate prices a
    password reset and an averted SLA breach identically, which is the first
    thing a finance team rejects. Supplied as
    `[{"name": ..., "share": ..., "cost_per_occasion": ...}, ...]`, shares
    summing to 1, it prices the measured occasions against the mix the
    customer actually agreed to. It is still entirely the caller's: nothing
    here measures which tier an occasion belonged to, and the tiers are echoed
    back in the response as inputs so a negotiated assumption can never be
    read back later as a finding.

    When a rate is available and the run is readable the response also carries
    `ledger`: one hash-chained line per counted memory, each hash covering the
    previous one, so editing a figure, dropping the memory that HURT, or
    reordering to bury it all break the chain. `commontrace.value.verify_ledger`
    recomputes it, and is written to be reimplementable by whoever audits the
    invoice.

    That chain alone only proves internal consistency, not who issued it --
    its genesis and algorithm are both public, so anyone with write access to
    wherever a ledger is stored could fabricate an entire replacement chain
    that verifies just as cleanly. `signing_key` (from `HubConfig.
    ledger_signing_key`, i.e. `HUB_LEDGER_SIGNING_KEY`) closes that: when set,
    the response also carries `signature` and `issued_at`, an HMAC-SHA256
    (`commontrace.value.sign_ledger`) over the chain's root bound to this org
    and this timestamp, checkable with `commontrace.value.
    verify_ledger_signature` against the same key. Left unset, `signature` is
    `None` and `signature_reason` says so explicitly -- silently returning an
    unsigned ledger with no signal would let a hash-chained invoice look more
    authenticated than it is.
    """
    causal = await causal_effects(session, org_id)
    effects = [
        experiment.CausalEffect(
            lesson_slug=e["trace_id"], n_injected=e["n_injected"],
            n_withheld=e["n_withheld"], rate_injected=e["rate_injected"],
            rate_withheld=e["rate_withheld"], effect=e["effect"],
            ci_low=e["ci_95"][0], ci_high=e["ci_95"][1], p_value=e["p_value"],
            significant=e["significant"],
            min_detectable_effect=e["min_detectable_effect"],
            verdict=e["verdict"], note=e["note"],
        )
        for e in causal.get("effects", [])
    ]
    audit = _integrity_from_wire(causal.get("integrity") or {})
    # A malformed rate card is the caller's mistake, not a server fault, and
    # it has to surface as one: silently falling back to the flat rate would
    # price the invoice against a number the customer thought they had
    # replaced. RateCard's own validation raises ValueError, which this
    # tool's error envelope already renders as a structured tool error.
    card = None
    if rate_tiers:
        card = value.RateCard(tiers=tuple(
            value.Tier(
                name=str(t.get("name") or ""),
                share=float(t.get("share", 0.0)),
                cost_per_occasion=float(t.get("cost_per_occasion", 0.0)),
            )
            for t in rate_tiers
        ))
    # Rebuilt from the wire projection rather than re-querying: causal_effects
    # computed it over the same assignments this figure is derived from, and
    # the two must agree about which traces collide or the total could be
    # certified against a different experiment than the one it prices.
    wire_overlap = causal.get("co_injection") or {}
    overlap = value.OccasionOverlap(
        shared_pairs=frozenset(
            frozenset(pair) for pair in wire_overlap.get("pairs", []) if len(pair) == 2
        ),
        unique_injected_occasions=int(wire_overlap.get("unique_injected_occasions", 0)),
    )
    wire_policy = causal.get("policy_effect") or {}
    policy = value.PolicyEffect(
        n_treated=int(wire_policy.get("n_treated", 0)),
        n_control=int(wire_policy.get("n_control", 0)),
        rate_treated=float(wire_policy.get("rate_treated", 0.0)),
        rate_control=float(wire_policy.get("rate_control", 0.0)),
        effect=float(wire_policy.get("effect", 0.0)),
        ci_low=float((wire_policy.get("ci_95") or [0.0, 0.0])[0]),
        ci_high=float((wire_policy.get("ci_95") or [0.0, 0.0])[1]),
        p_value=float(wire_policy.get("p_value", 1.0)),
        significant=bool(wire_policy.get("significant", False)),
        readable=bool(wire_policy.get("readable", False)),
        reason=str(wire_policy.get("reason", "")),
    )
    report = value.compute(
        effects, audit, value_per_occasion=value_per_occasion, rate_card=card,
        overlap=overlap,
    )
    report = dataclasses.replace(report, policy=policy)
    ledger = report.ledger()
    titles = {e["trace_id"]: e.get("title") for e in causal.get("effects", [])}

    # Issuer signature over the chain's root. Computed even for an empty
    # ledger (ledger_root falls back to the chain genesis) -- "zero counted
    # lines this period" is itself a fact worth being able to authenticate,
    # not just a nonzero invoice.
    issued_at = datetime.now(timezone.utc).isoformat()
    if signing_key:
        signature = value.sign_ledger(
            ledger, signing_key.encode("utf-8"), org_id=org_id, issued_at=issued_at
        )
        signature_reason = ""
    else:
        signature = None
        signature_reason = (
            "HUB_LEDGER_SIGNING_KEY is not configured on this deployment: the "
            "ledger above is hash-chained (internally consistent, checkable "
            "with commontrace.value.verify_ledger) but not cryptographically "
            "signed by the issuer, so a party with write access to the "
            "underlying store could still fabricate a whole replacement chain "
            "that verifies just as cleanly. See hub/DEPLOYMENT.md."
        )

    return {
        "readable": report.readable,
        "reason": report.reason,
        "occasions_improved": report.occasions_improved,
        "ci_95": [report.ci_low, report.ci_high],
        "n_counted": report.n_counted,
        "n_excluded": report.n_excluded,
        # Whether those per-trace contributions may be ADDED, which is a
        # different question from whether each one is readable. False when
        # two counted traces were injected on the same occasions: the sum
        # would attribute one improved occasion more than once, and this is
        # the quantity an invoice is computed from. The per-trace figures
        # below stand either way.
        "aggregate_readable": report.aggregate_readable,
        "aggregate_reason": report.aggregate_reason,
        # How many DISTINCT occasions received anything -- the ceiling the
        # total cannot exceed, and the denominator needed to judge whether a
        # number is large.
        "unique_occasions": report.unique_occasions,
        # The same total over EVERY measured trace, not only the ones whose
        # effect cleared significance. `occasions_improved` selects on the
        # data it reports, which biases its magnitude away from zero; this
        # does not. Reported beside it so the size of that selection is a
        # figure rather than a caveat. Never billed on -- it includes
        # effects the experiment did not establish.
        "occasions_improved_unselected": report.occasions_improved_unselected,
        "n_examined": report.n_examined,
        # The aggregate that stays valid when the per-trace sum does not,
        # carried through from causal_effects so both surfaces report the
        # same comparison over the same occasions.
        "policy_effect": causal.get("policy_effect"),
        "value_per_occasion": value_per_occasion,
        # Echoed back as INPUTS. The tiers and the mix are contractual; only
        # `occasions_improved` above was measured, and keeping the two
        # labelled apart on the wire is what stops a negotiated assumption
        # being quoted later as a finding.
        "rate_tiers": [
            {"name": t.name, "share": t.share, "cost_per_occasion": t.cost_per_occasion}
            for t in (card.tiers if card else ())
        ],
        "rate_applied": report.rate,
        "money": report.money,
        "money_range": list(report.money_range) if report.money_range else None,
        "ledger": [
            {"index": e.index, "trace_id": e.slug, "verdict": e.verdict,
             "occasions_improved": e.occasions_improved, "rate": e.rate,
             "money": e.money, "previous_hash": e.previous_hash,
             "entry_hash": e.entry_hash}
            for e in ledger
        ],
        # Issuer authentication for the ledger above. `None` when this
        # deployment has no signing key configured -- see signature_reason.
        "issued_at": issued_at,
        "signature": signature,
        "signature_algorithm": "HMAC-SHA256" if signature else None,
        "signature_reason": signature_reason,
        "memories": [
            {"trace_id": m.slug, "title": titles.get(m.slug, "(deleted trace)"),
             "verdict": m.verdict, "n_injected": m.n_injected, "effect": m.effect,
             "occasions_improved": m.occasions_improved,
             "ci_95": [m.ci_low, m.ci_high], "counted": m.counted,
             "why_not": m.why_not}
            for m in report.memories
        ],
        "integrity": causal.get("integrity"),
        "note": (
            "The occasion count is MEASURED; any currency comes from the rate you "
            "supplied. Memories measured as HURTING are subtracted rather than "
            "dropped -- a figure that sums only the winners is not a measurement. "
            "Underpowered memories contribute nothing, because an effect that was "
            "not established multiplied by a volume is a large number with no "
            "evidence under it."
        ),
    }


# --- The working set: memory that costs nothing per query ---------------
#
# WHY THIS EXISTS
#
# Retrieval is not free, and this product's own measurements say how much:
# one `search_traces` page costs roughly 1,160 tokens, paid again on every
# call, and paid whether or not the corpus had anything useful to say. An
# agent doing twenty lookups in a session spends ~23,000 tokens on
# retrieval alone.
#
# The comparison worth making is against the simplest design in the field.
# Nous Research's Hermes Agent (MIT) keeps memory in two small files read
# once at session start and pasted into the system prompt, where they stay
# unchanged for the whole session -- deliberately, so the provider's prefix
# cache is never invalidated. That buys zero marginal cost per query, for a
# total budget of roughly 1,300 tokens. At twenty lookups it is ~26x
# cheaper than retrieving.
#
# What it cannot do is decide what belongs in those 1,300 tokens. Hermes
# fills them by asking the agent to curate its own notes; Mem0, Zep and
# Letta fill their equivalents by automatic extraction and grade themselves
# on recall benchmarks (LoCoMo, LongMemEval) -- "did you remember the
# fact", never "did remembering it make the work go better".
#
# This Hub can answer the second question, because it already runs a
# randomized holdout per trace. So the working set is selected by MEASURED
# CAUSAL EFFECT: a trace earns a place in the always-on block by having
# been established as HELPS by the fleet's own experiment, and nothing
# else gets in. The experiment stops being only a report and becomes the
# PROMOTION MECHANISM.
#
# That also resolves what would otherwise be a real methodological
# problem. A trace pinned into every session's prompt is injected on every
# occasion, which would quietly destroy the control arm it is still being
# measured against. Restricting the block to traces whose effect is
# already established means nothing under test is ever pinned: a trace is
# either still being randomized, or it has graduated. Never both.

# Hermes Agent's MEMORY.md ships a 2,200-character budget and its own
# usage gauge; this default is deliberately the same order of magnitude,
# for the same reason (a block big enough to matter and small enough that
# a cached system prompt stays cheap). Callers can ask for less.
DEFAULT_WORKING_SET_CHARS = 2000
MAX_WORKING_SET_CHARS = 8000

# How long a measured effect stays fresh enough to PIN. A policy number,
# not a measurement -- and the honest half of a problem this design creates
# for itself.
#
# Every competing memory system ages memory out on a PROXY for usefulness.
# Mem0 scales retrieval rank by an Ebbinghaus-style recency/access curve
# (roughly 0.3x-1.5x, a soft rerank rather than a delete); the 2026 survey
# literature converges on "differential exponential decay keyed to
# relevance, access frequency and temporal pattern". Every one of those is
# a guess dressed as a measurement, because none of those systems can see
# whether a memory still WORKS -- only whether it was recently read. A
# lesson nobody happened to retrieve decays; an obsolete lesson everyone
# keeps retrieving does not.
#
# This Hub measures the thing itself, and therefore has to confront what
# the measurement's age means. Promotion here is what freezes it: a pinned
# trace is injected on EVERY occasion, so it is never withheld, so it stops
# accumulating the withheld arm its effect was computed from. Graduation
# ends the experiment for that trace. Left alone, "has a measured causal
# effect" quietly becomes "had one once, against a world that has since
# moved on" -- and the upstream fix, the API change, or the dependency bump
# that made the lesson obsolete are all invisible to it.
#
# So graduation expires. Past this horizon an entry leaves the pinned
# block, which is not a demotion so much as a renewal: leaving the block is
# precisely what allows the trace to be randomized again, and randomization
# is the only thing that can produce fresh evidence. It re-earns its place
# when the experiment answers for it a second time. This horizon is
# therefore the RENEWAL PERIOD of the working set. The default is
# deliberately generous -- long enough that a genuinely stable lesson is
# not churned, short enough that no fleet's system prompt carries a claim
# nobody has checked in half a year.
DEFAULT_EVIDENCE_HORIZON_DAYS = 180
MAX_EVIDENCE_HORIZON_DAYS = 36500
# Enough of a solution to act on without fetching the trace. A caller that
# needs the whole thing has the id and `get_trace`.
_WORKING_SET_ENTRY_CHARS = 320


def _evidence_age_days(last_measured_at: str, now: datetime) -> float | None:
    """Days since the last resolved observation behind an effect estimate.

    `None` when the estimate carries no date at all, which the caller treats
    exactly as it treats an expired one: an effect whose evidence cannot be
    dated cannot be shown to be current, and a block that every future
    session inherits is the wrong place to assume in its favour.
    """
    if not last_measured_at:
        return None
    # Clamped at zero: `now` is this replica's clock and the timestamp is the
    # database's, so a few seconds of skew between them is ordinary and must
    # not surface as a negative age.
    return max(0.0, (now - datetime.fromisoformat(last_measured_at)).total_seconds() / 86400.0)


def _working_set_entry(title: str, solution: str, trace_id: str, effect: float, n: int) -> str:
    """One line of the block: what to do, how well it is known to work."""
    body = " ".join((solution or "").split())
    if len(body) > _WORKING_SET_ENTRY_CHARS:
        body = body[:_WORKING_SET_ENTRY_CHARS].rsplit(" ", 1)[0] + "…"
    head = " ".join((title or "").split())
    return f"- {head} → {body} ({effect:+.0%} resolution over {n} measured occasions) [{trace_id}]"


async def working_set(
    session: AsyncSession,
    org_id: str,
    budget_chars: int = DEFAULT_WORKING_SET_CHARS,
    evidence_horizon_days: int = DEFAULT_EVIDENCE_HORIZON_DAYS,
) -> dict:
    """The fleet's proven memory, small enough to pin to a system prompt.

    Returns a block to paste ONCE at session start and leave unchanged, so
    it costs its tokens a single time and rides the provider's prefix cache
    for the rest of the session, instead of being re-fetched per query.

    Membership is earned, not curated: only traces the running holdout has
    established as HELPS appear here, ranked by how many occasions each one
    actually improved (effect x times injected -- `commontrace/value.py`'s
    quantity, not a popularity count). A trace still under test is
    deliberately absent, because pinning it would inject it on every
    occasion and destroy the control arm that is still measuring it.

    Degrades honestly rather than inventing a block. With no established
    effects yet -- a fleet in its first weeks, or one with no experiment
    running -- this returns `established: false` and an empty block, and
    says which it was, instead of silently falling back to a
    most-retrieved list that would look identical to a measured one while
    carrying no evidence at all. `search_traces` is the right tool until
    the experiment has an answer.
    """
    budget_chars = _clamp_int(
        budget_chars, 1, MAX_WORKING_SET_CHARS, DEFAULT_WORKING_SET_CHARS
    )
    evidence_horizon_days = _clamp_int(
        evidence_horizon_days, 1, MAX_EVIDENCE_HORIZON_DAYS, DEFAULT_EVIDENCE_HORIZON_DAYS
    )
    causal = await causal_effects(session, org_id)
    audit = causal.get("integrity") or {}

    # A compromised experiment yields no block, for the same reason it
    # yields no value figure (commontrace/value.py): if a named mechanism
    # is biasing the effects, it biases the selection made from them, and
    # a prompt that every future session inherits is the worst possible
    # place to bake in a biased choice.
    if not audit.get("effects_readable", True):
        return {
            "block": "", "entries": [], "established": False,
            "chars_used": 0, "budget_chars": budget_chars, "gauge": f"[0% — 0/{budget_chars} chars]",
            "reason": (
                "The experiment these effects came from is COMPROMISED, so nothing has been "
                "promoted. Read `fleet_outcomes.causal.integrity` and fix what it names; a "
                "working set chosen from biased effects would carry that bias into every "
                "future session's prompt."
            ),
            "note": "",
        }

    helps = [
        e for e in causal.get("effects", [])
        if e.get("verdict") == experiment.VERDICT_HELPS
    ]
    helps.sort(key=lambda e: (e.get("effect") or 0.0) * (e.get("n_injected") or 0), reverse=True)

    if not helps:
        return {
            "block": "", "entries": [], "established": False,
            "chars_used": 0, "budget_chars": budget_chars, "gauge": f"[0% — 0/{budget_chars} chars]",
            "reason": (
                "No trace has an established causal effect yet, so nothing has earned a place "
                "in an always-on block. This is a statement about evidence, not about the "
                "corpus: keep using `search_traces` (which reaches everything), keep reporting "
                "outcomes with `record_occasion_outcome`, and traces will be promoted here as "
                "the experiment answers for them."
            ),
            "note": "",
        }

    bodies = dict(
        (
            await session.execute(
                select(Trace.id, Trace.solution_text).where(
                    Trace.org_id == org_id,
                    Trace.id.in_([e["trace_id"] for e in helps]),
                    # A trace amend_trace has since superseded, or one an
                    # operator has since purged (hub/manage.py:purge_trace),
                    # is no longer this fleet's own current answer. The
                    # effect above was measured against the TEXT that was
                    # actually shown on each occasion, not against the
                    # trace's identity -- so unlike search_traces/
                    # commons_visible (hub/models.py:Trace.superseded_at),
                    # there is no safe "resolve forward to the new head"
                    # here: inheriting an old effect onto amended wording
                    # would be evidence for content nobody ever tested.
                    # Excluding it from this query, and therefore from
                    # `helps` below, is the same "degrades honestly rather
                    # than inventing a block" rule this function already
                    # applies to a compromised experiment -- it drops out of
                    # the pinned block entirely rather than being pinned
                    # with stale or (pre-existing gap, closed by the same
                    # filter) missing text.
                    Trace.superseded_at.is_(None),
                )
            )
        ).all()
    )
    helps = [e for e in helps if e["trace_id"] in bodies]
    if not helps:
        return {
            "block": "", "entries": [], "established": False,
            "chars_used": 0, "budget_chars": budget_chars, "gauge": f"[0% — 0/{budget_chars} chars]",
            "reason": (
                "Every trace with an established causal effect has since been amended or "
                "removed, so none of them are this fleet's current answer any more -- each "
                "effect was measured against wording that no longer exists. `search_traces` "
                "reaches the current versions; they earn a place here once the running "
                "experiment establishes an effect for the new wording."
            ),
            "note": "",
        }

    # Graduation expires (DEFAULT_EVIDENCE_HORIZON_DAYS above has the full
    # argument). An effect nobody has observed inside the horizon is not
    # evidence that the lesson still works -- it is evidence that it worked,
    # once. Dropping it here is what returns the trace to the randomizer,
    # which is the only mechanism that can produce a fresh answer.
    now = datetime.now(timezone.utc)
    ages = {
        e["trace_id"]: _evidence_age_days(e.get("last_measured_at") or "", now)
        for e in helps
    }
    expired = [
        e for e in helps
        if ages[e["trace_id"]] is None or ages[e["trace_id"]] > evidence_horizon_days
    ]
    helps = [e for e in helps if e not in expired]
    if not helps:
        oldest = min(
            (ages[e["trace_id"]] for e in expired if ages[e["trace_id"]] is not None),
            default=None,
        )
        measured = f"{oldest:.0f} days ago" if oldest is not None else "at an unrecorded time"
        return {
            "block": "", "entries": [], "established": False,
            "chars_used": 0, "budget_chars": budget_chars, "gauge": f"[0% — 0/{budget_chars} chars]",
            "reason": (
                f"Every established effect for this fleet was last measured {measured}, past "
                f"the {evidence_horizon_days}-day evidence horizon, so nothing is pinned. This "
                "is not a finding that those lessons stopped working -- it is that pinning a "
                "trace stops it being withheld, which stops the experiment that measured it, "
                "so the evidence has not moved since. Leaving the block is what returns them "
                "to the randomizer: keep reporting outcomes with `record_occasion_outcome` and "
                "each one is promoted again as soon as the experiment re-establishes it."
            ),
            "note": "",
        }

    lines: list[str] = []
    entries: list[dict] = []
    used = 0
    for e in helps:
        line = _working_set_entry(
            e.get("title") or "", bodies.get(e["trace_id"], ""), e["trace_id"],
            e.get("effect") or 0.0, e.get("n_injected") or 0,
        )
        if used + len(line) + 1 > budget_chars:
            # Ranked by measured contribution, so the first thing that does
            # not fit ends the block -- a smaller later entry squeezed in
            # ahead of a larger, better-evidenced one would make the block's
            # contents depend on their lengths rather than on the evidence.
            break
        lines.append(line)
        used += len(line) + 1
        entries.append({
            "trace_id": e["trace_id"], "title": e.get("title"),
            "effect": e.get("effect"), "n_injected": e.get("n_injected"),
            "occasions_improved": round((e.get("effect") or 0.0) * (e.get("n_injected") or 0), 2),
            # Structured metadata ONLY -- deliberately not rendered into
            # `block`. The age changes every day, and a block whose text
            # changes daily invalidates the provider's prefix cache on every
            # session, which is the entire saving this function exists to
            # produce (see the header above). A caller that wants to show the
            # age reads it from here.
            "last_measured_at": e.get("last_measured_at") or "",
            "evidence_age_days": (
                round(ages[e["trace_id"]], 1) if ages[e["trace_id"]] is not None else None
            ),
        })

    pct = round(100 * used / budget_chars) if budget_chars else 0
    gauge = f"[{pct}% — {used:,}/{budget_chars:,} chars]"
    header = (
        f"## Fleet memory — {len(entries)} lesson(s) with a measured effect\n"
        f"{gauge}\n"
    )
    return {
        "block": header + "\n".join(lines),
        "entries": entries,
        "established": True,
        "chars_used": used,
        "budget_chars": budget_chars,
        "gauge": gauge,
        "reason": "",
        "note": (
            "Paste this ONCE at session start and do not change it mid-session: an unchanged "
            "system prompt keeps the provider's prefix cache valid, which is what makes this "
            "memory cost its tokens once per session instead of once per query. Everything "
            "here has an established causal effect; anything still being measured is "
            "deliberately absent and reachable via `search_traces`. "
            "PASS `entries[].trace_id` BACK as `pinned` on every search_traces and "
            "holdout_assign call for the rest of this session: these traces are in your "
            "prompt from now on, so the experiment must stop drawing them into its control "
            "arm -- a trace cannot serve as its own control while the agent can still read it."
        ),
    }


def _integrity_from_wire(wire: dict) -> integrity.IntegrityReport | None:
    """Rebuild just enough of the audit for `value.compute` to gate on.

    Only the verdict and the blocking findings matter to it; the projections
    and counts are for humans. Reconstructed rather than recomputed so the
    value figure is gated by exactly the audit the caller was shown.
    """
    if not wire:
        return None
    findings = [
        integrity.Finding(
            check=str(f.get("check", "")), severity=str(f.get("severity", "")),
            headline=str(f.get("headline", "")), detail=str(f.get("detail", "")),
        )
        for f in wire.get("findings", [])
    ]
    return integrity.IntegrityReport(
        verdict=str(wire.get("verdict", integrity.VERDICT_SOUND)),
        findings=findings, projections=[],
        n_assignments=int(wire.get("n_assignments", 0)),
        n_resolved=int(wire.get("n_resolved", 0)),
        unit=str(wire.get("unit", integrity.UNIT_TRACE)),
    )


# --- Fleet outcomes: is this actually working for this customer? --------
#
# `Trace.outcome` has carried the five business-outcome fields, and the
# `baseline` before/after flag, since the schema was written. Every
# contribute_trace writes them. Until this function the Hub read that
# column in exactly two places -- copying it onto the wire projection and
# carrying it forward on amend -- and computed nothing from it, so a
# deployment holding a year of a fleet's outcome history could not answer
# whether the product was working.
#
# See hub/outcomes.py for the statistics, and for the caveat that governs
# every use of this number: it is a before/after comparison, not a causal
# estimate, and the module refuses to let a caller forget that.


async def fleet_outcomes(
    session: AsyncSession, org_id: str, agent_type: str = "", alpha: float = outcomes.DEFAULT_ALPHA
) -> dict:
    """Has this fleet's agent performance changed since its baseline window?

    Org-scoped like every other read in this module: the comparison is a
    fleet against its own earlier self, never against another customer.
    That is not only the isolation rule -- it is the only comparison that
    means anything, since two fleets running different agents on different
    task mixes have no shared denominator.

    Deliberately not metered (hub/plans.py). This reads the caller's own
    traces, the same as search_traces and list_tags, and the Knowledge Base
    allowance exists for the one call that reads the operator's corpus. A
    customer being charged to ask whether the product is working would also
    be the single worst place in the system to put a meter.

    Quarantined traces are excluded, matching every other read path: a
    trace held pending abuse review should not move a number the customer
    is going to quote.
    """
    # ONE grouped aggregate, not one row per trace.
    #
    # The first version of this selected `Trace.outcome` for every matching
    # row and counted in Python. That is the shape STRATEGY.md §13.2 warns
    # about: `python -m hub.bench_scaling` measured it growing with the
    # customer's own corpus at an exponent of 1.12 -- superlinear, so every
    # doubling of a successful customer's history more than doubled the
    # cost of answering "is this working?". Most of that was not the scan;
    # it was transferring tens of thousands of JSONB blobs over the wire and
    # building a Python dict for each one, to compute six integers.
    #
    # Counting in the database removes the transfer entirely. The scan is
    # still proportional to the org's history -- a question about all of
    # history cannot be answered without reading all of it, short of a
    # materialized rollup (see hub/DEPLOYMENT.md's note on when that
    # becomes worth building) -- but the constant is smaller by orders of
    # magnitude and nothing crosses the network per row.
    #
    # `jsonb_typeof(...) = 'boolean'` is not defensive noise: it is the SQL
    # spelling of outcomes._is_bool. A client that sent the STRING "true"
    # would otherwise be cast by `::boolean` into a success and silently
    # invert the rate. Likewise 'number' for the cost means, which keeps a
    # misfiled boolean from being averaged in as 1.
    def _bool_field(field: str):
        return and_(
            func.jsonb_typeof(Trace.outcome[field]) == "boolean",
            Trace.outcome[field].astext.cast(Boolean),
        )

    def _bool_recorded(field: str):
        return func.jsonb_typeof(Trace.outcome[field]) == "boolean"

    is_baseline = case(
        (
            and_(
                func.jsonb_typeof(Trace.outcome["baseline"]) == "boolean",
                Trace.outcome["baseline"].astext.cast(Boolean),
            ),
            True,
        ),
        else_=False,
    ).label("is_baseline")

    columns = [is_baseline, func.count().label("n")]
    for _name, field, _direction in outcomes.PROPORTION_METRICS:
        columns.append(func.count().filter(_bool_field(field)).label(f"{field}_true"))
        columns.append(func.count().filter(_bool_recorded(field)).label(f"{field}_n"))
    for _name, field in outcomes.MEAN_METRICS:
        numeric = func.jsonb_typeof(Trace.outcome[field]) == "number"
        columns.append(
            func.avg(Trace.outcome[field].astext.cast(Float)).filter(numeric).label(f"{field}_avg")
        )
        columns.append(func.count().filter(numeric).label(f"{field}_n"))

    where = [Trace.org_id == org_id, Trace.quarantined.is_(False)]
    if agent_type:
        reject_unstorable_text(agent_type, "agent_type")
        where.append(Trace.agent_type == agent_type)

    grouped = (
        await session.execute(select(*columns).where(*where).group_by(is_baseline))
    ).all()

    arms = {True: outcomes.Tally(), False: outcomes.Tally()}
    for row in grouped:
        arms[bool(row.is_baseline)] = outcomes.Tally(
            n=row.n,
            props={
                field: (getattr(row, f"{field}_true"), getattr(row, f"{field}_n"))
                for _n, field, _d in outcomes.PROPORTION_METRICS
            },
            means={
                field: (
                    float(getattr(row, f"{field}_avg"))
                    if getattr(row, f"{field}_avg") is not None
                    else None,
                    getattr(row, f"{field}_n"),
                )
                for _n, field in outcomes.MEAN_METRICS
            },
        )

    report = outcomes.compare_tallies(arms[True], arms[False], alpha=alpha)
    report["org_id"] = org_id
    report["agent_type"] = agent_type
    report["n_traces"] = arms[True].n + arms[False].n
    return report


# --- Knowledge Base community submissions -------------------------------
#
# The one path by which a customer can influence Knowledge Base content --
# and even then, only indirectly. submit_kb_entry writes a
# KnowledgeBaseSubmission row, a table entirely separate from `Trace`; it
# is invisible to commons_overlap/commons_search (which only ever read
# `Trace.commons_source == "seed"`) and to every other org's
# search_traces/get_trace (which are org-scoped to Trace, not this table).
# review_kb_submission -- an operator-trust-level action, called from
# hub/manage.py, never from an MCP tool -- is the only function that can
# turn an approved submission into a real Trace. See
# hub/models.py:KnowledgeBaseSubmission and hub/plans.py "why
# bonus_commons_queries is not the same mistake twice" for the reasoning.

MAX_PENDING_SUBMISSIONS_PER_ORG = 20
VALID_SUBMISSION_DECISIONS = ("approve", "reject")


async def _reserve_kb_submission_slot(session: AsyncSession, org_id: str) -> None:
    """Enforce MAX_PENDING_SUBMISSIONS_PER_ORG for a caller about to insert a
    new KnowledgeBaseSubmission row.

    Bounds how large the operator's review queue can be forced to grow by
    one org -- not a quality gate (review is), just an anti-griefing cap so
    a queue is never buried under one org's backlog.

    SELECT ... FOR UPDATE on the org's own row, for the same reason
    _reserve_trace_slot takes it: count-then-insert is a TOCTOU race under
    concurrent callers for the SAME org without it -- N coroutines racing
    when the org is one submission away from the cap can each COUNT before
    any of the others' INSERT is visible, so all N pass a check only one of
    them should have (hub/tests/test_concurrency_audit.py
    TestSubmitKbEntryPendingQuotaRace reproduced the queue growing to 15
    against a cap of 5 before this lock was added). A different org's row
    lock never blocks this one.
    """
    await session.execute(
        select(Organization.id).where(Organization.id == org_id).with_for_update()
    )
    pending = int(await session.scalar(
        select(func.count()).select_from(KnowledgeBaseSubmission).where(
            KnowledgeBaseSubmission.org_id == org_id,
            KnowledgeBaseSubmission.status == "pending",
        )
    ) or 0)
    if pending >= MAX_PENDING_SUBMISSIONS_PER_ORG:
        raise TraceRejected(
            f"{pending} submission(s) already awaiting review; wait for those to be "
            "reviewed before proposing more"
        )


def _submission_to_wire(s: KnowledgeBaseSubmission) -> dict:
    return {
        "id": s.id,
        "title": s.title,
        "context_text": s.context_text,
        "solution_text": s.solution_text,
        "tags": list(s.tags or []),
        "agent_type": s.agent_type,
        "rationale": s.rationale,
        "status": s.status,
        "created_at": _iso(s.created_at),
        "reviewed_at": _iso(s.reviewed_at) if s.reviewed_at else None,
        "reviewed_by": s.reviewed_by,
        "rejection_reason": s.rejection_reason,
        "resulting_trace_id": s.resulting_trace_id,
        "credit_awarded": s.credit_awarded,
    }


def _submission_idempotent_replay_or_conflict(
    existing: KnowledgeBaseSubmission,
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
            f"idempotency_key {idempotency_key!r} was already used for a different submit_kb_entry "
            "payload; reuse a key only to retry the exact same request"
        )
    return _submission_to_wire(existing)


async def submit_kb_entry(
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
    rationale: str = "",
    actor: str = AUDIT_ACTOR_UNKNOWN,
    idempotency_key: str | None = None,
) -> dict:
    """Propose a Knowledge Base entry for operator review. Nothing is
    published by this call -- the submission is created with
    status='pending' and stays that way until hub/manage.py
    review-submission decides on it.

    Validated like contribute_trace (schema, size limits, rate limit --
    reusing the same checks because the content shape is identical) with
    one deliberate omission: no quarantine heuristic. Every submission is
    read by a human at review time regardless, so a cheap spam heuristic
    here would only be a second, redundant gate -- the review step already
    is quarantine, done properly.

    Raises TraceRejected (bad schema/oversized/too many still-open
    submissions) or RateLimited (429-shaped) without storing anything.
    """
    tags = tags or []

    # reject_unstorable_text before every len() check -- see
    # contribute_trace's identical fix for why: len() itself raises an
    # uncaught TypeError for a non-string value (submit_kb_entry(rationale=123)
    # used to crash with "TypeError: object of type 'int' has no len()"),
    # never reaching the isinstance check that would turn it into a clean
    # ValueError.
    reject_unstorable_text(rationale, "rationale")
    if idempotency_key is not None:
        reject_unstorable_text(idempotency_key, "idempotency_key")
    if idempotency_key is not None and len(idempotency_key) > 128:
        raise TraceRejected(f"idempotency_key exceeds 128 chars ({len(idempotency_key)})")
    if len(rationale) > 500:
        raise TraceRejected(f"rationale exceeds 500 chars ({len(rationale)})")

    if idempotency_key is not None:
        existing = (
            await session.execute(
                select(KnowledgeBaseSubmission).where(
                    KnowledgeBaseSubmission.org_id == org_id,
                    KnowledgeBaseSubmission.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _submission_idempotent_replay_or_conflict(
                existing, idempotency_key, title, context_text, solution_text, tags, agent_type
            )

    # Namespaced within the same limiter/config contribute_trace uses,
    # rather than a new config knob: proposing a Knowledge Base entry is a
    # deliberate, occasional action, not a bulk capture path, so it does
    # not need its own tuning -- but it gets its own bucket so a fleet
    # capturing traces at volume cannot starve its own ability to submit.
    allowed, retry_after = await rate_limiter.check(f"kb_submit:{org_id}")
    if not allowed:
        raise RateLimited(
            f"org {org_id} exceeded submit_kb_entry rate limit", retry_after=retry_after
        )

    await _reserve_kb_submission_slot(session, org_id)

    wire = {
        "id": str(uuid.uuid4()),
        "title": title,
        "context_text": context_text,
        "solution_text": solution_text,
        "tags": tags,
        "agent_type": agent_type,
    }
    validate_trace(wire)  # raises SchemaValidationError -> hard reject
    validate_size(wire, config)  # raises TraceRejected -> hard reject

    submission = KnowledgeBaseSubmission(
        org_id=org_id,
        title=title,
        context_text=context_text,
        solution_text=solution_text,
        tags=tags,
        agent_type=agent_type,
        rationale=rationale,
        idempotency_key=idempotency_key,
        request_hash=(
            _contribute_request_hash(title, context_text, solution_text, tags, agent_type)
            if idempotency_key is not None else None
        ),
    )
    session.add(submission)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = (
            await session.execute(
                select(KnowledgeBaseSubmission).where(
                    KnowledgeBaseSubmission.org_id == org_id,
                    KnowledgeBaseSubmission.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return _submission_idempotent_replay_or_conflict(
            existing, idempotency_key, title, context_text, solution_text, tags, agent_type
        )

    await audit.record(
        session, actor=actor, action="submit_kb_entry", org_id=org_id,
        target_type="kb_submission", target_id=submission.id,
        summary=f"agent_type={agent_type or '?'} title_len={len(title)} n_tags={len(tags)}",
    )
    return _submission_to_wire(submission)


async def list_my_kb_submissions(session: AsyncSession, org_id: str, limit: int = 50) -> list[dict]:
    """An org's own proposals and their review status. Org-scoped like
    every other read path in this module -- a pending or rejected
    submission is never visible to any other org, and an approved one is
    visible to other orgs only as an ordinary Knowledge Base Trace, not as
    this row."""
    rows = (await session.execute(
        select(KnowledgeBaseSubmission)
        .where(KnowledgeBaseSubmission.org_id == org_id)
        .order_by(KnowledgeBaseSubmission.created_at.desc())
        .limit(_clamp_int(limit, 1, 200, 50))
    )).scalars().all()
    return [_submission_to_wire(s) for s in rows]


async def list_kb_submissions(
    session: AsyncSession, status: str | None = None, limit: int = 100
) -> list[dict]:
    """The operator's review queue (or full history, if status=None).
    Cross-org by design -- this IS the operator-trust-level surface, same
    tier as purge-trace -- so it is called only from hub/manage.py, never
    exposed as an MCP tool."""
    stmt = select(KnowledgeBaseSubmission)
    if status is not None:
        stmt = stmt.where(KnowledgeBaseSubmission.status == status)
    stmt = stmt.order_by(KnowledgeBaseSubmission.created_at.asc()).limit(_clamp_int(limit, 1, 1000, 100))
    rows = (await session.execute(stmt)).scalars().all()
    return [_submission_to_wire(s) for s in rows]


async def review_kb_submission(
    session: AsyncSession,
    submission_id: str,
    decision: str,
    operator_org_id: str,
    reviewer: str,
    rejection_reason: str = "",
    credit: int | None = None,
) -> dict | None:
    """The one deliberate action that can turn a customer's proposal into
    Knowledge Base content. Returns None for an unknown or already-decided
    submission id; raises ValueError for a decision other than
    'approve'/'reject'.

    On approve: creates a new Trace owned by `operator_org_id` -- never the
    submitting org, exactly like hub/manage.py:commons_seed -- with
    commons_source='seed', and permanently raises the submitting org's
    Knowledge Base query allowance by `credit`
    (plans.SUBMISSION_ACCEPTANCE_CREDIT unless overridden).

    On reject: the submission is marked rejected with `rejection_reason`.
    No Trace, no credit. That silence is the entire adverse-selection
    defense (hub/plans.py "why bonus_commons_queries is not the same
    mistake twice"): a submission that does not clear review earns
    nothing, so filler cannot be a strategy for extracting allowance.
    """
    if decision not in VALID_SUBMISSION_DECISIONS:
        raise ValueError(f"decision must be one of {VALID_SUBMISSION_DECISIONS}, got {decision!r}")
    reject_unstorable_text(reviewer, "reviewer")
    reject_unstorable_text(rejection_reason, "rejection_reason")
    if not _is_uuid(submission_id):
        return None

    submission = (
        await session.execute(
            select(KnowledgeBaseSubmission)
            .where(
                KnowledgeBaseSubmission.id == submission_id,
                KnowledgeBaseSubmission.status == "pending",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if submission is None:
        return None

    now = datetime.now(timezone.utc)
    if decision == "reject":
        submission.status = "rejected"
        submission.reviewed_at = now
        submission.reviewed_by = reviewer
        submission.rejection_reason = rejection_reason
        await audit.record(
            session, actor=reviewer, action="review_kb_submission", org_id=submission.org_id,
            target_type="kb_submission", target_id=submission.id,
            summary=f"rejected reason_len={len(rejection_reason)}",
        )
        return _submission_to_wire(submission)

    awarded = (
        plans.SUBMISSION_ACCEPTANCE_CREDIT if credit is None
        # No upper bound in the original (an operator's own call, not a
        # customer-facing one), preserved here via 2**63-1 rather than
        # introducing a new cap -- only the OverflowError/TypeError/
        # ValueError-on-malformed-input gap is being closed.
        else _clamp_int(credit, 0, 2**63 - 1, plans.SUBMISSION_ACCEPTANCE_CREDIT)
    )
    trace = Trace(
        org_id=operator_org_id,
        title=submission.title,
        context_text=submission.context_text,
        solution_text=submission.solution_text,
        tags=list(submission.tags or []),
        agent_type=submission.agent_type,
        shared_with_commons=True,
        shared_at=now,
        shared_rationale=(
            submission.rationale or "community-contributed substrate knowledge, operator-reviewed"
        )[:500],
        commons_signature=commons.signature_for(
            submission.title, submission.context_text, list(submission.tags or [])
        ),
        commons_source="seed",
    )
    session.add(trace)
    await session.flush()
    # Bypasses contribute_trace/_reserve_trace_slot entirely (an operator's
    # own curation decision, not customer traffic -- see this function's
    # lack of a plan check above), but it is still a real row landing in
    # operator_org_id's own trace table and must be counted the same way.
    await _adjust_trace_count(session, operator_org_id, +1)

    submission.status = "approved"
    submission.reviewed_at = now
    submission.reviewed_by = reviewer
    submission.resulting_trace_id = trace.id
    submission.credit_awarded = awarded

    # An atomic SQL-level increment, not `org = await session.get(...);
    # org.bonus_commons_queries = org.bonus_commons_queries + awarded`:
    # the submission row locked above (with_for_update) only serializes
    # concurrent reviews of THAT ONE submission -- two DIFFERENT pending
    # submissions for the SAME org, approved concurrently, do not
    # conflict on it at all, so both could read the same starting
    # bonus_commons_queries and each independently compute their own
    # `old + awarded`. Whichever commit lands second then overwrites the
    # column with its own stale total, silently discarding the first
    # approval's credit even though its audit log entry and
    # submission.credit_awarded both still say it was granted (reproduced
    # under 2-way concurrent approval of two distinct submissions for the
    # same org). `Organization.bonus_commons_queries + awarded` computed
    # by Postgres at UPDATE time, under that row's own lock, is the same
    # atomic-increment pattern this file already uses for
    # Trace.retrievals a few functions up.
    await session.execute(
        update(Organization)
        .where(Organization.id == submission.org_id)
        .values(bonus_commons_queries=Organization.bonus_commons_queries + awarded)
    )

    await audit.record(
        session, actor=reviewer, action="review_kb_submission", org_id=submission.org_id,
        target_type="kb_submission", target_id=submission.id,
        summary=f"approved -> trace={trace.id} credit={awarded}",
    )
    return _submission_to_wire(submission)


# --- Knowledge Base maintenance (operator-trust-level) ------------------
#
# Everything above grows the corpus: `commons_seed` in bulk,
# `review_kb_submission` one accepted proposal at a time. Nothing above
# maintains it, and a curated corpus that only grows is one that decays --
# the entries stay, the world moves, and the product keeps serving answers
# that used to be right. DATA_RETENTION.md flagged the missing half
# plainly ("there is no CLI command to correct or remove a single
# Knowledge Base entry after commons-seed has loaded it, short of a direct
# database operation"); these three functions are it.
#
# WHY THIS SCALES, WHICH IS THE ACTUAL POINT. Operator curation has an
# obvious objection: reviewing a corpus is O(corpus), so the model breaks
# somewhere past a few thousand entries. It breaks only if the operator has
# to FIND the bad entries. It does not, because using the corpus already
# generates the signal that locates them -- every query credits
# `commons_hits`, every fleet that tries an answer can vote on it, and
# `feedback_tag` says what kind of wrong it was. `kb_review_queue` turns
# that exhaust into a work list ordered by damage done, so review cost
# tracks the ERROR RATE rather than the corpus size. That is the same
# mechanism that lets Stack Overflow and Wikipedia stay usable at a scale
# no editorial staff could read: readers find the errors, editors
# adjudicate them.


async def retract_kb_entry(
    session: AsyncSession,
    trace_id: str,
    reason: str = "",
    actor: str = audit.ACTOR_OPERATOR_CLI,
) -> dict | None:
    """Withdraw one entry from the Knowledge Base. Returns its wire shape,
    or None if `trace_id` is not a live Knowledge Base entry.

    Deliberately not a delete. The row, its votes, and its accumulated
    `commons_hits` all survive, because the question an operator asks after
    a retraction -- "how many fleets did we serve this to before we pulled
    it, and what did they say about it" -- is answerable only from exactly
    the data a DELETE would destroy. `purge-trace` remains the path for
    actually removing content (see DATA_RETENTION.md §3); this is the path
    for un-publishing it, which is a different and far more common need.

    Idempotent in the way that matters: retracting an already-retracted
    entry returns None rather than overwriting the original timestamp and
    reason with a second, less informative pair.
    """
    if not _is_uuid(trace_id):
        return None
    stmt = select(Trace).where(Trace.id == trace_id, *commons_visible())
    trace = (await session.execute(stmt)).scalar_one_or_none()
    if trace is None:
        return None

    reason = (reason or "")[:200]
    reject_unstorable_text(reason, "reason")
    trace.commons_retracted_at = datetime.now(timezone.utc)
    trace.commons_retraction_reason = reason
    await session.flush()
    await audit.record(
        session,
        actor=actor,
        action="retract_kb_entry",
        org_id=trace.org_id,
        target_type="trace",
        target_id=trace.id,
        summary=f"hits_at_retraction={trace.commons_hits} reason={trace.commons_retraction_reason or '-'}",
    )
    # Projected through the same narrow wire shape the Knowledge Base
    # queries use, with the retraction stated explicitly -- the row no
    # longer satisfies commons_visible(), so `standing` computed from it
    # would describe an entry nobody can reach.
    wire = _to_commons_wire(trace)
    wire["standing"] = "retracted"
    wire["retracted_at"] = _iso(trace.commons_retracted_at)
    wire["retraction_reason"] = trace.commons_retraction_reason
    return wire


async def restore_kb_entry(
    session: AsyncSession, trace_id: str, actor: str = audit.ACTOR_OPERATOR_CLI
) -> dict | None:
    """Undo a retraction. Returns the restored entry, or None if `trace_id`
    is not a retracted Knowledge Base entry.

    Exists because retraction is the right response to a *suspected*
    problem and suspicion is sometimes wrong. Without a cheap undo, the
    honest operator move on an ambiguous report is to leave a possibly-bad
    entry serving traffic while investigating, which is the wrong default.
    """
    if not _is_uuid(trace_id):
        return None
    stmt = select(Trace).where(
        Trace.id == trace_id,
        Trace.shared_with_commons.is_(True),
        Trace.commons_source == "seed",
        Trace.commons_retracted_at.isnot(None),
    )
    trace = (await session.execute(stmt)).scalar_one_or_none()
    if trace is None:
        return None

    trace.commons_retracted_at = None
    trace.commons_retraction_reason = ""
    await session.flush()
    await audit.record(
        session,
        actor=actor,
        action="restore_kb_entry",
        org_id=trace.org_id,
        target_type="trace",
        target_id=trace.id,
        summary="restored to the Knowledge Base",
    )
    return _to_commons_wire(trace)


# A single vote tagged `security_concern` puts an entry at the top of the
# review queue regardless of how the rest of the tally looks. This is the
# one place the "votes inform, the operator decides" rule is applied at
# n=1, and the asymmetry is the point: the cost of reading one spurious
# report is a minute of an operator's time, and the cost of missing a real
# one is bad security advice served from a corpus customers were told to
# trust, to every fleet whose failure it matches, for as long as nobody
# looks. Note what it still does NOT do -- it does not retract, hide, or
# de-rank anything on its own.
URGENT_FEEDBACK_TAGS = ("security_concern",)


async def kb_review_queue(session: AsyncSession, limit: int = 50) -> list[dict]:
    """Which Knowledge Base entries need a human, worst first.

    Four buckets, in priority order, each carrying why it is listed:

    * `urgent`     -- somebody flagged a security concern on it.
    * `disputed`   -- a majority of the fleets that tried it say it failed.
    * `stale`      -- its declared freshness horizon has passed.
    * `never_hit`  -- it has never matched anyone's failure. Not an error;
                      it is either content nobody needs or content worded
                      so differently from how fleets describe the failure
                      that the matcher cannot find it, and both are worth
                      an operator's attention eventually.

    Within a bucket, ordered by `commons_hits` descending -- how much
    traffic the entry is actually affecting. A wrong answer nobody reaches
    is a smaller problem than a wrong answer served a thousand times, and
    an operator working top-down should be spending attention in that
    order.

    Retracted entries are absent: they are already dealt with.
    """
    limit = _clamp_int(limit, 1, 500, 50)
    queue = await _kb_review_queue_full(session)
    return queue[:limit]


async def count_kb_review_queue(session: AsyncSession) -> int:
    """The true size of kb_review_queue's list, uncapped by `limit` --
    for a summary tile, which must not read as a total when it is actually
    `min(true_count, limit)`. Shares _kb_review_queue_full with
    kb_review_queue itself rather than re-deriving the four-bucket
    classification a second way that could silently drift from it."""
    return len(await _kb_review_queue_full(session))


async def kb_review_queue_and_total(session: AsyncSession, limit: int = 50) -> tuple[list[dict], int]:
    """(kb_review_queue(limit), count_kb_review_queue()) from ONE
    _kb_review_queue_full call, for a caller (hub/admin.py's KB dashboard)
    that needs both the bounded list and the true total in the same
    request -- calling kb_review_queue and count_kb_review_queue
    separately would each independently re-run the same Trace query and
    the Vote flag-count query behind _kb_review_queue_full."""
    limit = _clamp_int(limit, 1, 500, 50)
    queue = await _kb_review_queue_full(session)
    return queue[:limit], len(queue)


async def _kb_review_queue_full(session: AsyncSession) -> list[dict]:
    """Every entry needing review, worst first, with no `limit` applied --
    see kb_review_queue's docstring for the four-bucket classification this
    implements. Split out so kb_review_queue (bounded, for actually listing
    entries) and count_kb_review_queue (a true count, for a summary tile)
    can't disagree about what counts as "needs attention"."""
    rows = (
        await session.execute(
            select(Trace).where(*commons_visible()).order_by(Trace.commons_hits.desc())
        )
    ).scalars().all()
    if not rows:
        return []

    # One grouped query for the flag counts rather than a per-entry lookup:
    # this is an operator report, but it still should not issue a query per
    # corpus entry.
    flagged = dict(
        (
            await session.execute(
                select(Vote.trace_id, func.count())
                .where(
                    Vote.trace_id.in_([r.id for r in rows]),
                    Vote.feedback_tag.in_(URGENT_FEEDBACK_TAGS),
                )
                .group_by(Vote.trace_id)
            )
        ).all()
    )

    now = datetime.now(timezone.utc)
    order = {"urgent": 0, "disputed": 1, "stale": 2, "never_hit": 3}
    queue: list[dict] = []
    for trace in rows:
        standing = standing_of(trace, now)
        n_flags = flagged.get(trace.id, 0)
        if n_flags:
            bucket, why = "urgent", f"{n_flags} security concern report(s)"
        elif standing == commons.STANDING_DISPUTED:
            bucket, why = (
                "disputed",
                f"{trace.commons_votes} votes, trust {trace.trust:.2f}",
            )
        elif standing == commons.STANDING_STALE:
            bucket, why = "stale", f"review due {_iso(trace.commons_review_after)}"
        elif trace.commons_hits == 0:
            bucket, why = "never_hit", "has never matched a real failure"
        else:
            continue
        queue.append(
            {
                "id": trace.id,
                "title": trace.title,
                "bucket": bucket,
                "why": why,
                "standing": standing,
                "commons_hits": trace.commons_hits,
                "vote_count": trace.commons_votes,
                "trust": trace.trust,
                "security_flags": n_flags,
            }
        )

    # `rows` is already hits-descending, and Python's sort is stable, so
    # sorting on the bucket alone preserves that ordering within each one.
    queue.sort(key=lambda item: order[item["bucket"]])
    return queue


# --- The CommonTrace Knowledge Base (opt-in, operator-curated) ---------
#
# commons_overlap and commons_search are the only two places in this module
# where a query reaches content outside the caller's own org. Neither one
# can ever reach another CUSTOMER's data: both scope their corpus to
# `Trace.commons_source == "seed"`, which is written only by
# `hub/manage.py:commons_seed` and by `review_kb_submission` above (itself
# operator-trust-level, called only from hub/manage.py) -- there is no
# MCP-exposed, customer-facing path that sets `shared_with_commons` on a
# customer's own trace, or that can set `commons_source == "seed"` at all.
# Everything else in this module stays unconditionally org-scoped;
# hub/tests/test_tenant_isolation.py passes unchanged. See hub/commons.py
# for why the query itself is a signature, never text.


async def commons_overlap(
    session: AsyncSession,
    org_id: str,
    failures: object,
    threshold: float = commons.DEFAULT_COMMONS_THRESHOLD,
    include_matches: bool = True,
    agent_type: str = "",
) -> dict:
    """Of the recurring failures this fleet keeps hitting, what fraction
    does the CommonTrace Knowledge Base already solve?

    The Knowledge Base is a corpus the OPERATOR authors and curates
    (`hub/manage.py:commons_seed`) -- public substrate knowledge (protocol
    semantics, vendor documentation, standards), never another customer's
    trace. There is no org-to-org sharing in this system: see hub/plans.py
    "why there is no org-to-org sharing here" for why that was the design
    and not an oversight.

    The caller sends MinHash signatures of its own failures -- computed
    locally, no failure text leaves the client. What comes back is drawn
    only from `commons_source == "seed"` rows, so the answer can never
    contain another customer's content even by accident.

    Quarantined rows are excluded, same as every other read path.
    """
    submitted = commons.validate_submitted_failures(failures)

    # float(threshold) alone raises an uncaught TypeError for None/list/dict
    # -- not a ValueError, so isfinite's own guard below never got a chance
    # to run for those. Reproduced live: commons_overlap(threshold=None)
    # crashed with "TypeError: float() argument must be a string or a real
    # number, not 'NoneType'".
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        raise commons.CommonsInputError("threshold must be a number") from None
    if not math.isfinite(threshold):
        raise commons.CommonsInputError("threshold must be a finite number")
    threshold = max(0.0, min(threshold, 1.0))

    # Metered here, and only here: this is the one call that reads the
    # operator-maintained Knowledge Base rather than the caller's own data.
    # Validation runs first so a malformed request is a 400 rather than a
    # silently consumed query -- charging for a call that returned an error
    # is the kind of thing customers notice and remember.
    #
    # An empty submission is not metered either: it compares nothing, so
    # billing it would be charging for a no-op.
    if submitted:
        plan, bonus = await _plan_and_bonus_for(session, org_id)
        if not plan.commons_access:
            raise plans.EntitlementExceeded(
                metric="commons_access", limit=0, used=0, plan=plan.name,
                remedy="The Knowledge Base is not included in this plan.",
            )
        allowance = plans.query_allowance(plan, bonus)
        if allowance != plans.UNLIMITED:
            # SELECT ... FOR UPDATE on the org's own row, same pattern as
            # _reserve_trace_slot: read-check-then-increment across two
            # separate statements (the read here, _meter's own atomic
            # increment below) is a TOCTOU race under concurrent calls from
            # the SAME org -- two requests can each read `used = allowance -
            # 1`, both pass the check, and both then increment, letting the
            # org exceed its allowance by however many requests raced. The
            # row lock serializes exactly this org's own concurrent calls
            # around the check; a different org's commons_overlap locks a
            # different row and is unaffected.
            await session.execute(
                select(Organization.id).where(Organization.id == org_id).with_for_update()
            )
            used = await _usage(session, org_id, METRIC_COMMONS_QUERIES)
            if not plans.within(allowance, used):
                raise plans.EntitlementExceeded(
                    metric=METRIC_COMMONS_QUERIES, limit=allowance, used=used, plan=plan.name,
                    remedy="Move to a plan with a larger Knowledge Base query allowance.",
                )
        # Metered before the scan rather than after: a query that times out
        # or errors mid-scan still consumed the corpus read it asked for,
        # and "only charge on success" is an invitation to cancel every
        # expensive call just before it returns.
        await _meter(session, org_id, METRIC_COMMONS_QUERIES)

    # commons_visible() carries the line that makes "no org-to-org sharing"
    # a guarantee rather than a policy: even if some future bug set
    # shared_with_commons on a customer's own trace, it still could not
    # surface here without ALSO being commons_source == "seed", which only
    # commons_seed and review_kb_submission write. Retracted entries are
    # excluded there too. hub/tests/test_commons_search.py and
    # test_commons.py both assert a non-seed shared row is invisible to
    # this scan.
    where = [
        *commons_visible(),
        Trace.commons_signature.isnot(None),
        Trace.org_id != org_id,
    ]
    # Optional semantic narrowing. Not an approximation: a support fleet's
    # failures genuinely should not be scored against CUDA substrate. It is
    # also the cheapest way to keep the scan small as the corpus grows,
    # because it runs in Postgres instead of Python.
    if agent_type:
        reject_unstorable_text(agent_type, "agent_type")
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

    # Up to MAX_SUBMITTED_FAILURES (500) signatures against up to
    # max_corpus_scan() (20,000, or 2,000 without numpy) corpus rows is a
    # CPU-bound comparison loop that can run long enough to stall the
    # single-threaded asyncio event loop -- starving every other request
    # this process is serving, not just this one. Offloaded to a worker
    # thread so the loop stays free to schedule other coroutines while it
    # runs; the GIL still serializes the actual comparisons, but that's a
    # throughput cost to this one call, not an availability cost to
    # everyone else's requests.
    best = await asyncio.to_thread(
        commons.best_matches, submitted, [r.commons_signature or [] for r in rows]
    )

    matches: list[dict] = []
    disputed_matches: list[dict] = []
    by_domain: dict[str, int] = {}
    n_covered = 0
    now = datetime.now(timezone.utc)

    hit_ids: list[str] = []
    for (label, _sig), (idx, sim) in zip(submitted, best):
        if idx < 0 or sim < threshold:
            continue
        hit = rows[idx]
        # Counted as matched regardless of standing: commons_hits answers
        # "how often was this entry served", which is what makes
        # `kb-review` able to rank a bad entry by how much traffic it is
        # misdirecting. An entry that stopped counting as coverage but is
        # still the top match for hundreds of failures is the single most
        # urgent thing in an operator's queue, and suppressing its hit
        # count would hide exactly that.
        hit_ids.append(hit.id)
        entry = {
            "failure_label": label,
            "similarity": round(sim, 4),
            "agent_type": hit.agent_type,
            "tags": list(hit.tags or []),
            # The payoff. Safe to return in full: `hit` is only in the
            # corpus because the operator curated it as public substrate
            # knowledge, never because another customer's trace leaked into
            # it (the commons_source == "seed" filter above is what
            # guarantees that).
            "trace": _to_commons_wire(hit, now),
        }
        if not commons.counts_as_coverage(entry["trace"]["standing"]):
            # A majority of the fleets that tried this entry reported it
            # did not work (hub/commons.py's standing model). It is not a
            # solved failure, so it does not enter the one figure this tool
            # exists to produce -- but it is still returned, separately and
            # labelled, because "the Knowledge Base has something about
            # this and it is contested" is a materially different answer
            # from "the Knowledge Base has nothing", and silently dropping
            # it would make the two indistinguishable to the caller.
            if include_matches:
                disputed_matches.append(entry)
            continue
        n_covered += 1
        key = hit.agent_type or "(unspecified)"
        by_domain[key] = by_domain.get(key, 0) + 1
        if include_matches:
            matches.append(entry)

    if hit_ids:
        # Atomic in-database increment, same pattern as the retrievals
        # counter: a read-modify-write through the ORM would lose counts
        # under concurrent queries, and this number is the operator's
        # quality signal for its own curated content
        # (hub/models.py:Trace.commons_hits) -- which entries actually
        # cover real recurring failures, worth keeping and expanding on,
        # versus which ones never hit and are candidates to prune. Counted
        # once per covered failure, not once per query, so re-running the
        # same report does not inflate an entry's standing for free -- but
        # a genuinely repeated need does register.
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
        # Capped per trace, per call: nothing on the wire stops a caller
        # from submitting the SAME signature hundreds of times in one
        # request (MAX_SUBMITTED_FAILURES allows up to 500), and without a
        # cap that credits whichever entry it best-matches once per
        # repetition -- turning one submitted failure, repeated, into
        # hundreds of hits on the operator's quality signal for that entry.
        # A small multiplicity from one call is the legitimate case (a
        # fleet hitting one substrate failure across a handful of distinct
        # tasks, submitted together -- see hub/tests/test_commons.py
        # test_two_failures_in_one_query_hitting_the_same_trace_both_count);
        # hundreds of repeats of the identical signature is not that, it is
        # the same submission counted as if it were hundreds of them, which
        # would make one entry look far more useful than it actually is.
        hit_counts = {
            tid: min(cnt, commons.MAX_HITS_PER_TRACE_PER_QUERY) for tid, cnt in hit_counts.items()
        }
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
    disputed_matches.sort(key=lambda m: m["similarity"], reverse=True)
    n_failures = len(submitted)
    return {
        "n_failures": n_failures,
        "n_commons_traces": len(rows),
        "n_commons_traces_total": total_corpus,
        "corpus_truncated": corpus_truncated,
        "n_covered": n_covered,
        "covered_fraction": (n_covered / n_failures) if n_failures else 0.0,
        # Matched at or above the threshold, then excluded from the count
        # above because the entry is disputed. Reported so the coverage
        # figure never moves without the caller being able to see why:
        # a number that fell because the field found an answer wrong is a
        # different event from one that fell because the corpus shrank.
        "n_disputed": len(disputed_matches),
        "threshold": threshold,
        "by_agent_type": dict(sorted(by_domain.items(), key=lambda kv: -kv[1])),
        "matches": matches,
        "disputed_matches": disputed_matches,
        "note": _commons_note(n_failures, len(rows), corpus_truncated, total_corpus),
    }


_SEARCH_NOTE = (
    "Ranked CANDIDATES, not coverage. Every result is a suggestion to judge, "
    "the way a search engine's results are: on the held-out evaluation a "
    "failure the commons does NOT contain still returns a non-empty list "
    "100% of the time, and the score distributions of true and absent "
    "matches overlap. Use `commons_overlap` -- thresholded, 0% false "
    "positives -- for any figure you intend to quote. See "
    "commons/eval/RESULTS.md. Read each candidate's `standing` before "
    "acting on it: `disputed` means a majority of the fleets that tried it "
    "reported it did not work, and those candidates are ranked last."
)


async def commons_search(
    session: AsyncSession,
    org_id: str,
    query_signature: object,
    limit: int = commons.DEFAULT_SEARCH_CANDIDATES,
    agent_type: str = "",
) -> dict:
    """Ask the CommonTrace Knowledge Base what it knows about one failure,
    and get back ranked candidate answers -- the lookup surface, as
    distinct from `commons_overlap`'s coverage percentage.

    WHY THIS EXISTS SEPARATELY FROM commons_overlap
    -----------------------------------------------
    They answer different questions and require opposite trades.
    `commons_overlap` answers "what fraction of my failures does the
    Knowledge Base already solve", emits a number a customer may quote, and
    therefore buys 0% false positives with a threshold. That threshold was
    measured to discard about nine of every ten real answers
    (commons/eval/RESULTS.md), which is the correct price for a quotable
    figure and the wrong price for looking something up.

    This tool ranks instead of thresholding. Measured on the same corpus,
    the same probes and the same signatures: 89.1% recall@1, 95.7%@5, 100%
    within the top 10 (commons/eval/search_modes.py). The privacy
    properties are unchanged -- the caller sends one MinHash signature, no
    failure text leaves the fleet, and what comes back is drawn only from
    `commons_source == "seed"` rows the operator curated (see
    hub/plans.py "why there is no org-to-org sharing here" -- this was
    never a pool of other customers' traces, and cannot become one).

    WHAT IT DELIBERATELY DOES NOT DO
    --------------------------------
    * It never reports coverage, and its result carries a note saying so.
      Absent failures return a non-empty list every time.
    * It does not credit `commons_hits`. A hit is the operator's quality
      signal for its own curated content -- "this entry covered a real
      recurring failure" -- established at the conservative threshold. A
      search *candidate* is not that, and crediting candidates would make
      the one metric that resists noise trivially inflatable.

    Scoping matches commons_overlap exactly: the caller's own traces are
    excluded (they are never in this corpus regardless -- see the
    commons_source filter below) and quarantined rows are excluded.
    """
    sig = commons.validate_query_signature(query_signature)

    try:
        limit = int(limit)
    except (TypeError, ValueError, OverflowError):
        # OverflowError alongside the other two: int(float("inf")) raises
        # it specifically, not ValueError, and it used to reach a caller
        # as an uncaught 500 (hub/commons.py:_coerce_signature's own
        # standing comment names exactly this failure mode at a different
        # call site).
        raise commons.CommonsInputError("limit must be an integer") from None
    limit = max(1, min(limit, commons.MAX_SEARCH_CANDIDATES))

    # Metered exactly like commons_overlap, and for the same reason: this is
    # a call that reads the operator-maintained Knowledge Base rather than
    # the caller's own data. Validation runs first so a malformed request
    # is never a silently consumed query.
    plan, bonus = await _plan_and_bonus_for(session, org_id)
    if not plan.commons_access:
        raise plans.EntitlementExceeded(
            metric="commons_access", limit=0, used=0, plan=plan.name,
            remedy="The Knowledge Base is not included in this plan.",
        )
    allowance = plans.query_allowance(plan, bonus)
    if allowance != plans.UNLIMITED:
        await session.execute(
            select(Organization.id).where(Organization.id == org_id).with_for_update()
        )
        used = await _usage(session, org_id, METRIC_COMMONS_QUERIES)
        if not plans.within(allowance, used):
            raise plans.EntitlementExceeded(
                metric=METRIC_COMMONS_QUERIES, limit=allowance, used=used, plan=plan.name,
                remedy="Move to a plan with a larger Knowledge Base query allowance.",
            )
    await _meter(session, org_id, METRIC_COMMONS_QUERIES)

    # See commons_overlap's identical filter: commons_visible() is what
    # guarantees the corpus can never contain another customer's trace, not
    # just a policy that happens to hold today.
    where = [
        *commons_visible(),
        Trace.commons_signature.isnot(None),
        Trace.org_id != org_id,
    ]
    if agent_type:
        reject_unstorable_text(agent_type, "agent_type")
        where.append(Trace.agent_type == agent_type)

    total_corpus = (
        await session.execute(select(func.count()).select_from(Trace).where(*where))
    ).scalar_one()

    rows = (
        await session.execute(
            select(Trace)
            .where(*where)
            .order_by(Trace.created_at.desc(), Trace.id.desc())
            .limit(commons.max_corpus_scan())
        )
    ).scalars().all()

    # Offloaded for the same reason commons_overlap offloads: a CPU-bound
    # scan on the event loop starves every other request this process is
    # serving, not just this one.
    ranked = await asyncio.to_thread(
        commons.rank_candidates, sig, [r.commons_signature or [] for r in rows], limit
    )

    now = datetime.now(timezone.utc)
    candidates = [
        {
            "rank": 0,
            "similarity": round(sim, 4),
            "commons_hits": rows[idx].commons_hits,
            "trace": _to_commons_wire(rows[idx], now),
        }
        for idx, sim in ranked
    ]
    # Disputed entries sort behind everything else, however similar. Within
    # each group similarity decides the order, and delivered value and trust
    # only break ties -- folding popularity into the score itself would let
    # a well-corroborated answer to a DIFFERENT question outrank the right
    # one, which is the failure mode a naive "rank by votes" blend has.
    #
    # Sorted to the back rather than filtered out, and that is the whole
    # difference between this tool and commons_overlap. Coverage is a claim,
    # so a contested entry is excluded from it. Lookup is "here is what
    # exists, judge it" -- and an entry the field disputes is still the most
    # relevant thing the corpus holds about your failure, plus the warning
    # that it did not work for the fleets who tried it. Dropping it would
    # answer "nothing found", which is false and strictly less useful.
    candidates.sort(
        key=lambda c: (
            not commons.counts_as_coverage(c["trace"]["standing"]),
            -c["similarity"],
            -c["commons_hits"],
            -(c["trace"].get("trust") or 0.0),
        )
    )
    for position, c in enumerate(candidates, start=1):
        c["rank"] = position

    return {
        "n_candidates": len(candidates),
        "n_disputed": sum(
            1 for c in candidates if not commons.counts_as_coverage(c["trace"]["standing"])
        ),
        "n_commons_traces": len(rows),
        "n_commons_traces_total": total_corpus,
        "corpus_truncated": total_corpus > len(rows),
        "candidates": candidates,
        "note": _SEARCH_NOTE,
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
    "the Knowledge Base does contain but your fleet words differently is counted "
    "as uncovered. Measured recall against known-present failures is roughly 1 in 9 "
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
            f"Compared against the {n_corpus:,} most recent of {total:,} Knowledge Base "
            f"entries (per-query scan limit). Real coverage is AT LEAST this figure -- "
            "treat it as a lower bound, and narrow with agent_type for a tighter answer. "
            + _FLOOR_CAVEAT
        )
    if n_corpus == 0:
        return (
            "The Knowledge Base has no entries yet, so this measures nothing. "
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
            f"The Knowledge Base holds only {n_corpus} entries so far; "
            "coverage will grow as the operator curates more. " + _FLOOR_CAVEAT
        )
    return _FLOOR_CAVEAT
