from __future__ import annotations

import json
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
        await _seed_entry(session_factory, orgs["operator"])
        async with session_scope(session_factory) as session:
            before = await crud.entitlements(session, orgs["reader"])
        for _ in range(3):
            await _browse(session_factory, orgs["reader"])
        async with session_scope(session_factory) as session:
            after = await crud.entitlements(session, orgs["reader"])
        assert (after["commons_queries"]["used"] == before["commons_queries"]["used"])

    async def test_browsing_does_not_count_as_a_hit(self, session_factory, orgs):
        entry_id = await _seed_entry(session_factory, orgs["operator"], hits=7)
        await _browse(session_factory, orgs["reader"])
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, entry_id)
        assert trace.commons_hits == 7


class TestGovernanceIsVisible:
    async def test_each_entry_carries_its_standing(self, session_factory, orgs):
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
        assert titles == ["solid one", "disputed one"]
        assert result["entries"][-1]["standing"] == commons.STANDING_DISPUTED

    async def test_a_stale_entry_says_so(self, session_factory, orgs):
        await _seed_entry(
            session_factory, orgs["operator"], trust=1.0, votes=5,
            review_after=datetime.now(timezone.utc) - timedelta(days=1),
        )
        result = await _browse(session_factory, orgs["reader"])
        assert result["entries"][0]["standing"] == commons.STANDING_STALE


async def _vote(session_factory, org_id, trace_id, vote_type, **kw):
    async with session_scope(session_factory) as session:
        return await crud.vote_trace(session, org_id, trace_id, vote_type, actor="test", **kw)


async def _fresh_org(session_factory, name):
    async with session_scope(session_factory) as session:
        org = Organization(name=name)
        session.add(org)
        await session.flush()
        return org.id


async def _first(session_factory, org_id):
    return (await _browse(session_factory, org_id))["entries"][0]


class TestTheCatalogueShowsWhyNotJustWhat:
    async def test_a_vote_reason_reaches_the_catalogue(
        self, session_factory, orgs, establish_orgs
    ):
        entry_id = await _seed_entry(session_factory, orgs["operator"])
        await establish_orgs(orgs["reader"])
        await _vote(session_factory, orgs["reader"], entry_id, "down", feedback_tag="outdated")

        assert (await _first(session_factory, orgs["reader"]))["concerns"] == {"outdated": 1}

    async def test_reasons_are_counted_per_tag_not_lumped_together(
        self, session_factory, orgs, establish_orgs
    ):
        entry_id = await _seed_entry(session_factory, orgs["operator"])
        third = await _fresh_org(session_factory, "third")
        await establish_orgs(orgs["reader"], orgs["other"], third)
        await _vote(session_factory, orgs["reader"], entry_id, "down", feedback_tag="outdated")
        await _vote(session_factory, orgs["other"], entry_id, "down", feedback_tag="outdated")
        await _vote(session_factory, third, entry_id, "down", feedback_tag="wrong")

        assert (await _first(session_factory, orgs["reader"]))["concerns"] == {
            "outdated": 2, "wrong": 1,
        }

    async def test_a_security_concern_raised_alongside_an_upvote_still_counts(
        self, session_factory, orgs, establish_orgs
    ):
        entry_id = await _seed_entry(session_factory, orgs["operator"])
        await establish_orgs(orgs["reader"])
        await _vote(session_factory, orgs["reader"], entry_id, "up",
                    feedback_tag="security_concern")

        assert (await _first(session_factory, orgs["reader"]))["concerns"] == {
            "security_concern": 1,
        }

    async def test_a_security_concern_shows_even_while_standing_is_unproven(
        self, session_factory, orgs, establish_orgs
    ):
        entry_id = await _seed_entry(session_factory, orgs["operator"], trust=0.5, votes=0)
        await establish_orgs(orgs["reader"])
        await _vote(session_factory, orgs["reader"], entry_id, "down",
                    feedback_tag="security_concern")

        entry = await _first(session_factory, orgs["reader"])
        assert entry["standing"] == commons.STANDING_UNPROVEN
        assert entry["concerns"] == {"security_concern": 1}

    async def test_an_untagged_vote_contributes_no_reason(
        self, session_factory, orgs, establish_orgs
    ):
        entry_id = await _seed_entry(session_factory, orgs["operator"])
        await establish_orgs(orgs["reader"])
        await _vote(session_factory, orgs["reader"], entry_id, "down")

        assert (await _first(session_factory, orgs["reader"]))["concerns"] == {}

    async def test_changing_a_vote_replaces_its_reason_rather_than_adding_one(
        self, session_factory, orgs, establish_orgs
    ):
        entry_id = await _seed_entry(session_factory, orgs["operator"])
        await establish_orgs(orgs["reader"])
        await _vote(session_factory, orgs["reader"], entry_id, "down", feedback_tag="outdated")
        await _vote(session_factory, orgs["reader"], entry_id, "down", feedback_tag="wrong")

        assert (await _first(session_factory, orgs["reader"]))["concerns"] == {"wrong": 1}


class TestReasonsObeyTheSameAntiAbuseBarAsStanding:
    async def test_a_fresh_orgs_flag_does_not_appear(self, session_factory, orgs):
        entry_id = await _seed_entry(session_factory, orgs["operator"])
        sock = await _fresh_org(session_factory, "sock")
        await _vote(session_factory, sock, entry_id, "down", feedback_tag="security_concern")

        assert (await _first(session_factory, orgs["reader"]))["concerns"] == {}

    async def test_five_minted_orgs_cannot_brand_an_entry_a_security_risk(
        self, session_factory, orgs
    ):
        entry_id = await _seed_entry(session_factory, orgs["operator"])
        for i in range(5):
            sock = await _fresh_org(session_factory, f"smear-{i}")
            await _vote(session_factory, sock, entry_id, "down",
                        feedback_tag="security_concern")

        assert (await _first(session_factory, orgs["reader"]))["concerns"] == {}

    async def test_a_flag_starts_counting_once_its_org_qualifies(
        self, session_factory, orgs, establish_orgs
    ):
        entry_id = await _seed_entry(session_factory, orgs["operator"])
        await _vote(session_factory, orgs["reader"], entry_id, "down",
                    feedback_tag="security_concern")
        assert (await _first(session_factory, orgs["reader"]))["concerns"] == {}

        await establish_orgs(orgs["reader"])
        assert (await _first(session_factory, orgs["reader"]))["concerns"] == {
            "security_concern": 1,
        }


class TestNoCrossTenantDisclosureThroughTheReasons:
    async def test_the_reasons_never_name_an_organisation(
        self, session_factory, orgs, establish_orgs
    ):
        entry_id = await _seed_entry(session_factory, orgs["operator"])
        await establish_orgs(orgs["reader"])
        await _vote(session_factory, orgs["reader"], entry_id, "down",
                    feedback_tag="wrong", feedback_text="broke our billing service")

        blob = json.dumps(await _first(session_factory, orgs["other"]))
        assert orgs["reader"] not in blob

    async def test_free_text_feedback_never_reaches_another_org(
        self, session_factory, orgs, establish_orgs
    ):
        secret = "internal-host-db7.corp.example"
        entry_id = await _seed_entry(session_factory, orgs["operator"])
        await establish_orgs(orgs["reader"])
        await _vote(session_factory, orgs["reader"], entry_id, "down",
                    feedback_tag="wrong", feedback_text=secret)

        entry = await _first(session_factory, orgs["other"])
        assert secret not in json.dumps(entry)
        assert entry["concerns"] == {"wrong": 1}
