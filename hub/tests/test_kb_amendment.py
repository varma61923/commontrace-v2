"""Correcting a Knowledge Base entry must leave the entry in place.

`commons_visible()` excludes superseded rows, and `amend_trace` INSERTs a
new row rather than mutating the original. Those two facts combined meant
that amending a Knowledge Base entry removed it from the Knowledge Base:
the original went invisible the moment it was superseded, and the new row
was a plain org trace.

That sits directly on the path this product's governance is built around.
`kb-review` tells an operator "this entry is disputed" or "this entry is
stale"; the natural remedy is to amend it; and amending it deleted it,
silently, along with its accumulated hits. Correcting an article is the
most ordinary act in a wiki and has to leave the article in place.

What these tests pin is that correction preserves membership, that it does
NOT preserve the votes cast against text that no longer exists, and that a
customer's own trace keeps the old behaviour -- re-sharing a correction
stays an explicit decision for content that is the org's to publish.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from hub import commons, crud
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def orgs(session_factory):
    async with session_scope(session_factory) as session:
        made = {}
        for name in ("operator", "reader", "customer"):
            org = Organization(name=name)
            session.add(org)
            await session.flush()
            made[name] = org.id
        return made


async def _seed_kb(session_factory, operator_org_id, title="Stripe webhook idempotency",
                   *, hits=0, votes=0, trust=1.0, review_after=None):
    async with session_scope(session_factory) as session:
        trace = Trace(
            org_id=operator_org_id, title=title, context_text="ctx " + title,
            solution_text="the original advice", tags=["stripe"], agent_type="code",
            shared_with_commons=True, shared_at=datetime.now(timezone.utc),
            shared_rationale="seed", commons_source="seed",
            commons_signature=commons.signature_for(title, "ctx " + title, ["stripe"]),
            commons_hits=hits, commons_votes=votes, trust=trust,
            commons_review_after=review_after,
        )
        session.add(trace)
        await session.flush()
        return trace.id


async def _amend(session_factory, config, org_id, trace_id, **kw):
    async with session_scope(session_factory) as session:
        return await crud.amend_trace(
            session, org_id, trace_id, config, make_rate_limiter(config),
            actor="operator-console", **kw,
        )


async def _browse(session_factory, org_id):
    async with session_scope(session_factory) as session:
        return await crud.browse_commons(session, org_id)


class TestCorrectingAnEntryKeepsItInTheKnowledgeBase:
    async def test_an_amended_entry_is_still_served(self, session_factory, config, orgs):
        """The regression. Before the fix this went 1 entry -> 0 entries."""
        await _seed_kb(session_factory, orgs["operator"])
        assert (await _browse(session_factory, orgs["reader"]))["total"] == 1

        entry_id = (await _browse(session_factory, orgs["reader"]))["entries"][0]["id"]
        await _amend(session_factory, config, orgs["operator"], entry_id,
                     solution_text="the corrected advice")

        after = await _browse(session_factory, orgs["reader"])
        assert after["total"] == 1, "correcting an entry must not remove it from the corpus"

    async def test_the_reader_sees_the_corrected_text_not_the_old(
        self, session_factory, config, orgs
    ):
        entry_id = await _seed_kb(session_factory, orgs["operator"])
        await _amend(session_factory, config, orgs["operator"], entry_id,
                     solution_text="the corrected advice")

        entry = (await _browse(session_factory, orgs["reader"]))["entries"][0]
        assert "corrected" in entry["solution_preview"]
        assert entry["id"] != entry_id, "the amendment is a new row, not a mutation"

    async def test_only_one_version_is_served_never_both(
        self, session_factory, config, orgs
    ):
        """The superseded original must stay invisible. Serving both would
        answer one question with two contradictory entries."""
        entry_id = await _seed_kb(session_factory, orgs["operator"])
        await _amend(session_factory, config, orgs["operator"], entry_id,
                     solution_text="the corrected advice")

        result = await _browse(session_factory, orgs["reader"])
        assert result["total"] == 1
        assert [e["id"] for e in result["entries"]] != [entry_id]

    async def test_a_chain_of_corrections_still_leaves_exactly_one(
        self, session_factory, config, orgs
    ):
        entry_id = await _seed_kb(session_factory, orgs["operator"])
        current = entry_id
        for n in range(3):
            amended = await _amend(session_factory, config, orgs["operator"], current,
                                   solution_text=f"revision {n}")
            current = amended["id"]

        result = await _browse(session_factory, orgs["reader"])
        assert result["total"] == 1
        assert "revision 2" in result["entries"][0]["solution_preview"]


class TestWhatCarriesForwardAndWhatDoesNot:
    async def test_hits_carry_forward(self, session_factory, config, orgs):
        """`commons_hits` measures how often the corpus was asked this
        question -- a property of the topic, not of the wording. Resetting
        it would drop a corrected entry into kb_review_queue's "never hit"
        bucket as though nobody had ever needed it."""
        entry_id = await _seed_kb(session_factory, orgs["operator"], hits=42)
        await _amend(session_factory, config, orgs["operator"], entry_id,
                     solution_text="corrected")

        assert (await _browse(session_factory, orgs["reader"]))["entries"][0]["hits"] == 42

    async def test_the_review_date_carries_forward(self, session_factory, config, orgs):
        """A correction is not a re-review. An entry whose version-pinned
        claim expires in March still expires in March after a typo fix."""
        due = datetime.now(timezone.utc) + timedelta(days=30)
        entry_id = await _seed_kb(session_factory, orgs["operator"], review_after=due)
        amended = await _amend(session_factory, config, orgs["operator"], entry_id,
                               solution_text="corrected")

        async with session_scope(session_factory) as session:
            stored = await session.get(Trace, amended["id"])
        assert stored.commons_review_after is not None

    async def test_votes_do_not_carry_forward(self, session_factory, config, orgs):
        """`trust`/`commons_votes` are aggregates over Vote rows, and those
        rows stay keyed to the text they judged. Copying the numbers onto a
        row with no underlying votes would contradict itself and then be
        silently overwritten by the next voter's recomputed tally."""
        entry_id = await _seed_kb(session_factory, orgs["operator"], votes=5, trust=0.2)
        await _amend(session_factory, config, orgs["operator"], entry_id,
                     solution_text="corrected")

        entry = (await _browse(session_factory, orgs["reader"]))["entries"][0]
        assert entry["votes"] == 0
        assert entry["standing"] == commons.STANDING_UNPROVEN

    async def test_the_signature_is_recomputed_from_the_corrected_text(
        self, session_factory, config, orgs
    ):
        """Carrying the old signature forward would leave a corrected entry
        answering to the OLD failure's fingerprint -- the quiet
        wrong-answer failure mode hub/commons.py refuses to risk."""
        entry_id = await _seed_kb(session_factory, orgs["operator"])
        async with session_scope(session_factory) as session:
            before = (await session.get(Trace, entry_id)).commons_signature

        amended = await _amend(session_factory, config, orgs["operator"], entry_id,
                               title="Stripe webhooks need replay protection")
        async with session_scope(session_factory) as session:
            after = (await session.get(Trace, amended["id"])).commons_signature

        assert after is not None
        assert after != before, "a retitled entry must not keep the old fingerprint"


class TestACustomersOwnTraceIsUnaffected:
    """The existing rule stands where its reasoning still applies: for
    content that is the org's to publish, re-sharing a correction is an
    explicit decision `amend_trace` must not make on its behalf."""

    async def test_an_ordinary_trace_does_not_become_a_kb_entry(
        self, session_factory, config, orgs
    ):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            trace = await crud.contribute_trace(
                session, orgs["customer"], config, rate_limiter,
                title="our own failure", context_text="ctx", solution_text="fix",
                tags=[], agent_type="code", actor="test",
            )
        amended = await _amend(session_factory, config, orgs["customer"], trace["id"],
                               solution_text="better fix")

        async with session_scope(session_factory) as session:
            stored = await session.get(Trace, amended["id"])
        assert stored.commons_source != "seed"
        assert stored.shared_with_commons is False
        assert (await _browse(session_factory, orgs["reader"]))["total"] == 0


class TestAReaderCanTellACorrectedEntryFromANewOne:
    """The honesty condition on resetting votes.

    `_carry_commons_forward` drops `trust`/`commons_votes` on amendment,
    for good reasons documented there. The consequence is that a freshly
    corrected entry and one nobody has ever tried both read `unproven`
    with zero votes -- indistinguishable, which makes the reset look like
    amnesia rather than a decision. Saying "revised" is what keeps it
    honest, and it is the same affordance a wiki's "last edited on"
    provides.
    """

    async def test_an_untouched_entry_reports_no_revisions(
        self, session_factory, config, orgs
    ):
        await _seed_kb(session_factory, orgs["operator"])
        entry = (await _browse(session_factory, orgs["reader"]))["entries"][0]
        assert entry["revisions"] == 0

    async def test_a_corrected_entry_reports_one(self, session_factory, config, orgs):
        entry_id = await _seed_kb(session_factory, orgs["operator"])
        await _amend(session_factory, config, orgs["operator"], entry_id,
                     solution_text="corrected")

        entry = (await _browse(session_factory, orgs["reader"]))["entries"][0]
        assert entry["revisions"] == 1
        # ...and it is otherwise indistinguishable from a new entry, which
        # is exactly why the count has to be there.
        assert entry["standing"] == commons.STANDING_UNPROVEN
        assert entry["votes"] == 0

    async def test_the_count_tracks_a_chain_of_corrections(
        self, session_factory, config, orgs
    ):
        entry_id = await _seed_kb(session_factory, orgs["operator"])
        current = entry_id
        for n in range(3):
            current = (await _amend(session_factory, config, orgs["operator"], current,
                                    solution_text=f"revision {n}"))["id"]

        assert (await _browse(session_factory, orgs["reader"]))["entries"][0]["revisions"] == 3
