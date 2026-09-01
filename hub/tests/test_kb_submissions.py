"""The community-submission channel: `submit_kb_entry` -> operator review
(`review_kb_submission`) -> an accepted submission becomes a Knowledge Base
entry and raises the submitting org's query allowance.

The property that matters most: a submission is NOT a second door into the
Knowledge Base the way the retired `share_trace` was. It writes to a table
`commons_overlap`/`commons_search` never read, so a pending or rejected
submission cannot surface to any org, submitter included, no matter how
exactly its signature matches. Only `review_kb_submission`'s approve path --
called only from hub/manage.py, never from an MCP tool -- can turn one into
a real `Trace(commons_source='seed')`. See hub/models.py:
KnowledgeBaseSubmission and hub/plans.py "why bonus_commons_queries is not
the same mistake twice" for why review, not opt-in, is what keeps this from
repeating the adverse-selection failure the retired design had.
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from hub import commons, crud, plans
from hub.abuse import RateLimited, TraceRejected, make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, Trace
from hub.schema_validation import SchemaValidationError

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def orgs(session_factory):
    async with session_scope(session_factory) as session:
        made = {}
        for name in ("submitter", "operator", "other"):
            o = Organization(name=name)
            session.add(o)
            await session.flush()
            made[name] = o.id
        return made


async def _submit(session_factory, config, org_id, title="A recurring failure", **kw):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        return await crud.submit_kb_entry(
            session, org_id, config, rate_limiter,
            title=title, context_text=kw.pop("context_text", "ctx"),
            solution_text=kw.pop("solution_text", "fix"),
            tags=kw.pop("tags", None), agent_type=kw.pop("agent_type", "code"),
            rationale=kw.pop("rationale", "substrate, not business logic"),
            actor="test", **kw,
        )


def _failure(label, title, context="ctx", tags=None):
    return {"label": label, "signature": commons.signature_for(title, context, tags or [])}


class TestSubmissionIsCreatedNotPublished:
    async def test_a_submission_starts_pending(self, session_factory, config, orgs):
        result = await _submit(session_factory, config, orgs["submitter"], "Stripe webhooks retry")
        assert result["status"] == "pending"
        assert result["id"]

    async def test_it_writes_no_trace_row_at_all(self, session_factory, config, orgs):
        await _submit(session_factory, config, orgs["submitter"], "Stripe webhooks retry")
        async with session_scope(session_factory) as session:
            n = await session.scalar(
                select(func.count()).select_from(Trace).where(Trace.org_id == orgs["submitter"])
            )
        assert n == 0

    async def test_a_pending_submission_never_surfaces_via_commons_overlap(
        self, session_factory, config, orgs
    ):
        """The strongest possible probe: query with the EXACT signature of
        the pending submission, from a DIFFERENT org. It must still find
        nothing -- commons_overlap only ever reads Trace rows, and a
        submission is not one."""
        await _submit(session_factory, config, orgs["submitter"], "Stripe webhooks retry", context_text="ctx")
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["other"], [_failure("f", "Stripe webhooks retry", "ctx")],
            )
        assert report["n_covered"] == 0

    async def test_a_pending_submission_never_surfaces_via_commons_search(
        self, session_factory, config, orgs
    ):
        await _submit(session_factory, config, orgs["submitter"], "Stripe webhooks retry", context_text="ctx")
        sig = commons.signature_for("Stripe webhooks retry", "ctx", [])
        async with session_scope(session_factory) as session:
            result = await crud.commons_search(session, orgs["other"], sig)
        assert result["candidates"] == []

    async def test_the_submitting_org_itself_cannot_see_its_pending_submission_as_coverage(
        self, session_factory, config, orgs
    ):
        await _submit(session_factory, config, orgs["submitter"], "Stripe webhooks retry", context_text="ctx")
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["submitter"], [_failure("f", "Stripe webhooks retry", "ctx")],
            )
        assert report["n_covered"] == 0


class TestSubmissionValidation:
    async def test_an_empty_title_is_rejected(self, session_factory, config, orgs):
        with pytest.raises(SchemaValidationError):
            await _submit(session_factory, config, orgs["submitter"], title="")

    async def test_an_oversized_solution_is_rejected(self, session_factory, config, orgs):
        with pytest.raises(TraceRejected):
            await _submit(
                session_factory, config, orgs["submitter"],
                solution_text="x" * (config.max_text_chars + 1),
            )

    async def test_an_oversized_rationale_is_rejected(self, session_factory, config, orgs):
        with pytest.raises(TraceRejected):
            await _submit(session_factory, config, orgs["submitter"], rationale="x" * 501)

    async def test_rejected_validation_stores_nothing(self, session_factory, config, orgs):
        with pytest.raises(SchemaValidationError):
            await _submit(session_factory, config, orgs["submitter"], title="")
        async with session_scope(session_factory) as session:
            rows = await crud.list_my_kb_submissions(session, orgs["submitter"])
        assert rows == []

    async def test_rate_limiting_applies(self, session_factory, orgs):
        from hub.config import HubConfig

        strict_config = HubConfig(
            database_url="postgresql+asyncpg://x/y", rate_limit_per_minute=0, rate_limit_burst=0,
        )
        rate_limiter = make_rate_limiter(strict_config)
        with pytest.raises(RateLimited):
            async with session_scope(session_factory) as session:
                await crud.submit_kb_entry(
                    session, orgs["submitter"], strict_config, rate_limiter,
                    title="t", context_text="c", solution_text="s", rationale="r",
                    actor="test",
                )

    async def test_too_many_pending_submissions_are_refused(self, session_factory, config, orgs):
        for i in range(crud.MAX_PENDING_SUBMISSIONS_PER_ORG):
            await _submit(session_factory, config, orgs["submitter"], title=f"failure {i}")
        with pytest.raises(TraceRejected):
            await _submit(session_factory, config, orgs["submitter"], title="one too many")

    async def test_an_idempotent_retry_replays_not_duplicates(self, session_factory, config, orgs):
        first = await _submit(
            session_factory, config, orgs["submitter"], "t", idempotency_key="k1",
        )
        replay = await _submit(
            session_factory, config, orgs["submitter"], "t", idempotency_key="k1",
        )
        assert replay["id"] == first["id"]
        async with session_scope(session_factory) as session:
            rows = await crud.list_my_kb_submissions(session, orgs["submitter"])
        assert len(rows) == 1

    async def test_reusing_a_key_with_a_different_payload_conflicts(self, session_factory, config, orgs):
        await _submit(session_factory, config, orgs["submitter"], "t", idempotency_key="k1")
        with pytest.raises(crud.IdempotencyKeyConflict):
            await _submit(session_factory, config, orgs["submitter"], "different title", idempotency_key="k1")


class TestListMyKbSubmissions:
    async def test_an_org_never_sees_another_orgs_submissions(self, session_factory, config, orgs):
        await _submit(session_factory, config, orgs["submitter"], "mine")
        async with session_scope(session_factory) as session:
            rows = await crud.list_my_kb_submissions(session, orgs["other"])
        assert rows == []

    async def test_an_org_sees_its_own(self, session_factory, config, orgs):
        await _submit(session_factory, config, orgs["submitter"], "mine")
        async with session_scope(session_factory) as session:
            rows = await crud.list_my_kb_submissions(session, orgs["submitter"])
        assert len(rows) == 1
        assert rows[0]["title"] == "mine"
        assert rows[0]["status"] == "pending"


class TestReviewSubmissionApprove:
    async def test_approving_creates_a_seed_trace_owned_by_the_operator(
        self, session_factory, config, orgs
    ):
        s = await _submit(session_factory, config, orgs["submitter"], "Stripe retries", context_text="ctx")
        async with session_scope(session_factory) as session:
            result = await crud.review_kb_submission(
                session, s["id"], "approve", orgs["operator"], reviewer="op",
            )
        assert result["status"] == "approved"
        trace_id = result["resulting_trace_id"]
        assert trace_id

        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
        assert trace.org_id == orgs["operator"]
        assert trace.org_id != orgs["submitter"]
        assert trace.commons_source == "seed"
        assert trace.shared_with_commons is True
        assert trace.commons_signature is not None

    async def test_the_approved_entry_is_now_visible_via_commons_overlap(
        self, session_factory, config, orgs
    ):
        s = await _submit(session_factory, config, orgs["submitter"], "Stripe retries", context_text="ctx")
        async with session_scope(session_factory) as session:
            await crud.review_kb_submission(session, s["id"], "approve", orgs["operator"], reviewer="op")

        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["other"], [_failure("f", "Stripe retries", "ctx")],
            )
        assert report["n_covered"] == 1

    async def test_approving_awards_credit_to_the_submitting_org(self, session_factory, config, orgs):
        s = await _submit(session_factory, config, orgs["submitter"], "t")
        async with session_scope(session_factory) as session:
            result = await crud.review_kb_submission(
                session, s["id"], "approve", orgs["operator"], reviewer="op", credit=7,
            )
        assert result["credit_awarded"] == 7
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, orgs["submitter"])
        assert org.bonus_commons_queries == 7

    async def test_approving_without_an_explicit_credit_uses_the_default(
        self, session_factory, config, orgs
    ):
        s = await _submit(session_factory, config, orgs["submitter"], "t")
        async with session_scope(session_factory) as session:
            result = await crud.review_kb_submission(session, s["id"], "approve", orgs["operator"], reviewer="op")
        assert result["credit_awarded"] == plans.SUBMISSION_ACCEPTANCE_CREDIT

    async def test_the_credit_actually_raises_the_query_allowance(
        self, session_factory, config, orgs, monkeypatch
    ):
        """The credit has to be spendable, not just displayed."""
        monkeypatch.setitem(
            plans.PLANS, "free",
            plans.Plan("free", max_traces=1_000, commons_queries_per_month=1,
                       commons_access=True, summary="test"),
        )
        s = await _submit(session_factory, config, orgs["submitter"], "shared thing")
        async with session_scope(session_factory) as session:
            await crud.review_kb_submission(
                session, s["id"], "approve", orgs["operator"], reviewer="op", credit=3,
            )

        # 1 granted + 3 earned = 4 queries before it refuses.
        for _ in range(4):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(
                    session, orgs["submitter"], [_failure("f", "shared thing")],
                )
        with pytest.raises(plans.EntitlementExceeded):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(
                    session, orgs["submitter"], [_failure("f", "shared thing")],
                )

    async def test_entitlements_reports_the_bonus_separately(self, session_factory, config, orgs):
        s = await _submit(session_factory, config, orgs["submitter"], "t")
        async with session_scope(session_factory) as session:
            await crud.review_kb_submission(
                session, s["id"], "approve", orgs["operator"], reviewer="op", credit=5,
            )
        async with session_scope(session_factory) as session:
            e = await crud.entitlements(session, orgs["submitter"])
        assert e["commons_queries"]["bonus_from_accepted_submissions"] == 5
        base = plans.PLANS["free"].commons_queries_per_month
        assert e["commons_queries"]["allowance"] == base + 5


class TestReviewSubmissionReject:
    async def test_rejecting_creates_no_trace_and_no_credit(self, session_factory, config, orgs):
        s = await _submit(session_factory, config, orgs["submitter"], "t")
        async with session_scope(session_factory) as session:
            result = await crud.review_kb_submission(
                session, s["id"], "reject", orgs["operator"], reviewer="op",
                rejection_reason="too generic",
            )
        assert result["status"] == "rejected"
        assert result["rejection_reason"] == "too generic"
        assert result["resulting_trace_id"] is None
        assert result["credit_awarded"] == 0

        async with session_scope(session_factory) as session:
            org = await session.get(Organization, orgs["submitter"])
            n_traces = await session.scalar(
                select(func.count()).select_from(Trace).where(Trace.commons_source == "seed")
            )
        assert org.bonus_commons_queries == 0
        assert n_traces == 0

    async def test_a_rejected_submission_stays_invisible_to_commons_overlap(
        self, session_factory, config, orgs
    ):
        s = await _submit(session_factory, config, orgs["submitter"], "t", context_text="ctx")
        async with session_scope(session_factory) as session:
            await crud.review_kb_submission(session, s["id"], "reject", orgs["operator"], reviewer="op")
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(session, orgs["other"], [_failure("f", "t", "ctx")])
        assert report["n_covered"] == 0


class TestReviewSubmissionEdgeCases:
    async def test_reviewing_twice_the_second_call_is_a_no_op(self, session_factory, config, orgs):
        s = await _submit(session_factory, config, orgs["submitter"], "t")
        async with session_scope(session_factory) as session:
            await crud.review_kb_submission(session, s["id"], "approve", orgs["operator"], reviewer="op")
        async with session_scope(session_factory) as session:
            second = await crud.review_kb_submission(
                session, s["id"], "approve", orgs["operator"], reviewer="op",
            )
        assert second is None

    async def test_an_unknown_submission_id_returns_none(self, session_factory, orgs):
        import uuid

        async with session_scope(session_factory) as session:
            result = await crud.review_kb_submission(
                session, str(uuid.uuid4()), "approve", orgs["operator"], reviewer="op",
            )
        assert result is None

    async def test_a_malformed_id_returns_none_not_raises(self, session_factory, orgs):
        async with session_scope(session_factory) as session:
            result = await crud.review_kb_submission(
                session, "not-a-uuid-at-all", "approve", orgs["operator"], reviewer="op",
            )
        assert result is None

    async def test_an_invalid_decision_raises_value_error(self, session_factory, config, orgs):
        s = await _submit(session_factory, config, orgs["submitter"], "t")
        with pytest.raises(ValueError):
            async with session_scope(session_factory) as session:
                await crud.review_kb_submission(
                    session, s["id"], "maybe", orgs["operator"], reviewer="op",
                )


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestQueryAllowanceBonusArithmetic:
    def test_bonus_adds_to_a_finite_allowance(self):
        plan = plans.Plan("x", max_traces=1, commons_queries_per_month=10, commons_access=True, summary="")
        assert plans.query_allowance(plan, bonus=5) == 15

    def test_unlimited_plan_stays_unlimited_regardless_of_bonus(self):
        plan = plans.Plan(
            "x", max_traces=1, commons_queries_per_month=plans.UNLIMITED,
            commons_access=True, summary="",
        )
        assert plans.query_allowance(plan, bonus=1000) == plans.UNLIMITED

    def test_a_negative_bonus_is_floored_at_zero(self):
        plan = plans.Plan("x", max_traces=1, commons_queries_per_month=10, commons_access=True, summary="")
        assert plans.query_allowance(plan, bonus=-5) == 10

    def test_no_bonus_defaults_to_the_plan_grant(self):
        plan = plans.Plan("x", max_traces=1, commons_queries_per_month=10, commons_access=True, summary="")
        assert plans.query_allowance(plan) == 10


class TestConcurrentSubmissionApproval:
    async def test_two_concurrent_approvals_of_the_same_submission_only_one_wins(
        self, session_factory, config, orgs
    ):
        """SELECT ... FOR UPDATE ... WHERE status='pending' means only the
        first of two concurrent reviewers can ever find the row still
        pending -- the second sees it already decided and gets None,
        never a double-credited org or two Trace rows for one submission.
        """
        s = await _submit(session_factory, config, orgs["submitter"], "t")

        async def one():
            async with session_scope(session_factory) as session:
                return await crud.review_kb_submission(
                    session, s["id"], "approve", orgs["operator"], reviewer="op",
                )

        results = await asyncio.gather(*(one() for _ in range(5)))
        assert sum(1 for r in results if r is not None) == 1

        async with session_scope(session_factory) as session:
            org = await session.get(Organization, orgs["submitter"])
        assert org.bonus_commons_queries == plans.SUBMISSION_ACCEPTANCE_CREDIT

    async def test_ten_distinct_submissions_approved_concurrently_all_credit(
        self, session_factory, config, orgs
    ):
        """Distinct from the test above: this is 10 DIFFERENT pending
        submissions from the SAME org, approved concurrently -- not
        repeated attempts on one submission. The `SELECT ... FOR UPDATE
        ... WHERE status='pending'` lock is per-submission, so it does
        nothing to serialize these against each other; each approval
        independently read the submitting org's bonus_commons_queries and
        wrote back `old + awarded` in plain Python, so whichever commit
        landed last overwrote the column with its own stale total and
        silently discarded every other concurrent approval's credit, even
        though each one's own submission.credit_awarded and audit log
        entry still say it was granted. Fixed via an atomic SQL-level
        increment (see hub/crud.py:review_kb_submission). Reproduced
        before that fix: bonus_commons_queries landed well under
        10 * SUBMISSION_ACCEPTANCE_CREDIT within a handful of runs.
        """
        submissions = [
            await _submit(session_factory, config, orgs["submitter"], title=f"failure {i}")
            for i in range(10)
        ]

        async def _approve(submission_id):
            async with session_scope(session_factory) as session:
                return await crud.review_kb_submission(
                    session, submission_id, "approve", orgs["operator"], reviewer="op",
                )

        results = await asyncio.gather(*[_approve(s["id"]) for s in submissions], return_exceptions=True)
        exceptions = [r for r in results if isinstance(r, BaseException)]
        assert not exceptions, f"unexpected exceptions approving 10 distinct submissions: {exceptions}"
        assert all(r is not None for r in results), "every distinct submission's own approval must succeed"

        async with session_scope(session_factory) as session:
            org = await session.get(Organization, orgs["submitter"])
        assert org.bonus_commons_queries == 10 * plans.SUBMISSION_ACCEPTANCE_CREDIT, (
            f"expected {10 * plans.SUBMISSION_ACCEPTANCE_CREDIT}, got {org.bonus_commons_queries} "
            "-- a concurrent approval's credit was lost"
        )
