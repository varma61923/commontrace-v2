from __future__ import annotations

import asyncio
import logging

from hub import alerts, events
from hub.db import session_scope

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 300

DEFAULT_WEBHOOK_INTERVAL_SECONDS = 30


async def _sweep_once(session_factory) -> None:
    async with session_scope(session_factory) as session:
        await alerts.check_rules(session)


async def run(session_factory, *, interval_seconds: int, stop_event: asyncio.Event) -> None:
    """Calls `_sweep_once` every `interval_seconds` until `stop_event` is set."""
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


async def run_connector_sweep(session_factory, *, interval_seconds: int, stop_event: asyncio.Event) -> None:
    from hub.connectors import service

    while not stop_event.is_set():
        try:
            finalized = await service.finalize_matured(session_factory)
            if finalized:
                logger.info("connector sweep: finalized %d outcome(s)", finalized)
        except Exception:
            logger.exception("connector sweep failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
