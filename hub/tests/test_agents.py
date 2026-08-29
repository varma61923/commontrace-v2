"""Agents under management: the expansion meter.

STRATEGY.md §12.6 concludes the variable to run this business on is
"agents under management, not logos", and §13.1 asserted it was already
measurable. It was not: `agent_type` is a CATEGORY ("support"), so a fleet
of 25 support agents shared one value and nothing in the system could count
agents at all. The deck's per-agent pricing was therefore unenforceable.

What is tested here is the part that is easy to get catastrophically wrong.
A storage cap that refuses a write costs the customer one trace. An agent
cap that refuses the wrong write takes a running fleet off the air. So the
central property below is not "the limit is enforced" -- it is that the
limit blocks EXPANSION and never blocks OPERATION.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from hub import crud, plans
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def orgs(session_factory):
    async with session_scope(session_factory) as session:
        made = {}
        for name in ("fleet", "neighbour"):
            o = Organization(name=name)
            session.add(o)
            await session.flush()
            made[name] = o.id
        return made


async def _contribute(session_factory, config, org_id, title, agent_id="", **kw):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        return await crud.contribute_trace(
            session, org_id, config, rate_limiter,
            title=title, context_text="ctx", solution_text="fix",
            tags=[], agent_type="support", agent_id=agent_id, actor="test", **kw,
        )


async def _set_plan(session_factory, org_id, name):
    async with session_scope(session_factory) as session:
        (await session.get(Organization, org_id)).plan = name


async def _agents(session_factory, org_id):
    async with session_scope(session_factory) as session:
        return await crud.agents_under_management(session, org_id)


async def _age_traces(session_factory, org_id, days):
    """Backdate every trace for an org, to test the trailing window."""
    async with session_scope(session_factory) as session:
        await session.execute(
            update(Trace).where(Trace.org_id == org_id).values(
                created_at=datetime.now(timezone.utc) - timedelta(days=days)
            )
        )


# --- Counting -----------------------------------------------------------


class TestCounting:
    async def test_distinct_named_agents_are_counted_once_each(self, session_factory, config, orgs):
        for i in range(3):
            for run in range(4):  # each agent writes repeatedly
                await _contribute(session_factory, config, orgs["fleet"], f"t{i}-{run}",
                                  agent_id=f"agent-{i}")
        a = await _agents(session_factory, orgs["fleet"])
        assert a["active"] == 3
        assert a["named"] == 3
        assert a["is_floor"] is False

    async def test_agent_type_does_not_distinguish_agents(self, session_factory, config, orgs):
        """The defect this column exists to fix: every one of these shares an
        agent_type, and before agent_id they were indistinguishable."""
        for i in range(5):
            await _contribute(session_factory, config, orgs["fleet"], f"t{i}", agent_id=f"a{i}")
        assert (await _agents(session_factory, orgs["fleet"]))["active"] == 5

    async def test_unattributed_traces_collapse_to_exactly_one_agent(self, session_factory, config, orgs):
        for i in range(10):
            await _contribute(session_factory, config, orgs["fleet"], f"t{i}")  # no agent_id
        a = await _agents(session_factory, orgs["fleet"])
        assert a["active"] == 1
        assert a["named"] == 0
        assert a["unattributed_traces"] == 10

    async def test_unattributed_presence_marks_the_count_as_a_floor(self, session_factory, config, orgs):
        await _contribute(session_factory, config, orgs["fleet"], "named", agent_id="a1")
        assert (await _agents(session_factory, orgs["fleet"]))["is_floor"] is False
        await _contribute(session_factory, config, orgs["fleet"], "anon")
        a = await _agents(session_factory, orgs["fleet"])
        assert a["is_floor"] is True
        assert a["active"] == 2  # a1 + the single unattributed sentinel

    async def test_an_empty_org_has_no_agents(self, session_factory, orgs):
        a = await _agents(session_factory, orgs["fleet"])
        assert a["active"] == 0
        assert a["is_floor"] is False

    async def test_another_orgs_agents_are_never_counted(self, session_factory, config, orgs):
        """Tenant isolation, same property hub/tests/test_tenant_isolation.py
        pins for every other read path."""
        for i in range(4):
            await _contribute(session_factory, config, orgs["neighbour"], f"n{i}", agent_id=f"n-{i}")
        assert (await _agents(session_factory, orgs["fleet"]))["active"] == 0
        assert (await _agents(session_factory, orgs["neighbour"]))["active"] == 4


class TestTrailingWindow:
    async def test_an_agent_idle_beyond_the_window_stops_counting(self, session_factory, config, orgs):
        await _contribute(session_factory, config, orgs["fleet"], "old", agent_id="retired")
        assert (await _agents(session_factory, orgs["fleet"]))["active"] == 1
        await _age_traces(session_factory, orgs["fleet"], plans.ACTIVE_AGENT_WINDOW_DAYS + 1)
        assert (await _agents(session_factory, orgs["fleet"]))["active"] == 0

    async def test_the_count_can_fall_which_is_the_whole_point(self, session_factory, config, orgs):
        """An all-time distinct count only ever grows, so it can never show
        churn and would bill forever for a decommissioned agent."""
        for i in range(3):
            await _contribute(session_factory, config, orgs["fleet"], f"t{i}", agent_id=f"a{i}")
        assert (await _agents(session_factory, orgs["fleet"]))["active"] == 3
        await _age_traces(session_factory, orgs["fleet"], plans.ACTIVE_AGENT_WINDOW_DAYS + 1)
        await _contribute(session_factory, config, orgs["fleet"], "still-here", agent_id="a0")
        assert (await _agents(session_factory, orgs["fleet"]))["active"] == 1

    async def test_stale_unattributed_traces_stop_marking_a_floor(self, session_factory, config, orgs):
        await _contribute(session_factory, config, orgs["fleet"], "anon")
        assert (await _agents(session_factory, orgs["fleet"]))["is_floor"] is True
        await _age_traces(session_factory, orgs["fleet"], plans.ACTIVE_AGENT_WINDOW_DAYS + 1)
        assert (await _agents(session_factory, orgs["fleet"]))["is_floor"] is False


# --- Enforcement: the part that must not take a fleet down ---------------


class TestEnforcementBlocksExpansionNotOperation:
    async def test_an_agent_already_active_keeps_working_at_the_cap(self, session_factory, config, orgs):
        """THE property. An org sitting exactly at its limit must keep
        serving the fleet it already has -- refusing those writes turns a
        commercial limit into a production outage."""
        cap = plans.get("free").max_agents
        for i in range(cap):
            await _contribute(session_factory, config, orgs["fleet"], f"t{i}", agent_id=f"a{i}")
        assert (await _agents(session_factory, orgs["fleet"]))["active"] == cap

        # Every existing agent writes again, repeatedly. None may be refused.
        for round_ in range(3):
            for i in range(cap):
                await _contribute(session_factory, config, orgs["fleet"],
                                  f"again-{round_}-{i}", agent_id=f"a{i}")
        assert (await _agents(session_factory, orgs["fleet"]))["active"] == cap

    async def test_a_new_agent_beyond_the_cap_is_refused(self, session_factory, config, orgs):
        cap = plans.get("free").max_agents
        for i in range(cap):
            await _contribute(session_factory, config, orgs["fleet"], f"t{i}", agent_id=f"a{i}")
        with pytest.raises(plans.EntitlementExceeded) as exc:
            await _contribute(session_factory, config, orgs["fleet"], "overflow", agent_id="one-too-many")
        assert exc.value.metric == "agents"
        assert exc.value.limit == cap
        assert "one-too-many" in exc.value.remedy

    async def test_the_refused_agents_trace_is_not_stored(self, session_factory, config, orgs):
        cap = plans.get("free").max_agents
        for i in range(cap):
            await _contribute(session_factory, config, orgs["fleet"], f"t{i}", agent_id=f"a{i}")
        with pytest.raises(plans.EntitlementExceeded):
            await _contribute(session_factory, config, orgs["fleet"], "overflow", agent_id="nope")
        async with session_scope(session_factory) as session:
            found = await session.scalar(
                select(Trace.id).where(Trace.org_id == orgs["fleet"], Trace.agent_id == "nope")
            )
        assert found is None

    async def test_unattributed_writes_are_never_refused_even_at_the_cap(self, session_factory, config, orgs):
        """A client that predates agent identity must not start failing
        because a metering concern its author never saw was added."""
        cap = plans.get("free").max_agents
        for i in range(cap):
            await _contribute(session_factory, config, orgs["fleet"], f"t{i}", agent_id=f"a{i}")
        result = await _contribute(session_factory, config, orgs["fleet"], "legacy-client")
        assert result["id"]

    async def test_a_downgrade_does_not_break_an_already_oversized_fleet(self, session_factory, config, orgs):
        """Over the cap because the LIMIT moved, not because the fleet grew.
        Those agents are already running; the overage belongs in the
        operator's usage report, not in failing production writes."""
        await _set_plan(session_factory, orgs["fleet"], "team")
        for i in range(plans.get("free").max_agents + 3):
            await _contribute(session_factory, config, orgs["fleet"], f"t{i}", agent_id=f"a{i}")
        await _set_plan(session_factory, orgs["fleet"], "free")  # downgrade

        for i in range(plans.get("free").max_agents + 3):  # everyone keeps writing
            await _contribute(session_factory, config, orgs["fleet"], f"after-{i}", agent_id=f"a{i}")
        a = await _agents(session_factory, orgs["fleet"])
        assert a["active"] > plans.get("free").max_agents

    async def test_an_agent_that_aged_out_can_be_replaced(self, session_factory, config, orgs):
        """Retiring an agent must actually free the slot, or the limit is a
        ratchet -- the same reason purging frees storage allowance."""
        cap = plans.get("free").max_agents
        for i in range(cap):
            await _contribute(session_factory, config, orgs["fleet"], f"t{i}", agent_id=f"a{i}")
        await _age_traces(session_factory, orgs["fleet"], plans.ACTIVE_AGENT_WINDOW_DAYS + 1)
        result = await _contribute(session_factory, config, orgs["fleet"], "fresh", agent_id="replacement")
        assert result["id"]

    async def test_unlimited_plans_never_enforce(self, session_factory, config, orgs):
        await _set_plan(session_factory, orgs["fleet"], "scale")
        for i in range(plans.get("free").max_agents + 5):
            await _contribute(session_factory, config, orgs["fleet"], f"t{i}", agent_id=f"a{i}")
        assert (await _agents(session_factory, orgs["fleet"]))["active"] == plans.get("free").max_agents + 5

    async def test_a_retry_of_an_existing_agent_is_not_a_new_registration(self, session_factory, config, orgs):
        """Idempotent replay stores nothing and registers no agent, so it
        must not be refused at the cap."""
        cap = plans.get("free").max_agents
        for i in range(cap):
            await _contribute(session_factory, config, orgs["fleet"], f"t{i}", agent_id=f"a{i}")
        first = await _contribute(session_factory, config, orgs["fleet"], "keyed",
                                  agent_id="a0", idempotency_key="k1")
        replay = await _contribute(session_factory, config, orgs["fleet"], "keyed",
                                   agent_id="a0", idempotency_key="k1")
        assert first["id"] == replay["id"]


class TestConcurrency:
    async def test_concurrent_new_agents_cannot_both_pass_the_last_slot(self, session_factory, config, orgs):
        """Count-then-insert is a TOCTOU race without the org row lock: two
        registrations racing for one remaining slot would both see room."""
        cap = plans.get("free").max_agents
        for i in range(cap - 1):
            await _contribute(session_factory, config, orgs["fleet"], f"t{i}", agent_id=f"a{i}")

        results = await asyncio.gather(
            _contribute(session_factory, config, orgs["fleet"], "race-x", agent_id="x"),
            _contribute(session_factory, config, orgs["fleet"], "race-y", agent_id="y"),
            return_exceptions=True,
        )
        refused = [r for r in results if isinstance(r, plans.EntitlementExceeded)]
        accepted = [r for r in results if isinstance(r, dict)]
        assert len(accepted) == 1, f"expected exactly one winner, got {results}"
        assert len(refused) == 1
        assert (await _agents(session_factory, orgs["fleet"]))["active"] == cap


class TestEntitlementsSurface:
    async def test_entitlements_reports_agents_with_its_limit(self, session_factory, config, orgs):
        await _contribute(session_factory, config, orgs["fleet"], "t", agent_id="a1")
        async with session_scope(session_factory) as session:
            ent = await crud.entitlements(session, orgs["fleet"])
        assert ent["agents"]["active"] == 1
        assert ent["agents"]["limit"] == plans.get("free").max_agents
        assert ent["agents"]["window_days"] == plans.ACTIVE_AGENT_WINDOW_DAYS

    async def test_reading_entitlements_consumes_no_quota(self, session_factory, config, orgs):
        """Same property the commons meter has: a meter that charges you for
        checking the meter ends up in a support thread."""
        async with session_scope(session_factory) as session:
            before = await crud.entitlements(session, orgs["fleet"])
            after = await crud.entitlements(session, orgs["fleet"])
        assert before["commons_queries"]["used"] == after["commons_queries"]["used"]


class TestStorage:
    async def test_agent_id_round_trips_and_is_rejected_when_oversized(self, session_factory, config, orgs):
        from hub.abuse import TraceRejected

        await _contribute(session_factory, config, orgs["fleet"], "t", agent_id="worker-7")
        async with session_scope(session_factory) as session:
            stored = await session.scalar(
                select(Trace.agent_id).where(Trace.org_id == orgs["fleet"])
            )
        assert stored == "worker-7"

        # Over the String(128) column width: must be a clean rejection, not
        # an opaque 500 from asyncpg's StringDataRightTruncation.
        with pytest.raises(TraceRejected):
            await _contribute(session_factory, config, orgs["fleet"], "t2", agent_id="x" * 129)
