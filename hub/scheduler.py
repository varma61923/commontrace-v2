"""Opt-in in-process alternatives to the external-cron sweeps `hub/alerts.py`
and `hub/events.py`'s own module docstrings otherwise commit to: `check_rules`
and `deliver_pending` are pure functions meant for an operator's own cron via
`hub.manage check-alerts`/`webhook-deliver`.

Off by default (`HUB_ALERT_SCHEDULER_ENABLED`, `HUB_WEBHOOK_SCHEDULER_ENABLED`)
-- a deployment that already points cron at those commands sees no change at
all: `build_app` never starts either loop unless asked to. Turning one on
gives the Hub process its own heartbeat instead, for an operator who would
rather not run cron next to it -- and for webhook delivery specifically, a
much shorter loop than any cron entry would sanely use: an org's subscriber
hears about a quarantine or an experiment verdict within
`webhook_scheduler_interval_seconds`, not whenever the next cron tick lands.

Running either loop on more than one replica against the same database is no
less safe than an operator's own overlapping cron entries would already be:

- `check_rules`'s cooldown is read-then-written within one transaction, so two
  sweeps racing the same instant can both see a rule as due and both fire it
  -- one extra `alert.triggered` delivery before the cooldown these writes
  just recorded catches up on the very next pass, not an unbounded duplicate
  loop.
- `deliver_pending` claims each row it attempts (see its own docstring), so
  two replicas sweeping at once divide the queue rather than double-send.
"""

from __future__ import annotations

import asyncio
import logging

from hub import alerts, events
from hub.db import session_scope

logger = logging.getLogger(__name__)

#: Matches hub/alerts.py's own DEFAULT_COOLDOWN_MINUTES order of
#: magnitude -- frequent enough that a crossed threshold is reported
#: promptly, infrequent enough that idle orgs cost one query every five
#: minutes, not one per second.
DEFAULT_INTERVAL_SECONDS = 300

#: Webhook deliveries are the one surface where "real time" is the whole
#: point (STRATEGY.md's alert-consumer-on-the-other-end use case), so this
#: defaults an order of magnitude shorter than the alert sweep above.
DEFAULT_WEBHOOK_INTERVAL_SECONDS = 30


async def _sweep_once(session_factory) -> None:
    async with session_scope(session_factory) as session:
        # org_id=None: every enabled rule across every org, in the one
        # query hub/alerts.py's check_rules already supports -- not a
        # per-org loop this module would have to invent.
        await alerts.check_rules(session)


async def run(session_factory, *, interval_seconds: int, stop_event: asyncio.Event) -> None:
    """Calls `_sweep_once` every `interval_seconds` until `stop_event` is set.

    A sweep that raises (a transient DB error, a bad rule) is logged and
    the loop keeps going -- one bad tick must not silence every org's
    alerts until the process restarts.
    """
    while not stop_event.is_set():
        try:
            await _sweep_once(session_factory)
        except Exception:
            logger.exception("alert scheduler sweep failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass


async def _deliver_webhooks_once(session_factory, *, signing_key: str, cipher, batch_size: int) -> None:
    async with session_scope(session_factory) as session:
        result = await events.deliver_pending(
            session, events.http_transport(),
            signing_key=signing_key, limit=batch_size, cipher=cipher,
        )
        if result.attempted:
            logger.info(
                "webhook scheduler: attempted=%d delivered=%d retrying=%d gave_up=%d",
                result.attempted, result.delivered, result.retrying, result.gave_up,
            )


async def run_webhook_delivery(
    session_factory, *, interval_seconds: int, stop_event: asyncio.Event,
    signing_key: str, cipher, batch_size: int = 100,
) -> None:
    """The webhook-queue counterpart to `run` above -- same shape (sweep,
    log-and-continue on failure, wake promptly on `stop_event` rather than
    sleeping through it), draining `hub/events.py`'s pending-delivery queue
    instead of evaluating alert rules.
    """
    while not stop_event.is_set():
        try:
            await _deliver_webhooks_once(
                session_factory, signing_key=signing_key, cipher=cipher, batch_size=batch_size,
            )
        except Exception:
            logger.exception("webhook delivery scheduler sweep failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
