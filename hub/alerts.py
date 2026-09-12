"""Threshold alerts and periodic reports on top of the existing webhook
pipeline (audit §8.3: "no alerting, scheduled reports, BI export").

Webhooks (6.3, hub/events.py) already tell a receiver WHEN something
happened. This module adds WHETHER a number an operator cares about has
crossed a line (`AlertRule` + `check_rules`), and a periodic summary of
what a number has been doing (`generate_report`) -- both delivered
through the SAME signed, at-least-once queue, so an org's already-
configured endpoint and signature verification cover these for free. An
alert or a report is a kind of event, not a second delivery mechanism.

NO IN-PROCESS SCHEDULER. `check_rules` and `generate_report` are pure
functions an operator's own cron invokes via `hub.manage check-alerts`/
`generate-report` -- the exact same shape as `webhook-deliver`'s existing
redelivery sweep. This Hub's server is request-driven with no background
loop, and adding one for this feature alone would be a bigger
architectural commitment than the feature is worth.

METRICS ARE A CLOSED, NAMED SET, NOT A FREE-FORM EXPRESSION LANGUAGE.
Each one is a small function against tables this Hub already has (never
a customer-supplied query string) -- "deny by construction" for an
unknown metric name, matching hub/rbac.py's own philosophy for an
unmapped tool: refused at rule-creation time, not silently skipped at
evaluation time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hub import audit, crud, events, plans
from hub.models import AlertRule, Organization, Trace

METRIC_QUARANTINE_RATE = "quarantine_rate"
METRIC_COMMONS_QUERIES_USED_PCT = "commons_queries_used_pct"
METRIC_TRACES_USED_PCT = "traces_used_pct"
METRICS = (
    METRIC_QUARANTINE_RATE, METRIC_COMMONS_QUERIES_USED_PCT, METRIC_TRACES_USED_PCT,
)

COMPARATOR_GT = "gt"
COMPARATOR_LT = "lt"
COMPARATORS = (COMPARATOR_GT, COMPARATOR_LT)

#: A rule that just fired stays quiet for at least this long even if the
#: metric is still past its threshold on the next check-alerts run.
DEFAULT_COOLDOWN_MINUTES = 60


class AlertError(ValueError):
    """A well-formed request this module refuses on its own terms (an
    unknown metric or comparator, a nonexistent org or rule) -- reported
    as `invalid_request`/`not_found`, the same convention hub/collab.py
    and hub/manage.py already use for their own input errors."""


async def _quarantine_rate(session: AsyncSession, org_id: str) -> float | None:
    """Percent of this org's traces currently quarantined. None (not
    computable) for an org with no traces yet -- a rate over zero traces
    is not a signal, it is division by zero wearing a signal's clothes."""
    total = await session.scalar(
        select(func.count()).select_from(Trace).where(Trace.org_id == org_id)
    )
    if not total:
        return None
    quarantined = await session.scalar(
        select(func.count()).select_from(Trace).where(
            Trace.org_id == org_id, Trace.quarantined.is_(True),
        )
    )
    return (quarantined or 0) / total * 100


async def _commons_queries_used_pct(session: AsyncSession, org_id: str) -> float | None:
    """None for an unlimited allowance (the operator plan) -- a percentage
    of infinity is not a number a threshold can compare against."""
    ent = await crud.entitlements(session, org_id)
    allowance = ent["commons_queries"]["allowance"]
    if allowance in (plans.UNLIMITED, 0):
        return None
    return ent["commons_queries"]["used"] / allowance * 100


async def _traces_used_pct(session: AsyncSession, org_id: str) -> float | None:
    ent = await crud.entitlements(session, org_id)
    limit = ent["traces"]["limit"]
    if limit in (plans.UNLIMITED, 0):
        return None
    return ent["traces"]["used"] / limit * 100


_METRIC_FUNCS = {
    METRIC_QUARANTINE_RATE: _quarantine_rate,
    METRIC_COMMONS_QUERIES_USED_PCT: _commons_queries_used_pct,
    METRIC_TRACES_USED_PCT: _traces_used_pct,
}


async def compute_metric(session: AsyncSession, org_id: str, metric: str) -> float | None:
    """The metric's current value for one org, or None if it cannot be
    computed right now (no data yet, or an unlimited entitlement)."""
    try:
        fn = _METRIC_FUNCS[metric]
    except KeyError:
        raise AlertError(
            f"unknown metric {metric!r}; known metrics: {', '.join(METRICS)}"
        ) from None
    return await fn(session, org_id)


def _check_holds(comparator: str, value: float, threshold: float) -> bool:
    return value > threshold if comparator == COMPARATOR_GT else value < threshold


async def create_rule(
    session: AsyncSession, org_id: str, metric: str, comparator: str, threshold: float,
    *, cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES, created_by: str = "",
) -> AlertRule:
    if metric not in METRICS:
        raise AlertError(f"unknown metric {metric!r}; known metrics: {', '.join(METRICS)}")
    if comparator not in COMPARATORS:
        raise AlertError(
            f"unknown comparator {comparator!r}; known comparators: {', '.join(COMPARATORS)}"
        )
    if cooldown_minutes < 1:
        raise AlertError(f"cooldown_minutes must be at least 1, got {cooldown_minutes}")
    org = await session.get(Organization, org_id)
    if org is None:
        raise AlertError(f"no such organization: {org_id}")
    rule = AlertRule(
        org_id=org_id, metric=metric, comparator=comparator, threshold=threshold,
        cooldown_minutes=cooldown_minutes, created_by=created_by,
    )
    session.add(rule)
    await session.flush()
    await audit.record(
        session, actor=created_by or audit.ACTOR_OPERATOR_CLI, action="alert_rule.create",
        org_id=org_id, target_type="alert_rule", target_id=rule.id,
        summary=f"{metric} {comparator} {threshold}",
    )
    return rule


async def list_rules(session: AsyncSession, org_id: str) -> list[AlertRule]:
    rows = await session.execute(
        select(AlertRule).where(AlertRule.org_id == org_id).order_by(AlertRule.created_at)
    )
    return list(rows.scalars())


async def delete_rule(session: AsyncSession, rule_id: str) -> bool:
    rule = await session.get(AlertRule, rule_id)
    if rule is None:
        return False
    await audit.record(
        session, actor=audit.ACTOR_OPERATOR_CLI, action="alert_rule.delete",
        org_id=rule.org_id, target_type="alert_rule", target_id=rule_id,
        summary=f"{rule.metric} {rule.comparator} {rule.threshold}",
    )
    await session.delete(rule)
    return True


async def check_rules(
    session: AsyncSession, org_id: str | None = None, *, now: datetime | None = None,
) -> list[dict]:
    """Evaluate every enabled rule (optionally scoped to one org), firing
    `alert.triggered` for any whose metric has crossed its threshold and
    whose cooldown has elapsed. Returns a summary of what fired, for the
    CLI to print -- the return value is not itself the alert; the emitted
    event is.
    """
    moment = now or datetime.now(timezone.utc)
    query = select(AlertRule).where(AlertRule.enabled.is_(True))
    if org_id is not None:
        query = query.where(AlertRule.org_id == org_id)
    fired = []
    for rule in (await session.execute(query)).scalars():
        if (
            rule.last_triggered_at is not None
            and moment - rule.last_triggered_at < timedelta(minutes=rule.cooldown_minutes)
        ):
            continue
        value = await compute_metric(session, rule.org_id, rule.metric)
        if value is None or not _check_holds(rule.comparator, value, rule.threshold):
            continue
        rule.last_triggered_at = moment
        await events.emit(session, rule.org_id, "alert.triggered", {
            "rule_id": rule.id, "metric": rule.metric, "comparator": rule.comparator,
            "threshold": rule.threshold, "value": round(value, 4),
        }, now=moment)
        fired.append({
            "rule_id": rule.id, "org_id": rule.org_id, "metric": rule.metric,
            "comparator": rule.comparator, "threshold": rule.threshold,
            "value": round(value, 4),
        })
    return fired


async def generate_report(session: AsyncSession, org_id: str) -> dict:
    """Emit one periodic summary for an org, over the same billing period
    `account_usage`/`entitlements` already report by -- so a report and a
    live `account_usage` call describe the same window rather than two
    that could disagree.

    A BI-shaped export already exists for experiment data
    (`export-assignments`, per-arm CSV); this is the operational-usage
    counterpart, delivered through the webhook pipeline instead of a
    file, since a periodic push is what "scheduled report" asks for.
    """
    org = await session.get(Organization, org_id)
    if org is None:
        raise AlertError(f"no such organization: {org_id}")
    ent = await crud.entitlements(session, org_id)
    allowance = ent["commons_queries"]["allowance"]
    payload = {
        "period": ent["period"],
        "plan": ent["plan"],
        "traces_total": ent["traces"]["used"],
        "commons_queries_used": ent["commons_queries"]["used"],
        "commons_queries_allowance": allowance,
    }
    await events.emit(session, org_id, "report.generated", payload)
    return payload
