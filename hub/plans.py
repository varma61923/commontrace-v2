"""Entitlements: what a plan actually permits, enforced in the Hub.

A pricing page is not a business model. The model only exists once the
server refuses the request that exceeds the plan, and that refusal is here.

WHAT IS METERED, AND WHY IT IS THIS
-----------------------------------
Two limits, chosen so that neither one can be gamed into charging for
something the customer did not get:

* `max_traces` -- how much of its own memory an org may store. Storage is
  real cost and grows monotonically, so it is the natural floor of any
  plan. It is counted over live rows, so purging frees the allowance
  (hub/manage.py:purge_trace) -- a deletion right that quietly does not
  give back what it cost would be a deletion right in name only.

* `commons_queries_per_month` -- how many times the org may ask the
  commons the coverage question. This is the metered unit because it is
  the only call whose value comes from *other people's* contributions;
  everything else an org does is with its own data, and charging per query
  against your own memory is rent, not price.

WHY CONTRIBUTION EARNS ALLOWANCE
--------------------------------
A knowledge commons where contributing is pure altruism fills with filler
and dies -- the standard failure mode, and the one STRATEGY.md §3 names.
`commons-value` already measures the thing that makes contribution
non-altruistic: `Trace.commons_hits`, the number of times an org's shared
knowledge actually covered someone else's failure. Measuring it and then
not paying for it would be the same mistake in a nicer shirt.

So the allowance is `plan grant + delivered hits x QUERY_CREDIT_PER_HIT`.
An org that puts real knowledge in pays less, mechanically, without anyone
negotiating. Note carefully what is credited: hits DELIVERED, not traces
SHARED. Sharing is free to do and easy to fake -- an org could dump ten
thousand junk traces in an afternoon. A hit requires that someone else's
genuine failure matched, at the shipped threshold, against a corpus that
excludes the sharer's own rows. It cannot be self-dealt.

Seeded rows are excluded from crediting for the same reason they are
excluded from the network-effect metric: the operator crediting itself for
its own primer is circular (hub/manage.py:commons_seed).

WHAT THIS FILE DOES NOT DO
--------------------------
It sets no prices in currency and takes no payment. Those belong to a
billing system that has to handle tax, dunning, refunds and disputes, and
faking them here would be theatre. What it does is make the entitlement
real, so that when a price is attached, there is something to attach it
to.
"""
from __future__ import annotations

from dataclasses import dataclass

# One delivered hit -- someone else's failure genuinely covered by this
# org's shared knowledge -- is worth this many commons queries. Set so a
# modestly useful contributor on the free plan stops needing to think about
# the limit at all, while a non-contributor still meets it. It is a policy
# number, not a measurement, and it is the one dial in this file that an
# operator should expect to turn.
QUERY_CREDIT_PER_HIT = 25

# The name every org gets until someone says otherwise. Chosen so that
# forgetting to set a plan fails closed into the *smallest* entitlement
# rather than an unlimited one.
DEFAULT_PLAN = "free"

UNLIMITED = -1


@dataclass(frozen=True)
class Plan:
    """`None` is never used for "no limit" here -- UNLIMITED is an explicit
    sentinel, because a None that means "unlimited" and a None that means
    "unset" look identical at the call site and one of them bills nothing."""

    name: str
    max_traces: int
    commons_queries_per_month: int
    commons_access: bool
    summary: str


PLANS: dict[str, Plan] = {
    "free": Plan(
        name="free",
        max_traces=1_000,
        commons_queries_per_month=20,
        commons_access=True,
        summary="Evaluate on real memory. Commons access included, because a "
                "commons nobody may query cannot demonstrate that it works.",
    ),
    "team": Plan(
        name="team",
        max_traces=50_000,
        commons_queries_per_month=1_000,
        commons_access=True,
        summary="A fleet's working memory plus routine commons coverage checks.",
    ),
    "scale": Plan(
        name="scale",
        max_traces=UNLIMITED,
        commons_queries_per_month=25_000,
        commons_access=True,
        summary="Unmetered storage; commons queries still metered, because "
                "they consume other orgs' contributions rather than your own.",
    ),
    # Not a customer plan. The org that owns seeded rows must be able to
    # load a corpus larger than any paid tier without that looking like
    # revenue, and must never be billed for priming its own commons.
    "operator": Plan(
        name="operator",
        max_traces=UNLIMITED,
        commons_queries_per_month=UNLIMITED,
        commons_access=True,
        summary="Operator-internal. Not for sale; excluded from revenue reporting.",
    ),
}

BILLABLE_PLANS = ("team", "scale")


class EntitlementExceeded(Exception):
    """Raised instead of silently truncating or silently serving.

    Both alternatives are worse than an error: truncating gives the
    customer a wrong answer they will act on, and serving past the limit
    means the plan is decorative. Carries the machine-readable fields a
    client needs to render an upgrade path rather than a stack trace.
    """

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
    """Resolve a plan name, failing closed.

    An unrecognized name -- a typo in an operator command, a plan removed
    in a later release while rows still reference it -- resolves to the
    smallest plan, never to an unlimited one. The failure mode of guessing
    wrong should be a customer contacting support, not an unbilled fleet.
    """
    return PLANS.get((plan_name or "").strip().lower() or DEFAULT_PLAN, PLANS[DEFAULT_PLAN])


def query_allowance(plan: Plan, delivered_hits: int) -> int:
    """Monthly commons-query allowance, including earned credit."""
    if plan.commons_queries_per_month == UNLIMITED:
        return UNLIMITED
    return plan.commons_queries_per_month + max(0, int(delivered_hits)) * QUERY_CREDIT_PER_HIT


def within(limit: int, used: int) -> bool:
    return limit == UNLIMITED or used < limit


def describe(limit: int) -> str:
    return "unlimited" if limit == UNLIMITED else f"{limit:,}"
