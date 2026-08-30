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
  CommonTrace Knowledge Base a question. This is metered separately from
  storage because it is the one call that reads content this org did not
  write: an operator-maintained corpus of substrate knowledge (public
  protocol semantics, vendor documentation, standards -- see
  `commons/seed/substrate-v1.jsonl`), not this org's own memory. Charging
  per query against your own memory would be rent, not price; this is
  metered because it is a genuinely separate resource.

* `max_agents` -- how many distinct agents an org may have ACTIVE at once.
  This is the expansion axis: STRATEGY.md §12.2 argues value here compounds
  with agents per fleet, tasks over time, and fleets per customer, and
  §12.6 concludes the variable to run on is "agents under management, not
  logos". A metric nobody counts cannot be run on, so it is counted here.

  Two properties of the count are deliberate and are the whole design:

  1. It is ACTIVE agents in a trailing window (ACTIVE_AGENT_WINDOW_DAYS),
     not distinct agents all-time. An all-time count only ever grows: it
     cannot show a fleet shrinking, so it cannot show churn, and it would
     bill a customer forever for an agent they ran once and decommissioned.
     That is the same defect the storage limit already avoids by counting
     live rows so purging frees allowance -- a number that can only go up
     is a vanity metric, not a meter.

  2. It is enforced at NEW-agent registration, never on every write. See
     hub/crud.py:_reserve_agent_slot. An org sitting exactly at its cap
     must keep serving its existing fleet; refusing their writes would turn
     a commercial limit into a production outage, which is never the right
     failure mode for infrastructure the customer is running live traffic
     through. Hitting the cap blocks EXPANSION, not OPERATION.

WHY THERE IS NO ORG-TO-ORG SHARING HERE
----------------------------------------
An earlier design routed the Knowledge Base through customer contribution:
an org could opt a trace of its own into a pool other orgs' queries could
match against, and earned extra query allowance for every hit that
delivered. That is a peer-to-peer commons, and it has a fatal problem
STRATEGY.md §3 already named and never solved: **adverse selection**. Why
would an org contribute knowledge that helps a competitor? The naive
answer ("reciprocity") fails because the most valuable lessons are the
most proprietary -- contribution stays voluntary, orgs contribute their
generic lessons and withhold their good ones, and the corpus fills with
filler. And it asks a customer to trust that their own trace text, however
"substrate-only" they judge it, will never leak anything competitively
sensitive to another org reading it.

There is no version of that trade a customer should take, so it is not
offered. What replaced it: the Knowledge Base is authored and curated by
the operator alone (`hub/manage.py:commons_seed`, `Trace.commons_source ==
"seed"`) -- the same relationship a team has to Stack Overflow or an
internal wiki, not to a competitor's support queue. No customer trace ever
becomes visible to another customer. `commons_access` is the one thing a
plan controls: whether this org may consult that corpus at all. Nothing
about using it costs another org anything or requires them to have shared
first, because there is no "them" to share with.

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

# How recently an agent must have written a trace to count as "under
# management". A policy number, and the second dial in this file.
#
# 30 days because it is the shortest window that survives an agent which
# runs on a monthly cadence (a billing-close reconciliation agent, a
# monthly report generator). Too short and a real, paid-for agent silently
# drops out of the count between runs, which reads as churn that did not
# happen; too long and a decommissioned agent keeps being billed, which is
# the all-time-count defect this window exists to avoid.
ACTIVE_AGENT_WINDOW_DAYS = 30

# Traces written by a client that sent no agent_id are attributed to this
# single sentinel agent per org.
#
# The alternative designs are both worse. Rejecting the write breaks every
# client that predates agent identity, for a metering concern the customer
# did not ask for. Counting such traces as zero agents makes the meter
# trivially avoidable by omitting one field, which is not a meter.
#
# Counting them as exactly ONE agent per org is backward compatible and
# cannot be gamed downward -- but it is also a FLOOR, not a measurement:
# an unknown number of real agents hides behind it. Every surface that
# reports the count therefore reports the unattributed trace count beside
# it (hub/crud.py:agents_under_management), so nobody -- operator or
# customer -- reads a floor as a total. Same discipline as the commons
# coverage number, which is published as a floor for the same reason.
UNATTRIBUTED_AGENT_ID = "unattributed"

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
    # Defaulted so that a Plan built ad hoc (only tests do this, to pin one
    # specific cap) does not accidentally enforce an agent limit it was not
    # written to test. The fail-closed guarantee this file cares about lives
    # in get(), which resolves every real plan through PLANS -- and
    # TestEveryPlanSetsMaxAgents asserts every entry there sets this
    # explicitly, so the permissive default can never silently reach a
    # customer.
    max_agents: int = -1  # UNLIMITED; defined below, referenced by value here


PLANS: dict[str, Plan] = {
    "free": Plan(
        name="free",
        max_traces=1_000,
        commons_queries_per_month=20,
        # 5 agents: an individual developer's fleet. Deliberately a real
        # working limit rather than 1 -- the product's own thesis is that
        # lessons compound ACROSS agents, so a tier that permits a single
        # agent cannot demonstrate the thing being sold.
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
    # Not a customer plan. The org that owns the operator-curated Knowledge
    # Base content must be able to write to it without that looking like
    # a customer's own storage usage or query volume.
    "operator": Plan(
        name="operator",
        max_traces=UNLIMITED,
        commons_queries_per_month=UNLIMITED,
        max_agents=UNLIMITED,
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


def query_allowance(plan: Plan) -> int:
    """Monthly Knowledge Base query allowance.

    A flat plan grant, not something an org can earn more of by
    contributing -- there is no customer contribution in this model to
    earn credit for. See this module's docstring, "why there is no
    org-to-org sharing here".
    """
    return plan.commons_queries_per_month


def within(limit: int, used: int) -> bool:
    return limit == UNLIMITED or used < limit


def describe(limit: int) -> str:
    return "unlimited" if limit == UNLIMITED else f"{limit:,}"
