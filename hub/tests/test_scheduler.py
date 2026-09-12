"""hub/scheduler.py: the opt-in in-process alternative to the external-cron
sweep hub/alerts.py's `check_rules` is otherwise meant for.

What these tests defend, in order of how badly getting it wrong would hurt:

1. **A sweep that raises does not end the loop.** A transient DB error or a
   bad rule must cost one tick, not silence every org's alerts until the
   process restarts.
2. **Shutdown is prompt.** `run` is meant to wake on `stop_event`, not sleep
   through it -- a long interval must not delay a graceful shutdown.
3. **The loop actually reaches `hub/alerts.py`'s `check_rules` against a
   real session**, not just a mocked stand-in: one sweep against a real due
   rule moves `last_triggered_at`, the same effect `hub.manage check-alerts`
   already produces via its own cron.

Wiring this into `build_app`'s lifespan (on/off, boot-and-shutdown-cleanly)
is covered by `hub/tests/test_build_app_startup.py`, which already exists
to drive that lifespan for exactly this kind of opt-in feature.
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from hub import alerts, scheduler
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org(session_factory) -> str:
    async with session_scope(session_factory) as session:
        organization = Organization(name="scheduler-org")
        session.add(organization)
        await session.flush()
        return organization.id


async def _run_until(stop_event: asyncio.Event, calls: list, n: int) -> None:
    while len(calls) < n:
        await asyncio.sleep(0.01)
    stop_event.set()


class TestRunLoop:
    async def test_it_sweeps_repeatedly_until_stopped(self, session_factory, monkeypatch):
        calls: list[int] = []

        async def fake_check_rules(session, *args, **kwargs):
            calls.append(1)
            return []

        monkeypatch.setattr(alerts, "check_rules", fake_check_rules)
        stop_event = asyncio.Event()
        stopper = asyncio.create_task(_run_until(stop_event, calls, 3))
        await asyncio.wait_for(
            scheduler.run(session_factory, interval_seconds=0, stop_event=stop_event),
            timeout=5,
        )
        await stopper
        assert len(calls) >= 3

    async def test_it_stops_promptly_instead_of_sleeping_out_the_interval(
        self, session_factory, monkeypatch
    ):
        """A one-hour interval must not delay shutdown by even a second:
        the wait is on the stop event, not a plain `asyncio.sleep`."""
        async def fake_check_rules(session, *args, **kwargs):
            return []

        monkeypatch.setattr(alerts, "check_rules", fake_check_rules)
        stop_event = asyncio.Event()
        stop_event.set()  # already stopped before the loop's first check
        await asyncio.wait_for(
            scheduler.run(session_factory, interval_seconds=3600, stop_event=stop_event),
            timeout=2,
        )

    async def test_a_sweep_that_raises_does_not_stop_the_loop(self, session_factory, monkeypatch):
        calls: list[int] = []

        async def flaky_check_rules(session, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient database error")
            return []

        monkeypatch.setattr(alerts, "check_rules", flaky_check_rules)
        stop_event = asyncio.Event()
        stopper = asyncio.create_task(_run_until(stop_event, calls, 2))
        await asyncio.wait_for(
            scheduler.run(session_factory, interval_seconds=0, stop_event=stop_event),
            timeout=5,
        )
        await stopper
        assert len(calls) >= 2


class TestSweepReachesRealAlertRules:
    async def test_a_due_rule_fires_through_a_real_sweep(self, session_factory, org):
        """No mock of hub/alerts.py here: create a rule and a trace that
        crosses its threshold, run the loop for one tick, and confirm the
        rule's cooldown was actually recorded -- proof `_sweep_once` reaches
        `check_rules` against a real database, not just that it is called."""
        async with session_scope(session_factory) as session:
            await alerts.create_rule(
                session, org, alerts.METRIC_QUARANTINE_RATE, alerts.COMPARATOR_GT, 0,
            )
            session.add(Trace(
                org_id=org, title="t", context_text="c", solution_text="s",
                tags=[], agent_type="code", quarantined=True,
            ))

        stop_event = asyncio.Event()

        async def stop_soon():
            await asyncio.sleep(0.1)
            stop_event.set()

        stopper = asyncio.create_task(stop_soon())
        await asyncio.wait_for(
            scheduler.run(session_factory, interval_seconds=0, stop_event=stop_event),
            timeout=5,
        )
        await stopper

        async with session_scope(session_factory) as session:
            rules = await alerts.list_rules(session, org)
        assert rules[0].last_triggered_at is not None
