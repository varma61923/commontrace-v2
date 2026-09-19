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


# --- The grounds for a verdict, not just the verdict --------------------


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
    """Standing is a verdict; these are its grounds.

    "disputed" flattens three very different situations -- stale, wrong,
    dangerous -- into one word, and each is a different decision for
    somebody about to apply the fix. A repository that publishes a verdict
    and withholds the reason asks to be trusted rather than read, which is
    the opposite of what makes a wiki's judgements worth anything.
    """

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
        """"It worked, but it worries me" is still a security report, and
        the most safety-relevant thing this system can receive. Counting
        reasons only on down-votes would silently discard it."""
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
        """The safety case this whole feature exists for. Standing is
        deliberately conservative -- one voice never condemns an entry --
        so a single security flag leaves it reading `unproven`, "not
        enough votes yet to say either way". Without the reason surfaced,
        a reader applies a fix somebody explicitly flagged as dangerous
        and sees no warning at all."""
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
        """vote_trace upserts on (trace, org), so one org holds one reason.
        A count that grew per click would let a single org manufacture a
        pile of flags by itself."""
        entry_id = await _seed_entry(session_factory, orgs["operator"])
        await establish_orgs(orgs["reader"])
        await _vote(session_factory, orgs["reader"], entry_id, "down", feedback_tag="outdated")
        await _vote(session_factory, orgs["reader"], entry_id, "down", feedback_tag="wrong")

        assert (await _first(session_factory, orgs["reader"]))["concerns"] == {"wrong": 1}


class TestReasonsObeyTheSameAntiAbuseBarAsStanding:
    """The boundary that keeps this from becoming a new smear vector.

    If flags counted from any org, minting five would let one person brand
    a rival's entry a security risk -- the same attack the standing bar
    stops, reopened one field over. Both numbers read through
    `crud._established_voters_only` precisely so they cannot drift apart.
    """

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
        """Withheld, never discarded -- the same property the standing bar
        has, so a legitimate newcomer's report is delayed rather than
        thrown away."""
        entry_id = await _seed_entry(session_factory, orgs["operator"])
        await _vote(session_factory, orgs["reader"], entry_id, "down",
                    feedback_tag="security_concern")
        assert (await _first(session_factory, orgs["reader"]))["concerns"] == {}

        await establish_orgs(orgs["reader"])
        assert (await _first(session_factory, orgs["reader"]))["concerns"] == {
            "security_concern": 1,
        }


class TestNoCrossTenantDisclosureThroughTheReasons:
    """The governance layer is exactly where nobody would think to look for
    a tenant leak, which is why it is checked explicitly."""

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
        """`feedback_text` is written by one customer and would be rendered
        to every other: a leak surface (a pasted trace naming internal
        hosts) and an injection surface. It stays with the operator's
        review queue, read by a human. The closed vocabulary carries the
        actionable part without either risk."""
        secret = "internal-host-db7.corp.example"
        entry_id = await _seed_entry(session_factory, orgs["operator"])
        await establish_orgs(orgs["reader"])
        await _vote(session_factory, orgs["reader"], entry_id, "down",
                    feedback_tag="wrong", feedback_text=secret)

        entry = await _first(session_factory, orgs["other"])
        assert secret not in json.dumps(entry)
        # ...while the actionable part did come through.
        assert entry["concerns"] == {"wrong": 1}
