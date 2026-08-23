"""Entitlements: the plan, actually enforced.

The commons already MEASURED value (`commons-value`: what each org's shared
knowledge delivered to other fleets). This is the other half -- capture --
and it is only real if the server refuses the request that exceeds the
plan. So what is tested here is refusal, metering that survives
concurrency, and the credit mechanism that makes contributing pay.

The single most expensive bug in this area is a meter that loses
increments under load, because it loses money in exact proportion to how
well the product is doing. It has its own test.
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from hub import commons, crud, plans
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, Trace, UsageCounter

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def orgs(session_factory):
    async with session_scope(session_factory) as session:
        made = {}
        for name in ("payer", "freeloader", "contributor"):
            o = Organization(name=name)
            session.add(o)
            await session.flush()
            made[name] = o.id
        return made


async def _contribute(session_factory, config, org_id, title, **kw):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        return await crud.contribute_trace(
            session, org_id, config, rate_limiter,
            title=title, context_text="ctx", solution_text="fix",
            tags=[], agent_type="code", actor="test", **kw,
        )


def _failure(label, title, context="ctx"):
    return {"label": label, "signature": commons.signature_for(title, context, [])}


async def _set_plan(session_factory, org_id, name):
    async with session_scope(session_factory) as session:
        (await session.get(Organization, org_id)).plan = name


# --- 1. Resolution fails closed -----------------------------------------


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestPlanResolution:
    """A plan that cannot be resolved must never resolve to more than the
    smallest one. Every other failure here is a bug; this one is revenue
    walking out the door, and it is the kind that shows up as a typo in an
    operator command or as a plan name removed in a later release while
    rows still reference it."""

    def test_unset_is_free(self):
        assert plans.get(None).name == "free"
        assert plans.get("").name == "free"

    def test_unknown_names_resolve_to_the_smallest_plan(self):
        for junk in ("enterprise", "tema", "SCALE-PLUS", "unlimited", "  "):
            assert plans.get(junk).name == "free", junk

    def test_no_customer_plan_grants_unlimited_commons_queries(self):
        """Storage may be unlimited on a large plan; commons queries never
        are, because they consume other orgs' contributions rather than the
        caller's own data. The operator plan is not a customer plan."""
        for name, plan in plans.PLANS.items():
            if name == "operator":
                continue
            assert plan.commons_queries_per_month != plans.UNLIMITED, name

    async def test_a_new_org_is_on_the_free_plan(self, session_factory, orgs):
        async with session_scope(session_factory) as session:
            assert (await session.get(Organization, orgs["payer"])).plan == "free"


# --- 2. Storage limits ---------------------------------------------------


class TestStorageEntitlement:
    async def test_refuses_a_write_past_the_trace_limit(self, session_factory, config, orgs, monkeypatch):
        monkeypatch.setitem(
            plans.PLANS, "free",
            plans.Plan("free", max_traces=2, commons_queries_per_month=20,
                       commons_access=True, summary="test"),
        )
        for i in range(2):
            await _contribute(session_factory, config, orgs["payer"], f"t{i}")
        with pytest.raises(plans.EntitlementExceeded) as exc:
            await _contribute(session_factory, config, orgs["payer"], "one too many")
        assert exc.value.metric == "traces"
        assert exc.value.used == 2

    async def test_the_refused_write_stored_nothing(self, session_factory, config, orgs, monkeypatch):
        monkeypatch.setitem(
            plans.PLANS, "free",
            plans.Plan("free", max_traces=1, commons_queries_per_month=20,
                       commons_access=True, summary="test"),
        )
        await _contribute(session_factory, config, orgs["payer"], "first")
        with pytest.raises(plans.EntitlementExceeded):
            await _contribute(session_factory, config, orgs["payer"], "second")
        async with session_scope(session_factory) as session:
            n = await session.scalar(
                select(Trace).where(Trace.org_id == orgs["payer"], Trace.title == "second")
            )
        assert n is None

    async def test_an_idempotent_retry_is_not_refused_at_the_cap(
        self, session_factory, config, orgs, monkeypatch
    ):
        """The replay path stores nothing, so refusing it would turn a safe
        retry into a hard failure at exactly the moment an org is at its
        limit -- and the caller cannot distinguish that from the original
        write having failed, which is the whole problem idempotency keys
        exist to solve."""
        monkeypatch.setitem(
            plans.PLANS, "free",
            plans.Plan("free", max_traces=1, commons_queries_per_month=20,
                       commons_access=True, summary="test"),
        )
        first = await _contribute(
            session_factory, config, orgs["payer"], "only one", idempotency_key="k1",
        )
        replay = await _contribute(
            session_factory, config, orgs["payer"], "only one", idempotency_key="k1",
        )
        assert replay["id"] == first["id"]

    async def test_one_orgs_usage_does_not_consume_anothers(
        self, session_factory, config, orgs, monkeypatch
    ):
        """The meter is org-scoped for the same reason every read path is.
        A shared counter would let one noisy tenant exhaust another's plan."""
        monkeypatch.setitem(
            plans.PLANS, "free",
            plans.Plan("free", max_traces=1, commons_queries_per_month=20,
                       commons_access=True, summary="test"),
        )
        await _contribute(session_factory, config, orgs["payer"], "mine")
        # Not raising is the assertion.
        await _contribute(session_factory, config, orgs["freeloader"], "theirs")

    async def test_amend_trace_is_refused_past_the_trace_limit(
        self, session_factory, config, orgs, monkeypatch
    ):
        """amend_trace INSERTs a new Trace row into the supersession chain
        (hub/crud.py:amend_trace's docstring) -- it consumes a storage slot
        exactly like contribute_trace, and an org already at its cap must
        not be able to keep growing storage by amending instead of
        contributing."""
        monkeypatch.setitem(
            plans.PLANS, "free",
            plans.Plan("free", max_traces=1, commons_queries_per_month=20,
                       commons_access=True, summary="test"),
        )
        original = await _contribute(session_factory, config, orgs["payer"], "only one")
        rate_limiter = make_rate_limiter(config)
        with pytest.raises(plans.EntitlementExceeded) as exc:
            async with session_scope(session_factory) as session:
                await crud.amend_trace(
                    session, orgs["payer"], original["id"], config, rate_limiter,
                    title="amended", actor="test",
                )
        assert exc.value.metric == "traces"

    async def test_amend_trace_refusal_stores_nothing(
        self, session_factory, config, orgs, monkeypatch
    ):
        monkeypatch.setitem(
            plans.PLANS, "free",
            plans.Plan("free", max_traces=1, commons_queries_per_month=20,
                       commons_access=True, summary="test"),
        )
        original = await _contribute(session_factory, config, orgs["payer"], "only one")
        rate_limiter = make_rate_limiter(config)
        with pytest.raises(plans.EntitlementExceeded):
            async with session_scope(session_factory) as session:
                await crud.amend_trace(
                    session, orgs["payer"], original["id"], config, rate_limiter,
                    title="amended", actor="test",
                )
        async with session_scope(session_factory) as session:
            count = await session.scalar(
                select(func.count()).select_from(Trace).where(Trace.org_id == orgs["payer"])
            )
        assert count == 1


# --- 3. The metered unit -------------------------------------------------


class TestCommonsQueryMetering:
    async def _share(self, session_factory, config, org_id, title):
        t = await _contribute(session_factory, config, org_id, title)
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, org_id, t["id"])
        return t

    async def test_a_query_consumes_allowance(self, session_factory, config, orgs):
        await self._share(session_factory, config, orgs["contributor"], "shared thing")
        async with session_scope(session_factory) as session:
            await crud.commons_overlap(
                session, orgs["payer"], [_failure("f", "shared thing")],
            )
        async with session_scope(session_factory) as session:
            assert await crud._usage(session, orgs["payer"], crud.METRIC_COMMONS_QUERIES) == 1

    async def test_refuses_past_the_allowance(self, session_factory, config, orgs, monkeypatch):
        monkeypatch.setitem(
            plans.PLANS, "free",
            plans.Plan("free", max_traces=1_000, commons_queries_per_month=2,
                       commons_access=True, summary="test"),
        )
        await self._share(session_factory, config, orgs["contributor"], "shared thing")
        for _ in range(2):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(session, orgs["payer"], [_failure("f", "shared thing")])
        with pytest.raises(plans.EntitlementExceeded) as exc:
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(session, orgs["payer"], [_failure("f", "shared thing")])
        assert exc.value.metric == crud.METRIC_COMMONS_QUERIES
        assert "commons" in exc.value.remedy.lower()

    async def test_an_empty_submission_is_not_charged(self, session_factory, orgs):
        """It compares nothing. Billing a no-op is the kind of line item a
        customer finds and then stops trusting the rest of the bill."""
        async with session_scope(session_factory) as session:
            await crud.commons_overlap(session, orgs["payer"], [])
        async with session_scope(session_factory) as session:
            assert await crud._usage(session, orgs["payer"], crud.METRIC_COMMONS_QUERIES) == 0

    async def test_a_rejected_submission_is_not_charged(self, session_factory, orgs):
        """Validation runs before metering, so a malformed request is an
        error rather than a silently consumed query."""
        with pytest.raises(commons.CommonsInputError):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(
                    session, orgs["payer"], [{"label": "f", "signature": [1, 2, 3]}],
                )
        async with session_scope(session_factory) as session:
            assert await crud._usage(session, orgs["payer"], crud.METRIC_COMMONS_QUERIES) == 0

    async def test_reading_the_meter_does_not_consume_the_meter(self, session_factory, orgs):
        async with session_scope(session_factory) as session:
            for _ in range(3):
                await crud.entitlements(session, orgs["payer"])
        async with session_scope(session_factory) as session:
            assert await crud._usage(session, orgs["payer"], crud.METRIC_COMMONS_QUERIES) == 0

    async def test_the_operator_plan_is_not_metered(self, session_factory, config, orgs):
        """The org that owns seeded rows must be able to prime the commons
        without that priming looking like consumption."""
        await _set_plan(session_factory, orgs["payer"], "operator")
        await self._share(session_factory, config, orgs["contributor"], "shared thing")
        for _ in range(3):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(session, orgs["payer"], [_failure("f", "shared thing")])
        async with session_scope(session_factory) as session:
            e = await crud.entitlements(session, orgs["payer"])
        assert e["commons_queries"]["allowance"] == plans.UNLIMITED
        assert e["commons_queries"]["remaining"] == plans.UNLIMITED


# --- 4. The race that loses money ---------------------------------------


class TestMeteringIsAtomic:
    async def test_concurrent_queries_all_get_counted(self, session_factory, config, orgs):
        """Twenty concurrent commons queries must consume twenty units.

        A read-modify-write meter loses increments here -- every pair of
        overlapping calls reads the same n and writes the same n+1, so an
        org gets free queries in exact proportion to how parallel its fleet
        is. That is not a rounding error; it is a discount that scales with
        the customer's size, applied to the customers who cost the most to
        serve. The upsert in crud._meter is what makes this pass.
        """
        t = await _contribute(session_factory, config, orgs["contributor"], "shared thing")
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, orgs["contributor"], t["id"])
        await _set_plan(session_factory, orgs["payer"], "team")

        async def one():
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(session, orgs["payer"], [_failure("f", "shared thing")])

        await asyncio.gather(*(one() for _ in range(20)))

        async with session_scope(session_factory) as session:
            assert await crud._usage(session, orgs["payer"], crud.METRIC_COMMONS_QUERIES) == 20

    async def test_the_meter_is_keyed_per_period_and_metric(self, session_factory, orgs):
        async with session_scope(session_factory) as session:
            await crud._meter(session, orgs["payer"], "metric_a")
            await crud._meter(session, orgs["payer"], "metric_b")
        async with session_scope(session_factory) as session:
            rows = (await session.execute(
                select(UsageCounter).where(UsageCounter.org_id == orgs["payer"])
            )).scalars().all()
        assert {r.metric for r in rows} == {"metric_a", "metric_b"}
        assert all(r.n == 1 for r in rows)


# --- 5. Contributing pays ------------------------------------------------


class TestEarnedAllowance:
    """The mechanism that makes a knowledge commons work rather than fill
    with filler. `commons-value` measures delivered value; this is what
    happens because of it."""

    async def _shared_and_hit(self, session_factory, config, orgs, hits):
        t = await _contribute(session_factory, config, orgs["contributor"], "shared thing")
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, orgs["contributor"], t["id"])
            row = await session.get(Trace, t["id"])
            row.commons_hits = hits
        return t

    async def test_delivered_hits_raise_the_allowance(self, session_factory, config, orgs):
        await self._shared_and_hit(session_factory, config, orgs, hits=4)
        async with session_scope(session_factory) as session:
            e = await crud.entitlements(session, orgs["contributor"])
        base = plans.PLANS["free"].commons_queries_per_month
        assert e["commons_queries"]["earned"] == 4 * plans.QUERY_CREDIT_PER_HIT
        assert e["commons_queries"]["allowance"] == base + 4 * plans.QUERY_CREDIT_PER_HIT

    async def test_sharing_without_delivering_earns_nothing(self, session_factory, config, orgs):
        """Sharing is free to do and trivial to fake in bulk -- an org could
        dump ten thousand junk traces in an afternoon. A hit requires that
        someone else's real failure matched against a corpus that excludes
        the sharer's own rows, so it cannot be self-dealt. Crediting shares
        instead of hits would make the filler problem the business model."""
        await self._shared_and_hit(session_factory, config, orgs, hits=0)
        async with session_scope(session_factory) as session:
            e = await crud.entitlements(session, orgs["contributor"])
        assert e["commons_queries"]["earned"] == 0

    async def test_seeded_rows_earn_the_operator_nothing(self, session_factory, config, orgs):
        """The operator crediting itself for priming its own commons is
        circular, exactly as it is for the network-effect metric."""
        t = await self._shared_and_hit(session_factory, config, orgs, hits=10)
        async with session_scope(session_factory) as session:
            (await session.get(Trace, t["id"])).commons_source = "seed"
        async with session_scope(session_factory) as session:
            e = await crud.entitlements(session, orgs["contributor"])
        assert e["delivered_hits"] == 0
        assert e["commons_queries"]["earned"] == 0

    async def test_earned_allowance_actually_lets_more_queries_through(
        self, session_factory, config, orgs, monkeypatch
    ):
        """The credit has to be spendable, not just displayed."""
        monkeypatch.setitem(
            plans.PLANS, "free",
            plans.Plan("free", max_traces=1_000, commons_queries_per_month=1,
                       commons_access=True, summary="test"),
        )
        monkeypatch.setattr(plans, "QUERY_CREDIT_PER_HIT", 5)
        await self._shared_and_hit(session_factory, config, orgs, hits=1)
        other = await _contribute(session_factory, config, orgs["payer"], "someone elses thing")
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, orgs["payer"], other["id"])

        # 1 granted + 5 earned = 6 queries before it refuses.
        for _ in range(6):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(
                    session, orgs["contributor"], [_failure("f", "someone elses thing")],
                )
        with pytest.raises(plans.EntitlementExceeded):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(
                    session, orgs["contributor"], [_failure("f", "someone elses thing")],
                )


# --- 6. What the client is told ------------------------------------------


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestTheErrorIsActionable:
    def test_it_is_not_reported_as_a_rate_limit(self):
        """A rate limit clears by waiting; a plan limit does not. A client
        that cannot tell them apart will retry a plan refusal on a backoff
        schedule forever, which looks to the customer like the product is
        broken rather than like they need a bigger plan."""
        from hub.server import _error_response

        body = _error_response(plans.EntitlementExceeded(
            metric="commons_queries", limit=20, used=20, plan="free",
            remedy="Share traces to the commons.",
        ))
        assert body["error"] == "entitlement_exceeded"
        assert body["error"] != "rate_limited"

    def test_it_carries_what_a_client_needs_to_render_an_upgrade_path(self):
        from hub.server import _error_response

        body = _error_response(plans.EntitlementExceeded(
            metric="traces", limit=1_000, used=1_000, plan="free", remedy="Purge or upgrade.",
        ))
        for field in ("metric", "limit", "used", "plan", "remedy"):
            assert field in body, field

    def test_it_leaks_no_internals(self):
        """Entitlement errors are the most-seen error in the product, so
        they are also the most likely place to leak structure."""
        from hub.server import _error_response

        body = _error_response(plans.EntitlementExceeded(
            metric="traces", limit=1, used=1, plan="free", remedy="Upgrade.",
        ))
        blob = " ".join(str(v) for v in body.values()).lower()
        for leak in ("select ", "traceback", "sqlalchemy", "asyncpg", "/app/", "org_id="):
            assert leak not in blob, leak
