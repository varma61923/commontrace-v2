"""Entitlements: what a plan actually permits, enforced in the Hub."""
from __future__ import annotations

from dataclasses import dataclass

ACTIVE_AGENT_WINDOW_DAYS = 30

UNATTRIBUTED_AGENT_ID = "unattributed"

DEFAULT_PLAN = "free"

UNLIMITED = -1

SUBMISSION_ACCEPTANCE_CREDIT = 25


@dataclass(frozen=True)
class Plan:
    name: str
    max_traces: int
    commons_queries_per_month: int
    commons_access: bool
    summary: str
    max_agents: int = -1


PLANS: dict[str, Plan] = {
    "free": Plan(
        name="free",
        max_traces=1_000,
        commons_queries_per_month=20,
        max_agents=5,
        commons_access=True,
        summary="Evaluate on real memory, with Knowledge Base access included -- an "
                "on-ramp nobody can try requires nothing to demonstrate its value.",
    ),
    "team": Plan(
        name="team",
        max_traces=50_000,
        commons_queries_per_month=1_000,
        max_agents=25,
        commons_access=True,
        summary="A fleet's working memory plus routine Knowledge Base lookups.",
    ),
    "scale": Plan(
        name="scale",
        max_traces=UNLIMITED,
        commons_queries_per_month=25_000,
        max_agents=UNLIMITED,
        commons_access=True,
        summary="Unmetered storage; Knowledge Base queries still metered, because "
                "that corpus is a separate resource from this org's own memory.",
    ),
    "operator": Plan(
        name="operator",
        max_traces=UNLIMITED,
        commons_queries_per_month=UNLIMITED,
        max_agents=UNLIMITED,
        commons_access=True,
        summary="Operator-internal. Not for sale; excluded from revenue reporting.",
    ),
}

VALUE_CAPTURE_SHARE = 0.20

VALUE_BILLED_ONLY_ON_ESTABLISHED_EFFECTS = True


def billable_value(value_report, share: float = VALUE_CAPTURE_SHARE) -> float | None:
    """The value-linked component, or None when there is nothing to bill on."""
    if value_report is None or not getattr(value_report, "readable", False):
        return None
    money = value_report.money
    if money is None:
        return None
    return money * share


BILLABLE_PLANS = ("team", "scale")


class EntitlementExceeded(Exception):
    """Raised instead of silently truncating or silently serving."""

    def __init__(self, metric: str, limit: int, used: int, plan: str, remedy: str):
        self.metric = metric
        self.limit = limit
        self.used = used
        self.plan = plan
        self.remedy = remedy
        super().__init__(
            f"{metric} limit reached for plan '{plan}': {used}/{limit}. {remedy}"
        )


def get(plan_name: str | None) -> Plan:
    """Resolve a plan name, failing closed."""
    return PLANS.get((plan_name or "").strip().lower() or DEFAULT_PLAN, PLANS[DEFAULT_PLAN])


def query_allowance(plan: Plan, bonus: int = 0) -> int:
    if plan.commons_queries_per_month == UNLIMITED:
        return UNLIMITED
    return plan.commons_queries_per_month + max(bonus, 0)


def within(limit: int, used: int) -> bool:
    return limit == UNLIMITED or used < limit


def describe(limit: int) -> str:
    return "unlimited" if limit == UNLIMITED else f"{limit:,}"
