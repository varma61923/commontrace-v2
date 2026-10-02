from __future__ import annotations

import asyncio
import pathlib
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from commontrace import experiment, integrity, value
from hub import commons, crud, plans
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, Trace, UsageCounter

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def orgs(session_factory):
    async with session_scope(session_factory) as session:
        made = {}
        for name in ("payer", "freeloader", "operator"):
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


async def _seed_kb_entry(session_factory, operator_org_id, title, context="ctx"):
    async with session_scope(session_factory) as session:
        trace = Trace(
            org_id=operator_org_id,
            title=title, context_text=context, solution_text="fix",
            tags=[], agent_type="code",
            shared_with_commons=True,
            shared_at=datetime.now(timezone.utc),
            shared_rationale="test fixture",
            commons_signature=commons.signature_for(title, context, []),
            commons_source="seed",
        )
        session.add(trace)
        await session.flush()
        return trace.id


def _failure(label, title, context="ctx"):
    return {"label": label, "signature": commons.signature_for(title, context, [])}


async def _set_plan(session_factory, org_id, name):
    async with session_scope(session_factory) as session:
        (await session.get(Organization, org_id)).plan = name


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestPlanResolution:
    def test_unset_is_free(self):
        assert plans.get(None).name == "free"
        assert plans.get("").name == "free"

    def test_unknown_names_resolve_to_the_smallest_plan(self):
        for junk in ("enterprise", "tema", "SCALE-PLUS", "unlimited", "  "):
            assert plans.get(junk).name == "free", junk

    def test_no_customer_plan_grants_unlimited_commons_queries(self):
        for name, plan in plans.PLANS.items():
            if name == "operator":
                continue
            assert plan.commons_queries_per_month != plans.UNLIMITED, name

    async def test_a_new_org_is_on_the_free_plan(self, session_factory, orgs):
        async with session_scope(session_factory) as session:
            assert (await session.get(Organization, orgs["payer"])).plan == "free"


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
        monkeypatch.setitem(
            plans.PLANS, "free",
            plans.Plan("free", max_traces=1, commons_queries_per_month=20,
                       commons_access=True, summary="test"),
        )
        await _contribute(session_factory, config, orgs["payer"], "mine")
        await _contribute(session_factory, config, orgs["freeloader"], "theirs")

    async def test_amend_trace_is_refused_past_the_trace_limit(
        self, session_factory, config, orgs, monkeypatch
    ):
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


class TestCommonsQueryMetering:
    async def test_a_query_consumes_allowance(self, session_factory, orgs):
        await _seed_kb_entry(session_factory, orgs["operator"], "shared thing")
        async with session_scope(session_factory) as session:
            await crud.commons_overlap(
                session, orgs["payer"], [_failure("f", "shared thing")],
            )
        async with session_scope(session_factory) as session:
            assert await crud._usage(session, orgs["payer"], crud.METRIC_COMMONS_QUERIES) == 1

    async def test_refuses_past_the_allowance(self, session_factory, orgs, monkeypatch):
        monkeypatch.setitem(
            plans.PLANS, "free",
            plans.Plan("free", max_traces=1_000, commons_queries_per_month=2,
                       commons_access=True, summary="test"),
        )
        await _seed_kb_entry(session_factory, orgs["operator"], "shared thing")
        for _ in range(2):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(session, orgs["payer"], [_failure("f", "shared thing")])
        with pytest.raises(plans.EntitlementExceeded) as exc:
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(session, orgs["payer"], [_failure("f", "shared thing")])
        assert exc.value.metric == crud.METRIC_COMMONS_QUERIES
        assert "knowledge base" in exc.value.remedy.lower()

    async def test_an_empty_submission_is_not_charged(self, session_factory, orgs):
        async with session_scope(session_factory) as session:
            await crud.commons_overlap(session, orgs["payer"], [])
        async with session_scope(session_factory) as session:
            assert await crud._usage(session, orgs["payer"], crud.METRIC_COMMONS_QUERIES) == 0

    async def test_a_rejected_submission_is_not_charged(self, session_factory, orgs):
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

    async def test_the_operator_plan_is_not_metered(self, session_factory, orgs):
        await _set_plan(session_factory, orgs["payer"], "operator")
        await _seed_kb_entry(session_factory, orgs["operator"], "shared thing")
        for _ in range(3):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(session, orgs["payer"], [_failure("f", "shared thing")])
        async with session_scope(session_factory) as session:
            e = await crud.entitlements(session, orgs["payer"])
        assert e["commons_queries"]["allowance"] == plans.UNLIMITED
        assert e["commons_queries"]["remaining"] == plans.UNLIMITED


class TestMeteringIsAtomic:
    async def test_concurrent_queries_all_get_counted(self, session_factory, orgs):
        await _seed_kb_entry(session_factory, orgs["operator"], "shared thing")
        await _set_plan(session_factory, orgs["payer"], "team")

        async def one():
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(session, orgs["payer"], [_failure("f", "shared thing")])

        await asyncio.gather(*(one() for _ in range(20)))

        async with session_scope(session_factory) as session:
            assert await crud._usage(session, orgs["payer"], crud.METRIC_COMMONS_QUERIES) == 20

    async def test_the_entitlement_check_itself_is_serialized_at_the_boundary(
        self, session_factory, orgs, monkeypatch
    ):
        monkeypatch.setitem(
            plans.PLANS, "free",
            plans.Plan("free", max_traces=1_000, commons_queries_per_month=3,
                       commons_access=True, summary="test"),
        )
        await _seed_kb_entry(session_factory, orgs["operator"], "shared thing")
        async with session_scope(session_factory) as session:
            await crud._meter(session, orgs["payer"], crud.METRIC_COMMONS_QUERIES)
            await crud._meter(session, orgs["payer"], crud.METRIC_COMMONS_QUERIES)

        results = []

        async def one():
            try:
                async with session_scope(session_factory) as session:
                    await crud.commons_overlap(session, orgs["payer"], [_failure("f", "shared thing")])
                results.append("ok")
            except plans.EntitlementExceeded:
                results.append("refused")

        await asyncio.gather(*(one() for _ in range(10)))

        assert results.count("ok") == 1, f"expected exactly 1 success, got {results}"
        async with session_scope(session_factory) as session:
            final_used = await crud._usage(session, orgs["payer"], crud.METRIC_COMMONS_QUERIES)
        assert final_used == 3, "usage must never exceed the allowance even under a concurrent race"

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


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestTheErrorIsActionable:
    def test_it_is_not_reported_as_a_rate_limit(self):
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
        from hub.server import _error_response

        body = _error_response(plans.EntitlementExceeded(
            metric="traces", limit=1, used=1, plan="free", remedy="Upgrade.",
        ))
        blob = " ".join(str(v) for v in body.values()).lower()
        for leak in ("select ", "traceback", "sqlalchemy", "asyncpg", "/app/", "org_id="):
            assert leak not in blob, leak


class TestTheValueLinkedPricingShape:
    @staticmethod
    def _effect(verdict, n_injected, size, lo, hi):
        return experiment.CausalEffect(
            lesson_slug="m", n_injected=n_injected, n_withheld=200,
            rate_injected=0.7, rate_withheld=0.7 - size, effect=size,
            ci_low=lo, ci_high=hi, p_value=0.01,
            significant=verdict in (experiment.VERDICT_HELPS, experiment.VERDICT_HURTS),
            min_detectable_effect=0.05, verdict=verdict, note="",
        )

    @staticmethod
    def _sound():
        return integrity.audit([
            integrity.Assignment("m", f"o{i}", i % 2 == 0, 0.5, "s",
                                 i % 3 == 0, None, "rev")
            for i in range(60)
        ])

    async def test_the_share_is_of_measured_value(self):
        report = value.compute(
            [self._effect(experiment.VERDICT_HELPS, 1000, 0.10, 0.05, 0.15)],
            self._sound(), value_per_occasion=20.0,
        )
        assert plans.billable_value(report) == pytest.approx(400.0)

    async def test_nothing_is_billable_when_the_experiment_is_not_readable(self):
        report = value.compute(
            [self._effect(experiment.VERDICT_HELPS, 1000, 0.10, 0.05, 0.15)],
            None, value_per_occasion=20.0,
        )
        assert plans.billable_value(report) is None

    async def test_a_memory_that_hurt_produces_a_negative_charge(self):
        report = value.compute(
            [self._effect(experiment.VERDICT_HURTS, 1000, -0.10, -0.15, -0.05)],
            self._sound(), value_per_occasion=20.0,
        )
        assert plans.billable_value(report) == pytest.approx(-400.0)

    async def test_no_rate_means_no_charge_rather_than_a_guessed_one(self):
        report = value.compute(
            [self._effect(experiment.VERDICT_HELPS, 1000, 0.10, 0.05, 0.15)],
            self._sound(),
        )
        assert report.readable
        assert plans.billable_value(report) is None

    async def test_the_share_is_a_ratio_and_no_currency_is_stored(self):
        assert 0 < plans.VALUE_CAPTURE_SHARE < 1
        source = pathlib.Path(plans.__file__).read_text(encoding="utf-8")
        for symbol in ("$", "USD", "EUR", "GBP"):
            assert symbol not in source, f"a currency reached hub/plans.py: {symbol}"

    async def test_the_share_is_overridable_without_editing_the_module(self):
        report = value.compute(
            [self._effect(experiment.VERDICT_HELPS, 1000, 0.10, 0.05, 0.15)],
            self._sound(), value_per_occasion=20.0,
        )
        assert plans.billable_value(report, share=0.5) == pytest.approx(1000.0)

    async def test_seats_alone_no_longer_describe_the_model(self):
        assert plans.get("team").max_agents > 0
        assert hasattr(plans, "VALUE_CAPTURE_SHARE")
        assert plans.VALUE_BILLED_ONLY_ON_ESTABLISHED_EFFECTS is True
