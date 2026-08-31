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
import hashlib
import math
import secrets
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, Float, and_, case, delete, distinct, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from commontrace import experiment
from hub import audit, commons, outcomes, plans
from hub import search as hub_search
from hub.abuse import (
    RateLimited,
    RateLimiter,
    TraceRejected,
    reject_embedded_nul,
    suspicion_reason,
    validate_size,
)
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
        # Whether this trace is in the CommonTrace Knowledge Base. For a
        # customer's own trace this is always False: only the operator-run
        # `hub/manage.py:commons_seed` ever sets it, and it sets it on rows
        # under the operator's own org_id, never a customer's. Kept on the
        # wire rather than dropped so that stays visibly, checkably true
        # rather than merely asserted -- a customer can see for themselves
        # that nothing of theirs is flagged.
        "shared_with_commons": trace.shared_with_commons,
        # Whether this trace is quarantined, and why. Surfaced (unlike the
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
        # so this is always False there.
        "quarantined": trace.quarantined,
        "quarantine_reason": trace.quarantine_reason,
    }


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
    """
    return [
        Trace.shared_with_commons.is_(True),
        Trace.commons_source == "seed",
        Trace.quarantined.is_(False),
        Trace.commons_retracted_at.is_(None),
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

    SELECT ... FOR UPDATE on the org's own row, same as before: count-then-
    insert is a TOCTOU race under concurrent callers for the SAME org
    without it, and a different org's row lock never blocks this one.
    """
    if plan.max_traces == plans.UNLIMITED:
        return
    await session.execute(
        select(Organization.id).where(Organization.id == org_id).with_for_update()
    )
    stored = int(await session.scalar(
        select(func.count()).select_from(Trace).where(Trace.org_id == org_id)
    ) or 0)
    if not plans.within(plan.max_traces, stored):
        raise plans.EntitlementExceeded(
            metric="traces", limit=plan.max_traces, used=stored, plan=plan.name,
            remedy="Purge traces you no longer need, or move to a plan with more storage.",
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
) -> dict:
    """Returns {"traces": [...], "limit", "offset", "has_more", "terms"}.

    Three deliberate properties, all visible to callers:

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

    3. **Query terms are OR-ed, not AND-ed** (`hub/search.py`). This is the
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
    limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
    offset = max(0, min(int(offset), MAX_SEARCH_OFFSET))
    if query:
        reject_embedded_nul(query, "query")
    for tag in tags or []:
        reject_embedded_nul(tag, "tag")

    stmt = select(Trace).where(Trace.org_id == org_id, Trace.quarantined.is_(False))
    chosen = hub_search.ChosenTerms((), (), ())
    if query:
        frequencies = (
            await session.execute(hub_search.term_frequency_stmt(org_id, query))
        ).all()
        chosen = hub_search.choose_terms([(row.lexeme, row.df) for row in frequencies])
        if chosen.used:
            tsquery = hub_search.tsquery_for(chosen.used)
            stmt = stmt.where(Trace.search_vector.op("@@")(tsquery))
            # id.desc() as a tiebreaker: created_at alone is not unique
            # enough under concurrent inserts (or two rows sharing a
            # timestamp) to make OFFSET/LIMIT paging deterministic --
            # without a total order, Postgres is free to break ties by
            # physical row order, which is not guaranteed stable across two
            # separate queries.
            stmt = stmt.order_by(
                hub_search.relevance(Trace.search_vector, tsquery).desc(),
                Trace.created_at.desc(),
                Trace.id.desc(),
            )
    else:
        stmt = stmt.order_by(Trace.created_at.desc(), Trace.id.desc())
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
            .where(Trace.id.in_([t.id for t in traces]))
            .values(retrievals=Trace.retrievals + 1)
        )
    return {
        "traces": await _hydrate(session, traces),
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

    # Trace.idempotency_key is String(128) -- checked here rather than left
    # to the INSERT below to enforce it: a too-long value raised
    # asyncpg.StringDataRightTruncation (a DataError), which is not an
    # IntegrityError and isn't caught by the IntegrityError handler further
    # down, so it reached the caller as an opaque HTTP 500 instead of a
    # clean rejection of a malformed request.
    if idempotency_key is not None and len(idempotency_key) > 128:
        raise TraceRejected(f"idempotency_key exceeds 128 chars ({len(idempotency_key)})")
    if idempotency_key is not None:
        reject_embedded_nul(idempotency_key, "idempotency_key")

    # Trace.agent_id is String(128). Checked here for the same reason
    # idempotency_key is above: left to the INSERT, an over-long value
    # raises asyncpg.StringDataRightTruncation (a DataError, not an
    # IntegrityError), which no handler below catches, so a malformed
    # request surfaces as an opaque HTTP 500 instead of a clean rejection.
    if len(agent_id) > 128:
        raise TraceRejected(f"agent_id exceeds 128 chars ({len(agent_id)})")

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
    if len(feedback_text) > MAX_FEEDBACK_TEXT_CHARS:
        raise ValueError(
            f"feedback_text exceeds {MAX_FEEDBACK_TEXT_CHARS} chars ({len(feedback_text)})"
        )
    reject_embedded_nul(feedback_text, "feedback_text")
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
    stmt = select(Trace).where(
        Trace.id == trace_id,
        or_(Trace.org_id == org_id, and_(*commons_visible())),
    )
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
    """
    seen: set[str] = {trace_id}
    frontier: set[str] = {trace_id}
    while frontier:
        rows = (
            await session.execute(
                select(Trace.id, Trace.supersedes_trace_id).where(
                    or_(Trace.id.in_(frontier), Trace.supersedes_trace_id.in_(frontier))
                )
            )
        ).all()
        next_frontier: set[str] = set()
        for tid, supersedes in rows:
            for candidate in (tid, supersedes):
                if candidate is not None and candidate not in seen:
                    seen.add(candidate)
                    next_frontier.add(candidate)
        frontier = next_frontier
    return seen


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
    await session.execute(
        delete(TraceRelation).where(
            TraceRelation.related_trace_id.in_(chain_ids),
        )
    )
    await session.execute(delete(Trace).where(Trace.id.in_(chain_ids), Trace.org_id == org_id))
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
    session: AsyncSession, org_id: str, token: str, actor: str = AUDIT_ACTOR_UNKNOWN
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

    trace_ids = (await session.execute(select(Trace.id).where(Trace.org_id == org_id))).scalars().all()
    if trace_ids:
        await session.execute(delete(TraceRelation).where(TraceRelation.related_trace_id.in_(trace_ids)))
    org_name = org.name
    await session.delete(org)
    await audit.record(
        session, actor=actor, action="confirm_org_deletion", org_id=org_id,
        target_type="org", target_id=org_id,
        summary=f"name={org_name!r} n_traces={len(trace_ids)} irreversible",
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
    actor: str = AUDIT_ACTOR_UNKNOWN,
) -> dict | None:
    """Creates a new Trace that supersedes `trace_id`, rather than mutating
    history in place -- consistent with Trace.supersedes_trace_id /
    Trace.depth being an amendment *chain*, not an overwrite."""
    if not _is_uuid(trace_id):
        return None
    stmt = select(Trace).where(Trace.id == trace_id, Trace.org_id == org_id)
    original = (await session.execute(stmt)).scalar_one_or_none()
    if original is None:
        return None

    # amend_trace is a WRITE path and carries caller-supplied content, so it
    # gets the same four guards contribute_trace does. Without them it was
    # the way around all of them: unlimited writes, unbounded storage growth
    # past plan.max_traces (amend_trace INSERTs a new row -- see this
    # function's docstring -- so it consumes a storage slot exactly like
    # contribute_trace and must be capped the same way), unvalidated
    # payloads (a title past the column width became a hard 500 rather than
    # a clean rejection), and spam that quarantine would have caught on the
    # way in.
    if not rate_limiter.allow(org_id):
        raise RateLimited(f"org {org_id} exceeded write rate limit")

    plan = await _plan_for(session, org_id)
    await _reserve_trace_slot(session, org_id, plan)

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
        quarantined=quarantined,
        quarantine_reason=quarantine_reason,
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
) -> dict:
    """For each trace eligible on this occasion, decide inject or withhold,
    record the decision, and return it.

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
    reject_embedded_nul(occasion_id, "occasion_id")

    org = await session.get(Organization, org_id)
    if org is None or org.holdout_rate <= 0 or not org.holdout_salt:
        raise ExperimentNotRunning(
            "No holdout experiment is running for this organization. An operator "
            "starts one with `python -m hub.manage start-experiment <org_id> [rate]`."
        )

    valid = list(
        (
            await session.execute(
                select(Trace.id).where(
                    Trace.org_id == org_id,
                    Trace.id.in_([t for t in trace_ids if _is_uuid(str(t))]),
                )
            )
        ).scalars().all()
    )

    decisions = []
    for trace_id in valid:
        withheld = experiment.is_held_out(
            trace_id, occasion_id, rate=org.holdout_rate, salt=org.holdout_salt
        )
        decisions.append({"trace_id": trace_id, "injected": not withheld})

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
        "inject": [d["trace_id"] for d in decisions if d["injected"]],
        "withhold": [d["trace_id"] for d in decisions if not d["injected"]],
        "note": (
            "Withheld traces must NOT be used on this occasion. Injecting one anyway "
            "moves it into the treated arm without the record saying so, which does "
            "not fail loudly -- it biases the measured effect toward zero. Report the "
            "result with record_occasion_outcome(occasion_id, succeeded)."
        ),
    }


async def holdout_for_results(
    session: AsyncSession,
    org_id: str,
    traces: list[dict],
    occasion_id: str,
    actor: str = AUDIT_ACTOR_UNKNOWN,
) -> dict:
    """Assign holdout arms for whatever a search just returned.

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
    """
    org = await session.get(Organization, org_id)
    if org is None or org.holdout_rate <= 0 or not org.holdout_salt:
        return {}
    ids = [t["id"] for t in traces if isinstance(t, dict) and t.get("id")]
    if not ids:
        return {}
    assignment = await holdout_assign(session, org_id, ids, occasion_id, actor=actor)
    return {
        "occasion_id": assignment["occasion_id"],
        "withhold": assignment["withhold"],
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
    reject_embedded_nul(occasion_id, "occasion_id")
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


async def causal_effects(session: AsyncSession, org_id: str, alpha: float = 0.05) -> dict:
    """Per-trace causal effect estimates from the running experiment.

    Analysis is `commontrace.experiment.analyze` unchanged: per-lesson
    two-proportion tests, Benjamini-Hochberg across the lessons that are
    adequately powered, a minimum detectable effect on the ones that are
    not, and an explicit UNDERPOWERED verdict so "cannot answer yet" never
    reads as "no effect".
    """
    org = await session.get(Organization, org_id)
    rows = (
        await session.execute(
            select(HoldoutObservation).where(
                HoldoutObservation.org_id == org_id,
                HoldoutObservation.succeeded.isnot(None),
                # Scoped to the CURRENT experiment. Observations from an
                # earlier salt were drawn from a different randomization
                # and pooling them would mix two experiments into one
                # comparison -- the exact failure Organization.holdout_salt
                # exists to make detectable.
                HoldoutObservation.salt == (org.holdout_salt if org else ""),
            )
        )
    ).scalars().all()

    observations = [
        experiment.HoldoutObservation(
            lesson_slug=r.trace_id, occasion_id=r.occasion_id, injected=r.injected,
            succeeded=bool(r.succeeded),
        )
        for r in rows
    ]
    effects = experiment.analyze(observations, alpha=alpha)

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
            }
            for e in effects
        ],
        "note": (
            "CAUSAL, unlike the before/after comparison alongside it: the two arms "
            "are the same fleet in the same window, differing only by whether the "
            "memory was injected. That is what makes this survive 'what else changed?'."
        ),
    }


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
        reject_embedded_nul(agent_type, "agent_type")
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

    if idempotency_key is not None and len(idempotency_key) > 128:
        raise TraceRejected(f"idempotency_key exceeds 128 chars ({len(idempotency_key)})")
    if len(rationale) > 500:
        raise TraceRejected(f"rationale exceeds 500 chars ({len(rationale)})")
    reject_embedded_nul(rationale, "rationale")
    if idempotency_key is not None:
        reject_embedded_nul(idempotency_key, "idempotency_key")

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
    if not rate_limiter.allow(f"kb_submit:{org_id}"):
        raise RateLimited(f"org {org_id} exceeded submit_kb_entry rate limit")

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
        .limit(max(1, min(int(limit), 200)))
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
    stmt = stmt.order_by(KnowledgeBaseSubmission.created_at.asc()).limit(max(1, min(int(limit), 1000)))
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
    reject_embedded_nul(reviewer, "reviewer")
    reject_embedded_nul(rejection_reason, "rejection_reason")
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

    awarded = plans.SUBMISSION_ACCEPTANCE_CREDIT if credit is None else max(0, int(credit))
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

    submission.status = "approved"
    submission.reviewed_at = now
    submission.reviewed_by = reviewer
    submission.resulting_trace_id = trace.id
    submission.credit_awarded = awarded

    org = await session.get(Organization, submission.org_id)
    if org is not None:
        org.bonus_commons_queries = org.bonus_commons_queries + awarded

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
    reject_embedded_nul(reason, "reason")
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
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 50

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
    return queue[:limit]


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

    threshold = float(threshold)
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
        reject_embedded_nul(agent_type, "agent_type")
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
    except (TypeError, ValueError):
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
        reject_embedded_nul(agent_type, "agent_type")
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
