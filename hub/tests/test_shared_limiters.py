from __future__ import annotations

import dataclasses
import pathlib
import re
import uuid

import pytest

import hub.abuse as abuse
from hub.abuse import PostgresRateLimiter, RateLimiter, make_named_limiter
from hub.tests.test_abuse import PG_TEST_DATABASE_URL, _skip_if_no_pg

HUB_DIR = pathlib.Path(__file__).resolve().parents[1]

_ALLOWED = {"abuse.py", "bench_concurrency.py"}
_DIRECT = re.compile(r"(?<![A-Za-z_])RateLimiter\(")


def test_no_hub_module_constructs_a_process_local_limiter_directly():
    offenders = []
    for path in sorted(HUB_DIR.rglob("*.py")):
        if "tests" in path.parts or path.name in _ALLOWED:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _DIRECT.search(line) and "PostgresRateLimiter(" not in line:
                offenders.append(f"{path.relative_to(HUB_DIR)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "construct limiters with hub.abuse.make_named_limiter so they follow "
        "HUB_RATE_LIMIT_BACKEND:\n" + "\n".join(offenders)
    )


def test_without_a_config_the_limiter_is_in_memory():
    assert isinstance(make_named_limiter(None, 10, 5, "x"), RateLimiter)


@pytest.mark.asyncio
async def test_build_app_puts_every_named_limiter_on_the_configured_backend(
    config, session_factory, monkeypatch
):
    from hub import admin, connector_routes, console, otlp, rest, server, signup

    made: dict[str, object] = {}

    def recording(cfg, per_minute, burst, name):
        made[name] = cfg.rate_limit_backend if cfg is not None else None
        return RateLimiter(per_minute=per_minute, burst=burst)

    for module in (abuse, admin, connector_routes, console, otlp, rest, server, signup):
        if hasattr(module, "make_named_limiter"):
            monkeypatch.setattr(module, "make_named_limiter", recording)

    cfg = dataclasses.replace(
        config,
        rate_limit_backend="postgres",
        admin_token="a" * 40,
        console_secret="c" * 40,
        signup_enabled=True,
        rest_api_enabled=True,
        otlp_ingest_enabled=True,
        connectors_enabled=True,
    )
    server.build_app(cfg, session_factory)

    expected = {
        "write", "read", "auth", "scim_auth", "admin_auth", "console_signin",
        "console_share_view", "signup", "rest_auth", "otlp_auth",
        "connector_auth", "readyz",
    }
    assert expected <= set(made), f"missing: {sorted(expected - set(made))}"
    assert {made[name] for name in expected} == {"postgres"}


@pytest.mark.asyncio
async def test_two_replicas_with_one_name_share_one_sign_in_budget():
    _skip_if_no_pg()
    cfg = dataclasses.replace(
        _pg_config(), rate_limit_backend="postgres"
    )
    name = f"console_signin-{uuid.uuid4().hex[:8]}"
    replica_a = make_named_limiter(cfg, 10, 5, name)
    replica_b = make_named_limiter(cfg, 10, 5, name)
    assert isinstance(replica_a, PostgresRateLimiter)

    admitted = 0
    for i in range(10):
        limiter = replica_a if i % 2 == 0 else replica_b
        allowed, _ = await limiter.check("203.0.113.7")
        admitted += allowed
    assert admitted == 5


def _pg_config():
    from hub.config import HubConfig

    return HubConfig(database_url=PG_TEST_DATABASE_URL)


@pytest.mark.asyncio
async def test_concurrent_callers_never_take_more_than_the_bucket_holds():
    import asyncio

    _skip_if_no_pg()
    cfg = dataclasses.replace(_pg_config(), rate_limit_backend="postgres")
    limiter = make_named_limiter(cfg, 1, 7, f"burst-{uuid.uuid4().hex[:8]}")
    results = await asyncio.gather(*[limiter.check("one-key") for _ in range(40)])
    assert sum(allowed for allowed, _ in results) == 7


@pytest.mark.asyncio
async def test_a_refusal_reports_how_long_until_the_next_token():
    _skip_if_no_pg()
    cfg = dataclasses.replace(_pg_config(), rate_limit_backend="postgres")
    limiter = make_named_limiter(cfg, 60, 1, f"retry-{uuid.uuid4().hex[:8]}")
    assert (await limiter.check("k"))[0] is True
    allowed, retry_after = await limiter.check("k")
    assert allowed is False
    assert 0.0 < retry_after <= 1.0


@pytest.mark.asyncio
async def test_refusals_never_mint_tokens():
    _skip_if_no_pg()
    cfg = dataclasses.replace(_pg_config(), rate_limit_backend="postgres")
    limiter = make_named_limiter(cfg, 1, 1, f"mint-{uuid.uuid4().hex[:8]}")
    assert (await limiter.check("k"))[0] is True
    for _ in range(200):
        assert (await limiter.check("k"))[0] is False


@pytest.mark.asyncio
async def test_a_zero_rate_limiter_denies_a_brand_new_key():
    _skip_if_no_pg()
    cfg = dataclasses.replace(_pg_config(), rate_limit_backend="postgres")
    limiter = make_named_limiter(cfg, 0, 5, f"zero-{uuid.uuid4().hex[:8]}")
    allowed, retry_after = await limiter.check("new")
    assert allowed is False and retry_after > 0
