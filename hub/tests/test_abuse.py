from __future__ import annotations

from dataclasses import dataclass

import pytest

from hub import abuse
from hub.abuse import RateLimiter, TraceRejected, resolve_client_key, suspicion_reason, validate_size
from hub.config import HubConfig


@dataclass
class _FakeAddr:
    host: str


class _FakeRequest:
    """The two attributes resolve_client_key() actually reads off a
    Starlette Request -- .client.host and .headers.get(...) -- without
    pulling in a real ASGI request for what is otherwise a pure function."""

    def __init__(self, client_host: str | None, headers: dict[str, str] | None = None):
        self.client = _FakeAddr(client_host) if client_host is not None else None
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}

    @property
    def headers(self):
        return self

    def get(self, key, default=""):
        return self._headers.get(key.lower(), default)


class TestResolveClientKey:
    """[SEC-HUB-01]: every client-address-keyed rate limiter (auth attempts,
    read requests, /readyz) used to key purely off request.client.host --
    the proxy's own address for every request behind ANY reverse proxy,
    including the Hub's own documented loopback+sidecar deployment shape,
    collapsing every real client into one shared bucket. trusted_proxy_hops
    opts a deployment into trusting X-Forwarded-For instead, but only the
    hop(s) a trusted proxy itself appended -- not whatever a client sent."""

    def test_default_zero_hops_never_looks_at_the_header(self):
        """The default (and every existing deployment/test, which never
        passes this parameter) must be byte-for-byte the old behavior:
        request.client.host, full stop -- even if a client sends a
        X-Forwarded-For header of its own."""
        req = _FakeRequest("203.0.113.9", headers={"X-Forwarded-For": "9.9.9.9"})
        assert resolve_client_key(req, 0) == "203.0.113.9"

    def test_no_client_and_zero_hops_is_unknown(self):
        req = _FakeRequest(None)
        assert resolve_client_key(req, 0) == "unknown"

    def test_one_trusted_hop_reads_the_right_most_entry(self):
        """A single trusted reverse proxy appends its own view of the
        connecting peer as the LAST entry in the chain -- everything to its
        left was written by whatever the proxy received, which for the
        first hop is entirely client-controlled."""
        req = _FakeRequest(
            "10.0.0.1",  # the proxy's own address, as seen by this process
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
        assert resolve_client_key(req, 1) == "203.0.113.9"

    def test_a_client_supplied_forged_prefix_does_not_override_the_trusted_hop(self):
        """The actual attack SEC-HUB-01's own naively-proposed fix
        (`.split(',')[0]`) would have reintroduced: a client sends its own
        fake entry ahead of what the proxy appends, hoping the leftmost
        value gets trusted. It must not -- only the right-most
        trusted_proxy_hops entries are proxy-authored."""
        req = _FakeRequest(
            "10.0.0.1",
            headers={"X-Forwarded-For": "9.9.9.9-forged, 203.0.113.9"},
        )
        assert resolve_client_key(req, 1) == "203.0.113.9"
        assert resolve_client_key(req, 1) != "9.9.9.9-forged"

    def test_two_trusted_hops_reads_the_second_from_the_right(self):
        req = _FakeRequest(
            "10.0.0.2",
            headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"},
        )
        assert resolve_client_key(req, 2) == "203.0.113.9"

    def test_fewer_entries_than_trusted_hops_falls_back_to_client_host(self):
        """A proxy that was supposed to append a hop didn't -- a
        misconfigured topology, or a request that reached this process
        without passing through the expected proxy chain. Must not trust
        whatever is there; fall back to the raw peer address instead."""
        req = _FakeRequest("10.0.0.1", headers={"X-Forwarded-For": "203.0.113.9"})
        assert resolve_client_key(req, 2) == "10.0.0.1"

    def test_missing_header_with_hops_configured_falls_back_to_client_host(self):
        req = _FakeRequest("10.0.0.1", headers={})
        assert resolve_client_key(req, 1) == "10.0.0.1"

    def test_no_client_and_missing_header_with_hops_configured_is_unknown(self):
        req = _FakeRequest(None, headers={})
        assert resolve_client_key(req, 1) == "unknown"


@pytest.fixture
def small_config() -> HubConfig:
    return HubConfig(
        database_url="postgresql+asyncpg://unused/unused",
        max_title_chars=20,
        max_text_chars=50,
        max_tags=3,
        max_tag_chars=10,
        max_trace_bytes=10_000,
        suspect_url_threshold=2,
    )


def test_oversized_title_rejected(small_config):
    with pytest.raises(TraceRejected):
        validate_size({"title": "x" * 21, "context_text": "c", "solution_text": "s", "tags": []}, small_config)


def test_oversized_text_rejected(small_config):
    with pytest.raises(TraceRejected):
        validate_size({"title": "t", "context_text": "c" * 51, "solution_text": "s", "tags": []}, small_config)


def test_too_many_tags_rejected(small_config):
    with pytest.raises(TraceRejected):
        validate_size(
            {"title": "t", "context_text": "c", "solution_text": "s", "tags": ["a", "b", "c", "d"]}, small_config
        )


def test_oversized_tag_rejected(small_config):
    with pytest.raises(TraceRejected):
        validate_size(
            {"title": "t", "context_text": "c", "solution_text": "s", "tags": ["way-too-long-a-tag"]}, small_config
        )


def test_within_limits_accepted(small_config):
    validate_size({"title": "t", "context_text": "c", "solution_text": "s", "tags": ["ok"]}, small_config)


def test_suspicion_flags_excessive_urls(small_config):
    text = " ".join(f"http://spam{i}.example.com" for i in range(5))
    reason = suspicion_reason(
        {"title": "t", "context_text": text, "solution_text": "s"}, small_config
    )
    assert reason is not None
    assert "URL" in reason


def test_suspicion_none_for_normal_content(small_config):
    reason = suspicion_reason(
        {
            "title": "React 19 hydration mismatch",
            "context_text": "Server and client render different markup for a Date field.",
            "solution_text": "Format the date deterministically on both sides, e.g. ISO-8601 with a fixed timezone.",
        },
        small_config,
    )
    assert reason is None


def test_rate_limiter_allows_up_to_burst_then_blocks():
    limiter = RateLimiter(per_minute=60, burst=3)
    assert limiter.allow("org-x") is True
    assert limiter.allow("org-x") is True
    assert limiter.allow("org-x") is True
    assert limiter.allow("org-x") is False


def test_rate_limiter_is_per_key():
    limiter = RateLimiter(per_minute=60, burst=1)
    assert limiter.allow("org-a") is True
    assert limiter.allow("org-b") is True  # independent bucket, not shared with org-a
    assert limiter.allow("org-a") is False


def test_rate_limiter_evicts_idle_buckets(monkeypatch):
    """Regression test for a real bug: `_buckets` never evicted an entry,
    so a long-running Hub accumulated one bucket per org that EVER called
    contribute_trace, without bound -- an org that called once and never
    came back kept its bucket forever. A bucket idle past the TTL should be
    swept, and a fresh call for that key afterward should behave exactly
    as if the org were new (full burst available), not as if some state
    survived."""
    clock = [1000.0]
    monkeypatch.setattr(abuse.time, "monotonic", lambda: clock[0])

    limiter = RateLimiter(per_minute=60, burst=2)
    limiter._IDLE_TTL_SECONDS = 100.0
    limiter._SWEEP_INTERVAL_SECONDS = 10.0

    assert limiter.allow("idle-org") is True
    assert "idle-org" in limiter._buckets
    assert limiter.allow("busy-org") is True  # keeps this bucket touched throughout

    # Advance past the idle TTL for idle-org, but keep busy-org fresh so the
    # sweep has something to distinguish "idle" from "just created".
    for _ in range(12):
        clock[0] += 10.0
        limiter.allow("busy-org")

    assert "idle-org" not in limiter._buckets, "idle bucket was never swept"
    assert "busy-org" in limiter._buckets, "an actively-used bucket must not be evicted"

    # A fresh call for the swept key behaves like a brand-new key -- full
    # burst capacity, not a resumed or half-empty bucket.
    assert limiter.allow("idle-org") is True
    assert limiter.allow("idle-org") is True


def test_zero_per_minute_denies_every_key_from_the_first_call():
    """A bucket for any never-seen key started pre-filled to `burst`
    tokens regardless of the configured rate, so per_minute=0 (an operator
    explicitly asking for zero throughput) previously still let each new
    key through its first `burst` calls before the "never refills" part
    kicked in. per_minute<=0 must deny unconditionally, independent of
    whatever burst is also configured, and for every key -- not just
    whichever key happened to exhaust its initial allowance first."""
    limiter = RateLimiter(per_minute=0, burst=5)
    for _ in range(5):
        assert limiter.allow("org-x") is False
    assert limiter.allow("org-brand-new") is False


def test_negative_per_minute_also_denies_every_key():
    limiter = RateLimiter(per_minute=-1, burst=5)
    assert limiter.allow("org-x") is False
    assert limiter.allow("idle-org") is False
