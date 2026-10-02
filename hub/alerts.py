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

DEFAULT_COOLDOWN_MINUTES = 60


class AlertError(ValueError):
    ...


async def _quarantine_rate(session: AsyncSession, org_id: str) -> float | None:
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
