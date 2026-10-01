"""The opt-in verified-key cache: off by default, bounded, never caches a failure, forgotten on revocation in this
process, and gone after its window. It must also make the request path skip the database when it is on."""
import pytest

from hub import auth
from hub.db import session_scope
from hub.models import Organization

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _off_afterwards():
    yield
    auth.configure_auth_cache(0)


async def _key(session_factory):
    async with session_scope(session_factory) as session:
        org = Organization(name="cache-org")
        session.add(org)
        await session.flush()
        issued = await auth.issue_api_key(session, org.id)
        return org.id, issued.raw_key, None


async def _verify(session_factory, raw):
    async with session_scope(session_factory) as session:
        return await auth.verify_api_key(session, raw)


async def test_it_is_off_by_default_and_remembers_nothing(session_factory, config):
    _, raw, _ = await _key(session_factory)
    assert auth._AUTH_CACHE_TTL == 0.0
    assert await _verify(session_factory, raw) is not None
    assert auth.cached_key(raw) is None and not auth._AUTH_CACHE


async def test_when_on_a_verified_key_is_remembered_by_its_hmac_not_the_raw_key(session_factory, config):
    org_id, raw, _ = await _key(session_factory)
    auth.configure_auth_cache(30)
    assert auth.cached_key(raw) is None
    verified = await _verify(session_factory, raw)
    assert auth.cached_key(raw) == verified and verified.org_id == org_id
    assert raw not in auth._AUTH_CACHE and auth._key_hmac(raw) in auth._AUTH_CACHE


async def test_a_failed_verification_is_never_cached(session_factory, config):
    auth.configure_auth_cache(30)
    assert await _verify(session_factory, "ct_live_not-a-real-key") is None
    assert auth.cached_key("ct_live_not-a-real-key") is None and not auth._AUTH_CACHE


async def test_it_expires_after_its_window(session_factory, config, monkeypatch):
    _, raw, _ = await _key(session_factory)
    auth.configure_auth_cache(5)
    await _verify(session_factory, raw)
    now = auth.time.monotonic()
    monkeypatch.setattr(auth.time, "monotonic", lambda: now + 4.9)
    assert auth.cached_key(raw) is not None
    monkeypatch.setattr(auth.time, "monotonic", lambda: now + 5.1)
    assert auth.cached_key(raw) is None


async def test_revoking_through_this_process_forgets_the_key_at_once(session_factory, config):
    _, raw, _ = await _key(session_factory)
    auth.configure_auth_cache(30)
    await _verify(session_factory, raw)
    assert auth.cached_key(raw) is not None
    async with session_scope(session_factory) as session:
        from sqlalchemy import select

        from hub.models import ApiKey
        key_id = (await session.execute(select(ApiKey.id))).scalars().first()
        await auth.revoke_api_key(session, key_id)
    assert auth.cached_key(raw) is None
    assert await _verify(session_factory, raw) is None


async def test_rotation_forgets_the_old_key(session_factory, config):
    _, raw, _ = await _key(session_factory)
    auth.configure_auth_cache(30)
    await _verify(session_factory, raw)
    async with session_scope(session_factory) as session:
        from sqlalchemy import select

        from hub.models import ApiKey
        key_id = (await session.execute(select(ApiKey.id))).scalars().first()
        await auth.rotate_api_key(session, key_id)
    assert auth.cached_key(raw) is None


async def test_the_cache_is_bounded(session_factory, config, monkeypatch):
    monkeypatch.setattr(auth, "_AUTH_CACHE_MAX", 3)
    auth.configure_auth_cache(30)
    for i in range(10):
        auth._remember(f"ct_live_{i}", auth.AuthenticatedKey(org_id="o", key_prefix="p"))
        assert len(auth._AUTH_CACHE) <= 3


@pytest.mark.parametrize("asked,got", [(-5, 0.0), (0, 0.0), (30, 30.0), (600, 60.0)])
async def test_the_window_is_clamped_to_zero_through_sixty_seconds(asked, got):
    auth.configure_auth_cache(asked)
    assert auth._AUTH_CACHE_TTL == got


@pytest.mark.parametrize("value,ok", [("0", True), ("60", True), ("61", False), ("-1", False), ("abc", False)])
async def test_the_setting_is_validated(monkeypatch, value, ok):
    from hub.config import HubConfig
    monkeypatch.setenv("HUB_DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("HUB_AUTH_CACHE_SECONDS", value)
    if ok:
        assert HubConfig.from_env().auth_cache_seconds == int(value)
    else:
        with pytest.raises((ValueError, RuntimeError)):
            HubConfig.from_env()


async def test_a_cached_key_skips_the_database_on_the_request_path(session_factory, config):
    """Through the real middleware: after one verification, the next request opens no session at all."""
    import httpx
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from hub import abuse, server

    _, raw, _ = await _key(session_factory)
    opened = []

    def counting_factory():
        opened.append(1)
        return session_factory()

    async def ok(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok)])
    limiter = abuse.make_rate_limiter(config)
    wrapped = server.ApiKeyAuthMiddleware(app, session_factory=counting_factory, protected_path="/mcp",
                                          auth_rate_limiter=limiter, read_rate_limiter=limiter)
    auth.configure_auth_cache(30)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://t") as client:
        headers = {"Authorization": f"Bearer {raw}"}
        assert (await client.get("/mcp", headers=headers)).status_code == 200
        first = len(opened)
        assert (await client.get("/mcp", headers=headers)).status_code == 200
        assert len(opened) == first


# --- Across replicas (hub/auth_invalidation.py) -------------------------------------------------------------------


@pytest.fixture
def _listener_off_afterwards():
    yield
    auth.set_auth_listener_live(False)
    auth.configure_auth_cache(0)


async def _raw_notify():
    """Another replica committing a revocation: a NOTIFY from a different connection, which never touches this
    process's cache directly."""
    import asyncpg

    from hub.abuse import _to_asyncpg_dsn
    from hub.tests.conftest import TEST_DATABASE_URL

    conn = await asyncpg.connect(_to_asyncpg_dsn(TEST_DATABASE_URL))
    try:
        await conn.execute("SELECT pg_notify($1, '')", auth.AUTH_INVALIDATION_CHANNEL)
    finally:
        await conn.close()


async def _until(predicate, timeout=5.0):
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


async def test_a_revocation_on_another_replica_clears_this_replicas_cache(
    session_factory, config, _listener_off_afterwards
):
    import asyncio

    from hub import auth_invalidation
    from hub.tests.conftest import TEST_DATABASE_URL

    _, raw, _ = await _key(session_factory)
    auth.configure_auth_cache(30, require_listener=True)
    stop = asyncio.Event()
    task = asyncio.create_task(auth_invalidation.listen(TEST_DATABASE_URL, stop))
    try:
        assert await _until(lambda: auth._AUTH_CACHE_LISTENER_LIVE)
        await _verify(session_factory, raw)
        assert auth.cached_key(raw) is not None
        await _raw_notify()
        assert await _until(lambda: auth.cached_key(raw) is None), "the NOTIFY did not clear the cache"
    finally:
        stop.set()
        await task
    assert auth._AUTH_CACHE_LISTENER_LIVE is False


async def test_without_a_live_listener_nothing_is_cached(session_factory, config, _listener_off_afterwards):
    _, raw, _ = await _key(session_factory)
    auth.configure_auth_cache(30, require_listener=True)
    assert await _verify(session_factory, raw) is not None
    assert auth.cached_key(raw) is None and not auth._AUTH_CACHE


async def test_losing_the_listener_empties_the_cache(session_factory, config, _listener_off_afterwards):
    _, raw, _ = await _key(session_factory)
    auth.configure_auth_cache(30, require_listener=True)
    auth.set_auth_listener_live(True)
    await _verify(session_factory, raw)
    assert auth.cached_key(raw) is not None
    auth.set_auth_listener_live(False)
    assert auth.cached_key(raw) is None and not auth._AUTH_CACHE


async def test_an_unreachable_database_leaves_the_listener_off_and_retrying(_listener_off_afterwards):
    import asyncio

    from hub import auth_invalidation

    attempts = []

    async def refuse(_dsn):
        attempts.append(1)
        raise OSError("connection refused")

    auth.configure_auth_cache(30, require_listener=True)
    stop = asyncio.Event()
    task = asyncio.create_task(auth_invalidation.listen("postgresql+asyncpg://x/y", stop, connect=refuse))
    await asyncio.sleep(0.8)
    stop.set()
    await task
    assert len(attempts) >= 2 and auth._AUTH_CACHE_LISTENER_LIVE is False


async def test_a_key_is_never_served_past_its_own_expiry(session_factory, config, _listener_off_afterwards):
    from datetime import datetime, timedelta, timezone

    auth.configure_auth_cache(30)
    soon = datetime.now(timezone.utc) + timedelta(seconds=2)
    auth._remember("ct_live_soon", auth.AuthenticatedKey(org_id="o", key_prefix="p", expires_at=soon))
    deadline, _ = auth._AUTH_CACHE[auth._key_hmac("ct_live_soon")]
    assert deadline - auth.time.monotonic() <= 2.0
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    auth._remember("ct_live_gone", auth.AuthenticatedKey(org_id="o", key_prefix="p", expires_at=past))
    assert auth._key_hmac("ct_live_gone") not in auth._AUTH_CACHE


async def test_a_rolled_back_revocation_notifies_no_one(session_factory, config, _listener_off_afterwards):
    """The NOTIFY rides the revoking transaction: no commit, no message."""
    import asyncio

    import asyncpg

    from hub.abuse import _to_asyncpg_dsn
    from hub.tests.conftest import TEST_DATABASE_URL

    received = []
    conn = await asyncpg.connect(_to_asyncpg_dsn(TEST_DATABASE_URL))
    await conn.add_listener(auth.AUTH_INVALIDATION_CHANNEL, lambda *a: received.append(a))
    try:
        _, raw, _ = await _key(session_factory)
        async with session_factory() as session:
            from sqlalchemy import select

            from hub.models import ApiKey
            key_id = (await session.execute(select(ApiKey.id))).scalars().first()
            await auth.revoke_api_key(session, key_id)
            await session.rollback()
        await asyncio.sleep(0.3)
        assert received == []
        async with session_scope(session_factory) as session:
            await auth.revoke_api_key(session, key_id)
        assert await _until(lambda: bool(received))
    finally:
        await conn.close()
