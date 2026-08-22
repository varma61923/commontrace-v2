from __future__ import annotations

import pytest

from hub import abuse
from hub.abuse import RateLimiter, TraceRejected, suspicion_reason, validate_size
from hub.config import HubConfig


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
    assert limiter.allow("idle-org") is False
