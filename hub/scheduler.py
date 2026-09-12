"""An opt-in in-process alternative to the external-cron sweep
`hub/alerts.py`'s own module docstring otherwise commits to: `check_rules`
and `generate_report` there are pure functions meant for an operator's own
cron via `hub.manage check-alerts`/`generate-report`, the same shape as
`webhook-deliver`'s existing redelivery sweep.

Off by default (`HUB_ALERT_SCHEDULER_ENABLED`) -- a deployment that already
points cron at those commands sees no change at all: `build_app` never
starts this loop unless asked to. Turning it on gives the Hub process its
own heartbeat instead, for an operator who would rather not run cron next
to it.

Running this on more than one replica against the same database is no
less safe than an operator's own overlapping cron entries would already
be: `check_rules`'s cooldown is read-then-written within one transaction,
so two sweeps racing the same instant can both see a rule as due and both
fire it -- one extra `alert.triggered` delivery before the cooldown these
writes just recorded catches up on the very next pass, not an unbounded
duplicate loop.
"""

from __future__ import annotations

import asyncio
import logging

from hub import alerts
from hub.db import session_scope

logger = logging.getLogger(__name__)

#: Matches hub/alerts.py's own DEFAULT_COOLDOWN_MINUTES order of
#: magnitude -- frequent enough that a crossed threshold is reported
#: promptly, infrequent enough that idle orgs cost one query every five
#: minutes, not one per second.
DEFAULT_INTERVAL_SECONDS = 300


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
