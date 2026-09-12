"""Threshold alerts and periodic reports (hub/alerts.py), delivered
through the existing webhook pipeline (hub/events.py) -- audit §8.3.

What these tests defend, in order of how badly getting it wrong would
hurt:

1. **An unknown metric or comparator is refused at rule-creation time**,
   not silently ignored at evaluation time -- "deny by construction",
   the same discipline hub/rbac.py applies to an unmapped tool.
2. **A metric that cannot be computed (no data, an unlimited plan) never
   fires** -- a rate over zero traces or a percentage of infinity is not
   a signal, and firing on one would be a false alarm baked into the
   product.
3. **Cooldown actually suppresses re-firing** on the very next check even
   though the condition still holds, and a fired rule DOES emit through
   hub/events.py -- proven by a real queued WebhookDelivery, not just a
   returned summary.
4. **Tenancy**: a rule never fires against another org's data, and
   `check_rules` scoped to one org never touches another's rules.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from hub import alerts, events, plans
from hub.db import session_scope
from hub.models import Organization, Trace, WebhookDelivery

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
URL = "https://example.invalid/hooks/commontrace"

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org(session_factory) -> str:
    async with session_scope(session_factory) as session:
        organization = Organization(name="alerts-org")
        session.add(organization)
        await session.flush()
        return organization.id


@pytest_asyncio.fixture
async def hooked_org(session_factory, org) -> str:
    """An org with a webhook endpoint subscribed to alert.triggered, so a
    fired rule is provably delivered, not just returned as a summary."""
    async with session_scope(session_factory) as session:
        await events.add_endpoint(
            session, org, URL, events=["alert.triggered", "report.generated"],
            signing_key="test-key",
        )
    return org


async def _add_traces(session_factory, org_id, n, *, quarantined=0):
    async with session_scope(session_factory) as session:
        for i in range(n):
            session.add(Trace(
                org_id=org_id, title=f"t{i}", context_text="c", solution_text="s",
                agent_type="support", quarantined=i < quarantined,
            ))


class TestCreateRule:
    async def test_creating_a_rule_returns_it(self, session_factory, org):
        async with session_scope(session_factory) as session:
            rule = await alerts.create_rule(
                session, org, alerts.METRIC_QUARANTINE_RATE, alerts.COMPARATOR_GT, 10.0,
            )
        assert rule.metric == alerts.METRIC_QUARANTINE_RATE
        assert rule.comparator == alerts.COMPARATOR_GT
        assert rule.threshold == 10.0
        assert rule.enabled is True

    async def test_an_unknown_metric_is_refused(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(alerts.AlertError, match="unknown metric"):
                await alerts.create_rule(
                    session, org, "trace_vibes", alerts.COMPARATOR_GT, 10.0,
                )

    async def test_an_unknown_comparator_is_refused(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(alerts.AlertError, match="unknown comparator"):
                await alerts.create_rule(
                    session, org, alerts.METRIC_QUARANTINE_RATE, "roughly", 10.0,
                )

    async def test_a_zero_cooldown_is_refused(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(alerts.AlertError, match="cooldown_minutes"):
                await alerts.create_rule(
                    session, org, alerts.METRIC_QUARANTINE_RATE, alerts.COMPARATOR_GT,
                    10.0, cooldown_minutes=0,
                )

    async def test_an_unknown_org_is_refused(self, session_factory):
        async with session_scope(session_factory) as session:
            with pytest.raises(alerts.AlertError, match="no such organization"):
                await alerts.create_rule(
                    session, "00000000-0000-0000-0000-000000000000",
                    alerts.METRIC_QUARANTINE_RATE, alerts.COMPARATOR_GT, 10.0,
                )


class TestListAndDeleteRule:
    async def test_listing_returns_rules_in_creation_order(self, session_factory, org):
        async with session_scope(session_factory) as session:
            await alerts.create_rule(
                session, org, alerts.METRIC_QUARANTINE_RATE, alerts.COMPARATOR_GT, 10.0,
            )
            await alerts.create_rule(
                session, org, alerts.METRIC_TRACES_USED_PCT, alerts.COMPARATOR_GT, 90.0,
            )
        async with session_scope(session_factory) as session:
            rules = await alerts.list_rules(session, org)
        assert [r.metric for r in rules] == [
            alerts.METRIC_QUARANTINE_RATE, alerts.METRIC_TRACES_USED_PCT,
        ]

    async def test_deleting_a_rule_removes_it(self, session_factory, org):
        async with session_scope(session_factory) as session:
            rule = await alerts.create_rule(
                session, org, alerts.METRIC_QUARANTINE_RATE, alerts.COMPARATOR_GT, 10.0,
            )
            rule_id = rule.id
        async with session_scope(session_factory) as session:
            assert await alerts.delete_rule(session, rule_id) is True
        async with session_scope(session_factory) as session:
            assert await alerts.list_rules(session, org) == []

    async def test_deleting_an_unknown_rule_reports_false(self, session_factory):
        async with session_scope(session_factory) as session:
            assert await alerts.delete_rule(
                session, "00000000-0000-0000-0000-000000000000"
            ) is False


class TestComputeMetric:
    async def test_quarantine_rate_is_none_with_no_traces(self, session_factory, org):
        async with session_scope(session_factory) as session:
            assert await alerts.compute_metric(
                session, org, alerts.METRIC_QUARANTINE_RATE
            ) is None

    async def test_quarantine_rate_reflects_the_fraction_quarantined(
        self, session_factory, org
    ):
        await _add_traces(session_factory, org, 4, quarantined=1)
        async with session_scope(session_factory) as session:
            rate = await alerts.compute_metric(session, org, alerts.METRIC_QUARANTINE_RATE)
        assert rate == 25.0

    async def test_commons_queries_used_pct_is_none_for_an_unlimited_plan(
        self, session_factory, org
    ):
        async with session_scope(session_factory) as session:
            o = await session.get(Organization, org)
            o.plan = "operator"
        async with session_scope(session_factory) as session:
            assert await alerts.compute_metric(
                session, org, alerts.METRIC_COMMONS_QUERIES_USED_PCT
            ) is None

    async def test_an_unknown_metric_raises(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(alerts.AlertError):
                await alerts.compute_metric(session, org, "trace_vibes")


class TestCheckRules:
    async def test_a_crossed_threshold_fires_and_queues_a_delivery(
        self, session_factory, hooked_org
    ):
        await _add_traces(session_factory, hooked_org, 4, quarantined=2)
        async with session_scope(session_factory) as session:
            await alerts.create_rule(
                session, hooked_org, alerts.METRIC_QUARANTINE_RATE,
                alerts.COMPARATOR_GT, 10.0,
            )
        async with session_scope(session_factory) as session:
            fired = await alerts.check_rules(session, hooked_org, now=NOW)
        assert len(fired) == 1
        assert fired[0]["metric"] == alerts.METRIC_QUARANTINE_RATE
        assert fired[0]["value"] == 50.0
        async with session_scope(session_factory) as session:
            deliveries = list((await session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.event_type == "alert.triggered",
                )
            )).scalars())
        assert len(deliveries) == 1
        assert deliveries[0].payload["metric"] == alerts.METRIC_QUARANTINE_RATE

    async def test_a_threshold_not_crossed_does_not_fire(self, session_factory, org):
        await _add_traces(session_factory, org, 4, quarantined=0)
        async with session_scope(session_factory) as session:
            await alerts.create_rule(
                session, org, alerts.METRIC_QUARANTINE_RATE, alerts.COMPARATOR_GT, 10.0,
            )
        async with session_scope(session_factory) as session:
            fired = await alerts.check_rules(session, org, now=NOW)
        assert fired == []

    async def test_an_incomputable_metric_does_not_fire(self, session_factory, org):
        """No traces yet -- quarantine_rate is None, never "0 > threshold"."""
        async with session_scope(session_factory) as session:
            await alerts.create_rule(
                session, org, alerts.METRIC_QUARANTINE_RATE, alerts.COMPARATOR_GT, -1.0,
            )
        async with session_scope(session_factory) as session:
            fired = await alerts.check_rules(session, org, now=NOW)
        assert fired == []

    async def test_cooldown_suppresses_an_immediate_refire(self, session_factory, org):
        await _add_traces(session_factory, org, 4, quarantined=2)
        async with session_scope(session_factory) as session:
            await alerts.create_rule(
                session, org, alerts.METRIC_QUARANTINE_RATE, alerts.COMPARATOR_GT,
                10.0, cooldown_minutes=60,
            )
        async with session_scope(session_factory) as session:
            first = await alerts.check_rules(session, org, now=NOW)
        assert len(first) == 1
        async with session_scope(session_factory) as session:
            second = await alerts.check_rules(
                session, org, now=NOW + timedelta(minutes=5),
            )
        assert second == []

    async def test_firing_again_after_the_cooldown_elapses(self, session_factory, org):
        await _add_traces(session_factory, org, 4, quarantined=2)
        async with session_scope(session_factory) as session:
            await alerts.create_rule(
                session, org, alerts.METRIC_QUARANTINE_RATE, alerts.COMPARATOR_GT,
                10.0, cooldown_minutes=30,
            )
        async with session_scope(session_factory) as session:
            await alerts.check_rules(session, org, now=NOW)
        async with session_scope(session_factory) as session:
            later = await alerts.check_rules(
                session, org, now=NOW + timedelta(minutes=31),
            )
        assert len(later) == 1

    async def test_a_disabled_rule_never_fires(self, session_factory, org):
        await _add_traces(session_factory, org, 4, quarantined=2)
        async with session_scope(session_factory) as session:
            rule = await alerts.create_rule(
                session, org, alerts.METRIC_QUARANTINE_RATE, alerts.COMPARATOR_GT, 10.0,
            )
            rule.enabled = False
        async with session_scope(session_factory) as session:
            fired = await alerts.check_rules(session, org, now=NOW)
        assert fired == []

    async def test_checking_one_org_never_fires_another_orgs_rule(
        self, session_factory, org
    ):
        async with session_scope(session_factory) as session:
            other = Organization(name="other-alerts-org")
            session.add(other)
            await session.flush()
            other_id = other.id
        await _add_traces(session_factory, other_id, 4, quarantined=4)
        async with session_scope(session_factory) as session:
            await alerts.create_rule(
                session, other_id, alerts.METRIC_QUARANTINE_RATE,
                alerts.COMPARATOR_GT, 10.0,
            )
        async with session_scope(session_factory) as session:
            fired = await alerts.check_rules(session, org, now=NOW)
        assert fired == []

    async def test_checking_every_org_fires_across_all_of_them(self, session_factory, org):
        async with session_scope(session_factory) as session:
            other = Organization(name="other-alerts-org-2")
            session.add(other)
            await session.flush()
            other_id = other.id
        await _add_traces(session_factory, org, 4, quarantined=2)
        await _add_traces(session_factory, other_id, 4, quarantined=2)
        async with session_scope(session_factory) as session:
            await alerts.create_rule(
                session, org, alerts.METRIC_QUARANTINE_RATE, alerts.COMPARATOR_GT, 10.0,
            )
            await alerts.create_rule(
                session, other_id, alerts.METRIC_QUARANTINE_RATE, alerts.COMPARATOR_GT, 10.0,
            )
        async with session_scope(session_factory) as session:
            fired = await alerts.check_rules(session, None, now=NOW)
        assert {f["org_id"] for f in fired} == {org, other_id}


class TestGenerateReport:
    async def test_a_report_is_queued_as_an_event(self, session_factory, hooked_org):
        await _add_traces(session_factory, hooked_org, 3)
        async with session_scope(session_factory) as session:
            report = await alerts.generate_report(session, hooked_org)
        assert report["traces_total"] == 3
        assert report["plan"] == "free"
        async with session_scope(session_factory) as session:
            deliveries = list((await session.execute(
                select(WebhookDelivery).where(WebhookDelivery.event_type == "report.generated")
            )).scalars())
        assert len(deliveries) == 1
        assert deliveries[0].payload["traces_total"] == 3

    async def test_an_unknown_org_is_refused(self, session_factory):
        async with session_scope(session_factory) as session:
            with pytest.raises(alerts.AlertError, match="no such organization"):
                await alerts.generate_report(
                    session, "00000000-0000-0000-0000-000000000000",
                )

    async def test_an_unlimited_allowance_is_reported_as_unlimited_not_a_lie(
        self, session_factory, org
    ):
        async with session_scope(session_factory) as session:
            o = await session.get(Organization, org)
            o.plan = "operator"
        async with session_scope(session_factory) as session:
            report = await alerts.generate_report(session, org)
        assert report["commons_queries_allowance"] == plans.UNLIMITED
