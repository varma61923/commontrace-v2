"""`crud.export_commons` -- handing over the corpus so matching can happen
on the client.

Every other Knowledge Base read answers a question ABOUT a failure, which
means telling the Hub that this fleet is asking and roughly what about. A
MinHash signature is a small disclosure, but it is one, and it has always
been the price of using the corpus.

This read removes the price: a fleet holding the records computes its own
coverage locally, with no query, no signature and no record that it looked.
What these tests pin is that the removal does not quietly cost anything
else -- the tenant boundary, the plan boundary, and the operator's own
control over whether their curation is downloadable at all.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from hub import commons, crud, plans
from hub.config import HubConfig
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def orgs(session_factory):
    async with session_scope(session_factory) as session:
        made = {}
        for name in ("operator", "reader", "other"):
            org = Organization(name=name)
            session.add(org)
            await session.flush()
            made[name] = org.id
        return made


def _exporting(config: HubConfig) -> HubConfig:
    """The same config with bulk export turned on, as an operator would."""
    import dataclasses
    return dataclasses.replace(config, commons_export_enabled=True)


async def _seed(session_factory, org_id, title="Pool exhausted", *,
                source="seed", shared=True, retracted=False, quarantined=False,
                solution="a long and complete solution " * 20):
    async with session_scope(session_factory) as session:
        trace = Trace(
            org_id=org_id, title=title, context_text="ctx", solution_text=solution,
            tags=["postgres"], agent_type="code",
            shared_with_commons=shared,
            shared_at=datetime.now(timezone.utc) if shared else None,
            shared_rationale="seed" if shared else "",
            commons_signature=commons.signature_for(title, "ctx", ["postgres"]),
            commons_source=source,
            commons_retracted_at=datetime.now(timezone.utc) if retracted else None,
            quarantined=quarantined,
        )
        session.add(trace)
        await session.flush()
        return trace.id


class TestTheOperatorDecidesWhetherToPublishInBulk:
    """Consulting the corpus is the product working. Handing over every
    record in one call is giving away what curation produced, so it is off
    unless an operator says otherwise."""

    async def test_it_is_refused_by_default(self, session_factory, config, orgs):
        await _seed(session_factory, orgs["operator"])
        async with session_scope(session_factory) as session:
            with pytest.raises(plans.EntitlementExceeded) as exc:
                await crud.export_commons(session, orgs["reader"], config)
        assert "HUB_COMMONS_EXPORT_ENABLED" in exc.value.remedy

    async def test_the_refusal_names_the_surface_that_still_works(
        self, session_factory, config, orgs
    ):
        """A deployment that will not bulk-export has not withdrawn the
        Knowledge Base; the per-failure tools are unaffected, and the error
        should not leave a reader thinking otherwise."""
        async with session_scope(session_factory) as session:
            with pytest.raises(plans.EntitlementExceeded) as exc:
                await crud.export_commons(session, orgs["reader"], config)
        assert "commons_search" in exc.value.remedy

    async def test_enabled_it_returns_the_corpus(self, session_factory, config, orgs):
        await _seed(session_factory, orgs["operator"], "a")
        await _seed(session_factory, orgs["operator"], "b")
        async with session_scope(session_factory) as session:
            result = await crud.export_commons(session, orgs["reader"], _exporting(config))
        assert result["n_entries"] == 2
        assert {e["title"] for e in result["entries"]} == {"a", "b"}


class TestTheBoundariesSurviveABulkRead:
    """A read that returns everything is exactly where a scoping mistake
    stops being a wrong number and becomes a disclosure."""

    async def test_another_orgs_private_trace_never_appears(
        self, session_factory, config, orgs
    ):
        await _seed(session_factory, orgs["other"], "private", source="org", shared=False)
        await _seed(session_factory, orgs["operator"], "curated")
        async with session_scope(session_factory) as session:
            result = await crud.export_commons(session, orgs["reader"], _exporting(config))
        assert [e["title"] for e in result["entries"]] == ["curated"]

    async def test_a_retracted_entry_is_not_exported(self, session_factory, config, orgs):
        await _seed(session_factory, orgs["operator"], "withdrawn", retracted=True)
        async with session_scope(session_factory) as session:
            result = await crud.export_commons(session, orgs["reader"], _exporting(config))
        assert result["n_entries"] == 0

    async def test_a_quarantined_entry_is_not_exported(self, session_factory, config, orgs):
        await _seed(session_factory, orgs["operator"], "held", quarantined=True)
        async with session_scope(session_factory) as session:
            result = await crud.export_commons(session, orgs["reader"], _exporting(config))
        assert result["n_entries"] == 0

    async def test_a_plan_without_the_knowledge_base_is_refused(
        self, session_factory, config, orgs, monkeypatch
    ):
        """The plan boundary still holds: bulk export must not be a second
        door into content the plan excludes."""
        await _seed(session_factory, orgs["operator"])

        real = crud._plan_and_bonus_for

        async def no_commons(session, org_id):
            plan, bonus = await real(session, org_id)
            import dataclasses
            return dataclasses.replace(plan, commons_access=False), bonus

        monkeypatch.setattr(crud, "_plan_and_bonus_for", no_commons)
        async with session_scope(session_factory) as session:
            with pytest.raises(plans.EntitlementExceeded) as exc:
                await crud.export_commons(session, orgs["reader"], _exporting(config))
        assert exc.value.metric == "commons_access"


class TestWhatTheClientNeedsToMatchOffline:
    async def test_solutions_are_full_not_previews(self, session_factory, config, orgs):
        """`browse_commons` truncates on purpose -- it is a shop window. This
        is the opposite: a corpus of truncated solutions cannot answer a
        question offline, which is the only reason to hold one."""
        long_solution = "step. " * 400
        await _seed(session_factory, orgs["operator"], solution=long_solution)
        async with session_scope(session_factory) as session:
            result = await crud.export_commons(session, orgs["reader"], _exporting(config))
        assert result["entries"][0]["solution_text"] == long_solution

    async def test_standing_travels_with_the_entry(self, session_factory, config, orgs):
        """A client that cannot see standing would rank a disputed entry as
        an equal answer, which is the one thing the Hub's own ranking is
        careful not to do."""
        await _seed(session_factory, orgs["operator"])
        async with session_scope(session_factory) as session:
            result = await crud.export_commons(session, orgs["reader"], _exporting(config))
        entry = result["entries"][0]
        assert entry["standing"] in commons.VALID_STANDINGS
        assert "trust" in entry and "votes" in entry


class TestItIsNotMeteredPerFailure:
    async def test_exporting_does_not_spend_the_consultation_allowance(
        self, session_factory, config, orgs
    ):
        """The allowance prices per-failure consultations. This is one bulk
        read that REPLACES them, and charging per record would price the
        private path far above the one that discloses more."""
        await _seed(session_factory, orgs["operator"], "a")
        await _seed(session_factory, orgs["operator"], "b")
        async with session_scope(session_factory) as session:
            before = await crud.entitlements(session, orgs["reader"])
        async with session_scope(session_factory) as session:
            await crud.export_commons(session, orgs["reader"], _exporting(config))
        async with session_scope(session_factory) as session:
            after = await crud.entitlements(session, orgs["reader"])
        assert (after["commons_queries"]["used"]
                == before["commons_queries"]["used"])

    async def test_exporting_does_not_credit_commons_hits(
        self, session_factory, config, orgs
    ):
        """`commons_hits` is the operator's quality signal for its own
        content -- 'this entry covered a real recurring failure'. A bulk
        download is not that, and crediting it would make the one metric
        that resists noise trivially inflatable."""
        trace_id = await _seed(session_factory, orgs["operator"])
        async with session_scope(session_factory) as session:
            await crud.export_commons(session, orgs["reader"], _exporting(config))
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
        assert trace.commons_hits == 0
