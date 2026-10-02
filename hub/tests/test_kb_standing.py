from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from hub import commons, crud, manage
from hub.db import session_scope
from hub.models import Organization, Trace, Vote

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def orgs(session_factory, establish_orgs):
    async with session_scope(session_factory) as session:
        made = {}
        for name in ("operator", "cust-a", "cust-b", "cust-c", "cust-d"):
            o = Organization(name=name)
            session.add(o)
            await session.flush()
            made[name] = o.id
    await establish_orgs([made[n] for n in ("cust-a", "cust-b", "cust-c", "cust-d")])
    return made


async def _seed(session_factory, operator_org_id, title, review_after=None, hits=0):
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
    for name in ("cust-a", "cust-b", "cust-c"):
        await _vote(session_factory, orgs[name], trace_id, "down", feedback_tag=tag)


def _failure(label, title):
    return {"label": label, "signature": commons.signature_for(title, "ctx " + title, ["substrate"])}


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestEntryStanding:
    def test_a_new_entry_with_no_votes_is_unproven(self):
        assert (
            commons.entry_standing(trust=0.5, votes=0, review_after=None)
            == commons.STANDING_UNPROVEN
        )

    def test_one_angry_org_cannot_dispute_an_entry(self):
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
        assert (
            commons.entry_standing(trust=1.0, votes=99, review_after=None)
            != commons.STANDING_STALE
        )

    def test_disputed_outranks_stale(self):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        assert (
            commons.entry_standing(trust=0.0, votes=5, review_after=past)
            == commons.STANDING_DISPUTED
        )

    def test_stale_outranks_established(self):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        assert (
            commons.entry_standing(trust=1.0, votes=10, review_after=past)
            == commons.STANDING_STALE
        )

    def test_a_naive_review_date_is_read_as_utc_not_a_crash(self):
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


class TestVoteCount:
    async def test_voting_records_the_total(self, session_factory, orgs):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        await _vote(session_factory, orgs["cust-a"], trace_id, "up")
        await _vote(session_factory, orgs["cust-b"], trace_id, "down")

        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
            assert trace.commons_votes == 2

    async def test_changing_a_vote_does_not_inflate_the_count(self, session_factory, orgs):
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
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        result = await _vote(session_factory, orgs["cust-a"], trace_id, "up")
        assert "votes" not in result


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
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        await _downvote_into_dispute(session_factory, orgs, trace_id)

        async with session_scope(session_factory) as session:
            result = await crud.commons_overlap(
                session, orgs["cust-d"], [_failure("f1", "webhook idempotency")]
            )
        assert result["n_covered"] == 0
        assert result["covered_fraction"] == 0.0

    async def test_a_disputed_match_is_still_reported_separately(self, session_factory, orgs):
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
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        await _downvote_into_dispute(session_factory, orgs, trace_id)

        async with session_scope(session_factory) as session:
            result = await crud.commons_overlap(
                session, orgs["cust-d"], [_failure("f1", "webhook idempotency")]
            )
        assert result["by_agent_type"] == {}
        assert sum(result["by_agent_type"].values()) == result["n_covered"]

    async def test_a_disputed_entry_still_accrues_hits(self, session_factory, orgs):
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
        past = datetime.now(timezone.utc) - timedelta(days=1)
        await _seed(session_factory, orgs["operator"], "webhook idempotency", review_after=past)

        async with session_scope(session_factory) as session:
            result = await crud.commons_overlap(
                session, orgs["cust-d"], [_failure("f1", "webhook idempotency")]
            )
        assert result["n_covered"] == 1
        assert result["matches"][0]["trace"]["standing"] == commons.STANDING_STALE


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
        assert [c["rank"] for c in result["candidates"]] == list(
            range(1, len(result["candidates"]) + 1)
        )


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
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        async with session_scope(session_factory) as session:
            await crud.retract_kb_entry(session, trace_id)

        assert await _vote(session_factory, orgs["cust-a"], trace_id, "up") is None

    async def test_the_owning_org_can_still_reach_its_own_row(self, session_factory, orgs):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        async with session_scope(session_factory) as session:
            await crud.retract_kb_entry(session, trace_id)

        result = await _vote(session_factory, orgs["operator"], trace_id, "up")
        assert result is not None
        assert result["id"] == trace_id

    async def test_retraction_keeps_the_row_its_votes_and_its_hits(self, session_factory, orgs):
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
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        async with session_scope(session_factory) as session:
            entry = await crud.retract_kb_entry(session, trace_id, reason="x" * 500)
        assert len(entry["retraction_reason"]) == 200

    async def test_retracting_a_non_kb_trace_returns_none(self, session_factory, orgs):
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
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        for name in ("cust-a", "cust-b", "cust-c", "cust-d"):
            await _vote(session_factory, orgs[name], trace_id, "down", feedback_tag="wrong")

        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
            assert trace.commons_retracted_at is None
            assert trace.shared_with_commons is True
            assert trace.quarantined is False

        async with session_scope(session_factory) as session:
            result = await crud.commons_search(
                session, orgs["cust-a"], commons.signature_for(
                    "webhook idempotency", "ctx webhook idempotency", ["substrate"]
                )
            )
        assert [c["trace"]["id"] for c in result["candidates"]] == [trace_id]

    async def test_a_security_flag_does_not_withdraw_an_entry_either(self, session_factory, orgs):
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


class TestReviewQueue:
    async def test_an_untouched_healthy_corpus_still_lists_unmatched_entries(
        self, session_factory, orgs
    ):
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
        quiet = await _seed(session_factory, orgs["operator"], "quiet one", hits=2)
        busy = await _seed(session_factory, orgs["operator"], "busy one", hits=900)
        await _downvote_into_dispute(session_factory, orgs, quiet)
        await _downvote_into_dispute(session_factory, orgs, busy)

        async with session_scope(session_factory) as session:
            queue = await crud.kb_review_queue(session)
        assert [i["id"] for i in queue] == [busy, quiet]

    async def test_a_retracted_entry_is_not_in_the_queue(self, session_factory, orgs):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        await _downvote_into_dispute(session_factory, orgs, trace_id)
        async with session_scope(session_factory) as session:
            await crud.retract_kb_entry(session, trace_id)
        async with session_scope(session_factory) as session:
            assert await crud.kb_review_queue(session) == []

    async def test_a_customers_own_trace_is_never_in_the_queue(self, session_factory, orgs):
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
        for i in range(4):
            await _seed(session_factory, orgs["operator"], f"entry {i}")
        async with session_scope(session_factory) as session:
            capped = await crud.kb_review_queue(session, limit=2)
            total = await crud.count_kb_review_queue(session)
        assert len(capped) == 2
        assert total == 4


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
        assert live

    async def test_kb_stats_needs_review_counts_all_four_kb_review_buckets(
        self, session_factory, orgs, capsys
    ):
        urgent = await _seed(session_factory, orgs["operator"], "urgent entry", hits=1)
        await _vote(session_factory, orgs["cust-a"], urgent, "down", feedback_tag="security_concern")
        await _seed(session_factory, orgs["operator"], "never hit", hits=0)

        await manage.kb_stats(session_factory=session_factory)
        out = capsys.readouterr().out
        assert "`kb-review` lists the 2 entry(ies) needing a decision." in out


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
        import json

        path = tmp_path / "kb.jsonl"
        path.write_text(
            json.dumps({"title": "t", "context_text": "c",
                        "solution_text": "s", "review_after": "next tuesday"})
            + "\n"
            + json.dumps({"title": "good", "context_text": "c", "solution_text": "s"})
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


async def _fresh_orgs(session_factory, n, prefix="sock"):
    async with session_scope(session_factory) as session:
        made = [Organization(name=f"{prefix}-{i}") for i in range(n)]
        session.add_all(made)
        await session.flush()
        return [o.id for o in made]


async def _standing_of(session_factory, trace_id):
    async with session_scope(session_factory) as session:
        trace = await session.get(Trace, trace_id)
        return commons.entry_standing(
            trust=trace.trust,
            votes=trace.commons_votes,
            review_after=trace.commons_review_after,
        )


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestVoteEligibility:
    def test_an_established_org_qualifies(self):
        assert commons.vote_counts_toward_standing(
            trace_count=commons.COMMONS_VOTER_MIN_TRACES,
            org_created_at=datetime.now(timezone.utc) - timedelta(days=30),
        )

    def test_an_org_that_has_captured_nothing_does_not(self):
        assert not commons.vote_counts_toward_standing(
            trace_count=0,
            org_created_at=datetime.now(timezone.utc) - timedelta(days=365),
        )

    def test_one_trace_short_does_not(self):
        assert not commons.vote_counts_toward_standing(
            trace_count=commons.COMMONS_VOTER_MIN_TRACES - 1,
            org_created_at=datetime.now(timezone.utc) - timedelta(days=365),
        )

    def test_a_minutes_old_org_does_not_however_busy(self):
        assert not commons.vote_counts_toward_standing(
            trace_count=10_000,
            org_created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )

    def test_a_missing_creation_timestamp_fails_closed(self):
        assert not commons.vote_counts_toward_standing(
            trace_count=10_000, org_created_at=None
        )

    def test_a_naive_timestamp_is_read_as_utc_not_crashed_on(self):
        assert commons.vote_counts_toward_standing(
            trace_count=commons.COMMONS_VOTER_MIN_TRACES,
            org_created_at=(datetime.now(timezone.utc) - timedelta(days=30)).replace(
                tzinfo=None
            ),
        )


class TestSockpuppetsCannotMoveStanding:
    async def test_five_fresh_orgs_cannot_dispute_an_entry(self, session_factory, orgs):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        for sock in await _fresh_orgs(session_factory, 5):
            await _vote(session_factory, sock, trace_id, "down")

        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
            assert trace.commons_votes == 0
            assert trace.trust == 0.5, "an uncounted vote must not move trust either"
        assert await _standing_of(session_factory, trace_id) == commons.STANDING_UNPROVEN

    async def test_fresh_orgs_cannot_manufacture_established_standing_either(
        self, session_factory, orgs
    ):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        for sock in await _fresh_orgs(session_factory, 6):
            await _vote(session_factory, sock, trace_id, "up")

        assert await _standing_of(session_factory, trace_id) == commons.STANDING_UNPROVEN

    async def test_sockpuppets_cannot_drown_out_real_voters(self, session_factory, orgs):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        await _downvote_into_dispute(session_factory, orgs, trace_id)
        assert await _standing_of(session_factory, trace_id) == commons.STANDING_DISPUTED

        for sock in await _fresh_orgs(session_factory, 20):
            await _vote(session_factory, sock, trace_id, "up")

        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
            assert trace.commons_votes == 3, "only the three established fleets count"
            assert trace.trust == 0.0
        assert await _standing_of(session_factory, trace_id) == commons.STANDING_DISPUTED

    async def test_the_vote_is_stored_not_discarded(self, session_factory, orgs):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        socks = await _fresh_orgs(session_factory, 4)
        for sock in socks:
            await _vote(session_factory, sock, trace_id, "down")

        async with session_scope(session_factory) as session:
            rows = (
                await session.execute(select(Vote).where(Vote.trace_id == trace_id))
            ).scalars().all()
        assert len(rows) == 4
        assert {r.org_id for r in rows} == set(socks)

    async def test_a_vote_recorded_today_starts_counting_once_the_org_qualifies(
        self, session_factory, orgs, establish_orgs
    ):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        newcomers = await _fresh_orgs(session_factory, 3, prefix="newcomer")
        for org_id in newcomers:
            await _vote(session_factory, org_id, trace_id, "down")
        assert await _standing_of(session_factory, trace_id) == commons.STANDING_UNPROVEN

        await establish_orgs(newcomers)
        await _vote(session_factory, newcomers[0], trace_id, "down")

        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
            assert trace.commons_votes == 3
        assert await _standing_of(session_factory, trace_id) == commons.STANDING_DISPUTED

    async def test_the_response_tells_the_voter_whether_it_counted(self, session_factory, orgs):
        trace_id = await _seed(session_factory, orgs["operator"], "webhook idempotency")
        sock = (await _fresh_orgs(session_factory, 1))[0]

        assert (await _vote(session_factory, sock, trace_id, "up"))["vote_counted"] is False
        assert (await _vote(session_factory, orgs["cust-a"], trace_id, "up"))["vote_counted"] is True

    async def test_an_org_rating_its_own_trace_is_never_held_back(self, session_factory):
        async with session_scope(session_factory) as session:
            own = Organization(name="brand-new")
            session.add(own)
            await session.flush()
            org_id = own.id
            trace = Trace(
                org_id=org_id,
                title="my own failure",
                context_text="ctx",
                solution_text="fix",
                tags=[],
                agent_type="code",
            )
            session.add(trace)
            await session.flush()
            trace_id = trace.id

        await _vote(session_factory, org_id, trace_id, "up")
        async with session_scope(session_factory) as session:
            stored = await session.get(Trace, trace_id)
            assert stored.commons_votes == 1
            assert stored.trust == 1.0
