"""Tests for Knowledge Base entry standing: the maintenance half.

Everything else about the Knowledge Base is about getting content INTO it
-- `commons_seed` in bulk, `review_kb_submission` one accepted community
proposal at a time. This file is about what happens to content once it is
in and the world moves on: an entry the fleets who tried it say does not
work, an entry whose version-pinned claim has expired, and an entry an
operator decides to withdraw.

Three properties are being pinned, and the third matters most:

1. Standing is computed correctly from the signals already collected
   (`Trace.trust`, `Trace.commons_votes`, `Trace.commons_review_after`).

2. The query layer acts on it: a disputed entry stops counting toward the
   coverage figure `commons_overlap` produces, and sorts last among
   `commons_search` candidates. A retracted entry disappears from all
   three Knowledge Base read paths at once.

3. Nothing here ever removes content on its own. The strongest automatic
   consequence of any number of downvotes is a smaller coverage claim and
   a worse rank -- both of which make the product's own claims more
   conservative, never less. Withdrawal is a human action. See
   hub/commons.py's "votes inform, the operator decides"; the tests in
   `TestVotesNeverRetract` are what make that a property rather than an
   intention.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from hub import commons, crud, manage
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def orgs(session_factory):
    """One operator org (the only one ever allowed to own Knowledge Base
    entries) and four customer orgs -- four because MIN_VOTES_FOR_STANDING
    is 3 and several tests need to cross it and then some."""
    async with session_scope(session_factory) as session:
        made = {}
        for name in ("operator", "cust-a", "cust-b", "cust-c", "cust-d"):
            o = Organization(name=name)
            session.add(o)
            await session.flush()
            made[name] = o.id
        return made


async def _seed(session_factory, operator_org_id, title, review_after=None, hits=0):
    """One Knowledge Base entry, built the way commons_seed builds them."""
    async with session_scope(session_factory) as session:
        trace = Trace(
            org_id=operator_org_id,
            title=title,
            context_text="ctx " + title,
            solution_text="do the thing",
            tags=["substrate"],
            agent_type="code",
            shared_with_commons=True,
            shared_at=datetime.now(timezone.utc),
            shared_rationale="test fixture",
            commons_signature=commons.signature_for(title, "ctx " + title, ["substrate"]),
            commons_source="seed",
            commons_review_after=review_after,
            commons_hits=hits,
        )
        session.add(trace)
        await session.flush()
        return trace.id


async def _vote(session_factory, org_id, trace_id, vote_type, feedback_tag=""):
    async with session_scope(session_factory) as session:
        return await crud.vote_trace(
            session, org_id, trace_id, vote_type, feedback_tag=feedback_tag, actor="test"
        )


async def _downvote_into_dispute(session_factory, orgs, trace_id, tag=""):
    """Three down-votes from three different customer orgs -- the minimum
    that can move an entry to `disputed`."""
    for name in ("cust-a", "cust-b", "cust-c"):
        await _vote(session_factory, orgs[name], trace_id, "down", feedback_tag=tag)


def _failure(label, title):
    return {"label": label, "signature": commons.signature_for(title, "ctx " + title, ["substrate"])}


# --- 1. The standing function itself (pure, no DB) ----------------------


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestEntryStanding:
    """Pure-function tests. The module-level `pytestmark` applies asyncio to
    every test in the file; these opt out of the resulting warning rather
    than dropping the mark the DB tests need."""

    def test_a_new_entry_with_no_votes_is_unproven(self):
        assert (
            commons.entry_standing(trust=0.5, votes=0, review_after=None)
            == commons.STANDING_UNPROVEN
        )

    def test_one_angry_org_cannot_dispute_an_entry(self):
        """The single most important constant in this model. `trust` is
        already 0.0 here -- a bare trust score would call this the worst
        entry in the corpus. One org is not the field."""
        assert (
            commons.entry_standing(trust=0.0, votes=1, review_after=None)
            == commons.STANDING_UNPROVEN
        )

    def test_two_orgs_still_cannot(self):
        assert (
            commons.entry_standing(trust=0.0, votes=2, review_after=None)
            == commons.STANDING_UNPROVEN
        )

    def test_three_orgs_voting_it_down_is_disputed(self):
        assert (
            commons.entry_standing(trust=0.0, votes=3, review_after=None)
            == commons.STANDING_DISPUTED
        )

    def test_a_split_vote_is_neither_disputed_nor_established(self):
        """trust exactly 0.5 is not a majority saying it failed, and it is
        nowhere near corroborated. The band between the two thresholds is
        'mixed results', and it stays its own thing."""
        assert (
            commons.entry_standing(trust=0.5, votes=10, review_after=None)
            == commons.STANDING_UNPROVEN
        )

    def test_a_well_corroborated_entry_is_established(self):
        assert (
            commons.entry_standing(trust=0.9, votes=10, review_after=None)
            == commons.STANDING_ESTABLISHED
        )

    def test_high_trust_from_too_few_votes_is_not_established(self):
        """Symmetry with the dispute floor: two enthusiastic orgs do not
        promote an entry any more than two unhappy ones demote it."""
        assert (
            commons.entry_standing(trust=1.0, votes=2, review_after=None)
            == commons.STANDING_UNPROVEN
        )

    def test_an_entry_past_its_review_date_is_stale(self):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        assert (
            commons.entry_standing(trust=0.5, votes=0, review_after=past)
            == commons.STANDING_STALE
        )

    def test_an_entry_before_its_review_date_is_not_stale(self):
        future = datetime.now(timezone.utc) + timedelta(days=365)
        assert (
            commons.entry_standing(trust=0.5, votes=0, review_after=future)
            == commons.STANDING_UNPROVEN
        )

    def test_no_review_date_means_never_stale(self):
        """Most substrate knowledge does not expire. Forcing a horizon onto
        every entry would make `stale` mean 'old' instead of 'due'."""
        assert (
            commons.entry_standing(trust=1.0, votes=99, review_after=None)
            != commons.STANDING_STALE
        )

    def test_disputed_outranks_stale(self):
        """Evidence from the field about the content beats a calendar
        date."""
        past = datetime.now(timezone.utc) - timedelta(days=1)
        assert (
            commons.entry_standing(trust=0.0, votes=5, review_after=past)
            == commons.STANDING_DISPUTED
        )

    def test_stale_outranks_established(self):
        """An entry can be well-corroborated AND overdue -- corroborated
        for the version it was written against. Overdue is the actionable
        half, so it wins."""
        past = datetime.now(timezone.utc) - timedelta(days=1)
        assert (
            commons.entry_standing(trust=1.0, votes=10, review_after=past)
            == commons.STANDING_STALE
        )

    def test_a_naive_review_date_is_read_as_utc_not_a_crash(self):
        """Comparing naive to aware datetimes raises TypeError in Python,
        which on this code path would be a 500 from a field whose only job
        is to schedule a review."""
        naive_past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        assert (
            commons.entry_standing(trust=0.5, votes=0, review_after=naive_past)
            == commons.STANDING_STALE
        )

    def test_only_disputed_is_excluded_from_coverage(self):
        assert not commons.counts_as_coverage(commons.STANDING_DISPUTED)
        for standing in (
            commons.STANDING_STALE,
            commons.STANDING_ESTABLISHED,
            commons.STANDING_UNPROVEN,
        ):
            assert commons.counts_as_coverage(standing), standing


# --- 2. Votes maintain the denormalized count ---------------------------


class TestVoteCount:
    async def test_voting_records_the_total(self, session_factory, orgs):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        await _vote(session_factory, orgs["cust-a"], trace_id, "up")
        await _vote(session_factory, orgs["cust-b"], trace_id, "down")

        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
            assert trace.commons_votes == 2

    async def test_changing_a_vote_does_not_inflate_the_count(self, session_factory, orgs):
        """vote_trace UPSERTs -- one org holds one standing vote. A count
        maintained by `+ 1` instead of by assignment would let a single org
        vote its way past MIN_VOTES_FOR_STANDING alone, which is precisely
        the thing that constant exists to prevent."""
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        for vote_type in ("up", "down", "up", "down"):
            await _vote(session_factory, orgs["cust-a"], trace_id, vote_type)

        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
            assert trace.commons_votes == 1
            assert (
                commons.entry_standing(
                    trust=trace.trust, votes=trace.commons_votes, review_after=None
                )
                == commons.STANDING_UNPROVEN
            )

    async def test_the_vote_response_carries_standing_and_count(self, session_factory, orgs):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        result = await _vote(session_factory, orgs["cust-a"], trace_id, "up")
        assert result["vote_count"] == 1
        assert result["standing"] == commons.STANDING_UNPROVEN

    async def test_the_projection_does_not_reuse_the_name_votes(self, session_factory, orgs):
        """`votes` on the owner's own projection (_to_wire) is the list of
        vote RECORDS, including free-text feedback. Two keys of the same
        name on two projections of one object, one an int and one a list of
        dicts, is how the wrong one gets shipped."""
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        result = await _vote(session_factory, orgs["cust-a"], trace_id, "up")
        assert "votes" not in result


# --- 3. commons_overlap: disputed entries stop being coverage -----------


class TestDisputedIsNotCoverage:
    async def test_an_undisputed_entry_counts_as_coverage(self, session_factory, orgs):
        await _seed(session_factory, orgs["operator"], "webhook idempotency")
        async with session_scope(session_factory) as session:
            result = await crud.commons_overlap(
                session, orgs["cust-d"], [_failure("f1", "webhook idempotency")]
            )
        assert result["n_covered"] == 1
        assert result["n_disputed"] == 0

    async def test_a_disputed_entry_does_not(self, session_factory, orgs):
        """The coverage figure is the one number this product tells
        customers to quote. An entry a majority of the fleets who tried it
        say did not work is not a solved failure."""
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        await _downvote_into_dispute(session_factory, orgs, trace_id)

        async with session_scope(session_factory) as session:
            result = await crud.commons_overlap(
                session, orgs["cust-d"], [_failure("f1", "webhook idempotency")]
            )
        assert result["n_covered"] == 0
        assert result["covered_fraction"] == 0.0

    async def test_a_disputed_match_is_still_reported_separately(self, session_factory, orgs):
        """'The Knowledge Base has something about this and it is
        contested' is a materially different answer from 'the Knowledge
        Base has nothing'. Dropping it silently would make the two
        indistinguishable to the caller."""
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        await _downvote_into_dispute(session_factory, orgs, trace_id)

        async with session_scope(session_factory) as session:
            result = await crud.commons_overlap(
                session, orgs["cust-d"], [_failure("f1", "webhook idempotency")]
            )
        assert result["n_disputed"] == 1
        assert len(result["disputed_matches"]) == 1
        assert result["disputed_matches"][0]["trace"]["id"] == trace_id
        assert result["disputed_matches"][0]["trace"]["standing"] == commons.STANDING_DISPUTED
        assert result["matches"] == []

    async def test_a_disputed_entry_is_excluded_from_the_agent_type_breakdown(
        self, session_factory, orgs
    ):
        """by_agent_type is a decomposition of n_covered. If a disputed
        match still incremented it, the parts would not sum to the whole
        and a reader would find coverage attributed to a domain the
        headline number says has none."""
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        await _downvote_into_dispute(session_factory, orgs, trace_id)

        async with session_scope(session_factory) as session:
            result = await crud.commons_overlap(
                session, orgs["cust-d"], [_failure("f1", "webhook idempotency")]
            )
        assert result["by_agent_type"] == {}
        assert sum(result["by_agent_type"].values()) == result["n_covered"]

    async def test_a_disputed_entry_still_accrues_hits(self, session_factory, orgs):
        """commons_hits answers 'how often was this served', which is what
        lets kb-review rank a bad entry by the traffic it is misdirecting.
        An entry that stopped counting as coverage but is still the top
        match for hundreds of failures is the most urgent thing in an
        operator's queue; suppressing its hit count would hide exactly
        that."""
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        await _downvote_into_dispute(session_factory, orgs, trace_id)

        async with session_scope(session_factory) as session:
            await crud.commons_overlap(
                session, orgs["cust-d"], [_failure("f1", "webhook idempotency")]
            )
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
            assert trace.commons_hits == 1

    async def test_a_stale_entry_still_counts_as_coverage(self, session_factory, orgs):
        """'Due for review' is not 'known wrong'. Letting the coverage
        figure fall on a calendar date would move the number with no
        evidence behind the move."""
        past = datetime.now(timezone.utc) - timedelta(days=1)
        await _seed(session_factory, orgs["operator"], "webhook idempotency", review_after=past)

        async with session_scope(session_factory) as session:
            result = await crud.commons_overlap(
                session, orgs["cust-d"], [_failure("f1", "webhook idempotency")]
            )
        assert result["n_covered"] == 1
        assert result["matches"][0]["trace"]["standing"] == commons.STANDING_STALE


# --- 4. commons_search: disputed entries rank last, but are returned ----


class TestSearchRanking:
    async def test_candidates_carry_standing(self, session_factory, orgs):
        await _seed(session_factory, orgs["operator"], "webhook idempotency")
        async with session_scope(session_factory) as session:
            result = await crud.commons_search(
                session, orgs["cust-d"], commons.signature_for(
                    "webhook idempotency", "ctx webhook idempotency", ["substrate"]
                )
            )
        assert result["candidates"][0]["trace"]["standing"] == commons.STANDING_UNPROVEN

    async def test_a_disputed_candidate_is_returned_not_dropped(self, session_factory, orgs):
        """The difference between this tool and commons_overlap. Coverage
        is a claim, so a contested entry is excluded from it. Lookup is
        'here is what exists, judge it' -- answering 'nothing found' when
        the corpus holds a contested answer is false and strictly less
        useful."""
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        await _downvote_into_dispute(session_factory, orgs, trace_id)

        async with session_scope(session_factory) as session:
            result = await crud.commons_search(
                session, orgs["cust-d"], commons.signature_for(
                    "webhook idempotency", "ctx webhook idempotency", ["substrate"]
                )
            )
        ids = [c["trace"]["id"] for c in result["candidates"]]
        assert trace_id in ids
        assert result["n_disputed"] == 1

    async def test_a_disputed_candidate_ranks_behind_a_worse_match(self, session_factory, orgs):
        """The disputed entry is the BETTER lexical match here -- the query
        is its own text. It still sorts last, because standing leads the
        sort key."""
        disputed_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        clean_id = await _seed(session_factory, orgs["operator"], "webhook retries duplicate")
        await _downvote_into_dispute(session_factory, orgs, disputed_id)

        async with session_scope(session_factory) as session:
            result = await crud.commons_search(
                session, orgs["cust-d"], commons.signature_for(
                    "webhook idempotency", "ctx webhook idempotency", ["substrate"]
                ), limit=10,
            )
        ranked = [c["trace"]["id"] for c in result["candidates"]]
        assert ranked.index(clean_id) < ranked.index(disputed_id)
        assert result["candidates"][-1]["trace"]["id"] == disputed_id
        # rank is renumbered after the sort, not carried from before it.
        assert [c["rank"] for c in result["candidates"]] == list(
            range(1, len(result["candidates"]) + 1)
        )


# --- 5. Retraction: gone from every read path at once -------------------


class TestRetraction:
    async def test_retracting_returns_the_entry_and_records_why(self, session_factory, orgs):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        async with session_scope(session_factory) as session:
            entry = await crud.retract_kb_entry(session, trace_id, reason="superseded by v3 docs")
        assert entry["id"] == trace_id
        assert entry["standing"] == "retracted"
        assert entry["retraction_reason"] == "superseded by v3 docs"

    async def test_a_retracted_entry_is_invisible_to_commons_overlap(self, session_factory, orgs):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        async with session_scope(session_factory) as session:
            await crud.retract_kb_entry(session, trace_id)

        async with session_scope(session_factory) as session:
            result = await crud.commons_overlap(
                session, orgs["cust-d"], [_failure("f1", "webhook idempotency")]
            )
        assert result["n_commons_traces"] == 0
        assert result["n_covered"] == 0
        assert result["n_disputed"] == 0

    async def test_a_retracted_entry_is_invisible_to_commons_search(self, session_factory, orgs):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        async with session_scope(session_factory) as session:
            await crud.retract_kb_entry(session, trace_id)

        async with session_scope(session_factory) as session:
            result = await crud.commons_search(
                session, orgs["cust-d"], commons.signature_for(
                    "webhook idempotency", "ctx webhook idempotency", ["substrate"]
                )
            )
        assert result["candidates"] == []

    async def test_a_retracted_entry_cannot_be_voted_on(self, session_factory, orgs):
        """The third read path. A filter applied in two of three places is
        the failure mode commons_visible() exists to make impossible."""
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        async with session_scope(session_factory) as session:
            await crud.retract_kb_entry(session, trace_id)

        assert await _vote(session_factory, orgs["cust-a"], trace_id, "up") is None

    async def test_the_owning_org_can_still_reach_its_own_row(self, session_factory, orgs):
        """Retraction un-publishes an entry from the Knowledge Base; it does
        not revoke the owning org's access to its own trace. vote_trace's
        two branches are independent and only the Knowledge Base one is
        gated."""
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        async with session_scope(session_factory) as session:
            await crud.retract_kb_entry(session, trace_id)

        result = await _vote(session_factory, orgs["operator"], trace_id, "up")
        assert result is not None
        assert result["id"] == trace_id

    async def test_retraction_keeps_the_row_its_votes_and_its_hits(self, session_factory, orgs):
        """Not a delete. 'How many fleets did we serve this to before we
        pulled it, and what did they say' is answerable only from exactly
        the data a DELETE would destroy."""
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency", hits=17)
        await _downvote_into_dispute(session_factory, orgs, trace_id)
        async with session_scope(session_factory) as session:
            await crud.retract_kb_entry(session, trace_id)

        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
            assert trace is not None
            assert trace.commons_hits == 17
            assert trace.commons_votes == 3
            assert trace.shared_with_commons is True

    async def test_retracting_twice_does_not_overwrite_the_first_reason(
        self, session_factory, orgs
    ):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        async with session_scope(session_factory) as session:
            await crud.retract_kb_entry(session, trace_id, reason="the real reason")
        async with session_scope(session_factory) as session:
            assert await crud.retract_kb_entry(session, trace_id, reason="oops") is None
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
            assert trace.commons_retraction_reason == "the real reason"

    async def test_a_long_reason_is_truncated_not_rejected(self, session_factory, orgs):
        """The column is String(200); an over-long reason reaching the DB
        would surface as an uncaught IntegrityError from an operator CLI."""
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        async with session_scope(session_factory) as session:
            entry = await crud.retract_kb_entry(session, trace_id, reason="x" * 500)
        assert len(entry["retraction_reason"]) == 200

    async def test_retracting_a_non_kb_trace_returns_none(self, session_factory, orgs):
        """An ordinary customer trace is not a Knowledge Base entry, so
        there is nothing to un-publish."""
        async with session_scope(session_factory) as session:
            trace = Trace(
                org_id=orgs["cust-a"], title="private", context_text="c",
                solution_text="s", tags=[], agent_type="code",
            )
            session.add(trace)
            await session.flush()
            trace_id = trace.id
        async with session_scope(session_factory) as session:
            assert await crud.retract_kb_entry(session, trace_id) is None

    async def test_a_malformed_id_returns_none_rather_than_raising(self, session_factory):
        async with session_scope(session_factory) as session:
            assert await crud.retract_kb_entry(session, "not-a-uuid") is None
            assert await crud.restore_kb_entry(session, "not-a-uuid") is None


class TestRestore:
    async def test_restoring_puts_it_back_in_every_read_path(self, session_factory, orgs):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        async with session_scope(session_factory) as session:
            await crud.retract_kb_entry(session, trace_id, reason="mistake")
        async with session_scope(session_factory) as session:
            restored = await crud.restore_kb_entry(session, trace_id)
        assert restored["id"] == trace_id

        async with session_scope(session_factory) as session:
            result = await crud.commons_overlap(
                session, orgs["cust-d"], [_failure("f1", "webhook idempotency")]
            )
        assert result["n_covered"] == 1
        assert await _vote(session_factory, orgs["cust-a"], trace_id, "up") is not None

    async def test_restoring_clears_the_reason(self, session_factory, orgs):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        async with session_scope(session_factory) as session:
            await crud.retract_kb_entry(session, trace_id, reason="mistake")
        async with session_scope(session_factory) as session:
            await crud.restore_kb_entry(session, trace_id)
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
            assert trace.commons_retracted_at is None
            assert trace.commons_retraction_reason == ""

    async def test_restoring_a_live_entry_returns_none(self, session_factory, orgs):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        async with session_scope(session_factory) as session:
            assert await crud.restore_kb_entry(session, trace_id) is None


class TestVotesNeverRetract:
    async def test_no_number_of_downvotes_withdraws_an_entry(self, session_factory, orgs):
        """The property the whole design turns on. A corpus where four
        downvotes can silently delete the operator's content is a corpus a
        competitor can edit. Disputed content is de-ranked and stops being
        counted as coverage -- both of which shrink this product's own
        claims -- and it keeps being SERVED until a human decides
        otherwise."""
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        for name in ("cust-a", "cust-b", "cust-c", "cust-d"):
            await _vote(session_factory, orgs[name], trace_id, "down", feedback_tag="wrong")

        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
            assert trace.commons_retracted_at is None
            assert trace.shared_with_commons is True
            assert trace.quarantined is False

        # Asked as a customer, not as the operator: commons_search excludes
        # the caller's own rows, and the operator owns every seeded entry.
        async with session_scope(session_factory) as session:
            result = await crud.commons_search(
                session, orgs["cust-a"], commons.signature_for(
                    "webhook idempotency", "ctx webhook idempotency", ["substrate"]
                )
            )
        assert [c["trace"]["id"] for c in result["candidates"]] == [trace_id]

    async def test_a_security_flag_does_not_withdraw_an_entry_either(self, session_factory, orgs):
        """`security_concern` is the one signal acted on at n=1 -- and what
        it does is put the entry at the top of an operator's queue, not
        remove it."""
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        await _vote(
            session_factory, orgs["cust-a"], trace_id, "down", feedback_tag="security_concern"
        )
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
            assert trace.commons_retracted_at is None
            queue = await crud.kb_review_queue(session)
        assert queue[0]["id"] == trace_id
        assert queue[0]["bucket"] == "urgent"


# --- 6. The review queue: what makes curation scale ---------------------


class TestReviewQueue:
    async def test_an_untouched_healthy_corpus_still_lists_unmatched_entries(
        self, session_factory, orgs
    ):
        """A brand-new entry has never matched anything, which is not an
        error -- but it is the lowest-priority bucket, not an omission."""
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        async with session_scope(session_factory) as session:
            queue = await crud.kb_review_queue(session)
        assert [i["id"] for i in queue] == [trace_id]
        assert queue[0]["bucket"] == "never_hit"

    async def test_a_healthy_entry_that_has_matched_is_absent(self, session_factory, orgs):
        await _seed(session_factory, orgs["operator"], "webhook idempotency", hits=5)
        async with session_scope(session_factory) as session:
            assert await crud.kb_review_queue(session) == []

    async def test_buckets_are_ordered_urgent_disputed_stale_never_hit(
        self, session_factory, orgs
    ):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        never_hit = await _seed(session_factory, orgs["operator"], "alpha never matched")
        stale = await _seed(session_factory, orgs["operator"], "beta stale", review_after=past, hits=4)
        disputed = await _seed(session_factory, orgs["operator"], "gamma disputed", hits=4)
        urgent = await _seed(session_factory, orgs["operator"], "delta urgent", hits=4)

        await _downvote_into_dispute(session_factory, orgs, disputed)
        await _vote(
            session_factory, orgs["cust-a"], urgent, "down", feedback_tag="security_concern"
        )

        async with session_scope(session_factory) as session:
            queue = await crud.kb_review_queue(session)
        assert [i["id"] for i in queue] == [urgent, disputed, stale, never_hit]
        assert [i["bucket"] for i in queue] == ["urgent", "disputed", "stale", "never_hit"]

    async def test_within_a_bucket_the_busiest_entry_comes_first(self, session_factory, orgs):
        """A wrong answer nobody reaches is a smaller problem than a wrong
        answer served a thousand times. This ordering is the reason review
        cost tracks the error rate rather than the corpus size."""
        quiet = await _seed(session_factory, orgs["operator"], "quiet one", hits=2)
        busy = await _seed(session_factory, orgs["operator"], "busy one", hits=900)
        await _downvote_into_dispute(session_factory, orgs, quiet)
        await _downvote_into_dispute(session_factory, orgs, busy)

        async with session_scope(session_factory) as session:
            queue = await crud.kb_review_queue(session)
        assert [i["id"] for i in queue] == [busy, quiet]

    async def test_a_retracted_entry_is_not_in_the_queue(self, session_factory, orgs):
        """Already dealt with."""
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        await _downvote_into_dispute(session_factory, orgs, trace_id)
        async with session_scope(session_factory) as session:
            await crud.retract_kb_entry(session, trace_id)
        async with session_scope(session_factory) as session:
            assert await crud.kb_review_queue(session) == []

    async def test_a_customers_own_trace_is_never_in_the_queue(self, session_factory, orgs):
        """The queue is over Knowledge Base entries, and it is built from
        the same commons_visible() filter the query paths use -- so an
        operator reading it is never shown a customer's private trace."""
        async with session_scope(session_factory) as session:
            trace = Trace(
                org_id=orgs["cust-a"], title="our internal escalation policy",
                context_text="c", solution_text="s", tags=[], agent_type="support",
            )
            session.add(trace)
            await session.flush()
        async with session_scope(session_factory) as session:
            assert await crud.kb_review_queue(session) == []

    async def test_the_limit_is_bounded_and_survives_garbage(self, session_factory, orgs):
        for i in range(4):
            await _seed(session_factory, orgs["operator"], f"entry {i}")
        async with session_scope(session_factory) as session:
            assert len(await crud.kb_review_queue(session, limit=2)) == 2
            assert len(await crud.kb_review_queue(session, limit=0)) == 1
            assert len(await crud.kb_review_queue(session, limit="nonsense")) == 4

    async def test_count_kb_review_queue_is_the_true_total_not_the_capped_length(
        self, session_factory, orgs
    ):
        """The admin Knowledge Base page's 'needs attention' tile used to
        read `len(kb_review_queue(limit=_MAX_ROWS))`, which silently reads
        as a total and stops matching the real queue size the moment it
        exceeds `_MAX_ROWS` -- the exact defect the overview page's
        traces/quarantined/keys tiles had for fleet-wide org counts.
        `count_kb_review_queue` must report every entry needing attention,
        independent of whatever `limit` a caller passes to the bounded
        list."""
        for i in range(4):
            await _seed(session_factory, orgs["operator"], f"entry {i}")
        async with session_scope(session_factory) as session:
            capped = await crud.kb_review_queue(session, limit=2)
            total = await crud.count_kb_review_queue(session)
        assert len(capped) == 2
        assert total == 4


# --- 7. The operator CLI ------------------------------------------------


class TestManageCommands:
    async def test_kb_retract_and_restore_round_trip(self, session_factory, orgs, capsys):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")

        assert await manage.kb_retract(trace_id, "stale advice", session_factory=session_factory)
        assert "retracted" in capsys.readouterr().out

        assert await manage.kb_restore(trace_id, session_factory=session_factory)
        assert "restored" in capsys.readouterr().out

    async def test_kb_retract_on_an_unknown_id_fails_cleanly(self, session_factory, capsys):
        assert not await manage.kb_retract(
            "00000000-0000-0000-0000-000000000000", session_factory=session_factory
        )
        assert "not a live Knowledge Base entry" in capsys.readouterr().err

    async def test_kb_restore_on_a_live_entry_fails_cleanly(self, session_factory, orgs, capsys):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        assert not await manage.kb_restore(trace_id, session_factory=session_factory)
        assert "not a retracted" in capsys.readouterr().err

    async def test_kb_review_prints_the_buckets(self, session_factory, orgs, capsys):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency", hits=3)
        await _downvote_into_dispute(session_factory, orgs, trace_id)

        assert await manage.kb_review(session_factory=session_factory)
        out = capsys.readouterr().out
        assert "DISPUTED" in out
        assert trace_id in out
        assert "kb-retract" in out

    async def test_kb_review_on_a_clean_corpus_says_so(self, session_factory, orgs, capsys):
        await _seed(session_factory, orgs["operator"], "webhook idempotency", hits=3)
        assert await manage.kb_review(session_factory=session_factory)
        assert "needs review" in capsys.readouterr().out

    async def test_kb_review_rejects_a_non_integer_limit(self, session_factory, capsys):
        assert not await manage.kb_review("lots", session_factory=session_factory)
        assert "must be an integer" in capsys.readouterr().err

    async def test_kb_stats_reports_the_standing_breakdown(self, session_factory, orgs, capsys):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency", hits=3)
        await _downvote_into_dispute(session_factory, orgs, trace_id)

        await manage.kb_stats(session_factory=session_factory)
        out = capsys.readouterr().out
        assert "standing" in out
        assert "disputed" in out
        assert "kb-review" in out

    async def test_kb_stats_counts_retracted_entries_separately(
        self, session_factory, orgs, capsys
    ):
        live = await _seed(session_factory, orgs["operator"], "still good", hits=1)
        gone = await _seed(session_factory, orgs["operator"], "pulled", hits=1)
        async with session_scope(session_factory) as session:
            await crud.retract_kb_entry(session, gone)

        await manage.kb_stats(session_factory=session_factory)
        out = capsys.readouterr().out
        assert "knowledge base entries:  1" in out
        assert "retracted:" in out
        assert live  # the surviving entry is the one counted above

    async def test_kb_stats_needs_review_counts_all_four_kb_review_buckets(
        self, session_factory, orgs, capsys
    ):
        """`kb-review` lists FOUR buckets needing attention: urgent
        (security-flagged), disputed, stale, and never_hit. `kb_stats`'s
        "needs review" hint used to be computed as
        `standings[disputed] + standings[stale]` -- `standing_of()` has no
        "urgent" or "never_hit" value at all, so a security-flagged entry
        or one that has never matched anything was invisible to this
        summary even while `kb-review` itself listed it first. Neither
        entry seeded below is disputed or stale, so the old computation
        would have reported 0 and suppressed the hint entirely."""
        urgent = await _seed(session_factory, orgs["operator"], "urgent entry", hits=1)
        await _vote(session_factory, orgs["cust-a"], urgent, "down", feedback_tag="security_concern")
        await _seed(session_factory, orgs["operator"], "never hit", hits=0)

        await manage.kb_stats(session_factory=session_factory)
        out = capsys.readouterr().out
        assert "`kb-review` lists the 2 entry(ies) needing a decision." in out


# --- 8. commons_seed's review_after field -------------------------------


class TestSeedReviewAfter:
    async def test_a_seeded_review_date_lands_on_the_row(self, session_factory, orgs, tmp_path):
        import json

        path = tmp_path / "kb.jsonl"
        path.write_text(
            json.dumps(
                {
                    "title": "React 19 hydrates Date differently than 18",
                    "context_text": "SSR mismatch on timestamps",
                    "solution_text": "Render dates client-side or pin the format",
                    "review_after": "2027-06-01",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        assert await manage.commons_seed(
            str(path), orgs["operator"], session_factory=session_factory
        )

        async with session_scope(session_factory) as session:
            trace = (
                await session.execute(select(Trace).where(Trace.commons_source == "seed"))
            ).scalar_one()
            assert trace.commons_review_after == datetime(2027, 6, 1, tzinfo=timezone.utc)

    async def test_an_entry_without_a_review_date_gets_none(self, session_factory, orgs, tmp_path):
        import json

        path = tmp_path / "kb.jsonl"
        path.write_text(
            json.dumps(
                {
                    "title": "Stripe webhook handlers need idempotency keys",
                    "context_text": "duplicate delivery on 500",
                    "solution_text": "Key on the event id",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        assert await manage.commons_seed(
            str(path), orgs["operator"], session_factory=session_factory
        )

        async with session_scope(session_factory) as session:
            trace = (
                await session.execute(select(Trace).where(Trace.commons_source == "seed"))
            ).scalar_one()
            assert trace.commons_review_after is None

    async def test_an_unparseable_review_date_skips_the_line(
        self, session_factory, orgs, tmp_path, capsys
    ):
        """Not silently ignored. A horizon that was meant to be set and
        quietly was not is worse than no horizon at all, because the
        operator believes the entry is being watched."""
        import json

        path = tmp_path / "kb.jsonl"
        path.write_text(
            json.dumps({"title": "t", "solution_text": "s", "review_after": "next tuesday"})
            + "\n"
            + json.dumps({"title": "good", "solution_text": "s"})
            + "\n",
            encoding="utf-8",
        )
        assert await manage.commons_seed(
            str(path), orgs["operator"], session_factory=session_factory
        )
        captured = capsys.readouterr()
        assert "not an ISO 8601" in captured.err
        assert "loaded 1 entry" in captured.out

    @pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
    def test_a_trailing_z_parses_on_every_supported_python(self):
        """`datetime.fromisoformat` only learned to accept "Z" in 3.11, and
        this package supports 3.10 -- so the most natural way to write a
        UTC timestamp would be rejected on exactly the older interpreter
        where the failure is least expected."""
        assert manage._parse_review_after("2027-06-01T00:00:00Z") == datetime(
            2027, 6, 1, tzinfo=timezone.utc
        )

    @pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
    def test_a_naive_timestamp_is_read_as_utc(self):
        assert manage._parse_review_after("2027-06-01T12:00:00") == datetime(
            2027, 6, 1, 12, 0, tzinfo=timezone.utc
        )

    @pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
    def test_non_strings_and_nonsense_return_none(self):
        for bad in (None, 12345, [], "next tuesday", ""):
            assert manage._parse_review_after(bad) is None
