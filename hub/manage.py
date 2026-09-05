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
                                       delivered, top/dead entries, orgs that query it,
                                       and the community-submission funnel (pending /
                                       approved / rejected)
    commons-seed <file.jsonl> <org_id>
                                   -> load or update the operator-curated Knowledge Base
                                       content, marked commons_source='seed' from a file
                                       the operator wrote
    kb-review [limit]              -> which Knowledge Base entries need a human, worst
                                       first: security-flagged, disputed by the fleets
                                       that tried them, past their review date, or never
                                       matched anything. Ordered by traffic affected
    kb-retract <trace_id> [reason] -> withdraw one entry from the Knowledge Base. Stops
                                       being served immediately; the row, its votes and
                                       its hit history are kept. Reversible
    kb-restore <trace_id>          -> put a retracted entry back into the Knowledge Base
    list-submissions [pending|approved|rejected]
                                   -> the community-submission review queue (or full
                                       history if no status given) -- see submit_kb_entry
                                       in hub/server.py for how a submission is created
    approve-submission <submission_id> <operator_org_id> [credit]
                                   -> accept a submission: publishes it as a new
                                       Knowledge Base entry owned by <operator_org_id>
                                       (never the submitting org) and permanently raises
                                       the submitting org's query allowance by [credit]
                                       (default: plans.SUBMISSION_ACCEPTANCE_CREDIT)
    reject-submission <submission_id> [reason]
                                   -> decline a submission. No entry, no credit -- the
                                       whole point of reviewing before crediting
                                       (hub/plans.py "why bonus_commons_queries is not
                                       the same mistake twice")
    set-plan <org_id> <plan>       -> change an org's entitlements (hub/plans.py):
                                       free | team | scale | operator
    usage [org_id]                 -> what each org is entitled to and has used this
                                       period
    retrieval [org_id]             -> is retrieval finding anything? searches, how many
                                       came back empty, and the miss rate, per org. A
                                       miss rate that stays high while the corpus grows
                                       is the churn about to happen. No query text is
                                       stored -- three integers per org per month
    revenue                        -> orgs on billable plans and what they consumed
    plan-experiment <org_id> [detect] [occasions]
                                   -> what holdout rate this org's OWN volume can answer
                                       with. Run before start-experiment: at a 10% holdout
                                       only one occasion in ten lands in the control arm,
                                       so a run answers ~10x slower than it looks
    start-experiment <org_id> [rate]
                                   -> begin a randomized holdout: withhold [rate] of
                                       eligible memory injections (default 0.2) so the
                                       fleet generates its own control arm. The only
                                       design here that supports a CAUSAL claim
    stop-experiment <org_id>       -> stop withholding. Observations are kept
    experiment <org_id>            -> what the holdout established, per trace: effect,
                                       95% CI, p-value, and an explicit UNDERPOWERED
                                       verdict so "cannot answer yet" never reads as
                                       "no effect"
    outcomes [org_id]              -> is the product working? before/after comparison of
                                       each fleet's recorded outcomes (resolution,
                                       repeated-error, escalation, frustration rates)
                                       with CIs, a multiple-comparisons correction, and
                                       a minimum detectable effect on every null result.
                                       Observed change, never a causal claim
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
import uuid
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from commontrace import experiment
from hub import audit, auth, commons, crud, outcomes, plans
from hub.config import HubConfig
from hub.db import make_engine, make_session_factory, session_scope
from hub.models import (
    ApiKey,
    AuditLogEntry,
    KnowledgeBaseSubmission,
    Organization,
    Trace,
    TraceRelation,
    UsageCounter,
    Vote,
)


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


def _parse_review_after(raw: object) -> datetime | None:
    """A seed file's `review_after` as a tz-aware datetime, or None if it is
    not an ISO 8601 date or timestamp.

    `datetime.fromisoformat` only learned to accept a trailing "Z" in 3.11,
    and this package supports 3.10 -- so the most natural way to write a UTC
    timestamp would be rejected on exactly the older interpreter where the
    failure is least expected. Normalized rather than documented around.
    A value with no timezone is read as UTC, matching every other timestamp
    in this schema.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def commons_seed(path: str, org_id: str, session_factory=None) -> bool:
    """Load or update the CommonTrace Knowledge Base from a JSONL file of
    curated substrate knowledge.

    This is the bulk-load path -- for individual community submissions, see
    `approve-submission`, which writes the same `commons_source='seed'`
    column through a different operator-run path (crud.review_kb_submission)
    after a customer proposes one via `submit_kb_entry`. Both are
    operator-run and both are the only two ways this column is ever set:
    there is no customer-facing tool that can write it directly, or that
    can publish a submission without an operator's own review-submission
    action deciding to. See hub/plans.py "why there is no org-to-org
    sharing here" for why that is a deliberate absence, not a gap: a
    customer's own trace should never become visible to another customer
    without a human at the operator judging it substrate knowledge first.

    Re-runnable: run it again after editing the source file to add new
    entries (existing ones are not deduplicated against by content, so
    editing in place and re-running will create fresh rows for unchanged
    lines too -- track what has already been loaded in the source file
    itself, or purge and reload for now).

    Each JSONL line: {"title", "context_text", "solution_text", "tags"?,
    "agent_type"?, "source"?, "review_after"?}. `source` should cite where
    the knowledge came from (a public postmortem, a vendor changelog) and
    is stored as the trace's shared_rationale so provenance survives.

    `review_after` is an ISO 8601 date or timestamp ("2027-06-01") after
    which the entry needs re-confirming, and it is how version-pinned
    substrate knowledge declares its own expiry at authoring time --
    "React 19 hydrates Date differently than 18" is true until it is not,
    and the moment to decide how long that is likely to hold is while
    writing it. Omit it for knowledge that does not expire, which is most
    of it. Past the date the entry shows up in `kb-review`; nothing is
    hidden or unpublished automatically. An unparseable value is a skipped
    line, not a silently ignored field -- a horizon that was meant to be
    set and quietly was not is worse than no horizon at all, because the
    operator believes the entry is being watched.

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
        if rec.get("review_after"):
            parsed = _parse_review_after(rec["review_after"])
            if parsed is None:
                bad += 1
                print(
                    f"  line {i}: review_after={rec['review_after']!r} is not an ISO 8601 "
                    "date or timestamp, skipped",
                    file=sys.stderr,
                )
                continue
            rec["review_after"] = parsed
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
                commons_review_after=rec.get("review_after"),
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

    This is a CONTENT QUALITY report, not a network-effect metric: how big
    is the corpus, how often does it actually cover a real recurring
    failure (Trace.commons_hits, incremented only by the conservative
    commons_overlap threshold), which entries are pulling weight, and which
    have never once matched anything and are candidates to revise or prune.
    Distinct customer orgs that have ever queried it is one adoption number
    worth watching -- readership, not authorship.

    Authorship has its own number now: the community-submission funnel
    (pending / approved / rejected -- see hub/models.py:
    KnowledgeBaseSubmission). Every accepted submission became an entry
    counted above; this section is what tells an operator whether the
    review queue itself needs attention, separately from whether its
    output is any good.
    """
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        entries = (
            await session.execute(
                select(Trace).where(*crud.commons_visible()).order_by(Trace.commons_hits.desc())
            )
        ).scalars().all()
        n_retracted = (
            await session.execute(
                select(func.count())
                .select_from(Trace)
                .where(
                    Trace.shared_with_commons.is_(True),
                    Trace.commons_source == "seed",
                    Trace.commons_retracted_at.isnot(None),
                )
            )
        ).scalar_one()
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
        submission_counts = dict(
            (
                await session.execute(
                    select(KnowledgeBaseSubmission.status, func.count())
                    .group_by(KnowledgeBaseSubmission.status)
                )
            ).all()
        )
        n_submitting_orgs = (
            await session.execute(
                select(func.count(func.distinct(KnowledgeBaseSubmission.org_id)))
            )
        ).scalar_one()
        # The true count behind ALL FOUR of kb-review's buckets (urgent,
        # disputed, stale, never_hit) -- not just the two (disputed/stale)
        # `standing_of()` alone can report. Recomputing "needs review" from
        # standings the way this used to would silently omit security-
        # flagged entries and never-hit ones, exactly the "capped/partial
        # result read as a total" defect hub/admin.py's overview/KB tiles
        # had before their own fix (crud.count_kb_review_queue exists
        # specifically so a summary number like this one can't disagree
        # with what `kb-review` actually lists).
        queue_total = await crud.count_kb_review_queue(session)

    if submission_counts:
        pending = submission_counts.get("pending", 0)
        approved = submission_counts.get("approved", 0)
        rejected = submission_counts.get("rejected", 0)
        decided = approved + rejected
        print("community submissions:")
        print(f"  pending review:  {pending}")
        print(f"  approved:        {approved}"
              + (f"  ({approved / decided:.0%} of reviewed)" if decided else ""))
        print(f"  rejected:        {rejected}")
        print(f"  submitting orgs: {n_submitting_orgs}")
        if pending:
            print(f"\n  `list-submissions pending` to review the {pending} waiting.")
        print()

    if not entries:
        print("The Knowledge Base has no entries yet. `commons-seed <file.jsonl> "
              "<operator_org_id>` loads one.")
        return

    total_hits = sum(t.commons_hits for t in entries)
    zero_hit = [t for t in entries if t.commons_hits == 0]
    now = datetime.now(timezone.utc)
    standings = Counter(crud.standing_of(t, now) for t in entries)

    print(f"knowledge base entries:  {len(entries)}")
    print(f"total hits delivered:    {total_hits}   (times an entry covered a real "
          "recurring failure, at the conservative threshold)")
    print(f"queried by:              {n_queriers} of {n_orgs_total} org(s) (ever, any period)")
    print(f"never matched anything:  {len(zero_hit)} of {len(entries)} entries")

    # The maintenance half of the same question. Corpus size says how much
    # was written; this says how much of it the field still stands behind.
    print("\nstanding (hub/commons.py:entry_standing):")
    for name in commons.VALID_STANDINGS:
        print(f"  {name + ':':<14} {standings.get(name, 0)}")
    if n_retracted:
        print(f"  {'retracted:':<14} {n_retracted}   (withdrawn by an operator, not served)")

    if queue_total:
        print(f"\n  `kb-review` lists the {queue_total} entry(ies) needing a decision.")

    if total_hits == 0:
        print(
            "\nNo entry has covered anyone's failure yet. Either nobody has queried the "
            "Knowledge Base, or its content does not yet overlap what fleets are hitting."
        )
        return

    print("\ntop entries by hits:")
    for trace in entries[:10]:
        if trace.commons_hits == 0:
            break
        print(f"  {trace.commons_hits:>4}  {trace.title[:70]}")

    if len(zero_hit) / len(entries) > 0.5:
        print(
            f"\nOver half the corpus ({len(zero_hit)}/{len(entries)}) has never matched a "
            "real query. Either these entries describe failures fleets are not actually "
            "hitting, or they are worded differently from how fleets describe them -- see "
            "commons/eval/RESULTS.md on lexical matching's recall limits."
        )


DEFAULT_HOLDOUT_RATE = 0.2


async def _observed_volume_and_baseline(session, org_id: str) -> tuple[int, float | None]:
    """This org's own monthly retrieval volume and success rate.

    Both read from org-scoped functions that already exist, because the whole
    point of planning ON THE HUB rather than on paper is that the Hub knows
    the numbers. A local `--plan` has to be told how many occasions to expect;
    here the fleet's own search volume is the estimate.

    Returns (searches this period, resolution rate or None). The rate is None
    when the org has recorded too few outcomes to read one, and the caller
    falls back to 0.5 -- where the variance peaks, so a plan built on no data
    cannot understate the sample.
    """
    health = await crud.search_health(session, org_id)
    searches = int(health.get("searches") or 0)

    baseline = None
    report = await crud.fleet_outcomes(session, org_id)
    for metric in report.get("metrics") or []:
        if metric.get("metric") == "resolution_rate":
            current = metric.get("current") or {}
            if current.get("rate") is not None and int(current.get("n") or 0) >= 20:
                baseline = float(current["rate"])
            break
    return searches, baseline


async def plan_experiment(
    org_id: str, detect: str = "0.10", occasions: str = "", session_factory=None
) -> bool:
    """What holdout rate can this org's volume actually answer with?

    Run BEFORE `start-experiment`. The failure it prevents is the expensive,
    silent one: an operator picks a rate, the fleet runs for a month, and the
    report says "not enough data yet". The occasions are spent, the window is
    gone, and the only fix had to be applied at the start.

    The arithmetic nobody does in their head: at a 10% holdout only one
    occasion in ten lands in the control arm, so a run reaches an answer about
    TEN TIMES slower than its occasion count suggests.
    """
    session_factory = session_factory or _default_session_factory()
    try:
        effect = float(detect)
    except (TypeError, ValueError):
        print(f"error: detect must be a number between 0 and 1, got {detect!r}", file=sys.stderr)
        return False
    if not 0 < effect < 1:
        print(f"error: detect must be strictly between 0 and 1, got {effect}", file=sys.stderr)
        return False

    async with session_scope(session_factory) as session:
        org = await session.get(Organization, org_id)
        if org is None:
            print(f"error: no such organization: {org_id}", file=sys.stderr)
            return False
        searches, observed = await _observed_volume_and_baseline(session, org_id)
        current_rate = org.holdout_rate or DEFAULT_HOLDOUT_RATE

    budget = None
    if occasions:
        try:
            budget = int(occasions)
        except (TypeError, ValueError):
            print(f"error: occasions must be a whole number, got {occasions!r}", file=sys.stderr)
            return False
    elif searches:
        budget = searches

    baseline = observed if observed is not None else 0.5
    design = experiment.plan(
        effect=effect, baseline=baseline, rate=current_rate, occasions_budget=budget
    )
    print(experiment.render_plan(design))
    print()
    print(
        f"_Baseline {baseline:.0%} "
        + ("from this org's own recorded outcomes._" if observed is not None
           else "assumed: too few recorded outcomes to read one. 50% is the most "
                "pessimistic, so this will not understate the sample._")
    )
    if budget and not occasions:
        print(f"_Budget {budget:,} from this org's searches this period. Pass an explicit "
              "count to plan a different window._")
    elif not budget:
        print("_No occasion budget: this org has not searched yet this period. Pass a "
              "count to size a window._")
    return design.verdict != "infeasible"


async def start_experiment(org_id: str, rate: str = str(DEFAULT_HOLDOUT_RATE), session_factory=None) -> bool:
    """Begin a randomized holdout for one org: withhold `rate` of eligible
    memory injections so the fleet generates its own control arm.

    This is the falsifier STRATEGY.md §13.2 calls "the cheapest in the
    document" and says to run first, and until now it could only be run
    against a local file store -- not against the Hub, which is the
    surface paying customers are actually on.

    A fresh salt is generated per experiment and never edited afterwards.
    Changing a salt mid-flight reshuffles every assignment, which silently
    mixes two randomizations into one comparison and produces a result
    that looks like ordinary noise rather than like a broken experiment --
    so restarting deliberately starts a NEW experiment rather than
    extending the old one, and `experiment` reports only the current salt's
    observations.

    Rate is a real trade and worth stating: withholding memory from a
    fraction of occasions means those occasions get a worse product on
    purpose. That is the price of knowing whether the product works at
    all, it is bounded by this number, and it should be a decision someone
    makes rather than a default nobody chose.
    """
    session_factory = session_factory or _default_session_factory()
    try:
        value = float(rate)
    except (TypeError, ValueError):
        print(f"error: rate must be a number between 0 and 1, got {rate!r}", file=sys.stderr)
        return False
    if not 0 < value < 1:
        print(
            f"error: rate must be strictly between 0 and 1, got {value}. "
            "0 withholds nothing (no control arm); 1 withholds everything (no treatment arm).",
            file=sys.stderr,
        )
        return False

    async with session_scope(session_factory) as session:
        org = await session.get(Organization, org_id)
        if org is None:
            print(f"error: no such organization: {org_id}", file=sys.stderr)
            return False
        previous = org.holdout_salt
        org.holdout_rate = value
        org.holdout_salt = uuid.uuid4().hex[:16]
        await session.flush()
        await audit.record(
            session, actor=audit.ACTOR_OPERATOR_CLI, action="start_experiment",
            org_id=org_id, target_type="org", target_id=org_id,
            summary=f"rate={value} salt={org.holdout_salt} previous_salt={previous or '-'}",
        )
        salt = org.holdout_salt

    print(f"experiment started for {org_id}")
    print(f"  holdout rate: {value:.0%} of eligible injections will be withheld")
    print(f"  salt:         {salt}")
    if previous:
        print(f"  NOTE: this replaces experiment {previous}. Its observations are kept but")
        print("        are no longer pooled -- they came from a different randomization.")
    async with session_scope(session_factory) as session:
        searches, observed = await _observed_volume_and_baseline(session, org_id)
    if searches:
        design = experiment.plan(
            effect=experiment.DEFAULT_PRACTICAL_EFFECT,
            baseline=observed if observed is not None else 0.5,
            rate=value, occasions_budget=searches,
        )
        # Said HERE, at the only moment the rate can still be changed for
        # free. An operator who learns this from the report a month later has
        # spent the window, and the fix was always a one-line decision taken
        # now.
        if design.verdict == "infeasible":
            print(f"  WARNING: at this org's observed {searches:,} search(es) per period, NO")
            print(f"           rate answers a {experiment.DEFAULT_PRACTICAL_EFFECT:.0%} effect"
                  f" -- it needs {design.n_per_arm:,} per arm.")
            print("           Run for longer, or accept a larger effect as the thing tested.")
        elif design.verdict == "raise_rate":
            print(f"  WARNING: {value:.0%} is too low for this org's observed {searches:,}")
            print(f"           search(es) per period. A {experiment.DEFAULT_PRACTICAL_EFFECT:.0%}"
                  f" effect needs a rate of {design.rate_for_budget:.0%}.")
            print(f"           `python -m hub.manage plan-experiment {org_id}` shows the working.")
    print("  The fleet's agents must call holdout_assign(...) before injecting, and")
    print("  record_occasion_outcome(...) afterwards, or nothing is measured.")
    print(f"  `python -m hub.manage experiment {org_id}` reads the result.")
    return True


async def stop_experiment(org_id: str, session_factory=None) -> bool:
    """End the holdout. Observations are kept; nothing further is withheld.

    Deliberately does not clear the salt: `experiment` still needs it to
    scope the analysis to the observations that experiment produced.
    """
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        org = await session.get(Organization, org_id)
        if org is None:
            print(f"error: no such organization: {org_id}", file=sys.stderr)
            return False
        if org.holdout_rate <= 0:
            print(f"No experiment is running for {org_id}.", file=sys.stderr)
            return False
        org.holdout_rate = 0.0
        await session.flush()
        await audit.record(
            session, actor=audit.ACTOR_OPERATOR_CLI, action="stop_experiment",
            org_id=org_id, target_type="org", target_id=org_id,
            summary=f"salt={org.holdout_salt}",
        )
    print(f"experiment stopped for {org_id}. Nothing further will be withheld.")
    print(f"  Observations are kept. `python -m hub.manage experiment {org_id}` still reads them.")
    return True


_EFFECT_MARK = {
    "HELPS": "HELPS      ",
    "HURTS": "HURTS      ",
    "NO_MEASURABLE_EFFECT": "no effect  ",
    "UNDERPOWERED": "not yet    ",
}


async def experiment_results(org_id: str, session_factory=None) -> bool:
    """What the randomized holdout has established, per trace.

    The only causal report in this system. `outcomes` compares a fleet
    against its own past and cannot rule out anything else that changed;
    this compares two arms of the same fleet in the same window.
    """
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        org = await session.get(Organization, org_id)
        if org is None:
            print(f"error: no such organization: {org_id}", file=sys.stderr)
            return False
        report = await crud.causal_effects(session, org_id)

    state = "running" if report["experiment_running"] else "stopped"
    print(f"{org.name}  ({org_id})   experiment {state}, "
          f"holdout rate {report['holdout_rate']:.0%}")
    print(f"  {report['n_observations']} resolved observation(s) across "
          f"{report['n_occasions']} occasion(s)")
    if not report["effects"]:
        print("\n  Nothing measured yet. Agents must call holdout_assign(...) before")
        print("  injecting and record_occasion_outcome(...) afterwards.")
        return True

    print()
    for e in report["effects"]:
        print(f"  {_EFFECT_MARK.get(e['verdict'], e['verdict']):<12} {e['title'][:52]}")
        print(f"      injected {e['rate_injected']:.0%} (n={e['n_injected']})  vs  "
              f"withheld {e['rate_withheld']:.0%} (n={e['n_withheld']})   "
              f"effect {e['effect']:+.1%}")
        if e["verdict"] in ("HELPS", "HURTS"):
            print(f"      95% CI [{e['ci_95'][0]:+.1%}, {e['ci_95'][1]:+.1%}]  p={e['p_value']:.4f}")
        if e["note"]:
            print(f"      {e['note']}")
    print(f"\n  {report['note']}")
    return True


_REVIEW_BUCKET_HEADINGS = {
    "urgent": "SECURITY-FLAGGED -- read these first",
    "disputed": "DISPUTED -- the fleets that tried these say they did not work",
    "stale": "PAST REVIEW DATE -- nobody has confirmed these are still true",
    "never_hit": "NEVER MATCHED -- content nobody needs, or worded so nobody finds it",
}


async def kb_review(limit: str = "50", session_factory=None) -> bool:
    """The Knowledge Base maintenance queue: which entries need a human,
    worst first (crud.kb_review_queue).

    This is the command that makes operator curation scale. The obvious
    objection to a corpus one party maintains is that reviewing it costs
    O(entries), so the model dies somewhere past a few thousand. It only
    dies if finding the bad entries is the expensive part -- and it is not,
    because every query and every vote already localizes them. What this
    prints is that exhaust, sorted by how much traffic each problem is
    actually affecting, so review cost tracks the error rate instead of the
    corpus size.

    Nothing here is automatic. Every line is a suggestion to a human who
    then runs `kb-retract`, edits the source file and re-seeds, or decides
    the entry is fine after all -- see hub/commons.py's "votes inform, the
    operator decides".
    """
    session_factory = session_factory or _default_session_factory()
    try:
        n = int(limit)
    except (TypeError, ValueError):
        print(f"error: limit must be an integer, got {limit!r}", file=sys.stderr)
        return False

    async with session_scope(session_factory) as session:
        queue = await crud.kb_review_queue(session, limit=n)

    if not queue:
        print("Nothing in the Knowledge Base needs review.")
        print("  (No entry is security-flagged, disputed, past its review date, or unmatched.)")
        return True

    current = None
    for item in queue:
        if item["bucket"] != current:
            current = item["bucket"]
            print(f"\n{_REVIEW_BUCKET_HEADINGS[current]}")
        print(f"  {item['id']}  {item['title'][:60]}")
        print(f"      {item['why']}  --  {item['commons_hits']} hit(s) delivered")

    print(f"\n{len(queue)} entry(ies) listed.")
    print("  `kb-retract <trace_id> \"<reason>\"` withdraws one. Reversible with `kb-restore`.")
    return True


async def kb_retract(trace_id: str, reason: str = "", session_factory=None) -> bool:
    """Withdraw one entry from the Knowledge Base.

    No confirmation prompt, unlike `purge-trace`/`purge-org`. That is not
    an inconsistency: those destroy data irreversibly, this one sets a
    timestamp and is undone by `kb-restore`. Putting a prompt in front of a
    reversible action trains operators to type y without reading, which is
    what makes the prompt in front of the irreversible one worthless.
    """
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        entry = await crud.retract_kb_entry(session, trace_id, reason=reason)

    if entry is None:
        print(
            f"error: {trace_id} is not a live Knowledge Base entry "
            "(already retracted, not seeded content, or no such trace).",
            file=sys.stderr,
        )
        return False
    print(f"retracted: {entry['title'][:70]}")
    print(f"  It had collected {entry['vote_count']} vote(s) of feedback before withdrawal.")
    print(f"  reason: {entry['retraction_reason'] or '(none given)'}")
    print("  No longer returned by commons_overlap, commons_search, or vote_trace.")
    print(f"  `kb-restore {entry['id']}` puts it back.")
    return True


async def kb_restore(trace_id: str, session_factory=None) -> bool:
    """Put a retracted entry back into the Knowledge Base."""
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        entry = await crud.restore_kb_entry(session, trace_id)

    if entry is None:
        print(
            f"error: {trace_id} is not a retracted Knowledge Base entry.",
            file=sys.stderr,
        )
        return False
    print(f"restored: {entry['title'][:70]}")
    print(f"  standing: {entry['standing']}  ({entry['vote_count']} vote(s), trust {entry['trust']:.2f})")
    return True


async def list_submissions(status: str | None = None, session_factory=None) -> bool | None:
    """The community-submission review queue, or full history if no status
    is given. `status`, when passed, must be 'pending', 'approved', or
    'rejected' (crud.review_kb_submission's own vocabulary).

    Returns False (not just prints an error) on an unrecognized status --
    main()'s `if result is False: return 2` is what turns that into a
    nonzero exit code for `list-submissions <bad-status>`; the previous
    `-> None` annotation didn't just under-describe this, it actively
    misdescribed the function's real, load-bearing contract to anyone
    reading the signature -- a future edit that "fixed" the return
    statement to match the stated `-> None` would have silently turned a
    real operator-facing error back into a reported exit code of 0.
    """
    session_factory = session_factory or _default_session_factory()
    if status is not None and status not in ("pending", "approved", "rejected"):
        print(f"error: status must be one of pending/approved/rejected, got {status!r}", file=sys.stderr)
        return False
    async with session_scope(session_factory) as session:
        rows = await crud.list_kb_submissions(session, status=status, limit=200)

    if not rows:
        print("no submissions" + (f" with status={status}" if status else ""))
        return None

    for s in rows:
        print(f"{s['id']}  status={s['status']}  submitted={s['created_at']}")
        print(f"    title: {s['title']!r}")
        if s["rationale"]:
            print(f"    rationale: {s['rationale']!r}")
        if s["status"] != "pending":
            print(f"    reviewed_by={s['reviewed_by']} at={s['reviewed_at']}")
            if s["status"] == "approved":
                print(f"    -> trace={s['resulting_trace_id']}  credit_awarded={s['credit_awarded']}")
            else:
                print(f"    reason: {s['rejection_reason']!r}")
    return None


async def approve_submission(
    submission_id: str, operator_org_id: str, credit: str | None = None, session_factory=None
) -> bool:
    """Accept a pending submission: publishes it as a new Knowledge Base
    entry owned by `operator_org_id` (never the submitting org -- same
    ownership rule as commons_seed) and permanently raises the submitting
    org's Knowledge Base query allowance. See
    hub/crud.py:review_kb_submission for the full contract."""
    session_factory = session_factory or _default_session_factory()
    credit_int = plans.SUBMISSION_ACCEPTANCE_CREDIT if credit is None else int(credit)
    async with session_scope(session_factory) as session:
        org = await session.get(Organization, operator_org_id)
        if org is None:
            print(f"error: no such organization: {operator_org_id}", file=sys.stderr)
            return False
        result = await crud.review_kb_submission(
            session, submission_id, "approve", operator_org_id,
            reviewer=audit.ACTOR_OPERATOR_CLI, credit=credit_int,
        )
    if result is None:
        print(f"error: no PENDING submission with id: {submission_id}", file=sys.stderr)
        return False
    print(f"approved {submission_id} -> new Knowledge Base entry {result['resulting_trace_id']}")
    # `result['credit_awarded']`, not the local `credit_int`: review_kb_submission
    # clamps the credit to [0, 2**63-1] before writing it, so a negative or
    # absurdly large --credit is silently bounded in the database while this
    # local variable still holds the raw, unclamped value the operator typed
    # -- printing that back would misdescribe what the write actually did.
    print(f"  credited {result['credit_awarded']} bonus Knowledge Base queries to the submitting org")
    return True


async def reject_submission(submission_id: str, reason: str = "", session_factory=None) -> bool:
    """Decline a pending submission. No entry is created and no credit is
    awarded -- exactly the outcome hub/plans.py "why bonus_commons_queries
    is not the same mistake twice" describes as the whole point."""
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        result = await crud.review_kb_submission(
            session, submission_id, "reject", operator_org_id="", reviewer=audit.ACTOR_OPERATOR_CLI,
            rejection_reason=reason,
        )
    if result is None:
        print(f"error: no PENDING submission with id: {submission_id}", file=sys.stderr)
        return False
    print(f"rejected {submission_id}")
    return True


_VERDICT_MARK = {
    outcomes.VERDICT_IMPROVED: "improved  ",
    outcomes.VERDICT_WORSENED: "WORSENED  ",
    outcomes.VERDICT_INCONCLUSIVE: "no change ",
    outcomes.VERDICT_INSUFFICIENT: "no data   ",
}


def _print_outcome_report(report: dict, indent: str = "") -> None:
    print(f"{indent}{report['headline']}")
    print(
        f"{indent}  {report['n_baseline_traces']} baseline trace(s), "
        f"{report['n_current_traces']} since."
    )
    for row in report["metrics"]:
        b, c = row["baseline"], row["current"]
        line = f"{indent}  {_VERDICT_MARK[row['verdict']]} {row['metric']:<20}"
        if b["rate"] is None or c["rate"] is None:
            print(f"{line} (no recorded values)")
            continue
        line += f" {b['rate']:.1%} (n={b['n']}) -> {c['rate']:.1%} (n={c['n']})"
        if row["delta"] is not None:
            line += f"  {row['delta']:+.1%}"
        if row["ci_95"]:
            line += f"  CI [{row['ci_95'][0]:+.1%}, {row['ci_95'][1]:+.1%}]"
        if row["p_value"] is not None:
            line += f"  p={row['p_value']:.3f}"
        print(line)
        if row["note"]:
            print(f"{indent}      {row['note']}")
    for row in report["cost"]:
        b, c = row["baseline"], row["current"]
        if b["value"] is None and c["value"] is None:
            continue
        b_txt = f"{b['value']:,.0f}" if b["value"] is not None else "-"
        c_txt = f"{c['value']:,.0f}" if c["value"] is not None else "-"
        print(f"{indent}  (mean)     {row['metric']:<20} {b_txt} -> {c_txt}")


async def fleet_outcomes(org_id: str | None = None, session_factory=None) -> bool:
    """Is the product actually working, per customer?

    With an org_id, the full before/after report for that fleet. Without
    one, a roll-up across every org -- which is the closest thing this
    system has to a churn dashboard, and a materially better one than
    `usage`/`revenue`. Those report consumption, which is a lagging
    indicator that looks healthy right up to the renewal a customer
    declines; this reports whether the thing they are paying for is moving
    their numbers, which is the leading one.

    Three things this deliberately does NOT do, because each would make the
    report more flattering and less true:

    * It does not describe any of this as caused by CommonTrace. See
      hub/outcomes.py's OBSERVATIONAL_CAVEAT, printed with every run.
    * It does not hide fleets that got worse, or sort them below the ones
      that improved. A `WORSENED` line is the most valuable line here.
    * It does not correct for multiple comparisons ACROSS orgs, and says
      so below rather than papering over it. Each org's report is
      internally corrected across its own four metrics; scanning fifty
      customers and quoting whichever three came back significant is a
      further multiple-comparisons problem that no correction inside a
      single report can fix for you.
    """
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        if org_id:
            org = await session.get(Organization, org_id)
            if org is None:
                print(f"error: no such organization: {org_id}", file=sys.stderr)
                return False
            targets = [(org.id, org.name)]
        else:
            targets = [
                (row.id, row.name)
                for row in (
                    await session.execute(select(Organization).order_by(Organization.name))
                ).scalars().all()
            ]
        reports = [
            (name, await crud.fleet_outcomes(session, oid)) for oid, name in targets
        ]

    if not reports:
        print("No organizations yet.")
        return True

    print(f"{outcomes.OBSERVATIONAL_CAVEAT}\n")

    measurable = [(n, r) for n, r in reports if r["n_baseline_traces"]]
    for name, report in reports:
        print(f"{name}  ({report['org_id']})")
        _print_outcome_report(report, indent="  ")
        print()

    if not org_id:
        worsened = [
            n for n, r in measurable
            if any(m["verdict"] == outcomes.VERDICT_WORSENED for m in r["metrics"])
        ]
        improved = [
            n for n, r in measurable
            if any(m["verdict"] == outcomes.VERDICT_IMPROVED for m in r["metrics"])
            and n not in worsened
        ]
        print(f"{len(measurable)} of {len(reports)} org(s) have a baseline window to compare against.")
        if worsened:
            print(f"  moved backwards on something:  {', '.join(worsened)}")
        if improved:
            print(f"  improved on something:         {', '.join(improved)}")
        if len(measurable) > 1:
            print(
                "\n  Reading several orgs at once is itself a multiple-comparisons\n"
                "  problem: each report is corrected across its own four metrics, and\n"
                "  nothing here corrects across orgs. At alpha=0.05, roughly one org in\n"
                "  twenty will show a 'significant' metric by chance alone."
            )
    return True


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


async def purge_trace(trace_id: str, session_factory=None) -> bool:
    """Permanently deletes one trace AND every trace in its amendment chain
    (see crud.amendment_chain -- shared with the self-service delete_trace
    MCP tool, which walks the identical lineage at a lower trust level).
    Votes and trace_relations rows keyed by trace_id cascade automatically
    (FK ondelete=CASCADE, hub/models.py); a relation row where a chain
    member is the *target* (related_trace_id) is not covered by that FK --
    related_trace_id is a plain column, not a foreign key, so it survives
    the source trace being deleted elsewhere. Clean it up explicitly here
    rather than leave a dangling reference behind."""
    session_factory = session_factory or _default_session_factory()
    async with session_scope(session_factory) as session:
        trace = await session.get(Trace, trace_id)
        if trace is None:
            print(f"error: no such trace: {trace_id}", file=sys.stderr)
            return False
        chain_ids = await crud.amendment_chain(session, trace_id)
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


async def retrieval(org_id: str | None = None, session_factory=None) -> bool:
    """How often each org's searches come back with nothing, this period.

    The live version of `hub/bench_retrieval.py`. The benchmark answers
    "does retrieval work" on a 46-record synthetic corpus with probes the
    same author wrote; this answers it on the fleet's own corpus with the
    fleet's own queries, which is the only version that settles STRATEGY.md
    §13.2's link 2 for a real customer.

    This exists because the defect `hub/search.py` documents -- every
    natural-language query returning nothing at all -- was invisible from
    the operator's side for the entire life of a deployment that had it. A
    search matching nothing returns HTTP 200 with an empty list, and
    `Trace.retrievals` counts rows RETURNED, so it incremented nothing and
    left no record of having been asked. A broken retrieval tier and a
    customer who has not stored much yet produced identical telemetry.

    Read it as a leading indicator, not a verdict. A high miss rate in an
    org's first week is what an almost-empty corpus looks like and is fine;
    a high miss rate that does not fall as `traces` grows is the shape that
    means the customer is asking questions this product cannot answer --
    which is the churn about to happen, visible while there is still time.
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
            return not org_id
        rows = [await crud.search_health(session, o.id) for o in orgs]

    print(f"billing period {rows[0]['period']} (UTC)")
    print(f"{'organization':<26} {'traces':>9} {'searches':>10} {'no match':>10} "
          f"{'miss rate':>11} {'unsearchable':>13}")
    print("-" * 84)
    for org, r in zip(orgs, rows):
        rate = "--" if r["miss_rate"] is None else f"{r['miss_rate']:.0%}"
        print(f"{org.name[:25]:<26} {r['traces']:>9,} {r['searches_with_terms']:>10,} "
              f"{r['empty']:>10,} {rate:>11} {r['no_terms']:>13,}")
    print()
    print("'searches' counts text searches that had at least one searchable term, first")
    print("page only -- paging through one result set is one act of retrieval, not several.")
    print("'no match' is how many of those returned nothing, and 'miss rate' is their ratio.")
    print("'unsearchable' is queries that reduced to no terms at all (empty, or only")
    print("stopwords); those are malformed requests, not retrieval misses, so they are")
    print("counted apart rather than folded in where they could mask a real problem.")
    print()
    print("No query text is stored anywhere. These are three integers per org per month;")
    print("a log of what a customer's agents were struggling with, in their own words,")
    print("would answer this no better and create exactly the retention liability")
    print("DATA_RETENTION.md exists to avoid.")
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
    "kb-review": (kb_review, 0, 1),
    "kb-retract": (kb_retract, 1, 2),
    "kb-restore": (kb_restore, 1, 1),
    "list-submissions": (list_submissions, 0, 1),
    "approve-submission": (approve_submission, 2, 3),
    "reject-submission": (reject_submission, 1, 2),
    "set-plan": (set_plan, 2, 2),
    "usage": (usage, 0, 1),
    "retrieval": (retrieval, 0, 1),
    "revenue": (revenue, 0, 0),
    "outcomes": (fleet_outcomes, 0, 1),
    "plan-experiment": (plan_experiment, 1, 3),
    "start-experiment": (start_experiment, 1, 2),
    "stop-experiment": (stop_experiment, 1, 1),
    "experiment": (experiment_results, 1, 1),
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
    try:
        reply = input(f"This will {action.upper()}. This cannot be undone. Type 'yes' to continue: ")
    except EOFError:
        # Ctrl-D at the prompt -- an entirely ordinary way to bail out of an
        # interactive confirmation, not an error condition. Left uncaught,
        # `input()` raising here reached the caller as a raw traceback
        # (reproduced: stdin hitting EOF at an interactive tty prompt raises
        # EOFError), which is exactly the "a traceback here reads as 'the
        # tool is broken'" failure this module's own confirmation prompts
        # exist to avoid -- and nothing destructive has happened yet at this
        # point, so treating it as "no" is safe.
        return False
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
