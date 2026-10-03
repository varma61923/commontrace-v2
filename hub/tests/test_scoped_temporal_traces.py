from __future__ import annotations

import pytest

from hub import crud
from hub.db import session_scope
from hub.models import Organization


@pytest.mark.asyncio
async def test_hub_contribute_and_search_with_scopes_and_as_of(config, session_factory):
    async with session_scope(session_factory) as session:
        org = Organization(name="scoped-test-org")
        session.add(org)
        await session.flush()
        org_id = org.id

        from hub.abuse import RateLimiter
        limiter = RateLimiter(per_minute=1000, burst=1000)

        await crud.contribute_trace(
            session, org_id, config, limiter,
            title="Global DB pooling",
            context_text="Global database connection configuration",
            solution_text="Set max connections to 20",
            tags=["db", "config"],
            scopes=[],
        )

        t_payments = await crud.contribute_trace(
            session, org_id, config, limiter,
            title="Payments webhook idempotency",
            context_text="Handling duplicate charges in Stripe webhook",
            solution_text="Use Idempotency-Key header on requests",
            tags=["payments", "stripe"],
            scopes=["payments"],
            valid_from="2026-01-01T00:00:00Z",
            valid_until="2027-01-01T00:00:00Z",
        )

        t_legacy = await crud.contribute_trace(
            session, org_id, config, limiter,
            title="Legacy billing gateway",
            context_text="Handling payments on old gateway",
            solution_text="Use legacy SOAP endpoint",
            tags=["payments"],
            scopes=["payments"],
            valid_from="2025-01-01T00:00:00Z",
            valid_until="2025-12-31T23:59:59Z",
        )

        found = await crud.search_traces(
            session, org_id, query="payments",
            scope="payments", as_of="2026-06-01T00:00:00Z",
        )
        found_ids = {t["id"] for t in found["traces"]}
        assert t_payments["id"] in found_ids
        assert t_legacy["id"] not in found_ids, "2025 legacy trace should be excluded by 2026 as_of"

        found_2025 = await crud.search_traces(
            session, org_id, query="payments",
            scope="payments", as_of="2025-06-01T00:00:00Z",
        )
        found_2025_ids = {t["id"] for t in found_2025["traces"]}
        assert t_legacy["id"] in found_2025_ids
        assert t_payments["id"] not in found_2025_ids, "2026 trace should be excluded by 2025 as_of"


async def _org(session, name):
    org = Organization(name=name)
    session.add(org)
    await session.flush()
    return org.id


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs, message", [
    ({"scopes": ["x" * 65]}, "64"),
    ({"scopes": [f"s{i}" for i in range(21)]}, "at most 20"),
    ({"scopes": ["pay\x00ments"]}, "NUL"),
    ({"scopes": "payments"}, "list"),
    ({"valid_from": "not-a-date"}, "valid_from"),
    ({"valid_from": "2026-05-01", "valid_until": "2026-01-01"}, "after"),
])
async def test_bad_routing_or_validity_is_rejected_not_stored(config, session_factory, kwargs, message):
    from hub.abuse import RateLimiter, TraceRejected

    async with session_scope(session_factory) as session:
        org_id = await _org(session, "scoped-validation-org")
        with pytest.raises(TraceRejected, match=message):
            await crud.contribute_trace(
                session, org_id, config, RateLimiter(per_minute=1000, burst=1000),
                title="Validation probe", context_text="ctx", solution_text="sol", tags=["probe"], **kwargs,
            )


@pytest.mark.asyncio
async def test_naive_dates_are_stored_as_utc_and_scopes_deduplicated(config, session_factory):
    from hub.abuse import RateLimiter

    async with session_scope(session_factory) as session:
        org_id = await _org(session, "scoped-naive-org")
        res = await crud.contribute_trace(
            session, org_id, config, RateLimiter(per_minute=1000, burst=1000),
            title="Naive date", context_text="ctx", solution_text="sol", tags=["probe"],
            scopes=[" payments ", "payments", "billing"], valid_from="2026-01-01",
        )
        trace = await crud.get_trace(session, org_id, res["id"])
        assert trace["scopes"] == ["payments", "billing"]
        assert trace["valid_from"].startswith("2026-01-01T00:00:00")


@pytest.mark.asyncio
async def test_an_unparseable_as_of_is_an_error_not_a_silently_ignored_filter(session_factory):
    async with session_scope(session_factory) as session:
        org_id = await _org(session, "scoped-asof-org")
        with pytest.raises(ValueError, match="as_of"):
            await crud.search_traces(session, org_id, as_of="invalid-date-format-abc")
