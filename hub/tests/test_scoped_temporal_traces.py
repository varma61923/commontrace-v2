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

        # Contribute a global trace (no scopes)
        await crud.contribute_trace(
            session, org_id, config, limiter,
            title="Global DB pooling",
            context_text="Global database connection configuration",
            solution_text="Set max connections to 20",
            tags=["db", "config"],
            scopes=[],
        )

        # Contribute a payments-scoped trace
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

        # Contribute a legacy trace valid only in 2025
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

        # Search with scope='payments' and as_of in 2026
        found = await crud.search_traces(
            session, org_id, query="payments",
            scope="payments", as_of="2026-06-01T00:00:00Z",
        )
        found_ids = {t["id"] for t in found["traces"]}
        assert t_payments["id"] in found_ids
        assert t_legacy["id"] not in found_ids, "2025 legacy trace should be excluded by 2026 as_of"

        # Search with as_of in 2025
        found_2025 = await crud.search_traces(
            session, org_id, query="payments",
            scope="payments", as_of="2025-06-01T00:00:00Z",
        )
        found_2025_ids = {t["id"] for t in found_2025["traces"]}
        assert t_legacy["id"] in found_2025_ids
        assert t_payments["id"] not in found_2025_ids, "2026 trace should be excluded by 2025 as_of"
