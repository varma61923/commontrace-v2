"""Tests for commontrace/hub_client.py's retry/backoff policy.

These test the decision logic (`_is_retryable`) directly rather than
standing up a failing Hub: the point being verified is *which* failures are
retried, and that is a pure predicate.
"""
import pytest

from commontrace import hub_client


class TestRetryPolicy:
    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError("read timeout"),
            ConnectionRefusedError("connection refused"),
            ConnectionResetError("connection reset by peer"),
            RuntimeError("server returned 503 Service Unavailable"),
            RuntimeError("upstream returned 502"),
        ],
    )
    def test_transport_failures_are_retried(self, exc):
        assert hub_client._is_retryable(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("401 Unauthorized"),
            RuntimeError("invalid, revoked, or expired API key"),
            RuntimeError("403 Forbidden"),
        ],
    )
    def test_auth_failures_are_not_retried(self, exc):
        """Retrying a rejected credential can never succeed, and hammering a
        server that may be rate-limiting auth failures makes it worse."""
        assert hub_client._is_retryable(exc) is False

    def test_unknown_failures_are_not_retried(self):
        # Default to not retrying: a bounded, fast, honest failure beats
        # silently multiplying the delay on an error we don't understand.
        assert hub_client._is_retryable(ValueError("something we don't recognize")) is False


class TestDefaults:
    def test_a_finite_timeout_is_configured(self):
        """The bug this guards: with no timeout, a Hub that accepts the
        connection then stalls hangs `commontrace sync` forever."""
        assert hub_client.DEFAULT_TIMEOUT_SECONDS > 0
        assert hub_client.DEFAULT_TIMEOUT_SECONDS < 300

    def test_retries_are_bounded(self):
        assert 1 <= hub_client.DEFAULT_MAX_ATTEMPTS <= 5

    def test_backoff_is_positive(self):
        assert hub_client.RETRY_BASE_DELAY_SECONDS > 0
