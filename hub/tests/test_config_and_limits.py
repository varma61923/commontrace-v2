"""Regression tests for the deployment-readiness pass, Hub side.

Two groups, both reproduced before being fixed:

  1. Config that accepted nonsense and failed later, somewhere else, in a way
     that named neither the variable nor the reason.
  2. The rate limiter: unbounded key growth reachable by an unauthenticated
     request, and refusals that never said when to come back.

Deliberately needs no database, so it runs in the fast CI job alongside
test_image_contents.py rather than waiting on the Postgres-backed one.
"""
from __future__ import annotations

import math

import pytest

from hub.abuse import RateLimiter
from hub.config import HubConfig, _env_int_in_range


class TestConfigRefusesNonsenseAtStartup:
    """Each of these previously started a process that then misbehaved:
    HUB_PORT=99999 died in uvicorn's bind, HUB_DB_POOL_SIZE=-1 in SQLAlchemy
    on first query, HUB_MAX_TITLE_CHARS=-5 rejected every contribute_trace
    with nothing anywhere saying why."""

    @pytest.mark.parametrize("name,value", [
        ("HUB_PORT", "99999"),
        ("HUB_PORT", "0"),
        ("HUB_DB_POOL_SIZE", "-1"),
        ("HUB_MAX_TITLE_CHARS", "-5"),
        ("HUB_MAX_REQUEST_BODY_BYTES", "0"),
        ("HUB_TRUSTED_PROXY_HOPS", "-2"),
        ("HUB_MAX_TAGS", "0"),
        ("HUB_RATE_LIMIT_BURST", "-1"),
    ])
    def test_an_out_of_range_value_is_refused(self, monkeypatch, name, value):
        monkeypatch.setenv("HUB_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        monkeypatch.setenv(name, value)
        with pytest.raises(ValueError, match=name):
            HubConfig.from_env()

    def test_the_message_names_the_variable_and_the_bound(self, monkeypatch):
        monkeypatch.setenv("HUB_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        monkeypatch.setenv("HUB_PORT", "99999")
        with pytest.raises(ValueError) as exc:
            HubConfig.from_env()
        assert "HUB_PORT must be between 1 and 65535, got 99999" in str(exc.value)

    def test_defaults_still_build(self, monkeypatch):
        for name in list(HubConfig.__dataclass_fields__):
            monkeypatch.delenv(f"HUB_{name.upper()}", raising=False)
        monkeypatch.setenv("HUB_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        assert HubConfig.from_env().port == 8420

    def test_a_rate_limit_of_zero_is_still_allowed(self, monkeypatch):
        """0 means "deny everything", which is a documented setting -- the
        range check must not have collaterally banned it."""
        monkeypatch.setenv("HUB_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        monkeypatch.setenv("HUB_RATE_LIMIT_PER_MINUTE", "0")
        assert HubConfig.from_env().rate_limit_per_minute == 0

    def test_a_non_integer_is_still_refused_by_name(self, monkeypatch):
        monkeypatch.setenv("HUB_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        monkeypatch.setenv("HUB_PORT", "eight-thousand")
        with pytest.raises(ValueError, match="HUB_PORT"):
            HubConfig.from_env()

    def test_the_helper_accepts_its_own_bounds(self):
        assert _env_int_in_range("HUB_NOT_SET_ANYWHERE", 5, 1, 10) == 5


class TestSignupAndBillingDefaultOff:
    """Same posture as HUB_ADMIN_TOKEN/HUB_CONSOLE_SECRET: unset means the
    corresponding routes are never registered (hub/server.py), so these
    just pin that the config layer itself defaults to the off/empty state
    rather than silently opting a fresh deployment in."""

    def test_signup_defaults_disabled(self, monkeypatch):
        monkeypatch.setenv("HUB_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        assert HubConfig.from_env().signup_enabled is False

    def test_signup_can_be_enabled(self, monkeypatch):
        monkeypatch.setenv("HUB_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        monkeypatch.setenv("HUB_SIGNUP_ENABLED", "true")
        assert HubConfig.from_env().signup_enabled is True

    def test_stripe_settings_default_empty(self, monkeypatch):
        monkeypatch.setenv("HUB_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        config = HubConfig.from_env()
        assert config.stripe_secret_key == ""
        assert config.stripe_webhook_secret == ""
        assert config.stripe_price_team == ""
        assert config.stripe_price_scale == ""

    def test_stripe_settings_read_from_env(self, monkeypatch):
        monkeypatch.setenv("HUB_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        monkeypatch.setenv("HUB_STRIPE_SECRET_KEY", "sk_test_123")
        monkeypatch.setenv("HUB_STRIPE_WEBHOOK_SECRET", "whsec_123")
        monkeypatch.setenv("HUB_STRIPE_PRICE_TEAM", "price_team_123")
        monkeypatch.setenv("HUB_STRIPE_PRICE_SCALE", "price_scale_123")
        config = HubConfig.from_env()
        assert config.stripe_secret_key == "sk_test_123"
        assert config.stripe_webhook_secret == "whsec_123"
        assert config.stripe_price_team == "price_team_123"
        assert config.stripe_price_scale == "price_scale_123"


class TestRateLimiterReportsWhenToComeBack:
    @pytest.mark.asyncio
    async def test_an_allowed_call_asks_for_no_wait(self):
        allowed, retry_after = await RateLimiter(per_minute=60, burst=5).check("k")
        assert allowed is True and retry_after == 0.0

    @pytest.mark.asyncio
    async def test_a_refused_call_says_how_long_until_a_token_exists(self):
        """Without this a refused client can only guess -- and every client
        guessing short against a limiter already saying no is what turns one
        burst into a sustained stampede."""
        limiter = RateLimiter(per_minute=60, burst=2)  # 1 token/sec
        for _ in range(2):
            assert (await limiter.check("k"))[0] is True
        allowed, retry_after = await limiter.check("k")
        assert allowed is False
        assert 0 < retry_after <= 1.01
        assert max(1, math.ceil(retry_after)) == 1

    @pytest.mark.asyncio
    async def test_a_deny_everything_limiter_does_not_promise_a_finite_wait(self):
        """per_minute=0 never refills; advertising a short wait would invite
        an endless retry loop."""
        allowed, retry_after = await RateLimiter(per_minute=0, burst=10).check("k")
        assert allowed is False
        assert retry_after >= 60

    @pytest.mark.asyncio
    async def test_allow_still_works_for_every_existing_caller(self):
        limiter = RateLimiter(per_minute=60, burst=1)
        assert await limiter.allow("k") is True
        assert await limiter.allow("k") is False


class TestRateLimiterMemoryIsBounded:
    @pytest.mark.asyncio
    async def test_tracked_keys_are_capped(self, monkeypatch):
        """The idle sweep alone evicts nothing until a bucket has been
        untouched for an hour. A client-address-keyed limiter is keyed on
        something the peer chooses (any address out of an IPv6 /64), so an
        unauthenticated flood could hold unbounded distinct keys live inside
        that window -- process memory growth caused by the very limiter
        meant to prevent it."""
        monkeypatch.setattr(RateLimiter, "_MAX_TRACKED_KEYS", 50)
        limiter = RateLimiter(per_minute=600, burst=10)
        for i in range(500):
            await limiter.allow(f"2001:db8::{i:x}")
        assert len(limiter._buckets) <= 50

    @pytest.mark.asyncio
    async def test_eviction_never_lowers_another_clients_limit(self):
        """A re-created bucket starts full, so the worst case is that a
        flooding client resets its OWN limit."""
        limiter = RateLimiter(per_minute=60, burst=1)
        assert await limiter.allow("victim") is True
        assert await limiter.allow("victim") is False   # victim is out of tokens
        limiter._evict_if_over_capacity(0.0)      # a no-op below capacity
        assert await limiter.allow("victim") is False   # still limited, not reset by others


class TestMetricsEndpoint:
    """Nothing in this Hub could answer "how many requests are we refusing,
    and why" without grepping JSON logs after the fact. Rate limiting in
    particular was invisible until a customer complained."""

    def _fresh(self):
        from hub.observability import Metrics
        return Metrics()

    def test_requests_are_counted_by_method_route_and_status(self):
        m = self._fresh()
        m.observe_request("POST", "/mcp", 200, 12.5)
        m.observe_request("POST", "/mcp", 200, 7.5)
        m.observe_request("POST", "/mcp", 429, 1.0)
        out = m.render()
        assert 'commontrace_hub_requests_total{method="POST",path="/mcp",status="200"} 2' in out
        assert 'commontrace_hub_requests_total{method="POST",path="/mcp",status="429"} 1' in out
        # A histogram now, not a plain summed counter -- see
        # hub/tests/test_observability.py::TestDurationHistogram for the
        # bucket/percentile behavior this replaced the old metric to get.
        assert 'commontrace_hub_request_duration_ms_sum{path="/mcp"} 21.00' in out
        assert 'commontrace_hub_request_duration_ms_count{path="/mcp"} 3' in out

    def test_an_arbitrary_path_cannot_inflate_label_cardinality(self):
        """An unbounded label set is the classic way a metrics endpoint
        becomes the outage it was installed to prevent."""
        m = self._fresh()
        for i in range(500):
            m.observe_request("GET", f"/does-not-exist-{i}", 404, 0.1)
        out = m.render()
        assert 'path="other"' in out
        assert "does-not-exist-1" not in out
        assert out.count("commontrace_hub_requests_total{") == 1

    def test_rate_limit_refusals_are_counted_per_limiter(self):
        """The HTTP limiter and the per-org WRITE limiter refuse at different
        layers -- a write refusal is returned inside a 200 MCP response, so a
        status-code counter alone never sees it."""
        m = self._fresh()
        m.observe_rate_limited("http")
        m.observe_rate_limited("write")
        m.observe_rate_limited("write")
        out = m.render()
        assert 'commontrace_hub_rate_limited_total{limiter="http"} 1' in out
        assert 'commontrace_hub_rate_limited_total{limiter="write"} 2' in out

    def test_no_tenant_identifier_ever_appears(self):
        """A scrape endpoint is a different trust boundary from an
        authenticated tool call: per-org labels would be both a cardinality
        problem and a privacy one."""
        m = self._fresh()
        m.observe_request("POST", "/mcp", 200, 1.0)
        m.observe_rate_limited("write")
        out = m.render()
        for forbidden in ("org", "ct_live_", "api_key", "query"):
            assert forbidden not in out, f"{forbidden!r} must never be exposed on /metrics"

    def test_output_is_valid_prometheus_text_format(self):
        m = self._fresh()
        m.observe_request("GET", "/healthz", 200, 0.5)
        lines = [ln for ln in m.render().splitlines() if ln]
        assert all(ln.startswith("#") or " " in ln for ln in lines)
        for ln in lines:
            if not ln.startswith("#"):
                assert float(ln.rsplit(" ", 1)[1]) >= 0


class TestAuthLimiterChargesOnlyFailedCredentials:
    """The auth-attempt limiter exists to bound the Argon2 CPU an
    unauthenticated source can force. It was charging every request,
    successful ones included, so it throttled the legitimate heavy client
    hardest -- a bulk `sync --push-traces` is hundreds of SUCCESSFUL
    authentications from one address against a 60/min budget, which made the
    anti-brute-force limiter, not the per-org fair-use one, the binding
    constraint on this product's own documented onboarding."""

    @pytest.mark.asyncio
    async def test_a_refund_returns_a_token(self):
        limiter = RateLimiter(per_minute=60, burst=2)
        assert await limiter.allow("1.2.3.4") is True
        assert await limiter.allow("1.2.3.4") is True
        assert await limiter.allow("1.2.3.4") is False
        limiter.refund("1.2.3.4")
        assert await limiter.allow("1.2.3.4") is True

    @pytest.mark.asyncio
    async def test_a_refund_never_exceeds_capacity(self):
        """Otherwise a long-lived valid client would accumulate an unbounded
        credit and the limiter would stop meaning anything for that key."""
        limiter = RateLimiter(per_minute=60, burst=2)
        for _ in range(50):
            limiter.refund("1.2.3.4")
        assert await limiter.allow("1.2.3.4") is True
        assert await limiter.allow("1.2.3.4") is True
        assert await limiter.allow("1.2.3.4") is False

    def test_refunding_an_unseen_key_is_a_no_op(self):
        limiter = RateLimiter(per_minute=60, burst=1)
        limiter.refund("never-seen")
        assert limiter._buckets == {}

    @pytest.mark.asyncio
    async def test_a_source_that_always_succeeds_is_never_throttled(self):
        """The valid-client path: spend then refund, indefinitely."""
        limiter = RateLimiter(per_minute=1, burst=1)
        for _ in range(200):
            assert await limiter.allow("1.2.3.4") is True
            limiter.refund("1.2.3.4")

    @pytest.mark.asyncio
    async def test_a_source_that_always_fails_is_still_throttled(self):
        """The brute-force path is unchanged: no refund, so the budget is
        spent exactly as before."""
        limiter = RateLimiter(per_minute=60, burst=5)
        allowed = [await limiter.allow("9.9.9.9") for _ in range(20)]
        assert allowed[:5] == [True] * 5
        assert False in allowed[5:]
