from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from hub import crud
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import KnowledgeBaseSubmission, Organization

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        organization = Organization(name="Participating Co")
        session.add(organization)
        await session.flush()
        return organization.id


async def _set_opt_in(session_factory, org_id, value: bool) -> None:
    async with session_scope(session_factory) as session:
        organization = await session.get(Organization, org_id)
        organization.commons_auto_contribute = value


async def _contribute(session_factory, config, org_id, **kw):
    defaults = dict(
        title="Connection pool exhausted",
        context_text="every request queued behind a saturated pool",
        solution_text="raised pool_size and set a command timeout",
        tags=["postgres"],
        agent_type="code",
    )
    defaults.update(kw)
    async with session_scope(session_factory) as session:
        return await crud.contribute_trace(
            session, org_id, config, make_rate_limiter(config), **defaults
        )


async def _submissions(session_factory, org_id):
    async with session_scope(session_factory) as session:
        return (
            await session.execute(
                select(KnowledgeBaseSubmission).where(KnowledgeBaseSubmission.org_id == org_id)
            )
        ).scalars().all()


class TestOffByDefault:
    async def test_a_new_org_is_not_participating(self, session_factory, org):
        async with session_scope(session_factory) as session:
            organization = await session.get(Organization, org)
        assert organization.commons_auto_contribute is False

    async def test_contributing_proposes_nothing_by_default(
        self, session_factory, config, org
    ):
        result = await _contribute(session_factory, config, org)
        assert result["auto_proposed_to_commons"] is False
        assert await _submissions(session_factory, org) == []


class TestOptedIn:
    async def test_contributing_also_proposes(self, session_factory, config, org):
        await _set_opt_in(session_factory, org, True)
        result = await _contribute(session_factory, config, org)
        assert result["auto_proposed_to_commons"] is True

        submissions = await _submissions(session_factory, org)
        assert len(submissions) == 1
        assert submissions[0].title == "Connection pool exhausted"

    async def test_the_proposal_is_pending_not_published(
        self, session_factory, config, org
    ):
        await _set_opt_in(session_factory, org, True)
        await _contribute(session_factory, config, org)
        submissions = await _submissions(session_factory, org)
        assert submissions[0].status == "pending"

    async def test_no_commons_visible_trace_is_created(self, session_factory, config, org):
        from hub.models import Trace
        await _set_opt_in(session_factory, org, True)
        await _contribute(session_factory, config, org)
        async with session_scope(session_factory) as session:
            visible = (
                await session.execute(select(Trace).where(*crud.commons_visible()))
            ).scalars().all()
        assert visible == []

    async def test_turning_it_back_off_stops_proposing(self, session_factory, config, org):
        await _set_opt_in(session_factory, org, True)
        await _contribute(session_factory, config, org, title="first one")
        await _set_opt_in(session_factory, org, False)
        result = await _contribute(session_factory, config, org, title="second one")
        assert result["auto_proposed_to_commons"] is False
        titles = {s.title for s in await _submissions(session_factory, org)}
        assert titles == {"first one"}


class TestWhatItRefusesToForward:
    async def test_a_quarantined_trace_is_never_proposed(
        self, session_factory, config, org
    ):
        await _set_opt_in(session_factory, org, True)
        spam = " ".join(
            f"http://spam{i}.example.com" for i in range(config.suspect_url_threshold + 1)
        )
        result = await _contribute(
            session_factory, config, org, title="spammy", context_text=spam)
        assert result["quarantined"] is True
        assert result["auto_proposed_to_commons"] is False
        assert await _submissions(session_factory, org) == []


class TestItNeverFailsTheCapture:
    async def test_a_failing_proposal_still_stores_the_trace(
        self, session_factory, config, org, monkeypatch
    ):
        async def exploding_submit(*a, **kw):
            raise RuntimeError("the review queue is on fire")

        monkeypatch.setattr(crud, "submit_kb_entry", exploding_submit)
        await _set_opt_in(session_factory, org, True)

        result = await _contribute(session_factory, config, org)
        assert result["id"]
        assert result["auto_proposed_to_commons"] is False

        from hub.models import Trace
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, result["id"])
        assert trace is not None


class TestIdempotency:
    async def test_a_retried_contribution_does_not_propose_twice(
        self, session_factory, config, org
    ):
        await _set_opt_in(session_factory, org, True)
        first = await _contribute(session_factory, config, org, idempotency_key="abc-123")
        second = await _contribute(session_factory, config, org, idempotency_key="abc-123")
        assert first["id"] == second["id"]
        assert len(await _submissions(session_factory, org)) == 1
