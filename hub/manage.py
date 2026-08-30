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
    kb-stats                       -> Knowledge Base content quality: corpus size, hits
                                       delivered, top/dead entries, orgs that query it
    commons-seed <file.jsonl> <org_id>
                                   -> load or update the operator-curated Knowledge Base
                                       content, marked commons_source='seed' -- the ONLY
                                       way content ever enters it (see hub/plans.py "why
                                       there is no org-to-org sharing here")
    set-plan <org_id> <plan>       -> change an org's entitlements (hub/plans.py):
                                       free | team | scale | operator
    usage [org_id]                 -> what each org is entitled to and has used this
                                       period
    revenue                        -> orgs on billable plans and what they consumed
    list-quarantined [org_id]      -> traces held pending review (id, org_id, title,
                                       reason, created_at), optionally filtered to one org
    release-quarantine <trace_id>  -> operator reviewed it and it's fine: clears the
                                       quarantine flag, trace becomes search_traces-eligible
    purge-trace <trace_id> [--yes] -> permanently deletes the trace AND every trace in
                                       its amendment chain (+ their votes and any relation
                                       edges referencing them). Irreversible. Prompts for
                                       interactive confirmation unless --yes is passed.
    purge-org <org_id> [--yes]     -> permanently deletes an org and everything scoped to
                                       it (api_keys, traces, votes -- FK ondelete=CASCADE).
                                       Irreversible. See DATA_RETENTION.md. Prompts for
                                       interactive confirmation unless --yes is passed.

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
        # Organization.name carries no uniqueness constraint (and adding one
        # via migration is not safe to do blindly -- it would fail outright
        # against any existing deployment that already has two orgs sharing
        # a name). The realistic risk isn't a lookup bug: every hub/manage.py
        # operation takes org_id, never name, so nothing programmatic can
        # resolve the wrong org this way. It's an operator scanning a
        # listing (`usage`, `list-quarantined`, ...) by eye and picking the
        # wrong row when two orgs look identical. Warn at the one point a
        # duplicate is actually introduced, rather than block it outright --
        # a shared display name across regional entities under one brand
        # may be entirely intentional.
        existing = (
            await session.execute(select(Organization.id).where(Organization.name == name))
        ).scalars().all()
        if existing:
            print(
                f"[WARN] another org already uses the name {name!r} "
                f"(org_id: {', '.join(existing)}) -- creating anyway.",
                file=sys.stderr,
            )
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


async def revoke_key(key_id: str, session_factory=None) -> bool:
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
            # Returning False (not just printing to stderr) is what makes
            # `main()` exit non-zero for this -- an operator/incident script
            # checking $? for a failed revoke must not see a false "0 = ok".
            print(f"error: no such API key: {key_id}", file=sys.stderr)
            return False
        await auth.revoke_api_key(session, key_id)
        await audit.record(
            session, actor=audit.ACTOR_OPERATOR_CLI, action="revoke_key",
            org_id=key.org_id, target_type="api_key", target_id=key_id,
        )
    print(f"revoked: {key_id}")
    return True


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
    """Aggregate counts, computed in the database rather than by loading
    every organization/api_key/trace/vote row as a full ORM object into
    Python just to len() or fmean() them -- a deployment with any real
    volume of traces previously materialized its ENTIRE traces table in
    memory (and paid the network transfer for all of it) every time an
    operator ran this."""
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        n_orgs = await session.scalar(select(func.count()).select_from(Organization)) or 0
        n_active_keys = await session.scalar(
            select(func.count()).select_from(ApiKey).where(ApiKey.revoked_at.is_(None))
        ) or 0
        n_traces = await session.scalar(select(func.count()).select_from(Trace)) or 0
        n_quarantined = await session.scalar(
            select(func.count()).select_from(Trace).where(Trace.quarantined.is_(True))
        ) or 0
        n_votes = await session.scalar(select(func.count()).select_from(Vote)) or 0
        mean_trust = await session.scalar(select(func.avg(Trace.trust))) if n_traces else None

    print(f"organizations:      {n_orgs}")
    print(f"active api keys:    {n_active_keys}")
    print(f"traces (total):     {n_traces}")
    print(f"traces (quarantined): {n_quarantined}")
    print(f"votes:               {n_votes}")
    print(f"mean trust:          {mean_trust:.3f}" if mean_trust is not None else "mean trust:          n/a")


async def commons_seed(path: str, org_id: str, session_factory=None) -> bool:
    """Load or update the CommonTrace Knowledge Base from a JSONL file of
    curated substrate knowledge.

    THIS IS THE ONLY WAY CONTENT EVER ENTERS THE KNOWLEDGE BASE. There is
    no customer-facing tool that sets `commons_source='seed'` -- see
    hub/plans.py "why there is no org-to-org sharing here" for why that is
    a deliberate absence, not a gap: a customer's own trace should never be
    able to become visible to another customer, and the surest way to
    guarantee that is to have exactly one, operator-run code path capable
    of writing this column at all.

    Re-runnable: run it again after editing the source file to add new
    entries (existing ones are not deduplicated against by content, so
    editing in place and re-running will create fresh rows for unchanged
    lines too -- track what has already been loaded in the source file
    itself, or purge and reload for now).

    Each JSONL line: {"title", "context_text", "solution_text", "tags"?,
    "agent_type"?, "source"?}. `source` should cite where the knowledge
    came from (a public postmortem, a vendor changelog) and is stored as
    the trace's shared_rationale so provenance survives.

    Seeded traces are owned by `org_id` -- give this a dedicated operator
    org, not a customer's, so nothing here is ever attributed to a customer
    who did not write it.
    """
    import json as _json

    session_factory = session_factory or _default_session_factory()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw_lines = [ln for ln in (line.strip() for line in fh) if ln]
    except OSError as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return False

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
        return False

    async with session_scope(session_factory) as session:
        org = await session.get(Organization, org_id)
        if org is None:
            print(f"error: no such organization: {org_id}", file=sys.stderr)
            return False

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

    print(f"loaded {added} entry(ies) into the Knowledge Base.")
    if bad:
        print(f"  {bad} line(s) skipped -- see errors above.", file=sys.stderr)
    print("  Run `python -m hub.manage kb-stats` to see corpus size and hit coverage.")
    return True


async def kb_stats(session_factory=None) -> None:
    """Is the CommonTrace Knowledge Base actually earning its query traffic?

    There is no customer contribution to measure here, on purpose (see
    hub/plans.py "why there is no org-to-org sharing here") -- so this is
    not a network-effect report, it is a CONTENT QUALITY report: how big is
    the corpus, how often does it actually cover a real recurring failure
    (Trace.commons_hits, incremented only by the conservative
    commons_overlap threshold), which entries are pulling weight, and which
    have never once matched anything and are candidates to revise or prune.
    Distinct customer orgs that have ever queried it is the one adoption
    number worth watching -- readership, not authorship.
    """
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        entries = (
            await session.execute(
                select(Trace.id, Trace.title, Trace.commons_hits)
                .where(
                    Trace.shared_with_commons.is_(True),
                    Trace.quarantined.is_(False),
                    Trace.commons_source == "seed",
                )
                .order_by(Trace.commons_hits.desc())
            )
        ).all()
        n_queriers = (
            await session.execute(
                select(func.count(func.distinct(UsageCounter.org_id))).where(
                    UsageCounter.metric == crud.METRIC_COMMONS_QUERIES
                )
            )
        ).scalar_one()
        n_orgs_total = (
            await session.execute(select(func.count()).select_from(Organization))
        ).scalar_one()

    if not entries:
        print("The Knowledge Base has no entries yet. `commons-seed <file.jsonl> "
              "<operator_org_id>` loads one.")
        return

    total_hits = sum(hits for _id, _title, hits in entries)
    zero_hit = [e for e in entries if e[2] == 0]

    print(f"knowledge base entries:  {len(entries)}")
    print(f"total hits delivered:    {total_hits}   (times an entry covered a real "
          "recurring failure, at the conservative threshold)")
    print(f"queried by:              {n_queriers} of {n_orgs_total} org(s) (ever, any period)")
    print(f"never matched anything:  {len(zero_hit)} of {len(entries)} entries")

    if total_hits == 0:
        print(
            "\nNo entry has covered anyone's failure yet. Either nobody has queried the "
            "Knowledge Base, or its content does not yet overlap what fleets are hitting."
        )
        return

    print("\ntop entries by hits:")
    for _id, title, hits in entries[:10]:
        if hits == 0:
            break
        print(f"  {hits:>4}  {title[:70]}")

    if len(zero_hit) / len(entries) > 0.5:
        print(
            f"\nOver half the corpus ({len(zero_hit)}/{len(entries)}) has never matched a "
            "real query. Either these entries describe failures fleets are not actually "
            "hitting, or they are worded differently from how fleets describe them -- see "
            "commons/eval/RESULTS.md on lexical matching's recall limits."
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


async def release_quarantine(trace_id: str, session_factory=None) -> bool:
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        trace = await session.get(Trace, trace_id)
        if trace is None:
            print(f"error: no such trace: {trace_id}", file=sys.stderr)
            return False
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
    return True


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


async def purge_trace(trace_id: str, session_factory=None) -> bool:
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
            return False
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
    return True


async def purge_org(org_id: str, session_factory=None) -> bool:
    """Permanently deletes an org and everything scoped to it (api_keys,
    traces, and traces' votes/trace_relations all cascade via FK
    ondelete=CASCADE). Irreversible -- see DATA_RETENTION.md §3."""
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        org = await session.get(Organization, org_id)
        if org is None:
            print(f"error: no such organization: {org_id}", file=sys.stderr)
            return False
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
    return True


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


async def set_plan(org_id: str, plan_name: str, session_factory=None) -> bool:
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
        return False

    async with session_scope(session_factory) as session:
        org = await session.get(Organization, org_id)
        if org is None:
            print(f"error: no such organization: {org_id}", file=sys.stderr)
            return False
        was, org.plan = org.plan, key
        await audit.record(
            session, actor=audit.ACTOR_OPERATOR_CLI, action="set_plan",
            org_id=org_id, target_type="org", target_id=org_id,
            summary=f"{was!r} -> {key!r}",
        )
    plan = plans.PLANS[key]
    print(f"{org_id}: {was} -> {key}")
    print(f"  traces:         {plans.describe(plan.max_traces)}")
    print(f"  commons/month:  {plans.describe(plan.commons_queries_per_month)}")
    print(f"  {plan.summary}")
    return True


async def usage(org_id: str | None = None, session_factory=None) -> bool:
    """Entitlements and consumption for the current period.

    A flat allowance per plan, with no earning mechanic: there is no
    customer contribution in this model to earn credit for (hub/plans.py
    "why there is no org-to-org sharing here"), so what an org has is
    simply what its plan grants.
    """
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        q = select(Organization).order_by(Organization.created_at)
        if org_id:
            q = q.where(Organization.id == org_id)
        orgs = (await session.execute(q)).scalars().all()
        if not orgs:
            # An empty fleet-wide listing is not an error (there is simply
            # nothing to show yet); a specific org_id that doesn't resolve
            # is -- that's the only branch that should fail the command.
            print("no organizations." if not org_id else f"error: no such organization: {org_id}",
                  file=sys.stderr if org_id else sys.stdout)
            return not org_id

        rows = [await crud.entitlements(session, o.id) for o in orgs]

    period = rows[0]["period"]
    print(f"billing period {period} (UTC)")
    print(f"{'organization':<26} {'plan':<9} {'agents':>12} {'traces':>14} "
          f"{'kb queries':>14}")
    print("-" * 86)
    any_floor = False
    total_agents = 0
    for org, r in zip(orgs, rows):
        q = r["commons_queries"]
        a = r["agents"]
        any_floor = any_floor or a["is_floor"]
        total_agents += a["active"]
        # A trailing '+' marks a floor: this org has traces from clients
        # that sent no agent_id, so its real agent count is at least this.
        agents = f"{a['active']:,}{'+' if a['is_floor'] else ''}/{plans.describe(a['limit'])}"
        traces = f"{r['traces']['used']:,}/{plans.describe(r['traces']['limit'])}"
        used = f"{q['used']:,}/{plans.describe(q['allowance'])}"
        print(f"{org.name[:25]:<26} {r['plan']:<9} {agents:>12} {traces:>14} {used:>14}")
    print()
    print(f"agents under management (all orgs): {total_agents:,}"
          f"{'+ -- see below' if any_floor else ''}")
    print(f"'agents' counts distinct agent_ids active in the last {plans.ACTIVE_AGENT_WINDOW_DAYS} "
          "days, so it can fall as well as rise.")
    if any_floor:
        print("A trailing '+' is a FLOOR, not a total: that org has traces whose client sent no")
        print("agent_id, and they collapse into one 'unattributed' agent however many really sent")
        print("them. Have those clients pass agent_id to make the number exact.")
    return True


async def revenue(session_factory=None) -> None:
    """Who is on a billable plan, and how much of each resource they used.

    Deliberately prints no currency. This repository implements the
    entitlement, not the invoice -- there is no payment processing here,
    and printing a dollar figure computed from a hardcoded rate would read
    as revenue reporting while being arithmetic on a number nobody agreed
    to. What it does show is the thing a price should be argued from: real
    consumption of the two metered resources, storage and Knowledge Base
    queries. There is no "delivered" side to net against -- customers do
    not contribute to what they consume in this model (hub/plans.py "why
    there is no org-to-org sharing here"), so consumption is the whole
    number, not one side of a ledger.
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
    print(f"{'organization':<26} {'plan':<9} {'traces':>10} {'kb queries':>12}")
    print("-" * 60)
    for org, r in rows:
        print(f"{org.name[:25]:<26} {org.plan:<9} {r['traces']['used']:>10,} "
              f"{r['commons_queries']['used']:>12,}")
    print("-" * 60)
    print(f"{len(billable)} billable org(s) of {len(orgs)}; "
          f"{total_q:,} Knowledge Base queries across all orgs this period.")
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
    "kb-stats": (kb_stats, 0, 0),
    "commons-seed": (commons_seed, 2, 2),
    "set-plan": (set_plan, 2, 2),
    "usage": (usage, 0, 1),
    "revenue": (revenue, 0, 0),
    "list-quarantined": (list_quarantined, 0, 1),
    "release-quarantine": (release_quarantine, 1, 1),
    # +1 on max_args: the optional trailing --yes flag, stripped in main()
    # before the underlying function ever sees it.
    "purge-trace": (purge_trace, 1, 2),
    "purge-org": (purge_org, 1, 2),
}

# Deletion here is permanent (no soft-delete, no undo -- see purge_trace/
# purge_org's own docstrings and DATA_RETENTION.md). Every other _COMMANDS
# entry either only reads, or is itself reversible (revoke-key has
# rotate-key, release-quarantine has nothing to reverse but also nothing to
# lose). Gated on an interactive prompt so a mistyped id or a fat-fingered
# extra Enter in a terminal session doesn't silently delete a customer's
# data; --yes bypasses it for scripted/automated use, which must ask for
# this explicitly rather than get it by default.
_DESTRUCTIVE_COMMANDS: dict[str, str] = {
    "purge-trace": "permanently delete this trace and its full amendment chain",
    "purge-org": "permanently delete this organization and everything scoped to it "
                 "(api_keys, traces, votes)",
}


def _confirm_destructive(action: str) -> bool:
    if not sys.stdin.isatty():
        print(
            f"error: refusing to {action} without --yes (stdin is not a terminal, "
            "cannot prompt for confirmation)",
            file=sys.stderr,
        )
        return False
    reply = input(f"This will {action.upper()}. This cannot be undone. Type 'yes' to continue: ")
    return reply.strip().lower() == "yes"


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

    if argv[0] in _DESTRUCTIVE_COMMANDS:
        skip_confirm = "--yes" in args
        args = [a for a in args if a != "--yes"]
        if len(args) != min_args:
            print(f"error: {argv[0]} takes {min_args} argument(s) (plus optional --yes), "
                  f"got {len(args)}", file=sys.stderr)
            return 2
        if not skip_confirm and not _confirm_destructive(_DESTRUCTIVE_COMMANDS[argv[0]]):
            print("aborted (no changes made)", file=sys.stderr)
            return 2

    try:
        result = asyncio.run(fn(*args))
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
    # Several commands (revoke-key, release-quarantine, purge-trace,
    # purge-org, commons-seed, set-plan, usage) print "error: ..." to
    # stderr on a failed lookup and return False instead of raising --
    # there is nothing exceptional about "that id doesn't exist", so it
    # isn't one of the exception branches above. Without checking the
    # return value here, every one of those failures still exited 0: an
    # automated incident script checking $? after `purge-org` (say, to
    # confirm a GDPR deletion actually happened) would see success on a
    # no-op. Commands with no failure path return None, which is not
    # `False`, so they are unaffected.
    if result is False:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
