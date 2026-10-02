from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from hub import crud, manage
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def orgs(session_factory):
    async with session_scope(session_factory) as session:
        made = {}
        for name in ("customer", "operator"):
            o = Organization(name=name)
            session.add(o)
            await session.flush()
            made[name] = o.id
        return made


async def _real_count(session_factory, org_id: str) -> int:
    async with session_scope(session_factory) as session:
        return int(
            await session.scalar(select(func.count()).select_from(Trace).where(Trace.org_id == org_id))
            or 0
        )


async def _stored_count(session_factory, org_id: str) -> int:
    async with session_scope(session_factory) as session:
        return int(await session.scalar(select(Organization.trace_count).where(Organization.id == org_id)))


async def _assert_accurate(session_factory, org_id: str):
    real, stored = await _real_count(session_factory, org_id), await _stored_count(session_factory, org_id)
    assert stored == real, f"trace_count={stored} but a real count(*) says {real}"
    return real


async def _contribute(session_factory, config, org_id, title, **kw):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        return await crud.contribute_trace(
            session, org_id, config, rate_limiter,
            title=title, context_text="ctx", solution_text="fix",
            tags=[], agent_type="code", actor="test", **kw,
        )


async def _amend(session_factory, config, org_id, trace_id, **kw):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        return await crud.amend_trace(
            session, org_id, trace_id, config, rate_limiter, actor="test", **kw,
        )


class TestContributeTraceMaintainsTheCounter:
    async def test_a_fresh_contribution_increments_by_one(self, session_factory, config, orgs):
        await _assert_accurate(session_factory, orgs["customer"])
        await _contribute(session_factory, config, orgs["customer"], "t1")
        assert await _assert_accurate(session_factory, orgs["customer"]) == 1
        await _contribute(session_factory, config, orgs["customer"], "t2")
        assert await _assert_accurate(session_factory, orgs["customer"]) == 2

    async def test_an_idempotent_replay_does_not_double_count(self, session_factory, config, orgs):
        await _contribute(session_factory, config, orgs["customer"], "t1", idempotency_key="k1")
        assert await _assert_accurate(session_factory, orgs["customer"]) == 1

        replay = await _contribute(session_factory, config, orgs["customer"], "t1", idempotency_key="k1")
        assert replay is not None
        assert await _assert_accurate(session_factory, orgs["customer"]) == 1

    async def test_one_orgs_count_does_not_affect_anothers(self, session_factory, config, orgs):
        await _contribute(session_factory, config, orgs["customer"], "t1")
        assert await _stored_count(session_factory, orgs["operator"]) == 0
        await _assert_accurate(session_factory, orgs["operator"])


class TestAmendTraceMaintainsTheCounter:
    async def test_amending_increments_by_one_it_is_a_new_row_not_a_mutation(
        self, session_factory, config, orgs
    ):
        original = await _contribute(session_factory, config, orgs["customer"], "t1")
        assert await _assert_accurate(session_factory, orgs["customer"]) == 1

        await _amend(session_factory, config, orgs["customer"], original["id"], title="t1-edited")
        assert await _assert_accurate(session_factory, orgs["customer"]) == 2

    async def test_an_amend_idempotent_replay_does_not_double_count(self, session_factory, config, orgs):
        original = await _contribute(session_factory, config, orgs["customer"], "t1")
        await _amend(session_factory, config, orgs["customer"], original["id"],
                     title="edit", idempotency_key="ak1")
        assert await _assert_accurate(session_factory, orgs["customer"]) == 2

        replay = await _amend(session_factory, config, orgs["customer"], original["id"],
                              title="edit", idempotency_key="ak1")
        assert replay is not None
        assert await _assert_accurate(session_factory, orgs["customer"]) == 2


class TestDeletionMaintainsTheCounter:
    async def test_deleting_a_single_trace_decrements_by_one(self, session_factory, config, orgs):
        trace = await _contribute(session_factory, config, orgs["customer"], "t1")
        await _contribute(session_factory, config, orgs["customer"], "t2")
        assert await _assert_accurate(session_factory, orgs["customer"]) == 2

        async with session_scope(session_factory) as session:
            deleted = await crud.delete_trace(session, orgs["customer"], trace["id"])
        assert deleted is True
        assert await _assert_accurate(session_factory, orgs["customer"]) == 1

    async def test_deleting_decrements_by_the_whole_amendment_chain(self, session_factory, config, orgs):
        original = await _contribute(session_factory, config, orgs["customer"], "t1")
        amended = await _amend(session_factory, config, orgs["customer"], original["id"], title="edit1")
        await _amend(session_factory, config, orgs["customer"], amended["id"], title="edit2")
        assert await _assert_accurate(session_factory, orgs["customer"]) == 3

        async with session_scope(session_factory) as session:
            deleted = await crud.delete_trace(session, orgs["customer"], original["id"])
        assert deleted is True
        assert await _assert_accurate(session_factory, orgs["customer"]) == 0

    async def test_purge_trace_decrements_the_whole_chain(self, session_factory, config, orgs):
        original = await _contribute(session_factory, config, orgs["customer"], "t1")
        await _amend(session_factory, config, orgs["customer"], original["id"], title="edit1")
        assert await _assert_accurate(session_factory, orgs["customer"]) == 2

        purged = await manage.purge_trace(original["id"], session_factory=session_factory)
        assert purged is True
        assert await _assert_accurate(session_factory, orgs["customer"]) == 0


class TestReviewKbSubmissionMaintainsTheCounter:
    async def test_approving_a_submission_increments_the_operator_orgs_count(
        self, session_factory, config, orgs
    ):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            submission = await crud.submit_kb_entry(
                session, orgs["customer"], config, rate_limiter,
                title="pattern", context_text="ctx", solution_text="fix",
            )
        assert await _stored_count(session_factory, orgs["operator"]) == 0

        async with session_scope(session_factory) as session:
            result = await crud.review_kb_submission(
                session, submission["id"], "approve", orgs["operator"], reviewer="op",
            )
        assert result is not None
        assert await _assert_accurate(session_factory, orgs["operator"]) == 1
        assert await _stored_count(session_factory, orgs["customer"]) == 0


class TestCommonsSeedMaintainsTheCounter:
    async def test_seeding_n_entries_increments_by_n_in_one_batched_call(
        self, session_factory, config, orgs, tmp_path
    ):
        path = tmp_path / "seed.jsonl"
        path.write_text(
            "\n".join(
                '{"title": "t%d", "context_text": "ctx %d", "solution_text": "fix %d"}'
                % (i, i, i)
                for i in range(5)
            )
        )
        ok = await manage.commons_seed(str(path), orgs["operator"], session_factory=session_factory)
        assert ok is True
        assert await _assert_accurate(session_factory, orgs["operator"]) == 5


class TestTheInvariantSurvivesAMixedSequence:
    async def test_contribute_amend_delete_interleaved(self, session_factory, config, orgs):
        org = orgs["customer"]
        a = await _contribute(session_factory, config, org, "a")
        await _assert_accurate(session_factory, org)
        b = await _contribute(session_factory, config, org, "b")
        await _assert_accurate(session_factory, org)
        a2 = await _amend(session_factory, config, org, a["id"], title="a-edited")
        await _assert_accurate(session_factory, org)
        await _contribute(session_factory, config, org, "c")
        await _assert_accurate(session_factory, org)

        async with session_scope(session_factory) as session:
            await crud.delete_trace(session, org, b["id"])
        await _assert_accurate(session_factory, org)

        await _amend(session_factory, config, org, a2["id"], title="a-edited-again")
        assert await _assert_accurate(session_factory, org) == 4
