"""Clear every replica's verified-key cache the moment a key stops being valid anywhere.

hub/auth.py's verified-key cache (HUB_AUTH_CACHE_SECONDS) was safe only within one process: a key revoked through
another replica, or by an operator command, could still be served for the whole window. That made a 6x throughput
gain (hub/bench_concurrency.py) a security trade an operator had to accept knowingly. This closes the gap rather
than documenting it.

Every change that decides whether a key verifies -- revocation, rotation, an org's deletion, a region pin --
calls `auth.announce_auth_change`, which sends a NOTIFY inside the same transaction. Postgres delivers it on
commit, to every connection LISTENing on the channel. Each replica holds one such connection here and clears its
cache when one arrives.

Fail-safe, not best-effort: while this listener is not connected (startup, a dropped connection, a restarting
database), `auth.cached_key` returns nothing and nothing new is remembered. A replica that might miss a
revocation does not cache, so the window can only ever be the time a NOTIFY takes to arrive.
"""
from __future__ import annotations

import asyncio
import logging

from hub import auth

logger = logging.getLogger("commontrace.hub.auth_invalidation")

#: Reconnect backoff bounds, in seconds.
_MIN_BACKOFF = 0.5
_MAX_BACKOFF = 30.0


def _asyncpg_dsn(database_url: str) -> str:
    from hub.abuse import _to_asyncpg_dsn

    return _to_asyncpg_dsn(database_url)


async def listen(database_url: str, stop_event: asyncio.Event, *, connect=None) -> None:
    """Hold a LISTEN connection until `stop_event` is set, reconnecting on loss.

    `connect` is injectable for tests; it defaults to asyncpg.connect.
    """
    if connect is None:
        import asyncpg

        connect = asyncpg.connect
    dsn = _asyncpg_dsn(database_url)
    backoff = _MIN_BACKOFF

    def on_notify(*_args) -> None:
        auth.clear_auth_cache()

    while not stop_event.is_set():
        conn = None
        lost = asyncio.Event()
        try:
            conn = await connect(dsn)
            conn.add_termination_listener(lambda *_a: lost.set())
            await conn.add_listener(auth.AUTH_INVALIDATION_CHANNEL, on_notify)
            auth.set_auth_listener_live(True)
            backoff = _MIN_BACKOFF
            stop_wait = asyncio.ensure_future(stop_event.wait())
            lost_wait = asyncio.ensure_future(lost.wait())
            try:
                await asyncio.wait({stop_wait, lost_wait}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stop_wait.cancel()
                lost_wait.cancel()
            if lost.is_set() and not stop_event.is_set():
                logger.warning("auth invalidation listener lost its connection; caching is off until it returns")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure means "not listening", handled the same way
            logger.warning("auth invalidation listener could not connect (%s); caching is off", exc)
        finally:
            auth.set_auth_listener_live(False)
            if conn is not None:
                try:
                    await conn.close()
                except Exception:  # noqa: BLE001 - closing a dead connection may fail; nothing to recover
                    pass
        if stop_event.is_set():
            break
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, _MAX_BACKOFF)
