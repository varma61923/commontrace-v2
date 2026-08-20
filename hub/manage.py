"""Operator CLI for org/API-key management and Hub monitoring:
`python -m hub.manage <command>`.

    create-org <name>               -> prints the new org's id
    issue-key <org_id> [days]       -> prints the raw key ONCE (see warning below);
                                        optional expiry in days (default: never expires)
    rotate-key <key_id>             -> revokes <key_id>, issues + prints a new raw key for the same org
    revoke-key <key_id>             -> revokes a key immediately
    list-orgs                       -> id, name, created_at, active_keys
    audit-log [org_id]              -> 100 most recent audited actions

    stats                          -> aggregate counts: orgs, active keys, traces
                                       (total/quarantined), votes, mean trust
    list-quarantined [org_id]      -> traces held pending review (id, org_id, title,
                                       reason, created_at), optionally filtered to one org
    release-quarantine <trace_id>  -> operator reviewed it and it's fine: clears the
                                       quarantine flag, trace becomes search_traces-eligible
    purge-trace <trace_id>         -> permanently deletes the trace AND every trace in
                                       its amendment chain (+ their votes and any relation
                                       edges referencing them). Irreversible.
    purge-org <org_id>             -> permanently deletes an org and everything scoped to
                                       it (api_keys, traces, votes -- FK ondelete=CASCADE).
                                       Irreversible. See DATA_RETENTION.md.

The raw API key is only ever available at issuance/rotation time -- it is
never stored in recoverable form (hub/auth.py hashes it with argon2 before
the row is written) and is not logged. Copy it to wherever the org will
configure their MCP client now; there is no way to retrieve it again later,
only to rotate to a new one.

This whole module is an operator/DB-access-trust-level tool, not exposed
over the six org-scoped MCP tools (hub/server.py) -- an org's own API key
grants read/write on its own traces, never account/data deletion. See
hub/README.md and DATA_RETENTION.md for why that boundary is deliberate.

Every command function below takes an optional `session_factory` (default
None -> built from HubConfig.from_env()) so tests can inject a fixture's
session_factory instead of monkeypatching module globals.
"""

from __future__ import annotations

import asyncio
import statistics
import sys

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hub import audit, auth
from hub.config import HubConfig
from hub.db import make_engine, make_session_factory, session_scope
from hub.models import ApiKey, AuditLogEntry, Organization, Trace, TraceRelation, Vote


def _default_session_factory() -> async_sessionmaker[AsyncSession]:
    return make_session_factory(make_engine(HubConfig.from_env()))


async def create_org(name: str, session_factory=None) -> None:
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        org = Organization(name=name)
        session.add(org)
        await session.flush()
        await audit.record(
            session, actor=audit.ACTOR_OPERATOR_CLI, action="create_org",
            org_id=org.id, target_type="org", target_id=org.id, summary=f"name={name!r}",
        )
        print(f"org_id: {org.id}")


async def issue_key(org_id: str, expires_days: str | None = None, session_factory=None) -> None:
    days = int(expires_days) if expires_days is not None else None
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id, expires_days=days)
        await audit.record(
            session, actor=audit.ACTOR_OPERATOR_CLI, action="issue_key",
            org_id=org_id, target_type="api_key", target_id=issued.key_id,
            summary=f"prefix={issued.key_prefix} expires_days={days if days is not None else 'never'}",
        )
    print(f"key_id: {issued.key_id}")
    if days is None:
        print("expires: never  (pass a day count, e.g. `issue-key <org_id> 90`, for a client-facing key)")
    else:
        print(f"expires: in {days} day(s)")
    print(f"api_key (shown once, store it now): {issued.raw_key}")


async def rotate_key(key_id: str, session_factory=None) -> None:
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        issued = await auth.rotate_api_key(session, key_id)
        await audit.record(
            session, actor=audit.ACTOR_OPERATOR_CLI, action="rotate_key",
            org_id=issued.org_id, target_type="api_key", target_id=issued.key_id,
            summary=f"replaces={key_id}",
        )
    print(f"revoked: {key_id}")
    print(f"new key_id: {issued.key_id}")
    print(f"new api_key (shown once, store it now): {issued.raw_key}")


async def revoke_key(key_id: str, session_factory=None) -> None:
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        key = await session.get(ApiKey, key_id)
        if key is None:
            # Without this guard, a mistyped or already-revoked key_id still
            # printed "revoked: <id>" -- a false success telling an operator
            # a credential was cut off when nothing happened -- and wrote an
            # audit row with org_id=None for a key that was never resolved,
            # unlike every other operator command (release_quarantine,
            # purge_trace, purge_org) which all refuse on a missing row.
            print(f"error: no such API key: {key_id}", file=sys.stderr)
            return
        await auth.revoke_api_key(session, key_id)
        await audit.record(
            session, actor=audit.ACTOR_OPERATOR_CLI, action="revoke_key",
            org_id=key.org_id, target_type="api_key", target_id=key_id,
        )
    print(f"revoked: {key_id}")


async def list_orgs(session_factory=None) -> None:
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        orgs = (await session.execute(select(Organization))).scalars().all()
        for org in orgs:
            n_keys = (
                await session.execute(select(ApiKey).where(ApiKey.org_id == org.id, ApiKey.revoked_at.is_(None)))
            ).scalars().all()
            print(f"{org.id}  {org.name!r}  created={org.created_at.isoformat()}  active_keys={len(n_keys)}")


async def stats(session_factory=None) -> None:
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        n_orgs = len((await session.execute(select(Organization))).scalars().all())
        n_active_keys = len(
            (await session.execute(select(ApiKey).where(ApiKey.revoked_at.is_(None)))).scalars().all()
        )
        traces = (await session.execute(select(Trace))).scalars().all()
        n_quarantined = sum(1 for t in traces if t.quarantined)
        n_votes = len((await session.execute(select(Vote))).scalars().all())
        mean_trust = statistics.fmean(t.trust for t in traces) if traces else None

    print(f"organizations:      {n_orgs}")
    print(f"active api keys:    {n_active_keys}")
    print(f"traces (total):     {len(traces)}")
    print(f"traces (quarantined): {n_quarantined}")
    print(f"votes:               {n_votes}")
    print(f"mean trust:          {mean_trust:.3f}" if mean_trust is not None else "mean trust:          n/a")


async def list_quarantined(org_id: str | None = None, session_factory=None) -> None:
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        stmt = select(Trace).where(Trace.quarantined.is_(True))
        if org_id:
            stmt = stmt.where(Trace.org_id == org_id)
        rows = (await session.execute(stmt.order_by(Trace.created_at.desc()))).scalars().all()

    if not rows:
        print("no quarantined traces" + (f" for org {org_id}" if org_id else ""))
        return
    for t in rows:
        print(f"{t.id}  org={t.org_id}  created={t.created_at.isoformat()}")
        print(f"    title: {t.title!r}")
        print(f"    reason: {t.quarantine_reason}")


async def release_quarantine(trace_id: str, session_factory=None) -> None:
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        trace = await session.get(Trace, trace_id)
        if trace is None:
            print(f"error: no such trace: {trace_id}", file=sys.stderr)
            return
        # Read the reason BEFORE the UPDATE. SQLAlchemy synchronizes the
        # in-session object with the values it just wrote, so reading
        # trace.quarantine_reason afterwards yields the new "" -- every audit
        # row recorded `was=`, losing precisely the fact the row exists to
        # preserve: why this trace was quarantined in the first place.
        previous_reason = trace.quarantine_reason
        org_id = trace.org_id
        await session.execute(
            update(Trace).where(Trace.id == trace_id).values(quarantined=False, quarantine_reason="")
        )
        await audit.record(
            session, actor=audit.ACTOR_OPERATOR_CLI, action="release_quarantine",
            org_id=org_id, target_type="trace", target_id=trace_id,
            summary=f"was={previous_reason[:100]}",
        )
    print(f"released from quarantine: {trace_id}")


async def _amendment_chain(session: AsyncSession, trace_id: str) -> set[str]:
    """Every trace id in `trace_id`'s amendment lineage: itself, every
    trace it (transitively) supersedes, and every trace that (transitively)
    supersedes it.

    amend_trace creates a NEW row that carries most of the original's
    content forward unchanged (hub/crud.py:amend_trace) -- title/context/
    solution_text can be identical or near-identical across the whole
    chain. A purge scoped to a single id in the middle of that chain
    leaves the same content sitting in its neighbors, which is exactly
    the gap a "delete this trace" request is supposed to close.
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


async def purge_trace(trace_id: str, session_factory=None) -> None:
    """Permanently deletes one trace AND every trace in its amendment chain
    (see _amendment_chain). Votes and trace_relations rows keyed by
    trace_id cascade automatically (FK ondelete=CASCADE, hub/models.py); a
    relation row where a chain member is the *target* (related_trace_id) is
    not covered by that FK -- related_trace_id is a plain column, not a
    foreign key, so it survives the source trace being deleted elsewhere.
    Clean it up explicitly here rather than leave a dangling reference
    behind."""
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        trace = await session.get(Trace, trace_id)
        if trace is None:
            print(f"error: no such trace: {trace_id}", file=sys.stderr)
            return
        chain_ids = await _amendment_chain(session, trace_id)
        await session.execute(delete(TraceRelation).where(TraceRelation.related_trace_id.in_(chain_ids)))
        org_id = trace.org_id
        await session.execute(delete(Trace).where(Trace.id.in_(chain_ids)))
        await audit.record(
            session, actor=audit.ACTOR_OPERATOR_CLI, action="purge_trace",
            org_id=org_id, target_type="trace", target_id=trace_id,
            summary=f"irreversible n_amendment_chain={len(chain_ids)}",
        )
    extra = len(chain_ids) - 1
    suffix = f" (+{extra} amendment-chain trace{'s' if extra != 1 else ''})" if extra else ""
    print(f"permanently deleted trace: {trace_id}{suffix}")


async def purge_org(org_id: str, session_factory=None) -> None:
    """Permanently deletes an org and everything scoped to it (api_keys,
    traces, and traces' votes/trace_relations all cascade via FK
    ondelete=CASCADE). Irreversible -- see DATA_RETENTION.md §3."""
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        org = await session.get(Organization, org_id)
        if org is None:
            print(f"error: no such organization: {org_id}", file=sys.stderr)
            return
        trace_ids = (await session.execute(select(Trace.id).where(Trace.org_id == org_id))).scalars().all()
        if trace_ids:
            # Same dangling-reference cleanup as purge_trace, batched for every
            # trace this org owns, before the cascade deletes them.
            await session.execute(delete(TraceRelation).where(TraceRelation.related_trace_id.in_(trace_ids)))
        org_name = org.name
        await session.delete(org)
        # Recorded AFTER the delete and deliberately NOT cascaded away with
        # it -- see AuditLogEntry's docstring: the purge is exactly the event
        # the trail must retain.
        await audit.record(
            session, actor=audit.ACTOR_OPERATOR_CLI, action="purge_org",
            org_id=org_id, target_type="org", target_id=org_id,
            summary=f"name={org_name!r} n_traces={len(trace_ids)} irreversible",
        )
    print(f"permanently deleted organization {org_id} and all its api_keys/traces/votes.")


async def audit_log(org_id: str | None = None, session_factory=None) -> None:
    """Most recent audit entries, optionally filtered to one org."""
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        stmt = select(AuditLogEntry)
        if org_id:
            stmt = stmt.where(AuditLogEntry.org_id == org_id)
        rows = (
            await session.execute(stmt.order_by(AuditLogEntry.created_at.desc()).limit(100))
        ).scalars().all()

    if not rows:
        print("no audit entries" + (f" for org {org_id}" if org_id else ""))
        return
    for r in rows:
        target = f"{r.target_type}:{r.target_id}" if r.target_type else "-"
        print(f"{r.created_at.isoformat()}  {r.actor:24s} {r.action:20s} {target}")
        if r.summary:
            print(f"    {r.summary}")


_COMMANDS = {
    "create-org": (create_org, 1, 1),
    "issue-key": (issue_key, 1, 2),
    "rotate-key": (rotate_key, 1, 1),
    "revoke-key": (revoke_key, 1, 1),
    "list-orgs": (list_orgs, 0, 0),
    "audit-log": (audit_log, 0, 1),
    "stats": (stats, 0, 0),
    "list-quarantined": (list_quarantined, 0, 1),
    "release-quarantine": (release_quarantine, 1, 1),
    "purge-trace": (purge_trace, 1, 1),
    "purge-org": (purge_org, 1, 1),
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in _COMMANDS:
        print(__doc__)
        return 1 if argv else 0

    fn, min_args, max_args = _COMMANDS[argv[0]]
    args = argv[1:]
    if not (min_args <= len(args) <= max_args):
        expected = str(min_args) if min_args == max_args else f"{min_args}-{max_args}"
        print(f"error: {argv[0]} takes {expected} argument(s), got {len(args)}", file=sys.stderr)
        return 2

    try:
        asyncio.run(fn(*args))
    except (ValueError, LookupError) as exc:
        # Operator mistakes -- a bad day count, an org id that doesn't exist.
        # A traceback here reads as "the tool is broken" rather than "you typed
        # something wrong", and this CLI is what an operator runs in production.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
