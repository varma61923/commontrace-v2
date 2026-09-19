"""`Organization.commons_auto_contribute` -- the opt IN to giving back.

This is the one flag in the schema that causes an org's own incident text
to leave its tenant, so it is also the one that needs the most explicit
tests. What they pin, in order of how badly getting it wrong would hurt:

1. **Off by default.** A migration, a fresh org, a restored backup -- none
   of them may produce an org that is participating without someone having
   said so.
2. **It changes who proposes, never what is published.** An auto-proposed
   entry lands in exactly the same operator-review queue a hand-written one
   does, and stays invisible to every other org until accepted. If this
   ever became a publish path it would re-open the org-to-org sharing
   design hub/plans.py retired over adverse selection.
3. **Quarantined traces are never proposed.** `suspicion_reason` flagged
   the content as probable spam; forwarding it anyway would make every
   participating org a spam relay into the operator's queue.
4. **A failed proposal never fails the capture.** The trace is the primary
   artifact and is already committed; turning a rate-limited proposal into
   a contribute error would make the caller retry the whole contribution.
"""
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
        """The safe value, asserted directly rather than inferred from the
        column default -- a server_default, an ORM default and a migration
        backfill are three different things and only one of them is what a
        fresh org actually gets."""
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
        """The whole safety argument for this flag. It changes who
        proposes; an operator still decides what the corpus holds."""
        await _set_opt_in(session_factory, org, True)
        await _contribute(session_factory, config, org)
        submissions = await _submissions(session_factory, org)
        assert submissions[0].status == "pending"

    async def test_no_commons_visible_trace_is_created(self, session_factory, config, org):
        """Belt and braces on the same property, asserted against the
        boundary filter itself rather than the submission's status: opting
        in must not put anything into the corpus other orgs read."""
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
        """Otherwise every participating org becomes a spam relay into the
        operator's review queue -- the exact content `suspicion_reason`
        exists to stop travelling."""
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
        """The trace is already committed by the time the proposal runs.
        Surfacing a proposal failure as a contribute error would make the
        caller retry the whole contribution -- re-contributing the trace,
        not just re-proposing it."""
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
        """The proposal inherits the contribution's idempotency by keying
        off the trace id, so a client retrying a contribute that already
        succeeded cannot produce a second proposal for the same trace."""
        await _set_opt_in(session_factory, org, True)
        first = await _contribute(session_factory, config, org, idempotency_key="abc-123")
        second = await _contribute(session_factory, config, org, idempotency_key="abc-123")
        # Same trace back both times -- the replay path.
        assert first["id"] == second["id"]
        assert len(await _submissions(session_factory, org)) == 1
