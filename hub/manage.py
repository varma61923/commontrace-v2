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
    commons-stats                  -> cross-org commons health: corpus size, how many
                                       distinct orgs contribute, concentration risk
    commons-value                  -> the pricing denominator: per org, what it shared
                                       and how much that DELIVERED to other fleets
    commons-seed <file.jsonl> <org_id>
                                   -> break the cold start with public substrate
                                       knowledge, marked commons_source='seed' so it
                                       never counts as a network effect
    set-plan <org_id> <plan>       -> change an org's entitlements (hub/plans.py):
                                       free | team | scale | operator
    usage [org_id]                 -> what each org is entitled to and has used this
                                       period, including allowance EARNED by contributing
    revenue                        -> orgs on billable plans, and what the commons
                                       delivered to each -- price against measured value
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
from datetime import datetime, timezone

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hub import audit, auth, commons, crud, plans
from hub.config import HubConfig
from hub.db import make_engine, make_session_factory, session_scope
from hub.models import ApiKey, AuditLogEntry, Organization, Trace, TraceRelation, UsageCounter, Vote


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


async def commons_seed(path: str, org_id: str, session_factory=None) -> None:
    """Seed the commons from a JSONL file of public substrate knowledge.

    THE COLD START, AND WHY THIS IS NOT CHEATING. An empty commons returns
    0% coverage to every prospect -- by construction, not as a finding --
    so the first customer sees nothing and never contributes, and the
    network effect never starts. Seeding breaks that.

    What makes it honest rather than a rigged demo is that every seeded
    row is marked `commons_source='seed'` and is reported SEPARATELY from
    org contributions everywhere it matters (commons-stats, commons-value).
    The metric that decides the company's direction is "how many distinct
    ORGS contribute", and that number must never quietly count the
    operator's own seeding. Seed content still delivers real value to a
    querying fleet -- it just does not count as evidence of a network
    effect, because it isn't any.

    Each JSONL line: {"title", "context_text", "solution_text", "tags"?,
    "agent_type"?, "source"?}. `source` should cite where the knowledge
    came from (a public postmortem, a vendor changelog) and is stored as
    the trace's shared_rationale so provenance survives.

    Seeded traces are owned by `org_id` -- give this a dedicated operator
    org, not a customer's, so no customer is credited with authorship they
    do not have.
    """
    import json as _json

    session_factory = session_factory or _default_session_factory()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw_lines = [ln for ln in (line.strip() for line in fh) if ln]
    except OSError as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return

    records, bad = [], 0
    for i, line in enumerate(raw_lines, 1):
        try:
            rec = _json.loads(line)
        except ValueError:
            bad += 1
            print(f"  line {i}: not valid JSON, skipped", file=sys.stderr)
            continue
        if not isinstance(rec, dict) or not rec.get("title") or not rec.get("solution_text"):
            bad += 1
            print(f"  line {i}: needs at least 'title' and 'solution_text', skipped", file=sys.stderr)
            continue
        records.append(rec)

    if not records:
        print(f"error: no usable records in {path}", file=sys.stderr)
        return

    async with session_scope(session_factory) as session:
        org = await session.get(Organization, org_id)
        if org is None:
            print(f"error: no such organization: {org_id}", file=sys.stderr)
            return

        added = 0
        for rec in records:
            title = str(rec["title"])[:1000]
            context_text = str(rec.get("context_text") or "")
            solution_text = str(rec["solution_text"])
            tags = [str(t) for t in (rec.get("tags") or []) if t is not None][:20]
            trace = Trace(
                org_id=org_id,
                title=title,
                context_text=context_text,
                solution_text=solution_text,
                tags=tags,
                agent_type=str(rec.get("agent_type") or "code"),
                shared_with_commons=True,
                shared_at=datetime.now(timezone.utc),
                shared_rationale=str(rec.get("source") or "operator-seeded public substrate knowledge")[:500],
                commons_signature=commons.signature_for(title, context_text, tags),
                commons_source="seed",
            )
            session.add(trace)
            added += 1
        await session.flush()
        await audit.record(
            session, actor=audit.ACTOR_OPERATOR_CLI, action="commons_seed",
            org_id=org_id, target_type="org", target_id=org_id,
            summary=f"seeded={added} skipped={bad} from={path!r}",
        )

    print(f"seeded {added} trace(s) into the commons as commons_source='seed'.")
    if bad:
        print(f"  {bad} line(s) skipped -- see errors above.", file=sys.stderr)
    print("  These are reported separately from org contributions by "
          "`commons-stats` and `commons-value`, so the network-effect")
    print("  metric is not inflated by operator seeding.")


async def commons_value(session_factory=None) -> None:
    """What each org puts into the commons, and what that delivered.

    This is the pricing denominator. Value-based pricing needs measured
    value, and "traces contributed" is vanity -- the number that matters is
    how many times a contributor's knowledge actually covered someone
    else's recurring failure (Trace.commons_hits). That same number is what
    makes contributing rational rather than altruistic, which is the
    standard reason knowledge-commons plays fail (STRATEGY.md §3).
    """
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        rows = (
            await session.execute(
                select(
                    Trace.org_id,
                    Trace.commons_source,
                    func.count(),
                    func.coalesce(func.sum(Trace.commons_hits), 0),
                )
                .where(Trace.shared_with_commons.is_(True), Trace.quarantined.is_(False))
                .group_by(Trace.org_id, Trace.commons_source)
            )
        ).all()
        names = dict(
            (await session.execute(select(Organization.id, Organization.name))).all()
        )

    if not rows:
        print("nothing in the commons yet -- no value to attribute.")
        return

    by_org: dict[tuple[str, str], tuple[int, int]] = {}
    for org_id, source, n_traces, hits in rows:
        by_org[(org_id, source or "org")] = (int(n_traces), int(hits))

    org_rows = sorted(
        ((k, v) for k, v in by_org.items() if k[1] != "seed"),
        key=lambda kv: -kv[1][1],
    )
    seed_rows = [(k, v) for k, v in by_org.items() if k[1] == "seed"]

    print(f"{'organization':<38} {'shared':>7} {'delivered':>10}")
    print("-" * 58)
    for (org_id, _src), (n_traces, hits) in org_rows:
        label = f"{(names.get(org_id) or '?')[:26]} {org_id[:8]}"
        print(f"{label:<38} {n_traces:>7} {hits:>10}")

    total_org_hits = sum(v[1] for k, v in by_org.items() if k[1] != "seed")
    total_seed_hits = sum(v[1] for _k, v in seed_rows)
    total_org_traces = sum(v[0] for k, v in by_org.items() if k[1] != "seed")
    total_seed_traces = sum(v[0] for _k, v in seed_rows)

    print("-" * 58)
    print(f"{'org-contributed':<38} {total_org_traces:>7} {total_org_hits:>10}")
    if seed_rows:
        print(f"{'operator-seeded (not a network effect)':<38} {total_seed_traces:>7} {total_seed_hits:>10}")

    print()
    if total_org_hits == 0 and total_seed_hits == 0:
        print("No commons trace has covered anyone's failure yet. Either nobody has run")
        print("`commons_overlap`, or the corpus does not yet overlap what fleets are hitting.")
        return

    delivered_total = total_org_hits + total_seed_hits
    if delivered_total and total_seed_hits / delivered_total > 0.5:
        print(f"Most delivered value ({total_seed_hits}/{delivered_total}) comes from operator")
        print("seeding, not from orgs. That is a working cold-start primer, not yet a")
        print("network effect -- the metric to watch is org-contributed value overtaking it.")
    elif org_rows:
        top_org, (_n, top_hits) = org_rows[0]
        if total_org_hits and top_hits / total_org_hits > 0.6:
            print(f"One org delivers {top_hits}/{total_org_hits} of all org-contributed value.")
            print("Concentration risk: their withdrawal would take most of the commons' worth")
            print("with it. Broadening contribution matters more than growing the corpus.")


async def commons_stats(session_factory=None) -> None:
    """Is the cross-org commons actually working?

    Corpus size alone is vanity. The number that matters is **how many
    distinct orgs have contributed**, because the whole thesis is a network
    effect: value to each participant grows with the number of *others*.
    A commons of 10,000 traces from one org is a single fleet's memory with
    extra steps; 500 traces from 40 orgs is the thing compounding.
    """
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        # Only org-contributed rows count toward the network-effect metric.
        # Operator seeding exists to break the cold start and is real value
        # to a querying fleet, but counting it here would mean "contributing
        # orgs" silently included ourselves -- and that number is the one
        # that decides whether (B) is working at all.
        rows = (
            await session.execute(
                select(Trace.org_id, func.count())
                .where(
                    Trace.shared_with_commons.is_(True),
                    Trace.quarantined.is_(False),
                    Trace.commons_source != "seed",
                )
                .group_by(Trace.org_id)
            )
        ).all()
        n_seeded = (
            await session.execute(
                select(func.count()).select_from(Trace).where(
                    Trace.shared_with_commons.is_(True),
                    Trace.quarantined.is_(False),
                    Trace.commons_source == "seed",
                )
            )
        ).scalar_one()
        n_orgs_total = (
            await session.execute(select(func.count()).select_from(Organization))
        ).scalar_one()
        # Seeded rows are excluded from the share-rate denominator too: they
        # were never a fleet's own captured experience, so counting them
        # would make "what fraction of real traces get shared" drift as the
        # operator seeds more.
        n_traces_total = (
            await session.execute(
                select(func.count()).select_from(Trace).where(Trace.commons_source != "seed")
            )
        ).scalar_one()

    n_contributors = len(rows)
    n_shared = sum(c for _, c in rows)

    print(f"commons traces (org):  {n_shared}")
    if n_seeded:
        print(f"commons traces (seed): {n_seeded}   <- operator-seeded, NOT a network effect")
    print(f"contributing orgs:     {n_contributors} of {n_orgs_total}")
    print(f"share rate:            {n_shared}/{n_traces_total} org trace(s) "
          f"({(n_shared / n_traces_total * 100) if n_traces_total else 0:.1f}%)")

    if n_contributors == 0:
        if n_seeded:
            # Distinguishing these matters: with a seeded corpus, queries do
            # return real matches, so saying "the commons is empty" would be
            # simply false. What is missing is not content -- it is evidence
            # that anyone other than the operator finds it worth contributing to.
            print(
                f"\nNo ORG has contributed yet. Queries do return matches (from {n_seeded} seeded\n"
                "trace(s)), so the commons is useful -- but a primer an operator loaded is\n"
                "not a network effect. The number to watch is this line reaching 1, then many."
            )
        else:
            print(
                "\nThe commons is empty, so `commons_overlap` returns 0% for everyone by\n"
                "construction -- not as a finding. Nothing compounds until orgs contribute."
            )
        return
    if n_contributors == 1:
        print(
            "\nOnly ONE org has contributed. Every other org's coverage number is\n"
            "measured against a single fleet's substrate, which is not yet a network\n"
            "effect -- it is one generous customer. Concentration risk, too: if they\n"
            "withdraw, the commons empties."
        )
        return

    largest = max(c for _, c in rows)
    concentration = largest / n_shared
    print(f"largest contributor:   {largest} traces ({concentration:.0%} of the corpus)")
    if concentration > 0.6:
        print(
            "\nOver 60% of the commons comes from one org. The coverage numbers other\n"
            "orgs see are mostly that one fleet's experience; treat the network effect\n"
            "as unproven until contribution spreads."
        )


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


async def set_plan(org_id: str, plan_name: str, session_factory=None) -> None:
    """Move an org between plans (hub/plans.py).

    Refuses an unknown name rather than falling back to the default. The
    runtime resolver fails *closed* to the smallest plan, which is the
    right behaviour for a stale row nobody can fix at 3am -- but an
    operator typing `python -m hub.manage set-plan <id> tema` deserves an
    error, not a customer silently downgraded to free.
    """
    session_factory = session_factory or _default_session_factory()
    key = (plan_name or "").strip().lower()
    if key not in plans.PLANS:
        print(f"error: unknown plan {plan_name!r}. Known: {', '.join(sorted(plans.PLANS))}",
              file=sys.stderr)
        return

    async with session_scope(session_factory) as session:
        org = await session.get(Organization, org_id)
        if org is None:
            print(f"error: no such organization: {org_id}", file=sys.stderr)
            return
        was, org.plan = org.plan, key
        await audit.record(
            session, actor=audit.ACTOR_OPERATOR_CLI, action="set_plan",
            org_id=org_id, target_type="org", target_id=org_id,
            summary=f"{was!r} -> {key!r}",
        )
    plan = plans.PLANS[key]
    print(f"{org_id}: {was} -> {key}")
    print(f"  traces:         {plans.describe(plan.max_traces)}")
    print(f"  commons/month:  {plans.describe(plan.commons_queries_per_month)} "
          f"(+{plans.QUERY_CREDIT_PER_HIT} per delivered hit)")
    print(f"  {plan.summary}")


async def usage(org_id: str | None = None, session_factory=None) -> None:
    """Entitlements and consumption for the current period.

    Shows granted and EARNED allowance separately, because the difference
    is the entire argument for contributing: an org that can see it is
    ahead on credit has a reason to keep sharing, and an org that cannot
    see it is being asked for a favour.
    """
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        q = select(Organization).order_by(Organization.created_at)
        if org_id:
            q = q.where(Organization.id == org_id)
        orgs = (await session.execute(q)).scalars().all()
        if not orgs:
            print("no organizations." if not org_id else f"error: no such organization: {org_id}",
                  file=sys.stderr if org_id else sys.stdout)
            return

        rows = [await crud.entitlements(session, o.id) for o in orgs]

    period = rows[0]["period"]
    print(f"billing period {period} (UTC)")
    print(f"{'organization':<26} {'plan':<9} {'traces':>14} "
          f"{'commons q':>12} {'granted':>8} {'earned':>7} {'hits':>6}")
    print("-" * 88)
    for org, r in zip(orgs, rows):
        q = r["commons_queries"]
        traces = f"{r['traces']['used']:,}/{plans.describe(r['traces']['limit'])}"
        used = f"{q['used']:,}/{plans.describe(q['allowance'])}"
        print(f"{org.name[:25]:<26} {r['plan']:<9} {traces:>14} "
              f"{used:>12} {plans.describe(q['granted']):>8} "
              f"{q['earned']:>7,} {r['delivered_hits']:>6,}")
    print()
    print(f"'earned' is allowance nobody paid for: {plans.QUERY_CREDIT_PER_HIT} commons queries "
          "per time this org's")
    print("shared knowledge covered another fleet's failure. Seeded rows are excluded.")


async def revenue(session_factory=None) -> None:
    """Who is on a billable plan, and what they measurably got for it.

    Deliberately prints no currency. This repository implements the
    entitlement, not the invoice -- there is no payment processing here,
    and printing a dollar figure computed from a hardcoded rate would read
    as revenue reporting while being arithmetic on a number nobody agreed
    to. What it does show is the thing a price should be argued from: how
    much of each paying org's consumption came from other orgs' knowledge,
    and how much of their own knowledge went the other way.
    """
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        orgs = (await session.execute(
            select(Organization).order_by(Organization.created_at)
        )).scalars().all()
        billable = [o for o in orgs if o.plan in plans.BILLABLE_PLANS]
        rows = [(o, await crud.entitlements(session, o.id)) for o in billable]
        total_q = int(await session.scalar(
            select(func.coalesce(func.sum(UsageCounter.n), 0)).where(
                UsageCounter.metric == crud.METRIC_COMMONS_QUERIES,
                UsageCounter.period == crud.billing_period(),
            )
        ) or 0)

    if not billable:
        print(f"No org is on a billable plan ({' or '.join(plans.BILLABLE_PLANS)}).")
        print(f"{len(orgs)} org(s) exist. `set-plan <org_id> team` moves one.")
        return

    print(f"billing period {crud.billing_period()} (UTC)")
    print(f"{'organization':<26} {'plan':<9} {'consumed':>10} {'delivered':>11} {'net':>7}")
    print("-" * 68)
    for org, r in rows:
        consumed = r["commons_queries"]["used"]
        delivered = r["delivered_hits"]
        print(f"{org.name[:25]:<26} {org.plan:<9} {consumed:>10,} "
              f"{delivered:>11,} {delivered - consumed:>+7,}")
    print("-" * 68)
    print(f"{len(billable)} billable org(s) of {len(orgs)}; "
          f"{total_q:,} commons queries across all orgs this period.")
    print()
    print("'consumed' is commons queries run; 'delivered' is times this org's shared")
    print("knowledge covered someone else's failure. A negative net is an org taking")
    print("more than it gives -- which is exactly who a price should fall on hardest,")
    print("and why the credit mechanism is the discount rather than a separate SKU.")
    print()
    print("No currency is printed here on purpose: this implements the entitlement,")
    print("not the invoice. Attaching a rate is a decision for whoever owns the P&L,")
    print("and this is the denominator to attach it to.")


_COMMANDS = {
    "create-org": (create_org, 1, 1),
    "issue-key": (issue_key, 1, 2),
    "rotate-key": (rotate_key, 1, 1),
    "revoke-key": (revoke_key, 1, 1),
    "list-orgs": (list_orgs, 0, 0),
    "audit-log": (audit_log, 0, 1),
    "stats": (stats, 0, 0),
    "commons-stats": (commons_stats, 0, 0),
    "commons-value": (commons_value, 0, 0),
    "commons-seed": (commons_seed, 2, 2),
    "set-plan": (set_plan, 2, 2),
    "usage": (usage, 0, 1),
    "revenue": (revenue, 0, 0),
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
    except SQLAlchemyError as exc:
        # Same operator-mistake category, one layer down: a malformed id
        # (`revoke-key not-a-uuid`) is rejected by the UUID column type
        # itself rather than by `session.get` returning None, and asyncpg
        # raises that as a driver-level error (typically
        # sqlalchemy.exc.DBAPIError wrapping an asyncpg DataError, not a
        # plain ValueError/LookupError) -- so it fell through the catch
        # above and dumped a raw traceback for the same kind of typo the
        # branch above already handles cleanly.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
