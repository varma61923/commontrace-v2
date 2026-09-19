"""`crud.browse_commons` -- the Knowledge Base as a catalogue.

The two query surfaces that existed both answer "what does the corpus know
about MY failure", and both take a MinHash signature so the caller never
sends its failure text. That is right for an agent mid-incident and wrong
for a person deciding whether this repository is worth opting into at all:
they have no failure yet, and nothing to sign.

So what these tests pin is mostly the BOUNDARIES that a new read path over
shared content could quietly lose:

1. The tenant boundary. This reads rows the caller did not write, which is
   exactly where `commons_source == "seed"` has to hold -- one org's
   private trace must never appear in another's browse, and a retracted
   entry must stop appearing in all of them.
2. The plan boundary. `commons_access` still gates it; an org without the
   Knowledge Base does not get to read it through a second door.
3. The metering decision, asserted rather than assumed: browsing does NOT
   spend a monthly consultation, and a test says so, because "does the
   catalogue cost a query" is a product decision that should fail loudly
   if someone changes it by accident.
4. Previews, not solutions. What comes back is the shop window.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from hub import commons, crud, plans
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def orgs(session_factory):
    async with session_scope(session_factory) as session:
        made = {}
        for name in ("reader", "operator", "other"):
            org = Organization(name=name)
            session.add(org)
            await session.flush()
            made[name] = org.id
        return made


async def _seed_entry(
    session_factory,
    org_id,
    title="Connection pool exhausted",
    context="every request queued behind a saturated pool",
    solution="raise pool_size and set a command timeout",
    tags=None,
    *,
    hits=0,
    trust=1.0,
    votes=0,
    retracted=False,
    review_after=None,
):
    tags = tags if tags is not None else ["postgres"]
    async with session_scope(session_factory) as session:
        trace = Trace(
            org_id=org_id,
            title=title,
            context_text=context,
            solution_text=solution,
            tags=tags,
            agent_type="code",
            shared_with_commons=True,
            shared_at=datetime.now(timezone.utc),
            shared_rationale="test fixture: operator-curated",
            commons_signature=commons.signature_for(title, context, tags),
            commons_source="seed",
            commons_hits=hits,
            commons_votes=votes,
            trust=trust,
            commons_review_after=review_after,
            commons_retracted_at=datetime.now(timezone.utc) if retracted else None,
        )
        session.add(trace)
        await session.flush()
        return trace.id


async def _browse(session_factory, org_id, **kw):
    async with session_scope(session_factory) as session:
        return await crud.browse_commons(session, org_id, **kw)


class TestItReturnsTheCorpus:
    async def test_a_seeded_entry_is_listed(self, session_factory, orgs):
        await _seed_entry(session_factory, orgs["operator"])
        result = await _browse(session_factory, orgs["reader"])
        assert result["total"] == 1
        assert result["entries"][0]["title"] == "Connection pool exhausted"

    async def test_it_returns_previews_not_full_solutions(self, session_factory, orgs):
        """The shop window, not the goods: a caller who wants the whole
        solution consults for it, and that consultation meters."""
        long_solution = "step. " * 400
        await _seed_entry(session_factory, orgs["operator"], solution=long_solution)
        result = await _browse(session_factory, orgs["reader"])
        entry = result["entries"][0]
        assert "solution_preview" in entry
        assert "solution_text" not in entry
        assert len(entry["solution_preview"]) < len(long_solution)
        assert entry["solution_preview"].endswith("…")

    async def test_it_pages(self, session_factory, orgs):
        for i in range(4):
            await _seed_entry(session_factory, orgs["operator"], title=f"entry {i}")
        first = await _browse(session_factory, orgs["reader"], limit=2)
        assert len(first["entries"]) == 2
        assert first["total"] == 4
        assert first["has_more"] is True

        last = await _browse(session_factory, orgs["reader"], limit=2, offset=2)
        assert len(last["entries"]) == 2
        assert last["has_more"] is False

    async def test_an_absurd_limit_is_clamped_not_rejected(self, session_factory, orgs):
        await _seed_entry(session_factory, orgs["operator"])
        result = await _browse(session_factory, orgs["reader"], limit=10_000)
        assert result["limit"] == crud.MAX_BROWSE_COMMONS_LIMIT

    async def test_it_filters_by_tag(self, session_factory, orgs):
        await _seed_entry(session_factory, orgs["operator"], title="pg one", tags=["postgres"])
        await _seed_entry(session_factory, orgs["operator"], title="redis one", tags=["redis"])
        result = await _browse(session_factory, orgs["reader"], tag="redis")
        assert [e["title"] for e in result["entries"]] == ["redis one"]
        assert result["total"] == 1


class TestTheTenantBoundary:
    async def test_a_private_trace_is_never_listed(self, session_factory, orgs):
        """The property the whole Hub is built on. A row that is simply an
        org's own trace -- no commons_source, never curated -- must not
        appear in anyone's browse, including its own org's."""
        async with session_scope(session_factory) as session:
            private = Trace(
                org_id=orgs["other"], title="our private incident",
                context_text="internal", solution_text="internal",
                tags=["postgres"], agent_type="code",
            )
            session.add(private)
            await session.flush()

        for viewer in ("reader", "other"):
            result = await _browse(session_factory, orgs[viewer])
            assert result["entries"] == [], viewer
            assert result["total"] == 0, viewer

    async def test_a_retracted_entry_stops_being_listed(self, session_factory, orgs):
        """An operator who pulls an entry expects it to stop being served,
        and that has to mean every read path -- this one included."""
        await _seed_entry(session_factory, orgs["operator"], title="pulled", retracted=True)
        result = await _browse(session_factory, orgs["reader"])
        assert result["entries"] == []

    async def test_a_quarantined_entry_is_not_listed(self, session_factory, orgs):
        entry_id = await _seed_entry(session_factory, orgs["operator"])
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, entry_id)
            trace.quarantined = True
        result = await _browse(session_factory, orgs["reader"])
        assert result["entries"] == []


class TestThePlanBoundary:
    async def test_a_plan_without_commons_access_is_refused(self, session_factory, orgs):
        """Not a second door into the Knowledge Base for an org whose plan
        excludes it."""
        no_access = next(
            (name for name, plan in plans.PLANS.items() if not plan.commons_access), None
        )
        if no_access is None:
            pytest.skip("every current plan includes commons_access")
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, orgs["reader"])
            org.plan = no_access
        with pytest.raises(plans.EntitlementExceeded):
            await _browse(session_factory, orgs["reader"])


class TestMetering:
    async def test_browsing_does_not_spend_a_consultation(self, session_factory, orgs):
        """A deliberate product decision, asserted so it cannot change by
        accident: metering the catalogue would tax exactly the moment this
        repository is trying to earn -- someone deciding whether to opt in."""
        await _seed_entry(session_factory, orgs["operator"])
        async with session_scope(session_factory) as session:
            before = await crud.entitlements(session, orgs["reader"])
        for _ in range(3):
            await _browse(session_factory, orgs["reader"])
        async with session_scope(session_factory) as session:
            after = await crud.entitlements(session, orgs["reader"])
        assert (after["commons_queries"]["used"] == before["commons_queries"]["used"])

    async def test_browsing_does_not_count_as_a_hit(self, session_factory, orgs):
        """`commons_hits` is the "this entry actually helped someone"
        signal that kb-review orders by. Looking at a catalogue entry is
        not the same as it resolving an incident, and inflating the metric
        here would quietly corrupt the operator's review queue."""
        entry_id = await _seed_entry(session_factory, orgs["operator"], hits=7)
        await _browse(session_factory, orgs["reader"])
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, entry_id)
        assert trace.commons_hits == 7


class TestGovernanceIsVisible:
    async def test_each_entry_carries_its_standing(self, session_factory, orgs):
        """The Stack-Overflow-shaped part: what the field thinks of an
        entry travels WITH the entry, rather than only tilting a ranking
        the reader cannot see."""
        await _seed_entry(session_factory, orgs["operator"], trust=1.0, votes=5)
        result = await _browse(session_factory, orgs["reader"])
        entry = result["entries"][0]
        assert entry["standing"] == commons.STANDING_ESTABLISHED
        assert entry["votes"] == 5
        assert entry["trust"] == 1.0

    async def test_an_unproven_entry_says_so(self, session_factory, orgs):
        await _seed_entry(session_factory, orgs["operator"], trust=1.0, votes=0)
        result = await _browse(session_factory, orgs["reader"])
        assert result["entries"][0]["standing"] == commons.STANDING_UNPROVEN

    async def test_a_disputed_entry_is_sorted_to_the_back_not_hidden(
        self, session_factory, orgs
    ):
        """Identical reasoning to commons_search's: "it did not work for
        the fleets who tried it" is information. Hiding it would answer a
        browse with a rosier corpus than the one that exists."""
        await _seed_entry(
            session_factory, orgs["operator"], title="disputed one",
            trust=0.1, votes=9, hits=500,
        )
        await _seed_entry(
            session_factory, orgs["operator"], title="solid one",
            trust=1.0, votes=5, hits=1,
        )
        result = await _browse(session_factory, orgs["reader"])
        titles = [e["title"] for e in result["entries"]]
        # Despite 500 hits against 1, the disputed entry ranks last.
        assert titles == ["solid one", "disputed one"]
        assert result["entries"][-1]["standing"] == commons.STANDING_DISPUTED

    async def test_a_stale_entry_says_so(self, session_factory, orgs):
        await _seed_entry(
            session_factory, orgs["operator"], trust=1.0, votes=5,
            review_after=datetime.now(timezone.utc) - timedelta(days=1),
        )
        result = await _browse(session_factory, orgs["reader"])
        assert result["entries"][0]["standing"] == commons.STANDING_STALE
