"""Age-based deletion, the things that outrank it, and a plan you read first.

WHY THIS EXISTS
---------------
The Hub deleted data in exactly two shapes: one trace, or one whole
organization. Both are manual, immediate and irreversible, which leaves the
question a data-protection reviewer actually asks -- "what happens to a
trace nobody touches for three years?" -- with the answer "nothing, forever".
Indefinite retention is not a policy; it is the absence of one, and it is
the shape that turns an ordinary breach into a breach of everything the
product has ever seen.

So: per-org policies that delete by OBJECT TYPE and STATUS after an age, a
plan you can read before anything happens, and two classes of thing that
outrank the policy.

WHY A PLAN IS A FIRST-CLASS OBJECT
----------------------------------
A purge is the one operation whose result cannot be inspected afterwards --
the evidence that it did the wrong thing is what it deleted. So the plan is
computed, rendered and digested first, and `apply` takes that digest back.
If the store moved in between (a trace was amended, an experiment started, a
hold was placed), the digest no longer matches and the apply is REFUSED
rather than silently deleting a set the operator never saw. Same discipline
as release.py's stale-base rejection, for the same reason: an operator who
approved one set of consequences must not get a different one.

`apply` is never the default and never implicit. There is no flag that
plans and deletes in one step.

WHAT OUTRANKS A RETENTION POLICY
--------------------------------
**A legal hold.** Litigation, an investigation or a regulator's notice
freezes data regardless of age, and a hold that merely *skipped* the rows
would be worse than none: the operator would read "purge complete" and
believe data was gone that is not. Held rows are counted and named in the
plan, so the output distinguishes "deleted" from "would have been, but is
frozen".

**A running experiment.** This one is specific to what this product is.
Holdout observations are the arms of a randomized trial. Deleting some of
them mid-run is not data hygiene -- it is differential attrition, and it
biases the causal estimate in whichever direction the deletion happened to
correlate with. Worse, it does so invisibly: the analysis sees a smaller,
apparently clean dataset. So while an org's holdout is running, its
observations are not purgeable at all, by any policy, and the plan says why.

A FLOOR NOBODY CAN CONFIGURE BELOW
----------------------------------
Each object type carries a minimum. The audit log's exists because the audit
log is what proves the purges happened -- a policy that deletes it a week
later would let an operator erase data and the record of having erased it,
in two steps that each look legitimate. The floor is a product decision, not
a customer setting, and configuring below it is refused at write time rather
than silently clamped: an operator who asked for 7 days and got 365 should
be told, not left believing the store honoured a number it did not.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select

from hub import audit, events
from hub.models import (
    AuditLogEntry,
    HoldoutObservation,
    KnowledgeBaseSubmission,
    LegalHold,
    Organization,
    RetentionPolicy,
    Trace,
    Vote,
)

_DIGEST_DOMAIN = "commontrace-retention-plan-v1"
_FIELD_SEP = "\x1f"

#: Deleting every row of a type regardless of state. Not a magic empty
#: string: a policy row whose status silently meant "everything" is the kind
#: of default that deletes a quarantined trace an operator was still
#: reviewing.
STATUS_ANY = "any"

BLOCKED_EXPERIMENT_RUNNING = (
    "the org's randomized holdout is running -- deleting arm assignments "
    "mid-experiment is differential attrition, not data hygiene"
)


class RetentionError(Exception):
    """A retention policy or purge could not be honoured as asked."""


class StalePlanError(RetentionError):
    """The store moved between planning and applying.

    Carries both digests so the caller can say what changed rather than
    only that something did.
    """

    def __init__(self, planned: str, current: str) -> None:
        super().__init__(
            "the store changed since this plan was computed, so nothing was "
            f"deleted (planned {planned[:12]}, now {current[:12]}). Re-plan "
            "and read the new plan before applying it."
        )
        self.planned = planned
        self.current = current


@dataclass(frozen=True)
class ObjectKind:
    """One purgeable table, and the vocabulary a policy may use about it."""

    name: str
    model: type
    #: The column that dates a row for retention purposes.
    timestamp: str
    #: Status name -> a SQLAlchemy condition selecting rows in that status.
    #: STATUS_ANY is added automatically and must not appear here.
    statuses: dict
    #: The shortest retention this type may be configured with, in days.
    floor_days: int
    floor_reason: str
    #: Said in the plan whenever rows of this type are up for deletion.
    warning: str = ""

    @property
    def status_names(self) -> tuple[str, ...]:
        return (STATUS_ANY, *sorted(self.statuses))


# `Trace.quarantined` is abuse-control state and `commons_retracted_at` is a
# withdrawn Knowledge Base entry: both are rows a human decided something
# about, and both are the rows an operator is most likely to want kept
# LONGER than ordinary content rather than shorter. They are separate
# statuses so a policy can say so.
_TRACE_STATUSES = {
    "active": lambda: (
        (Trace.quarantined.is_(False)) & (Trace.commons_retracted_at.is_(None))
    ),
    "quarantined": lambda: Trace.quarantined.is_(True),
    "retracted": lambda: Trace.commons_retracted_at.isnot(None),
}

# An observation with no outcome is not "old data" in the same sense as one
# with an outcome: it is the attrition question itself (hub/crud.py's export
# includes these rows deliberately). Separating the statuses lets an operator
# keep the answered ones and drop the abandoned ones, which is the usual
# intent, without having to express it as one age for both.
_OBSERVATION_STATUSES = {
    "reported": lambda: HoldoutObservation.succeeded.isnot(None),
    "unreported": lambda: HoldoutObservation.succeeded.is_(None),
}

_SUBMISSION_STATUSES = {
    name: (lambda n=name: KnowledgeBaseSubmission.status == n)
    for name in ("pending", "approved", "rejected")
}

KINDS: dict[str, ObjectKind] = {
    kind.name: kind
    for kind in (
        ObjectKind(
            name="trace",
            model=Trace,
            timestamp="created_at",
            statuses=_TRACE_STATUSES,
            floor_days=30,
            floor_reason=(
                "a trace younger than 30 days may still be the evidence behind "
                "an open dispute about a lesson distilled from it"
            ),
        ),
        ObjectKind(
            name="vote",
            model=Vote,
            timestamp="created_at",
            statuses={},
            floor_days=30,
            floor_reason="votes feed the trust score a retrieval decision used",
        ),
        ObjectKind(
            name="holdout_observation",
            model=HoldoutObservation,
            timestamp="created_at",
            statuses=_OBSERVATION_STATUSES,
            floor_days=90,
            floor_reason=(
                "an observation is one arm of a randomized trial; 90 days is "
                "shorter than most experiments this product recommends running"
            ),
            warning=(
                "Deleting observations destroys the evidence behind every value "
                "report and signed ledger that counted them. Take "
                "`export-assignments` first if any invoice depends on this "
                "experiment -- the export's digest is what the ledger signature "
                "commits to, and it cannot be recomputed from deleted rows."
            ),
        ),
        ObjectKind(
            name="kb_submission",
            model=KnowledgeBaseSubmission,
            timestamp="created_at",
            statuses=_SUBMISSION_STATUSES,
            floor_days=30,
            floor_reason="a decided submission is the record of a credit awarded",
        ),
        ObjectKind(
            name="audit_log",
            model=AuditLogEntry,
            timestamp="created_at",
            statuses={},
            # The longest floor here, and the one that is least negotiable.
            floor_days=365,
            floor_reason=(
                "the audit log is what proves a purge happened; a policy that "
                "deleted it soon after would let data and the record of its "
                "deletion both disappear in two individually legitimate steps"
            ),
        ),
    )
}


def kind_or_error(object_type: str) -> ObjectKind:
    try:
        return KINDS[object_type]
    except KeyError:
        known = ", ".join(sorted(KINDS))
        raise RetentionError(
            f"unknown object type {object_type!r}; retention is configurable "
            f"for: {known}"
        ) from None


def check_status(kind: ObjectKind, status: str) -> str:
    if status != STATUS_ANY and status not in kind.statuses:
        raise RetentionError(
            f"{kind.name} has no status {status!r}; use one of "
            f"{', '.join(kind.status_names)}"
        )
    return status


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# --- policies ---------------------------------------------------------------

async def set_policy(
    session,
    org_id: str,
    object_type: str,
    max_age_days: int,
    *,
    status: str = STATUS_ANY,
    note: str = "",
) -> RetentionPolicy:
    """Create or update one org's policy for one (object type, status).

    A request below the type's floor is REFUSED, not clamped. Silently
    storing 365 when the operator asked for 7 leaves them believing the
    store honours a number it does not, and they find out from an auditor.
    """
    kind = kind_or_error(object_type)
    check_status(kind, status)
    if max_age_days <= 0:
        raise RetentionError(
            "a retention age must be a positive number of days; to delete "
            "everything of a type, purge it explicitly rather than "
            "configuring an age of zero"
        )
    if max_age_days < kind.floor_days:
        raise RetentionError(
            f"{kind.name} cannot be retained for less than {kind.floor_days} "
            f"days (asked for {max_age_days}): {kind.floor_reason}"
        )

    existing = (
        await session.execute(
            select(RetentionPolicy).where(
                RetentionPolicy.org_id == org_id,
                RetentionPolicy.object_type == object_type,
                RetentionPolicy.status == status,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        existing = RetentionPolicy(
            org_id=org_id, object_type=object_type, status=status,
            max_age_days=max_age_days, note=note,
        )
        session.add(existing)
    else:
        existing.max_age_days = max_age_days
        existing.note = note
        existing.updated_at = _now()
    return existing


async def remove_policy(
    session, org_id: str, object_type: str, *, status: str = STATUS_ANY
) -> bool:
    kind = kind_or_error(object_type)
    check_status(kind, status)
    result = await session.execute(
        delete(RetentionPolicy).where(
            RetentionPolicy.org_id == org_id,
            RetentionPolicy.object_type == object_type,
            RetentionPolicy.status == status,
        )
    )
    return bool(result.rowcount)


async def policies_for(session, org_id: str) -> list[RetentionPolicy]:
    rows = await session.execute(
        select(RetentionPolicy)
        .where(RetentionPolicy.org_id == org_id)
        .order_by(RetentionPolicy.object_type, RetentionPolicy.status)
    )
    return list(rows.scalars())


# --- legal holds ------------------------------------------------------------

async def place_hold(
    session,
    org_id: str,
    *,
    reason: str,
    placed_by: str,
    object_type: str = "",
    target_id: str = "",
) -> LegalHold:
    """Freeze rows against every retention policy.

    `object_type=""` holds everything the org has; a type with no
    `target_id` holds all rows of that type. A reason is required and is
    not cosmetic -- a hold nobody can explain later is one nobody dares
    release, and holds that are never released quietly become the
    indefinite retention this module exists to end.
    """
    if not reason.strip():
        raise RetentionError(
            "a legal hold needs a reason: it overrides the org's retention "
            "policy indefinitely, and the next operator has to be able to "
            "tell whether it still applies"
        )
    if object_type:
        kind_or_error(object_type)
    if target_id and not object_type:
        raise RetentionError(
            "a hold on one object needs its type as well as its id, so it "
            "can be matched without searching every table"
        )
    hold = LegalHold(
        org_id=org_id, object_type=object_type, target_id=target_id,
        reason=reason.strip(), placed_by=placed_by,
    )
    session.add(hold)
    await session.flush()
    # The reason is NOT sent: it is free text about a legal matter, and the
    # event's job is to say a freeze exists, not to describe why to a third
    # party's ticket system.
    with contextlib.suppress(events.EventError):
        await events.emit(session, org_id, "legal_hold.placed", {
            "hold_id": hold.id, "object_type": object_type,
            "target_id": target_id,
        })
    return hold


async def release_hold(session, hold_id: str, *, reason: str = "") -> LegalHold:
    hold = (
        await session.execute(select(LegalHold).where(LegalHold.id == hold_id))
    ).scalar_one_or_none()
    if hold is None:
        raise RetentionError(f"no legal hold with id {hold_id}")
    if hold.released_at is not None:
        raise RetentionError(
            f"legal hold {hold_id} was already released at {hold.released_at}"
        )
    hold.released_at = _now()
    hold.release_reason = reason
    with contextlib.suppress(events.EventError):
        await events.emit(session, hold.org_id, "legal_hold.released", {
            "hold_id": hold.id,
        })
    return hold


async def active_holds(session, org_id: str) -> list[LegalHold]:
    rows = await session.execute(
        select(LegalHold)
        .where(LegalHold.org_id == org_id, LegalHold.released_at.is_(None))
        .order_by(LegalHold.placed_at)
    )
    return list(rows.scalars())


def _holds_for_kind(holds, kind: ObjectKind) -> tuple[list, list]:
    """(blanket holds, per-object holds) that apply to this type."""
    blanket, targeted = [], []
    for hold in holds:
        if hold.object_type and hold.object_type != kind.name:
            continue
        (targeted if hold.target_id else blanket).append(hold)
    return blanket, targeted


# --- the plan ---------------------------------------------------------------

@dataclass(frozen=True)
class Bucket:
    """What one policy would do, and what stopped it."""

    object_type: str
    status: str
    max_age_days: int
    cutoff: str
    #: Rows older than the cutoff, before holds and blocks are applied.
    n_matched: int = 0
    #: Of those, the ones a legal hold freezes.
    n_held: int = 0
    #: Ids that would actually be deleted, sorted. The plan's digest is over
    #: these, so an apply cannot delete a row the operator did not see
    #: counted.
    doomed: tuple[str, ...] = field(default_factory=tuple)
    #: Set when the whole bucket is refused for a reason that is not a hold.
    blocked: str = ""
    warning: str = ""

    @property
    def n_doomed(self) -> int:
        return 0 if self.blocked else len(self.doomed)


@dataclass(frozen=True)
class PurgePlan:
    org_id: str
    computed_at: str
    buckets: tuple[Bucket, ...] = field(default_factory=tuple)
    hold_reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def digest(self) -> str:
        """Identifies exactly what this plan would delete.

        Over the doomed ids, not over counts: two different sets of rows can
        have the same count, and a digest that could not tell them apart
        would let an apply delete a set the operator never read.
        """
        rows = _FIELD_SEP.join(
            f"{b.object_type}/{b.status}:" + ",".join(b.doomed)
            for b in sorted(self.buckets, key=lambda b: (b.object_type, b.status))
            if not b.blocked
        )
        return hashlib.sha256(
            (_DIGEST_DOMAIN + _FIELD_SEP + self.org_id + _FIELD_SEP + rows)
            .encode("utf-8")
        ).hexdigest()

    @property
    def n_doomed(self) -> int:
        return sum(b.n_doomed for b in self.buckets)

    @property
    def n_held(self) -> int:
        return sum(b.n_held for b in self.buckets)

    @property
    def is_empty(self) -> bool:
        return self.n_doomed == 0

    def render(self) -> str:
        lines = [
            f"Retention plan for org {self.org_id}",
            f"computed {self.computed_at}",
            f"plan {self.digest[:16]}",
            "",
        ]
        if not self.buckets:
            lines.append(
                "No retention policy is configured, so nothing expires. Data "
                "is kept indefinitely until someone deletes it by hand."
            )
            return "\n".join(lines)

        for bucket in sorted(self.buckets, key=lambda b: (b.object_type, b.status)):
            label = f"{bucket.object_type} [{bucket.status}]"
            lines.append(
                f"{label}: keep {bucket.max_age_days}d "
                f"(older than {bucket.cutoff})"
            )
            if bucket.blocked:
                lines.append(f"    BLOCKED, nothing deleted: {bucket.blocked}")
            else:
                lines.append(
                    f"    {bucket.n_doomed} to delete"
                    + (f", {bucket.n_held} frozen by a legal hold"
                       if bucket.n_held else "")
                )
            if bucket.warning and bucket.n_doomed:
                lines.append(f"    ! {bucket.warning}")
        lines.append("")
        if self.hold_reasons:
            lines.append("Legal holds in force:")
            lines.extend(f"  - {r}" for r in self.hold_reasons)
            lines.append("")
        lines.append(
            f"TOTAL: {self.n_doomed} rows would be deleted, "
            f"{self.n_held} frozen by a legal hold."
        )
        lines.append(
            "Nothing has been deleted. This is a plan; apply it with the "
            f"digest above ({self.digest[:16]})."
        )
        return "\n".join(lines)


async def plan(session, org_id: str, *, now: datetime.datetime | None = None) -> PurgePlan:
    """What the org's policies would delete right now. Reads only."""
    moment = now or _now()
    policies = await policies_for(session, org_id)
    holds = await active_holds(session, org_id)

    org = (
        await session.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    experiment_running = bool(org is not None and org.holdout_rate > 0)

    buckets: list[Bucket] = []
    for policy in policies:
        kind = KINDS.get(policy.object_type)
        if kind is None:
            # A policy for a type this build no longer knows about. Reported
            # rather than ignored: a policy the operator believes is running
            # and that silently matches nothing is the worst of both.
            buckets.append(Bucket(
                object_type=policy.object_type, status=policy.status,
                max_age_days=policy.max_age_days, cutoff="",
                blocked=(
                    f"this build has no object type {policy.object_type!r}, so "
                    "the policy matches nothing"
                ),
            ))
            continue

        cutoff = moment - datetime.timedelta(days=policy.max_age_days)
        common = dict(
            object_type=kind.name, status=policy.status,
            max_age_days=policy.max_age_days, cutoff=cutoff.isoformat(),
            warning=kind.warning,
        )

        if kind.name == "holdout_observation" and experiment_running:
            buckets.append(Bucket(blocked=BLOCKED_EXPERIMENT_RUNNING, **common))
            continue

        model = kind.model
        conditions = [
            model.org_id == org_id,
            getattr(model, kind.timestamp) < cutoff,
        ]
        if policy.status != STATUS_ANY:
            conditions.append(kind.statuses[policy.status]())

        matched = list(
            (await session.execute(select(model.id).where(*conditions))).scalars()
        )

        blanket, targeted = _holds_for_kind(holds, kind)
        if blanket:
            buckets.append(Bucket(
                n_matched=len(matched), n_held=len(matched), doomed=(), **common
            ))
            continue
        frozen = {h.target_id for h in targeted}
        doomed = tuple(sorted(i for i in matched if i not in frozen))
        buckets.append(Bucket(
            n_matched=len(matched),
            n_held=len(matched) - len(doomed),
            doomed=doomed,
            **common,
        ))

    return PurgePlan(
        org_id=org_id,
        computed_at=moment.isoformat(),
        buckets=tuple(buckets),
        hold_reasons=tuple(
            f"{h.object_type or 'everything'}"
            + (f"/{h.target_id}" if h.target_id else "")
            + f": {h.reason} (placed by {h.placed_by} at {h.placed_at})"
            for h in holds
        ),
    )


async def apply(
    session,
    org_id: str,
    expect_digest: str,
    *,
    actor: str = audit.ACTOR_OPERATOR_CLI,
    now: datetime.datetime | None = None,
) -> PurgePlan:
    """Delete exactly what a plan with this digest described.

    The plan is RECOMPUTED here rather than trusted from the caller: a plan
    object that travelled through a CLI, a queue or an operator's terminal
    is a claim about the past, and the only safe thing to do with it is to
    check that the present still agrees. If it does not, nothing is deleted
    and the caller is told both digests.
    """
    fresh = await plan(session, org_id, now=now)
    if fresh.digest != expect_digest:
        raise StalePlanError(expect_digest, fresh.digest)

    deleted: dict[str, int] = {}
    for bucket in fresh.buckets:
        if bucket.blocked or not bucket.doomed:
            continue
        model = KINDS[bucket.object_type].model
        result = await session.execute(
            delete(model).where(model.id.in_(list(bucket.doomed)))
        )
        deleted[bucket.object_type] = (
            deleted.get(bucket.object_type, 0) + (result.rowcount or 0)
        )

    # Announced to whoever asked to be told (hub/events.py). Only counts and
    # the plan digest cross the wire -- what was deleted is exactly the
    # information a webhook must not carry.
    with contextlib.suppress(events.EventError):
        await events.emit(session, org_id, "retention.purged", {
            "plan": fresh.digest,
            "n_deleted": sum(deleted.values()),
            "n_held": fresh.n_held,
        })

    # Logged even when nothing matched. "The purge ran and deleted nothing"
    # and "the purge never ran" are different facts, and only one of them
    # means the schedule is broken.
    await audit.record(
        session,
        actor=actor,
        org_id=org_id,
        action="retention.purge",
        target_type="org",
        target_id=org_id,
        summary=(
            f"plan {fresh.digest[:12]}: deleted "
            + (", ".join(f"{n} {t}" for t, n in sorted(deleted.items())) or "nothing")
            + (f"; {fresh.n_held} frozen by legal hold" if fresh.n_held else "")
        ),
    )
    return fresh


async def counts(session, org_id: str) -> dict[str, int]:
    """How many rows of each purgeable type the org holds right now.

    For the export bundle and for telling an operator what a first policy
    would be up against.
    """
    out: dict[str, int] = {}
    for name, kind in KINDS.items():
        total = await session.scalar(
            select(func.count())
            .select_from(kind.model)
            .where(kind.model.org_id == org_id)
        )
        out[name] = int(total or 0)
    return out
