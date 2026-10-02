"""Org-scoped data access layer for the six Hub MCP tools."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import math
import secrets
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean,
    Float,
    Select,
    and_,
    case,
    delete,
    distinct,
    func,
    literal,
    or_,
    select,
    union,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from commontrace import (
    decay,
    distill,
    experiment,
    harm,
    integrity,
    prereg,
    raw_export,
    revision,
    value,
)
from hub import audit, commons, commons_cache, outcomes, plans
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

AUDIT_ACTOR_UNKNOWN = "unknown"


logger = logging.getLogger("commontrace.hub.crud")


class IdempotencyKeyConflict(ValueError):
    ...


def _contribute_request_hash(
    title: str,
    context_text: str,
    solution_text: str,
    tags: list[str],
    agent_type: str,
    outcome: dict | None = None,
    profile: str = "",
) -> str:
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
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError, OverflowError):
        return default


async def _votes_by_trace(session: AsyncSession, trace_ids: list[str]) -> dict[str, list[dict]]:
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


BRIEF_PREVIEW_CHARS = 240


def _preview(text: str, limit: int = BRIEF_PREVIEW_CHARS) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _to_wire(trace: Trace, votes: list[dict], related: list[dict], *, brief: bool = False) -> dict:
    out = {
        "id": trace.id,
        "title": trace.title,
        "context_text": _preview(trace.context_text) if brief else trace.context_text,
        "solution_text": _preview(trace.solution_text) if brief else trace.solution_text,
        "tags": list(trace.tags or []),
        "agent_type": trace.agent_type,
        "created_at": _iso(trace.created_at),
        "trust": trace.trust,
        "quarantined": trace.quarantined,
    }
    optional = {
        "agent_id": trace.agent_id,
        "profile": trace.profile,
        "scopes": list(trace.scopes or []),
        "valid_from": _iso(trace.valid_from) if trace.valid_from else "",
        "valid_until": _iso(trace.valid_until) if trace.valid_until else "",
        "extensions": dict(trace.extensions or {}),
        "watch_condition": trace.watch_condition,
        "review_after": trace.review_after,
        "supersedes_trace_id": trace.supersedes_trace_id or "",
        "superseded_by_trace_id": trace.superseded_by_trace_id or "",
        "superseded_at": trace.superseded_at.isoformat() if trace.superseded_at else "",
        "contributor": trace.contributor,
        "retrievals": trace.retrievals,
        "depth": trace.depth,
        "votes": votes,
        "related": related,
        "outcome": dict(trace.outcome or {}),
        "shared_with_commons": trace.shared_with_commons,
        "quarantine_reason": trace.quarantine_reason,
    }
    if brief:
        out["brief"] = True
        out.update({k: v for k, v in optional.items() if v})
    else:
        out.update(optional)
    return out


def commons_visible() -> list:
    return [
        Trace.shared_with_commons.is_(True),
        Trace.commons_source == "seed",
        Trace.quarantined.is_(False),
        Trace.commons_retracted_at.is_(None),
        Trace.superseded_at.is_(None),
    ]


@dataclasses.dataclass
class _CommonsCorpus:
    total: int
    ids: list[str]
    signatures: object
    rows: dict[str, Trace] | None


async def _commons_corpus(session: AsyncSession, org_id: str, agent_type: str) -> _CommonsCorpus:
    cap = commons.max_corpus_scan()
    if commons_cache.available():
        snap = await commons_cache.snapshot(session, commons_visible())
        total, ids, signatures = commons_cache.select_for(snap, org_id, agent_type, cap)
        return _CommonsCorpus(total=total, ids=ids, signatures=signatures, rows=None)

    where = [
        *commons_visible(),
        Trace.commons_signature.isnot(None),
        Trace.org_id != org_id,
    ]
    if agent_type:
        where.append(Trace.agent_type == agent_type)
    total = (
        await session.execute(select(func.count()).select_from(Trace).where(*where))
    ).scalar_one()
    rows = (
        await session.execute(
            select(Trace)
            .where(*where)
            .order_by(Trace.created_at.desc(), Trace.id.desc())
            .limit(cap)
        )
    ).scalars().all()
    return _CommonsCorpus(
        total=total,
        ids=[r.id for r in rows],
        signatures=[r.commons_signature or [] for r in rows],
        rows={r.id: r for r in rows},
    )


async def _commons_rows(
    session: AsyncSession, corpus: _CommonsCorpus, ids: list[str]
) -> dict[str, Trace]:
    if corpus.rows is not None:
        return {i: corpus.rows[i] for i in ids if i in corpus.rows}
    if not ids:
        return {}
    got = (
        await session.execute(select(Trace).where(Trace.id.in_(set(ids)), *commons_visible()))
    ).scalars().all()
    return {r.id: r for r in got}


def standing_of(trace: Trace, now: datetime | None = None) -> str:
    return commons.entry_standing(
        trust=trace.trust,
        votes=trace.commons_votes,
        review_after=trace.commons_review_after,
        now=now,
    )


def _to_commons_wire(trace: Trace, now: datetime | None = None) -> dict:
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
    trace_ids = [t.id for t in traces]
    votes = await _votes_by_trace(session, trace_ids)
    related = await _related_by_trace(session, trace_ids)
    return [_to_wire(t, votes.get(t.id, []), related.get(t.id, []), brief=brief) for t in traces]


async def _hydrate_one(session: AsyncSession, trace: Trace) -> dict:
    return (await _hydrate(session, [trace]))[0]


METRIC_COMMONS_QUERIES = "commons_queries"

METRIC_SEARCHES = "searches"
METRIC_SEARCHES_EMPTY = "searches_empty"
METRIC_SEARCHES_NO_TERMS = "searches_no_terms"


def billing_period(now: datetime | None = None) -> str:
    """The current billing period as 'YYYY-MM', in UTC."""
    now = now or datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


async def _plan_for(session: AsyncSession, org_id: str) -> plans.Plan:
    org = await session.get(Organization, org_id)
    return plans.get(org.plan if org is not None else None)


async def _plan_and_bonus_for(session: AsyncSession, org_id: str) -> tuple[plans.Plan, int]:
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
    if delta == 0:
        return
    await session.execute(
        update(Organization)
        .where(Organization.id == org_id)
        .values(trace_count=func.greatest(0, Organization.trace_count + delta))
    )


def _active_agent_cutoff(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) - timedelta(days=plans.ACTIVE_AGENT_WINDOW_DAYS)


async def agents_under_management(session: AsyncSession, org_id: str) -> dict:
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
    """Everything an org is entitled to and has used this period."""
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
            "bonus_from_accepted_submissions": bonus,
        },
        "traces": {"used": traces, "limit": plan.max_traces},
        "agents": {**(await agents_under_management(session, org_id)), "limit": plan.max_agents},
    }


async def search_traces(
    session: AsyncSession,
    org_id: str,
    query: str = "",
    tags: list[str] | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    offset: int = 0,
    brief: bool = False,
    scope: str = "",
    as_of: str | datetime | None = None,
) -> dict:
    """Returns {"traces": [...], "limit", "offset", "has_more", "terms"}."""
    limit = _clamp_int(limit, 1, MAX_SEARCH_LIMIT, DEFAULT_SEARCH_LIMIT)
    offset = _clamp_int(offset, 0, MAX_SEARCH_OFFSET, 0)
    brief = bool(brief)
    if query:
        reject_unstorable_text(query, "query")
    if tags is not None and not isinstance(tags, list):
        raise ValueError(f"tags must be a list of strings, got {type(tags).__name__}")
    for tag in tags or []:
        reject_unstorable_text(tag, "tag")

    stmt = select(Trace).where(
        Trace.org_id == org_id, Trace.quarantined.is_(False), Trace.superseded_at.is_(None)
    )
    if scope:
        stmt = stmt.where(or_(Trace.scopes.contains([scope]), Trace.scopes == []))
    if as_of:
        as_of_dt: datetime | None = None
        if isinstance(as_of, str):
            try:
                as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            except ValueError:
                pass
        elif isinstance(as_of, datetime):
            as_of_dt = as_of
        if as_of_dt:
            stmt = stmt.where(or_(Trace.valid_from.is_(None), Trace.valid_from <= as_of_dt))
            stmt = stmt.where(or_(Trace.valid_until.is_(None), Trace.valid_until > as_of_dt))
    chosen = hub_search.ChosenTerms((), (), ())
    failed_outcome = case((Trace.outcome["resolved"].astext == "false", 1), else_=0)
    if query:
        frequencies = (
            await session.execute(hub_search.term_frequency_stmt(org_id, query))
        ).all()
        chosen = hub_search.choose_terms([(row.lexeme, row.df) for row in frequencies])
        if chosen.used:
            tsquery = hub_search.tsquery_for(chosen.used)
            stmt = stmt.where(Trace.search_vector.op("@@")(tsquery))
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

    harmful: dict[str, dict] = {}
    if query and not chosen.used:
        rows: list[Trace] = []
        has_more = False
    else:
        harmful = await _withdrawn_traces(session, org_id)
        page_stmt = stmt.where(Trace.id.notin_(list(harmful))) if harmful else stmt
        rows = list((await session.execute(page_stmt.offset(offset).limit(limit + 1))).scalars().all())
        has_more = len(rows) > limit
    traces = list(rows[:limit])
    withdrawn: list[dict] = []
    if harmful:
        traces, withdrawn = await _withdraw_from_page(
            session, org_id, stmt, traces, harmful, offset=offset, limit=limit,
        )

    if query:
        if offset == 0:
            await _record_search(session, org_id, terms=list(chosen.all_terms), results=len(traces))

    if traces:
        await session.execute(
            update(Trace)
            .where(Trace.org_id == org_id, Trace.id.in_([t.id for t in traces]))
            .values(retrievals=Trace.retrievals + 1)
        )
    result = {
        "traces": await _hydrate(session, traces, brief=brief),
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "terms": list(chosen.all_terms),
        "terms_ignored": list(chosen.ignored),
    }
    if withdrawn:
        result["withdrawn"] = withdrawn
        result["withdrawn_note"] = harm.note(len(withdrawn))
    await _attach_evidence(session, org_id, result)
    return result


async def _withdrawn_traces(session: AsyncSession, org_id: str) -> dict[str, dict]:
    org = await session.get(Organization, org_id)
    if org is None or org.harm_policy != harm.POLICY_WITHDRAW:
        return {}
    evidence = await causal_evidence(session, org_id)
    if not evidence["available"]:
        return {}
    return harm.hurts(evidence["by_trace"])


async def _withdraw_from_page(
    session: AsyncSession,
    org_id: str,
    unfiltered: Select,
    traces: list[Trace],
    harmful: dict[str, dict],
    *,
    offset: int,
    limit: int,
) -> tuple[list[Trace], list[dict]]:
    would_have_shown = (
        await session.execute(
            unfiltered.with_only_columns(Trace.id).offset(offset).limit(limit)
        )
    ).scalars().all()
    withdrawn = [
        {"id": trace_id, "reason": harm.REASON, "evidence": harmful[trace_id]}
        for trace_id in would_have_shown if trace_id in harmful
    ]

    if traces:
        originals = list((await session.execute(
            select(Trace).where(
                Trace.org_id == org_id, Trace.id.in_(list(harmful)),
                Trace.quarantined.is_(False), Trace.superseded_at.is_(None),
            )
        )).scalars().all())
        page = await _hydrate(session, traces)
        representative_of = _cluster_representatives(page + await _hydrate(session, originals))
        harmful_units = {representative_of.get(t.id, t.id): t.id for t in originals}
        kept: list[Trace] = []
        for trace in traces:
            unit = representative_of.get(trace.id, trace.id)
            if unit in harmful_units:
                withdrawn.append({
                    "id": trace.id,
                    "reason": "near_duplicate_of_withdrawn",
                    "duplicate_of": harmful_units[unit],
                    "evidence": harmful[harmful_units[unit]],
                })
            else:
                kept.append(trace)
        traces = kept

    titles = dict((await session.execute(
        select(Trace.id, Trace.title).where(
            Trace.org_id == org_id, Trace.id.in_([w["id"] for w in withdrawn]))
    )).all()) if withdrawn else {}
    for entry in withdrawn:
        entry["title"] = titles.get(entry["id"], "")
    return traces, withdrawn


async def _record_search(session: AsyncSession, org_id: str, *, terms: list[str], results: int) -> None:
    await _meter(session, org_id, METRIC_SEARCHES)
    if results:
        return
    await _meter(session, org_id, METRIC_SEARCHES_EMPTY if terms else METRIC_SEARCHES_NO_TERMS)


ACTIVITY_WEEKS = 12


async def console_activity(
    session: AsyncSession, org_id: str, *, weeks: int = ACTIVITY_WEEKS, now: datetime | None = None,
) -> dict:
    """Weekly series for the console's Overview charts, counts only."""
    now = now or datetime.now(timezone.utc)
    this_week = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    starts = [this_week - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    since = starts[0]

    def _key(value) -> str:
        return value.date().isoformat() if value is not None else ""

    week = func.date_trunc("week", Trace.created_at)
    trace_counts = {
        _key(w): int(n) for w, n in (await session.execute(
            select(week, func.count(Trace.id))
            .where(Trace.org_id == org_id, Trace.quarantined.is_(False), Trace.created_at >= since)
            .group_by(week)
        )).all()
    }

    org = await session.get(Organization, org_id)
    salt = org.holdout_salt if org is not None else ""
    per_occasion = (
        select(
            func.max(HoldoutObservation.resolved_at).label("at"),
            func.bool_or(HoldoutObservation.injected).label("treated"),
            func.bool_or(HoldoutObservation.succeeded).label("ok"),
        )
        .where(
            HoldoutObservation.org_id == org_id,
            HoldoutObservation.salt == salt,
            HoldoutObservation.succeeded.is_not(None),
            HoldoutObservation.resolved_at >= since,
        )
        .group_by(HoldoutObservation.occasion_id)
        .subquery()
    )
    occasion_week = func.date_trunc("week", per_occasion.c.at)
    arms: dict[tuple[str, bool], tuple[int, int]] = {}
    for w, treated, n, ok in (await session.execute(
        select(occasion_week, per_occasion.c.treated, func.count(),
               func.sum(case((per_occasion.c.ok.is_(True), 1), else_=0)))
        .group_by(occasion_week, per_occasion.c.treated)
    )).all():
        arms[(_key(w), bool(treated))] = (int(n), int(ok or 0))

    keys = [s.date().isoformat() for s in starts]
    return {
        "weeks": keys,
        "traces": [trace_counts.get(k, 0) for k in keys],
        "treated": [list(arms.get((k, True), (0, 0))) for k in keys],
        "control": [list(arms.get((k, False), (0, 0))) for k in keys],
        "experiment_running": bool(org and org.holdout_rate > 0),
    }


async def search_health(
    session: AsyncSession, org_id: str, period: str | None = None
) -> dict:
    """How often this org's searches come back with nothing, this month."""
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
    scopes: list[str] | None = None,
    valid_from: str | datetime | None = None,
    valid_until: str | datetime | None = None,
) -> dict:
    tags = tags or []
    scopes = scopes or []

    valid_from_dt: datetime | None = None
    if isinstance(valid_from, str) and valid_from:
        try:
            valid_from_dt = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
        except ValueError:
            pass
    elif isinstance(valid_from, datetime):
        valid_from_dt = valid_from

    valid_until_dt: datetime | None = None
    if isinstance(valid_until, str) and valid_until:
        try:
            valid_until_dt = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
        except ValueError:
            pass
    elif isinstance(valid_until, datetime):
        valid_until_dt = valid_until

    if idempotency_key is not None:
        reject_unstorable_text(idempotency_key, "idempotency_key")
    if idempotency_key is not None and len(idempotency_key) > 128:
        raise TraceRejected(f"idempotency_key exceeds 128 chars ({len(idempotency_key)})")

    reject_unstorable_text(agent_id, "agent_id")
    if len(agent_id) > 128:
        raise TraceRejected(f"agent_id exceeds 128 chars ({len(agent_id)})")

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

    plan = await _plan_for(session, org_id)
    await _reserve_trace_slot(session, org_id, plan)
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
    validate_trace(wire)
    validate_size(wire, config)

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
        scopes=scopes,
        valid_from=valid_from_dt,
        valid_until=valid_until_dt,
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
        await session.rollback()
        existing = (
            await session.execute(
                select(Trace).where(Trace.org_id == org_id, Trace.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return _idempotent_replay_or_conflict(
            existing, idempotency_key, title, context_text, solution_text, tags, agent_type, outcome,
            profile,
        )

    await _adjust_trace_count(session, org_id, +1)

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
    auto_proposed = await _maybe_auto_contribute(
        session, org_id, trace, config, rate_limiter,
        title=title, context_text=context_text, solution_text=solution_text,
        tags=tags, agent_type=agent_type, actor=actor,
    )
    return {
        "id": trace.id,
        "quarantined": trace.quarantined,
        "quarantine_reason": trace.quarantine_reason,
        "possible_duplicates": possible_duplicates,
        "auto_proposed_to_commons": auto_proposed,
    }


async def _maybe_auto_contribute(
    session: AsyncSession,
    org_id: str,
    trace: Trace,
    config: HubConfig,
    rate_limiter: RateLimiter,
    *,
    title: str,
    context_text: str,
    solution_text: str,
    tags: list[str],
    agent_type: str,
    actor: str,
) -> bool:
    if trace.quarantined:
        return False
    opted_in = await session.scalar(
        select(Organization.commons_auto_contribute).where(Organization.id == org_id)
    )
    if not opted_in:
        return False
    try:
        await submit_kb_entry(
            session, org_id, config, rate_limiter,
            title=title,
            context_text=context_text,
            solution_text=solution_text,
            tags=tags,
            agent_type=agent_type,
            rationale="auto-proposed: this organization opted in to contributing",
            actor=actor,
            idempotency_key=f"auto-contribute:{trace.id}",
        )
    except Exception:  # noqa: BLE001 - see point 2 above; never fail the capture
        logger.warning(
            "auto-contribute proposal failed for org=%s trace=%s", org_id, trace.id,
            exc_info=True,
        )
        return False
    return True


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
    return await _hydrate_one(session, existing)


MAX_CONTENT_SEARCH_LIMIT = 100
_CONTENT_SNIPPET_RADIUS = 60
_SQLSTATE_INVALID_REGEX = "2201B"


def _snippet_at(text: str, start: int, end: int) -> str:
    lo = max(0, start - _CONTENT_SNIPPET_RADIUS)
    hi = min(len(text), end + _CONTENT_SNIPPET_RADIUS)
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    return prefix + text[lo:hi] + suffix


async def search_trace_content(
    session: AsyncSession, org_id: str, pattern: str, *, regex: bool = False,
    limit: int = MAX_CONTENT_SEARCH_LIMIT,
) -> list[dict]:
    limit = _clamp_int(limit, 1, MAX_CONTENT_SEARCH_LIMIT, MAX_CONTENT_SEARCH_LIMIT)
    reject_unstorable_text(pattern, "pattern")
    if regex:
        title_hit = Trace.title.op("~*")(pattern)
        context_hit = Trace.context_text.op("~*")(pattern)
        solution_hit = Trace.solution_text.op("~*")(pattern)
    else:
        like = f"%{pattern}%"
        title_hit = Trace.title.ilike(like)
        context_hit = Trace.context_text.ilike(like)
        solution_hit = Trace.solution_text.ilike(like)
    stmt = (
        select(
            Trace.id, Trace.title, Trace.context_text, Trace.solution_text,
            Trace.created_at, Trace.quarantined,
            title_hit.label("title_hit"), context_hit.label("context_hit"),
            solution_hit.label("solution_hit"),
        )
        .where(Trace.org_id == org_id, or_(title_hit, context_hit, solution_hit))
        .order_by(Trace.created_at)
        .limit(limit)
    )
    try:
        rows = (await session.execute(stmt)).all()
    except DBAPIError as exc:
        if regex and getattr(exc.orig, "sqlstate", None) == _SQLSTATE_INVALID_REGEX:
            raise ValueError(f"pattern is not a valid regular expression: {exc.orig}") from None
        raise

    results = []
    for row in rows:
        if row.title_hit:
            field, text = "title", row.title
        elif row.context_hit:
            field, text = "context_text", row.context_text
        else:
            field, text = "solution_text", row.solution_text
        if regex:
            snippet = _snippet_at(text, 0, min(len(text), 2 * _CONTENT_SNIPPET_RADIUS))
        else:
            idx = text.lower().find(pattern.lower())
            start = idx if idx >= 0 else 0
            snippet = _snippet_at(text, start, start + len(pattern))
        results.append({
            "id": row.id,
            "title": row.title,
            "created_at": row.created_at.isoformat(),
            "quarantined": row.quarantined,
            "matched_field": field,
            "snippet": snippet,
        })
    return results


MAX_SUBJECT_IDS_PER_TRACE = 20
MAX_SUBJECT_ID_CHARS = 256


def _clean_subject_ids(subject_ids: list) -> list[str]:
    if not isinstance(subject_ids, (list, tuple)):
        raise ValueError("subject_ids must be a list")
    if len(subject_ids) > MAX_SUBJECT_IDS_PER_TRACE:
        raise ValueError(
            f"too many subject_ids ({len(subject_ids)}); the maximum is {MAX_SUBJECT_IDS_PER_TRACE}"
        )
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in subject_ids:
        value = str(raw).strip()
        if not value:
            continue
        if len(value) > MAX_SUBJECT_ID_CHARS:
            raise ValueError(f"subject_id exceeds {MAX_SUBJECT_ID_CHARS} chars")
        reject_unstorable_text(value, "subject_id")
        if value not in seen:
            seen.add(value)
            cleaned.append(value)
    return cleaned


async def tag_trace_subjects(
    session: AsyncSession, org_id: str, trace_id: str, subject_ids: list,
    actor: str = AUDIT_ACTOR_UNKNOWN,
) -> dict | None:
    if not _is_uuid(trace_id):
        return None
    trace = await session.get(Trace, trace_id)
    if trace is None or trace.org_id != org_id:
        return None
    cleaned = _clean_subject_ids(subject_ids)
    trace.subject_ids = cleaned
    await audit.record(
        session, actor=actor, action="tag_trace_subjects", org_id=org_id,
        target_type="trace", target_id=trace_id,
        summary=f"{len(cleaned)} subject id(s)",
    )
    return {"id": trace.id, "subject_ids": cleaned}


async def find_traces_by_subject(session: AsyncSession, org_id: str, subject_id: str) -> list[dict]:
    subject_id = str(subject_id or "").strip()
    if not subject_id:
        return []
    reject_unstorable_text(subject_id, "subject_id")
    stmt = (
        select(Trace.id, Trace.title, Trace.created_at, Trace.quarantined, Trace.subject_ids)
        .where(Trace.org_id == org_id, Trace.subject_ids.any(subject_id))
        .order_by(Trace.created_at)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "id": row.id, "title": row.title, "created_at": row.created_at.isoformat(),
            "quarantined": row.quarantined, "subject_ids": list(row.subject_ids),
        }
        for row in rows
    ]


async def purge_traces_by_subject(
    session: AsyncSession, org_id: str, subject_id: str, actor: str = AUDIT_ACTOR_UNKNOWN,
) -> dict:
    subject_id = str(subject_id or "").strip()
    if not subject_id:
        return {"purged": 0, "ids": []}
    matched = (
        await session.execute(
            select(Trace.id).where(Trace.org_id == org_id, Trace.subject_ids.any(subject_id))
        )
    ).scalars().all()
    if not matched:
        return {"purged": 0, "ids": []}
    expanded: set[str] = set()
    for trace_id in matched:
        expanded |= await amendment_chain(session, trace_id)
    own_ids = (
        await session.execute(
            select(Trace.id).where(Trace.id.in_(expanded), Trace.org_id == org_id)
        )
    ).scalars().all()
    for trace_id in matched:
        await delete_trace(session, org_id, trace_id, actor=actor)
    remaining = set(
        (await session.execute(select(Trace.id).where(Trace.id.in_(own_ids)))).scalars().all()
    )
    deleted_ids = [trace_id for trace_id in own_ids if trace_id not in remaining]
    if deleted_ids:
        await audit.record(
            session, actor=actor, action="purge_traces_by_subject", org_id=org_id,
            target_type="trace", target_id=deleted_ids[0],
            summary=f"purged {len(deleted_ids)} trace(s) for one subject id",
        )
    return {"purged": len(deleted_ids), "ids": deleted_ids}


def _is_uuid(value: str) -> bool:
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
        return None
    await session.execute(
        update(Trace)
        .where(Trace.id == trace_id, Trace.org_id == org_id)
        .values(retrievals=Trace.retrievals + 1)
    )
    return await _hydrate_one(session, trace)


def _established_voters_only(stmt):
    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=commons.COMMONS_VOTER_MIN_AGE_HOURS
    )
    return stmt.join(Organization, Organization.id == Vote.org_id).where(
        Organization.trace_count >= commons.COMMONS_VOTER_MIN_TRACES,
        Organization.created_at <= cutoff,
    )


async def _concerns_for(
    session: AsyncSession, trace_ids: list[str]
) -> dict[str, dict[str, int]]:
    if not trace_ids:
        return {}
    stmt = _established_voters_only(
        select(Vote.trace_id, Vote.feedback_tag, func.count()).where(
            Vote.trace_id.in_(trace_ids),
            Vote.feedback_tag != "",
        )
    )
    rows = (
        await session.execute(stmt.group_by(Vote.trace_id, Vote.feedback_tag))
    ).all()
    concerns: dict[str, dict[str, int]] = {}
    for trace_id, tag, count in rows:
        concerns.setdefault(trace_id, {})[tag] = int(count)
    return concerns


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
    if feedback_tag not in VALID_FEEDBACK_TAGS:
        raise ValueError(f"feedback_tag must be one of {VALID_FEEDBACK_TAGS!r}, got {feedback_tag!r}")
    reject_unstorable_text(feedback_text, "feedback_text")
    if len(feedback_text) > MAX_FEEDBACK_TEXT_CHARS:
        raise ValueError(
            f"feedback_text exceeds {MAX_FEEDBACK_TEXT_CHARS} chars ({len(feedback_text)})"
        )
    if not _is_uuid(trace_id):
        return None

    stmt = select(Trace).where(
        Trace.id == trace_id,
        or_(Trace.org_id == org_id, and_(*commons_visible())),
    ).with_for_update()
    trace = (await session.execute(stmt)).scalar_one_or_none()
    if trace is None:
        return None
    is_owner = trace.org_id == org_id

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

    tally_is_public = trace.commons_source == "seed"
    counted = select(Vote.vote_type, func.count()).where(Vote.trace_id == trace_id)
    if tally_is_public:
        counted = _established_voters_only(counted)
    counts = (await session.execute(counted.group_by(Vote.vote_type))).all()
    tally = dict(counts)
    up_count, down_count = tally.get("up", 0), tally.get("down", 0)
    total = up_count + down_count
    new_trust = (up_count / total) if total else 0.5

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
    vote_counted = None
    if tally_is_public:
        voter = await session.get(Organization, org_id)
        vote_counted = commons.vote_counts_toward_standing(
            trace_count=(voter.trace_count if voter else 0),
            org_created_at=(voter.created_at if voter else None),
        )
    if is_owner:
        wire = await _hydrate_one(session, trace)
        if vote_counted is not None:
            wire["vote_counted"] = vote_counted
        return wire
    wire = _to_commons_wire(trace)
    wire["vote_counted"] = vote_counted
    return wire


async def amendment_chain(session: AsyncSession, trace_id: str) -> set[str]:
    seed = literal(trace_id, type_=Trace.id.type)
    ancestors = select(seed.label("id")).cte(name="ancestors", recursive=True)
    ancestors = ancestors.union(
        select(Trace.supersedes_trace_id.label("id"))
        .join(ancestors, Trace.id == ancestors.c.id)
        .where(Trace.supersedes_trace_id.isnot(None))
    )
    descendants = select(ancestors.c.id.label("id")).cte(name="descendants", recursive=True)
    descendants = descendants.union(
        select(Trace.id.label("id")).join(descendants, Trace.supersedes_trace_id == descendants.c.id)
    )
    rows = (
        await session.execute(union(select(ancestors.c.id), select(descendants.c.id)))
    ).scalars().all()
    return set(rows)


async def delete_trace(session: AsyncSession, org_id: str, trace_id: str, actor: str = AUDIT_ACTOR_UNKNOWN) -> bool:
    if not _is_uuid(trace_id):
        return False
    trace = await session.get(Trace, trace_id)
    if trace is None or trace.org_id != org_id:
        return False

    chain_ids = await amendment_chain(session, trace_id)
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
    await _adjust_trace_count(session, org_id, -len(own_chain_ids))
    await audit.record(
        session, actor=actor, action="delete_trace", org_id=org_id,
        target_type="trace", target_id=trace_id,
        summary=f"irreversible n_amendment_chain={len(chain_ids)}",
    )
    return True


DELETION_GRACE_SECONDS = 300
DELETION_TOKEN_TTL_HOURS = 24


class DeletionNotReady(ValueError):
    ...


class SubscriptionCancellationFailed(RuntimeError):
    ...


def _hash_deletion_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


async def request_org_deletion(session: AsyncSession, org_id: str, actor: str = AUDIT_ACTOR_UNKNOWN) -> dict:
    """Start the two-step self-service account deletion. Deletes nothing."""
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
    from hub import auth

    await auth.announce_auth_change(session)
    await audit.record(
        session, actor=actor, action="confirm_org_deletion", org_id=org_id,
        target_type="org", target_id=org_id,
        summary=f"name={org_name!r} n_traces={len(trace_ids)} "
                f"stripe_subscription_cancelled={had_subscription} irreversible",
    )
    return True


def _carry_commons_forward(
    original: Trace, title: str, context_text: str, tags: list[str]
) -> dict:
    if original.commons_source != "seed":
        return {}
    return {
        "shared_with_commons": original.shared_with_commons,
        "shared_at": original.shared_at,
        "shared_rationale": original.shared_rationale,
        "commons_source": original.commons_source,
        "commons_signature": commons.signature_for(title, context_text, tags),
        "commons_hits": original.commons_hits,
        "commons_review_after": original.commons_review_after,
        "commons_retracted_at": original.commons_retracted_at,
    }


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
    if not _is_uuid(trace_id):
        return None

    if idempotency_key is not None:
        reject_unstorable_text(idempotency_key, "idempotency_key")
    if idempotency_key is not None and len(idempotency_key) > 128:
        raise TraceRejected(f"idempotency_key exceeds 128 chars ({len(idempotency_key)})")
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

    allowed, retry_after = await rate_limiter.check(org_id)
    if not allowed:
        raise RateLimited(f"org {org_id} exceeded write rate limit", retry_after=retry_after)

    plan = await _plan_for(session, org_id)
    await _reserve_trace_slot(session, org_id, plan)

    resolved_title = title if title is not None else original.title
    resolved_context = context_text if context_text is not None else original.context_text
    resolved_solution = solution_text if solution_text is not None else original.solution_text
    resolved_tags = tags if tags is not None else list(original.tags or [])
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
        agent_id=original.agent_id,
        profile=original.profile,
        extensions=dict(original.extensions or {}),
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
        # Carry forward routing and temporal validity from the original trace
        scopes=list(original.scopes or []),
        valid_from=original.valid_from,
        valid_until=original.valid_until,
        **_carry_commons_forward(original, resolved_title, resolved_context, resolved_tags),
    )
    original.superseded_at = datetime.now(timezone.utc)
    original.superseded_by_trace_id = amended_id

    session.add(amended)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = (
            await session.execute(
                select(Trace).where(Trace.org_id == org_id, Trace.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return await _amend_idempotent_replay_or_conflict(
            session, existing, idempotency_key, trace_id, title, context_text, solution_text, tags, outcome
        )

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
    stmt = (
        select(func.unnest(Trace.tags))
        .distinct()
        .where(Trace.org_id == org_id, Trace.quarantined.is_(False))
    )
    tags = (await session.execute(stmt)).scalars().all()
    return sorted(tags)


class ExperimentNotRunning(ValueError):
    """holdout_assign was called for an org with no experiment configured."""


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
    """Assign holdout arms for whatever a search just returned."""
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
    """Close the loop: how did this occasion go?"""
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
            HoldoutObservation.succeeded.is_(None),
        )
        .values(succeeded=succeeded, resolved_at=datetime.now(timezone.utc))
    )
    await session.flush()
    return {"occasion_id": occasion_id, "observations_resolved": result.rowcount or 0}


def _integrity_wire(report: integrity.IntegrityReport) -> dict:
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


_EVIDENCE_TTL_SECONDS = 300.0
_EVIDENCE_CACHE_MAX_ORGS = 1024
_evidence_cache: dict[str, tuple[tuple, float, dict]] = {}


async def _evidence_key(session: AsyncSession, org_id: str) -> tuple:
    org = await session.get(Organization, org_id)
    salt = org.holdout_salt if org else ""
    observed, resolved, last_resolved = (
        await session.execute(
            select(
                func.count(),
                func.count(HoldoutObservation.succeeded),
                func.max(HoldoutObservation.resolved_at),
            ).where(HoldoutObservation.org_id == org_id, HoldoutObservation.salt == salt)
        )
    ).one()
    prereg = json.dumps(org.holdout_prereg, sort_keys=True, default=str) if org and org.holdout_prereg else ""
    return (salt, org.holdout_rate if org else 0.0, prereg, observed > 0, resolved, last_resolved)


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


async def causal_evidence(session: AsyncSession, org_id: str) -> dict:
    """Per-trace causal evidence for this org, cached as described above."""
    key = await _evidence_key(session, org_id)
    cached = _evidence_cache.get(org_id)
    now = time.monotonic()
    if cached is not None and cached[0] == key and now - cached[1] < _EVIDENCE_TTL_SECONDS:
        return cached[2]

    measured_at = datetime.now(timezone.utc).isoformat()
    if not key[3]:
        evidence = {"available": False, "reason": "no experiment data", "measured_at": measured_at, "by_trace": {}}
    else:
        causal = await causal_effects(session, org_id)
        if not (causal.get("integrity") or {}).get("effects_readable", True):
            evidence = {
                "available": False,
                "reason": (
                    "The experiment is COMPROMISED, so no effects are shown. Read "
                    "`fleet_outcomes.causal.integrity` for what to fix."
                ),
                "measured_at": measured_at,
                "by_trace": {},
            }
        else:
            evidence = {
                "available": True,
                "reason": "",
                "measured_at": measured_at,
                "by_trace": {
                    e["trace_id"]: {
                        "verdict": e["verdict"],
                        "effect": _round(e.get("effect")),
                        "ci_95": [_round(v) for v in (e.get("ci_95") or [])],
                        "n_injected": e.get("n_injected"),
                        "n_withheld": e.get("n_withheld"),
                        "last_measured_at": e.get("last_measured_at") or "",
                    }
                    for e in causal.get("effects", [])
                },
            }

    if org_id not in _evidence_cache and len(_evidence_cache) >= _EVIDENCE_CACHE_MAX_ORGS:
        _evidence_cache.pop(min(_evidence_cache, key=lambda k: _evidence_cache[k][1]))
    _evidence_cache[org_id] = (key, now, evidence)
    return evidence


async def _attach_evidence(session: AsyncSession, org_id: str, result: dict) -> None:
    evidence = await causal_evidence(session, org_id)
    if not evidence["available"] and evidence["reason"] == "no experiment data":
        return
    result["evidence"] = {
        "available": evidence["available"],
        "reason": evidence["reason"],
        "measured_at": evidence["measured_at"],
    }
    if not evidence["available"]:
        return
    for trace in result.get("traces", []):
        trace["evidence"] = evidence["by_trace"].get(trace.get("id"), {"verdict": "NOT_MEASURED"})


async def holdout_assignments(session: AsyncSession, org_id: str) -> list:
    org = await session.get(Organization, org_id)
    rows = (
        await session.execute(
            select(
                HoldoutObservation.trace_id,
                HoldoutObservation.occasion_id,
                HoldoutObservation.injected,
                HoldoutObservation.succeeded,
                HoldoutObservation.salt,
                HoldoutObservation.created_at,
                HoldoutObservation.resolved_at,
                HoldoutObservation.trace_revision,
            ).where(
                HoldoutObservation.org_id == org_id,
                HoldoutObservation.salt == (org.holdout_salt if org else ""),
            )
        )
    ).all()

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
    return assignments


async def causal_effects(session: AsyncSession, org_id: str, alpha: float = 0.05) -> dict:
    """Per-trace causal effect estimates from the running experiment."""
    assignments = await holdout_assignments(session, org_id)
    org = await session.get(Organization, org_id)
    report = integrity.audit(assignments, unit=integrity.UNIT_TRACE)
    _overlap = value.overlap_from_assignments(assignments)
    _policy = value.policy_effect(assignments)
    _evidence_digest = raw_export.digest_of(assignments)
    _registered = None
    if org is not None and org.holdout_prereg:
        try:
            _registered = prereg.Preregistration.from_dict(org.holdout_prereg)
        except prereg.PreregError:
            _registered = None
    _first_observation = min(
        (a.at for a in assignments if a.at is not None), default=None
    )
    _prereg_check = prereg.check(
        _registered,
        actual_salt=(org.holdout_salt if org else ""),
        actual_holdout_rate=(org.holdout_rate if org else None),
        actual_detectable=experiment.DEFAULT_PRACTICAL_EFFECT,
        actual_occasions=len({a.occasion_id for a in assignments}),
        first_observation_at=_first_observation,
    )

    unique, _ = integrity.normalize(assignments)
    observations = [
        experiment.HoldoutObservation(
            lesson_slug=r.lesson, occasion_id=r.occasion_id, injected=r.injected,
            succeeded=bool(r.succeeded),
        )
        for r in unique if r.succeeded is not None
    ]
    effects = experiment.analyze(observations, alpha=alpha, sequential=True)

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
        "integrity": _integrity_wire(report),
        "preregistration": {
            "registered": _prereg_check.registered,
            "clean": _prereg_check.clean,
            "fingerprint": _prereg_check.fingerprint,
            "note": _prereg_check.note,
            "deviations": [
                {"field": d.field, "promised": d.promised, "actual": d.actual,
                 "detail": d.detail}
                for d in _prereg_check.deviations
            ],
            "registered_design": (org.holdout_prereg or None) if org else None,
        },
        "evidence_digest": _evidence_digest,
        "co_injection": {
            "pairs": [sorted(pair) for pair in sorted(
                _overlap.shared_pairs, key=lambda p: sorted(p)
            )],
            "unique_injected_occasions": _overlap.unique_injected_occasions,
        },
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
    evidence_horizon_days: int | None = decay.DEFAULT_HORIZON_DAYS,
) -> dict:
    """What this fleet's memory was worth, causally, in its own units."""
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
    last_measured = {
        e["trace_id"]: e.get("last_measured_at") or ""
        for e in causal.get("effects", [])
    }
    report = value.compute(
        effects, audit, value_per_occasion=value_per_occasion, rate_card=card,
        overlap=overlap,
        last_measured=last_measured,
        evidence_horizon_days=evidence_horizon_days,
    )
    report = dataclasses.replace(report, policy=policy)
    ledger = report.ledger()
    titles = {e["trace_id"]: e.get("title") for e in causal.get("effects", [])}

    issued_at = datetime.now(timezone.utc).isoformat()
    evidence_digest = str(causal.get("evidence_digest") or "")
    prereg_fingerprint = str(
        ((causal.get("preregistration") or {}).get("fingerprint")) or ""
    )
    if signing_key:
        signature = value.sign_ledger(
            ledger, signing_key.encode("utf-8"), org_id=org_id, issued_at=issued_at,
            evidence_digest=evidence_digest, prereg_fingerprint=prereg_fingerprint,
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
        "aggregate_readable": report.aggregate_readable,
        "aggregate_reason": report.aggregate_reason,
        "evidence": (
            {
                "horizon_days": report.decay.horizon_days,
                "n_stale": len(report.decay.stale),
                "n_withheld": len(report.decay.withheld),
                "due_for_remeasurement": list(report.decay.due_for_remeasurement),
            }
            if report.decay is not None else None
        ),
        "unique_occasions": report.unique_occasions,
        "occasions_improved_unselected": report.occasions_improved_unselected,
        "n_examined": report.n_examined,
        "policy_effect": causal.get("policy_effect"),
        "evidence_digest": evidence_digest,
        "preregistration": causal.get("preregistration"),
        "value_per_occasion": value_per_occasion,
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


DEFAULT_WORKING_SET_CHARS = 2000
MAX_WORKING_SET_CHARS = 8000

DEFAULT_EVIDENCE_HORIZON_DAYS = decay.DEFAULT_HORIZON_DAYS
MAX_EVIDENCE_HORIZON_DAYS = 36500
_WORKING_SET_ENTRY_CHARS = 320


def _evidence_age_days(last_measured_at: str, now: datetime) -> float | None:
    if not last_measured_at:
        return None
    return max(0.0, (now - datetime.fromisoformat(last_measured_at)).total_seconds() / 86400.0)


def _working_set_entry(title: str, solution: str, trace_id: str, effect: float, n: int) -> str:
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
    """The fleet's proven memory, small enough to pin to a system prompt."""
    budget_chars = _clamp_int(
        budget_chars, 1, MAX_WORKING_SET_CHARS, DEFAULT_WORKING_SET_CHARS
    )
    evidence_horizon_days = _clamp_int(
        evidence_horizon_days, 1, MAX_EVIDENCE_HORIZON_DAYS, DEFAULT_EVIDENCE_HORIZON_DAYS
    )
    causal = await causal_effects(session, org_id)
    audit = causal.get("integrity") or {}

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
            break
        lines.append(line)
        used += len(line) + 1
        entries.append({
            "trace_id": e["trace_id"], "title": e.get("title"),
            "effect": e.get("effect"), "n_injected": e.get("n_injected"),
            "occasions_improved": round((e.get("effect") or 0.0) * (e.get("n_injected") or 0), 2),
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


async def fleet_outcomes(
    session: AsyncSession, org_id: str, agent_type: str = "", alpha: float = outcomes.DEFAULT_ALPHA
) -> dict:
    """Has this fleet's agent performance changed since its baseline window?"""
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


MAX_PENDING_SUBMISSIONS_PER_ORG = 20
VALID_SUBMISSION_DECISIONS = ("approve", "reject")


async def _reserve_kb_submission_slot(session: AsyncSession, org_id: str) -> None:
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
    tags = tags or []

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
    validate_trace(wire)
    validate_size(wire, config)

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
    await _adjust_trace_count(session, operator_org_id, +1)

    submission.status = "approved"
    submission.reviewed_at = now
    submission.reviewed_by = reviewer
    submission.resulting_trace_id = trace.id
    submission.credit_awarded = awarded

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


async def retract_kb_entry(
    session: AsyncSession,
    trace_id: str,
    reason: str = "",
    actor: str = audit.ACTOR_OPERATOR_CLI,
) -> dict | None:
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
    wire = _to_commons_wire(trace)
    wire["standing"] = "retracted"
    wire["retracted_at"] = _iso(trace.commons_retracted_at)
    wire["retraction_reason"] = trace.commons_retraction_reason
    return wire


async def restore_kb_entry(
    session: AsyncSession, trace_id: str, actor: str = audit.ACTOR_OPERATOR_CLI
) -> dict | None:
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


URGENT_FEEDBACK_TAGS = ("security_concern",)


async def kb_review_queue(session: AsyncSession, limit: int = 50) -> list[dict]:
    """Which Knowledge Base entries need a human, worst first."""
    limit = _clamp_int(limit, 1, 500, 50)
    queue = await _kb_review_queue_full(session)
    return queue[:limit]


async def count_kb_review_queue(session: AsyncSession) -> int:
    return len(await _kb_review_queue_full(session))


async def kb_review_queue_and_total(session: AsyncSession, limit: int = 50) -> tuple[list[dict], int]:
    limit = _clamp_int(limit, 1, 500, 50)
    queue = await _kb_review_queue_full(session)
    return queue[:limit], len(queue)


async def _kb_review_queue_full(session: AsyncSession) -> list[dict]:
    rows = (
        await session.execute(
            select(Trace).where(*commons_visible()).order_by(Trace.commons_hits.desc())
        )
    ).scalars().all()
    if not rows:
        return []

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

    queue.sort(key=lambda item: order[item["bucket"]])
    return queue


async def commons_overlap(
    session: AsyncSession,
    org_id: str,
    failures: object,
    threshold: float = commons.DEFAULT_COMMONS_THRESHOLD,
    include_matches: bool = True,
    agent_type: str = "",
) -> dict:
    submitted = commons.validate_submitted_failures(failures)

    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        raise commons.CommonsInputError("threshold must be a number") from None
    if not math.isfinite(threshold):
        raise commons.CommonsInputError("threshold must be a finite number")
    threshold = max(0.0, min(threshold, 1.0))

    if submitted:
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

    if agent_type:
        reject_unstorable_text(agent_type, "agent_type")

    corpus = await _commons_corpus(session, org_id, agent_type)
    total_corpus = corpus.total
    corpus_truncated = total_corpus > len(corpus.ids)

    best = await asyncio.to_thread(commons.best_matches, submitted, corpus.signatures)

    matched_rows = await _commons_rows(
        session, corpus, [corpus.ids[idx] for idx, sim in best if idx >= 0 and sim >= threshold]
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
        hit = matched_rows.get(corpus.ids[idx])
        if hit is None:
            continue
        hit_ids.append(hit.id)
        entry = {
            "failure_label": label,
            "similarity": round(sim, 4),
            "agent_type": hit.agent_type,
            "tags": list(hit.tags or []),
            "trace": _to_commons_wire(hit, now),
        }
        if not commons.counts_as_coverage(entry["trace"]["standing"]):
            if include_matches:
                disputed_matches.append(entry)
            continue
        n_covered += 1
        key = hit.agent_type or "(unspecified)"
        by_domain[key] = by_domain.get(key, 0) + 1
        if include_matches:
            matches.append(entry)

    hitter = await session.get(Organization, org_id) if hit_ids else None
    hits_count = bool(hit_ids) and commons.hit_counts_toward_quality_signal(
        trace_count=(hitter.trace_count if hitter else 0),
        org_created_at=(hitter.created_at if hitter else None),
    )

    if hit_ids and hits_count:
        hit_counts = Counter(hit_ids)
        hit_counts = {
            tid: min(cnt, commons.MAX_HITS_PER_TRACE_PER_QUERY) for tid, cnt in hit_counts.items()
        }
        increment = case(*((Trace.id == tid, cnt) for tid, cnt in hit_counts.items()), else_=0)
        await session.execute(
            update(Trace).where(Trace.id.in_(hit_counts)).values(commons_hits=Trace.commons_hits + increment)
        )

    matches.sort(key=lambda m: m["similarity"], reverse=True)
    disputed_matches.sort(key=lambda m: m["similarity"], reverse=True)
    n_failures = len(submitted)
    return {
        "n_failures": n_failures,
        "n_commons_traces": len(corpus.ids),
        "n_commons_traces_total": total_corpus,
        "corpus_truncated": corpus_truncated,
        "n_covered": n_covered,
        "covered_fraction": (n_covered / n_failures) if n_failures else 0.0,
        "n_disputed": len(disputed_matches),
        "threshold": threshold,
        "by_agent_type": dict(sorted(by_domain.items(), key=lambda kv: -kv[1])),
        "matches": matches,
        "disputed_matches": disputed_matches,
        "note": _commons_note(n_failures, len(corpus.ids), corpus_truncated, total_corpus),
    }


_SEARCH_NOTE = (
    "Ranked CANDIDATES, not coverage. Every result is a suggestion to judge, "
    "the way a search engine's results are: on the held-out evaluation a "
    "failure the commons does NOT contain still returns a non-empty list "
    "100% of the time, and the score distributions of true and absent "
    "matches overlap. Use `commons_overlap` -- thresholded, 0% false "
    "positives -- for any figure you intend to quote. See "
    "The evaluation. Read each candidate's `standing` before "
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
    sig = commons.validate_query_signature(query_signature)

    try:
        limit = int(limit)
    except (TypeError, ValueError, OverflowError):
        raise commons.CommonsInputError("limit must be an integer") from None
    limit = max(1, min(limit, commons.MAX_SEARCH_CANDIDATES))

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

    if agent_type:
        reject_unstorable_text(agent_type, "agent_type")

    corpus = await _commons_corpus(session, org_id, agent_type)
    total_corpus = corpus.total

    ranked = await asyncio.to_thread(commons.rank_candidates, sig, corpus.signatures, limit)
    ranked_rows = await _commons_rows(session, corpus, [corpus.ids[idx] for idx, _sim in ranked])

    now = datetime.now(timezone.utc)
    candidates = [
        {
            "rank": 0,
            "similarity": round(sim, 4),
            "commons_hits": ranked_rows[corpus.ids[idx]].commons_hits,
            "trace": _to_commons_wire(ranked_rows[corpus.ids[idx]], now),
        }
        for idx, sim in ranked
        if corpus.ids[idx] in ranked_rows
    ]
    candidates.sort(
        key=lambda c: (
            not commons.counts_as_coverage(c["trace"]["standing"]),
            -c["similarity"],
            -(c["trace"].get("trust") or 0.0),
            (c["trace"].get("created_at") or "", c["trace"]["id"]),
        )
    )
    for position, c in enumerate(candidates, start=1):
        c["rank"] = position

    return {
        "n_candidates": len(candidates),
        "n_disputed": sum(
            1 for c in candidates if not commons.counts_as_coverage(c["trace"]["standing"])
        ),
        "n_commons_traces": len(corpus.ids),
        "n_commons_traces_total": total_corpus,
        "corpus_truncated": total_corpus > len(corpus.ids),
        "candidates": candidates,
        "note": _SEARCH_NOTE,
    }


BROWSE_COMMONS_LIMIT = 25
MAX_BROWSE_COMMONS_LIMIT = 100


MAX_EXPORT_COMMONS = 5000


async def export_commons(
    session: AsyncSession,
    org_id: str,
    config: HubConfig,
    *,
    limit: int = MAX_EXPORT_COMMONS,
) -> dict:
    """The whole curated Knowledge Base corpus, for matching on the client."""
    if not config.commons_export_enabled:
        raise plans.EntitlementExceeded(
            metric="commons_export", limit=0, used=0, plan="",
            remedy="This deployment does not publish its Knowledge Base corpus in "
                   "bulk. Consult it per failure with commons_search, or ask the "
                   "operator to set HUB_COMMONS_EXPORT_ENABLED=true.",
        )

    plan, _bonus = await _plan_and_bonus_for(session, org_id)
    if not plan.commons_access:
        raise plans.EntitlementExceeded(
            metric="commons_access", limit=0, used=0, plan=plan.name,
            remedy="The Knowledge Base is not included in this plan.",
        )

    limit = _clamp_int(limit, 1, MAX_EXPORT_COMMONS, MAX_EXPORT_COMMONS)
    rows = (
        await session.execute(
            select(Trace)
            .where(*commons_visible())
            .order_by(Trace.created_at.asc())
            .limit(limit)
        )
    ).scalars().all()

    now = datetime.now(timezone.utc)
    return {
        "entries": [
            {
                "title": t.title,
                "context_text": t.context_text,
                "solution_text": t.solution_text,
                "tags": list(t.tags or []),
                "agent_type": t.agent_type,
                "standing": commons.entry_standing(
                    trust=t.trust or 0.0,
                    votes=t.commons_votes or 0,
                    review_after=t.commons_review_after,
                    now=now,
                ),
                "trust": t.trust or 0.0,
                "votes": t.commons_votes or 0,
            }
            for t in rows
        ],
        "n_entries": len(rows),
        "truncated": len(rows) >= limit,
    }


async def browse_commons(
    session: AsyncSession,
    org_id: str,
    *,
    tag: str = "",
    limit: int = BROWSE_COMMONS_LIMIT,
    offset: int = 0,
) -> dict:
    limit = _clamp_int(limit, 1, MAX_BROWSE_COMMONS_LIMIT, BROWSE_COMMONS_LIMIT)
    offset = _clamp_int(offset, 0, 100_000, 0)

    plan, _bonus = await _plan_and_bonus_for(session, org_id)
    if not plan.commons_access:
        raise plans.EntitlementExceeded(
            metric="commons_access", limit=0, used=0, plan=plan.name,
            remedy="The Knowledge Base is not included in this plan.",
        )

    conditions = list(commons_visible())
    tag = (tag or "").strip()
    if tag:
        conditions.append(Trace.tags.any(tag))

    total = await session.scalar(
        select(func.count()).select_from(Trace).where(*conditions)
    )
    is_disputed = case(
        (
            and_(
                Trace.commons_votes >= commons.MIN_VOTES_FOR_STANDING,
                Trace.trust < commons.DISPUTED_TRUST_CEILING,
            ),
            1,
        ),
        else_=0,
    )
    rows = (
        await session.execute(
            select(Trace)
            .where(*conditions)
            .order_by(
                is_disputed.asc(),
                Trace.trust.desc(),
                Trace.created_at.desc(),
                Trace.id.desc(),
            )
            .limit(limit + 1)
            .offset(offset)
        )
    ).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    my_votes: dict[str, str] = {}
    if rows:
        vote_rows = (
            await session.execute(
                select(Vote.trace_id, Vote.vote_type).where(
                    Vote.org_id == org_id,
                    Vote.trace_id.in_([trace.id for trace in rows]),
                )
            )
        ).all()
        my_votes = {trace_id: vote_type for trace_id, vote_type in vote_rows}

    concerns = await _concerns_for(session, [trace.id for trace in rows])

    now = datetime.now(timezone.utc)
    entries = []
    for trace in rows:
        standing = commons.entry_standing(
            trust=trace.trust or 0.0,
            votes=trace.commons_votes or 0,
            review_after=trace.commons_review_after,
            now=now,
        )
        entries.append({
            "id": trace.id,
            "title": trace.title,
            "context_preview": _preview(trace.context_text),
            "solution_preview": _preview(trace.solution_text),
            "tags": list(trace.tags or []),
            "agent_type": trace.agent_type,
            "standing": standing,
            "trust": trace.trust or 0.0,
            "votes": trace.commons_votes or 0,
            "hits": trace.commons_hits or 0,
            "my_vote": my_votes.get(trace.id, ""),
            "concerns": concerns.get(trace.id, {}),
            "revisions": trace.depth or 0,
            "created_at": _iso(trace.created_at),
        })
    entries.sort(
        key=lambda e: (not commons.counts_as_coverage(e["standing"]), -e["trust"])
    )

    return {
        "entries": entries,
        "total": total or 0,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
    }


_FLOOR_CAVEAT = (
    "This figure is a FLOOR, not an estimate: matching is lexical, so a failure "
    "the Knowledge Base does contain but your fleet words differently is counted "
    "as uncovered. Measured recall against known-present failures is roughly 1 in 9 "
    "Matches are reliable; misses are not evidence of absence."
)


def _commons_note(
    n_failures: int, n_corpus: int, truncated: bool = False, total: int = 0
) -> str:
    if truncated:
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
