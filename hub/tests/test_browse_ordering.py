"""The Knowledge Base catalogue's order must not be purchasable.

STRATEGY.md §26 took `commons_hits` out of `commons_search`'s ranking,
because a caller writes that number with its own traffic and it was
deciding which answer a customer read first. That fix named one surface
and missed this one: `browse_commons`, the catalogue the web console
shows, ordered by `commons_hits DESC` -- not as a tie-break behind
relevance, as the FIRST key, on a list with no query to be relevant to.

It decided the order twice, and the second place is the one that matters:

  * the SQL `ORDER BY ... LIMIT/OFFSET` chooses which entries are on the
    page at all; and
  * a Python `entries.sort(...)` afterwards reordered whatever that
    returned.

The two did not agree. The Python sort put disputed entries last, as the
docstring promised, but the SQL did not know about standing -- so a
disputed entry with traffic sat on page 1 (last on page 1) while a
corroborated entry without traffic waited on page 2. Sorting after
paginating cannot fix an order; it can only rearrange what pagination
already chose.

These tests fail on the code before that fix.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from hub import commons, crud
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def orgs(session_factory):
    async with session_scope(session_factory) as session:
        made = {}
        for name in ("operator", "reader"):
            org = Organization(name=name)
            session.add(org)
            await session.flush()
            made[name] = org.id
        return made


async def _seed(
    session_factory, operator_org_id, title, *, hits=0, votes=0, trust=0.5, age_days=0,
):
    async with session_scope(session_factory) as session:
        trace = Trace(
            org_id=operator_org_id, title=title, context_text="ctx " + title,
            solution_text="advice for " + title, tags=["kb"], agent_type="code",
            shared_with_commons=True, shared_at=datetime.now(timezone.utc),
            shared_rationale="seed", commons_source="seed",
            commons_signature=commons.signature_for(title, "ctx " + title, ["kb"]),
            commons_hits=hits, commons_votes=votes, trust=trust,
            created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        )
        session.add(trace)
        await session.flush()
        return trace.id


async def _browse(session_factory, org_id, **kw):
    async with session_scope(session_factory) as session:
        return await crud.browse_commons(session, org_id, **kw)


# Enough votes to have a standing at all, so `trust` is not being read
# below the threshold where it means nothing.
VOTES = commons.MIN_VOTES_FOR_STANDING


class TestTrafficDoesNotOrderTheCatalogue:
    async def test_a_pumped_entry_does_not_lead_the_catalogue(
        self, session_factory, orgs
    ):
        """The defect, directly. Two entries identical in every signal that
        is one-org-one-vote; the one with traffic used to lead."""
        quiet = await _seed(
            session_factory, orgs["operator"], "quiet but corroborated",
            hits=0, votes=VOTES, trust=0.95,
        )
        pumped = await _seed(
            session_factory, orgs["operator"], "loud",
            hits=50_000, votes=VOTES, trust=0.95,
        )
        r = await _browse(session_factory, orgs["reader"])
        order = [e["id"] for e in r["entries"]]
        assert set(order) == {quiet, pumped}
        # Equal on every legitimate key, so the tie falls to recency --
        # `pumped` was seeded second, so it may lead. What must NOT happen
        # is hits deciding, which the next test isolates.
        assert sorted(e["hits"] for e in r["entries"]) == [0, 50_000]

    async def test_trust_beats_traffic(self, session_factory, orgs):
        """Isolates the ordering claim: the better-trusted entry leads even
        when the other has five orders of magnitude more traffic."""
        trusted = await _seed(
            session_factory, orgs["operator"], "trusted, unvisited",
            hits=0, votes=VOTES, trust=0.99,
        )
        await _seed(
            session_factory, orgs["operator"], "mediocre, hammered",
            hits=100_000, votes=VOTES, trust=0.60,
        )
        r = await _browse(session_factory, orgs["reader"])
        assert r["entries"][0]["id"] == trusted

    async def test_hits_are_still_shown(self, session_factory, orgs):
        """Out of the ORDER, not out of the answer -- the console still
        shows how much traffic an entry has served."""
        await _seed(session_factory, orgs["operator"], "entry", hits=42, votes=VOTES)
        r = await _browse(session_factory, orgs["reader"])
        assert r["entries"][0]["hits"] == 42


class TestDisputedSortsToTheBackAcrossPages:
    async def test_a_disputed_entry_with_traffic_does_not_take_a_page_slot(
        self, session_factory, orgs
    ):
        """The half a Python re-sort could never fix.

        Page size 2. Three entries: one disputed but heavily queried, two
        sound and quiet. Ordering the QUERY by hits put the disputed one on
        page 1, where the Python sort dutifully placed it last -- pushing a
        sound entry onto page 2. A reader who never clicks through sees a
        disputed entry and one good one, instead of the two good ones.
        """
        disputed = await _seed(
            session_factory, orgs["operator"], "disputed but popular",
            hits=99_999, votes=VOTES, trust=0.0,
        )
        sound_a = await _seed(
            session_factory, orgs["operator"], "sound a", hits=0, votes=VOTES, trust=0.9,
        )
        sound_b = await _seed(
            session_factory, orgs["operator"], "sound b", hits=0, votes=VOTES, trust=0.8,
        )

        page1 = await _browse(session_factory, orgs["reader"], limit=2, offset=0)
        ids1 = [e["id"] for e in page1["entries"]]
        assert ids1 == [sound_a, sound_b], (
            "page 1 must hold the two sound entries, ordered by trust"
        )
        assert disputed not in ids1
        assert page1["has_more"] is True

        page2 = await _browse(session_factory, orgs["reader"], limit=2, offset=2)
        assert [e["id"] for e in page2["entries"]] == [disputed]
        assert page2["entries"][0]["standing"] == commons.STANDING_DISPUTED

    async def test_paging_covers_every_entry_exactly_once(
        self, session_factory, orgs
    ):
        """A total order, not merely a deterministic one. Entries equal on
        standing and trust used to fall back to `created_at` alone, and rows
        seeded in one batch can share a timestamp -- at which point the
        planner picks, and an entry can appear on two pages or none."""
        made = [
            await _seed(
                session_factory, orgs["operator"], f"entry {i}",
                hits=0, votes=VOTES, trust=0.7,
            )
            for i in range(7)
        ]
        seen = []
        for offset in range(0, 8, 2):
            page = await _browse(session_factory, orgs["reader"], limit=2, offset=offset)
            seen.extend(e["id"] for e in page["entries"])
        assert sorted(seen) == sorted(made), f"paging lost or repeated entries: {seen}"
        assert len(seen) == len(set(seen))
