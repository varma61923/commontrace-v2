from __future__ import annotations

import pytest

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
