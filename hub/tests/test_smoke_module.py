"""Tests for hub/smoke.py -- the post-deploy verification tool.

A verification tool that cannot itself fail is worse than none: it reports
green against a broken deployment and someone hands out a key. These tests
cover the reporting and diagnosis logic; the tool's end-to-end behaviour
against a live server is exercised by the `compose-stack` CI job.
"""
import argparse
import asyncio

import pytest

from hub import smoke


class TestReporter:
    def test_a_passing_check_records_no_failure(self, capsys):
        report = smoke.Reporter()
        assert report.check("thing works", True) is True
        assert report.failures == []
        assert "[PASS]" in capsys.readouterr().out

    def test_a_failing_check_is_recorded_and_goes_to_stderr(self, capsys):
        report = smoke.Reporter()
        assert report.check("thing works", False, "because reasons") is False
        assert report.failures == ["thing works"]
        captured = capsys.readouterr()
        assert "[FAIL]" in captured.err and "because reasons" in captured.err
        # stdout stays clean so a failure is visible when stdout is piped away
        assert "[FAIL]" not in captured.out


class TestPreflightDiagnosis:
    """The MCP client collapses an HTTP 401 into a generic JSON-RPC internal
    error, so without the raw preflight an operator cannot tell a rejected
    credential from a crashed server. These are the messages they act on."""

    @pytest.fixture
    def fake_post(self, monkeypatch):
        def install(status_code=None, raises=None):
            class _Response:
                def __init__(self, code):
                    self.status_code = code

            def _post(url, **kwargs):
                if raises is not None:
                    raise raises
                return _Response(status_code)

            monkeypatch.setattr(smoke, "_preflight_post", _post, raising=False)
            import sys
            import types
            stub = types.ModuleType("httpx")
            stub.post = _post
            monkeypatch.setitem(sys.modules, "httpx", stub)
        return install

    def test_unreachable_server_names_the_url_and_readyz(self, fake_post):
        fake_post(raises=OSError("connection refused"))
        message = smoke._preflight("http://down.example/mcp", "ct_live_x")
        assert message and "could not reach" in message and "/readyz" in message

    @pytest.mark.parametrize("code", [401, 403])
    def test_rejected_credentials_say_so(self, fake_post, code):
        fake_post(status_code=code)
        message = smoke._preflight("http://up.example/mcp", "ct_live_x")
        assert message and "rejected the API key" in message

    def test_404_points_at_the_endpoint_path(self, fake_post):
        fake_post(status_code=404)
        message = smoke._preflight("http://up.example/wrong", "ct_live_x")
        assert message and "/mcp" in message

    def test_5xx_is_reported_as_reachable_but_failing(self, fake_post):
        fake_post(status_code=503)
        message = smoke._preflight("http://up.example/mcp", "ct_live_x")
        assert message and "503" in message

    def test_a_healthy_endpoint_returns_no_problem(self, fake_post):
        fake_post(status_code=200)
        assert smoke._preflight("http://up.example/mcp", "ct_live_x") is None

    def test_a_429_is_reported_as_rate_limited_not_accepted(self, fake_post):
        """The regression this guards: ApiKeyAuthMiddleware's auth-attempt
        limiter runs BEFORE the key is parsed at all, so a 429 says nothing
        about whether the key was accepted -- it fires identically for a
        real key, a bogus one, or none. Falling through to `None` (this
        function's "the key was accepted" contract) made a rate-limited
        probe with a BOGUS key read as "the server accepted a bogus key",
        a false and alarming security failure. A 429 must be its own
        message, distinct from both None and a rejection."""
        fake_post(status_code=429)
        message = smoke._preflight("http://up.example/mcp", "ct_live_x")
        assert message is not None
        assert "429" in message
        assert "rejected the API key" not in message  # not a rejection ...
        assert "rate" in message.lower()  # ... an inconclusive rate limit


class TestRejectsBadCredentials:
    """`_rejects_bad_credentials` used to open an MCP session with a bogus
    key and treat ANY exception -- a TLS failure, a timeout, a proxy reset,
    or a genuine 401/403 -- as proof the server rejects bad credentials.
    All of those raise identically from inside an MCP session, so a smoke
    run against an unreachable/misconfigured Hub reported [PASS] "an
    invalid API key is refused" without the server having rejected
    anything, or even having been reached at all."""

    pytestmark = pytest.mark.asyncio

    @pytest.fixture
    def fake_post(self, monkeypatch):
        def install(status_code=None, raises=None):
            class _Response:
                def __init__(self, code):
                    self.status_code = code

            def _post(url, **kwargs):
                if raises is not None:
                    raise raises
                return _Response(status_code)

            import sys
            import types
            stub = types.ModuleType("httpx")
            stub.post = _post
            monkeypatch.setitem(sys.modules, "httpx", stub)
        return install

    async def test_a_genuine_401_is_a_pass(self, fake_post):
        fake_post(status_code=401)
        report = smoke.Reporter()
        await smoke._rejects_bad_credentials("http://up.example/mcp", report)
        assert report.failures == []

    async def test_a_genuine_403_is_a_pass(self, fake_post):
        fake_post(status_code=403)
        report = smoke.Reporter()
        await smoke._rejects_bad_credentials("http://up.example/mcp", report)
        assert report.failures == []

    async def test_the_server_accepting_the_bogus_key_is_a_fail(self, fake_post):
        fake_post(status_code=200)
        report = smoke.Reporter()
        await smoke._rejects_bad_credentials("http://up.example/mcp", report)
        assert report.failures != []

    async def test_a_connection_error_is_a_fail_not_a_silent_pass(self, fake_post):
        """The actual regression: previously this exact case (server
        unreachable, key never actually evaluated) reported [PASS]."""
        fake_post(raises=OSError("connection refused"))
        report = smoke.Reporter()
        await smoke._rejects_bad_credentials("http://down.example/mcp", report)
        assert report.failures != []

    async def test_an_unrelated_5xx_is_a_fail_not_a_silent_pass(self, fake_post):
        fake_post(status_code=503)
        report = smoke.Reporter()
        await smoke._rejects_bad_credentials("http://up.example/mcp", report)
        assert report.failures != []

    async def test_a_429_probing_the_bogus_key_is_a_fail_but_not_misreported_as_accepted(
        self, fake_post, capsys
    ):
        """The regression this guards: before _preflight had its own 429
        branch, this exact case printed "the server ACCEPTED a bogus key"
        -- a false, alarming claim about a check that never actually ran,
        caused only by the smoke check's own request volume tripping the
        Hub's auth-attempt limiter. Still correctly a [FAIL] (this check
        could not confirm the property it exists to confirm), but the
        printed detail must say why, accurately."""
        fake_post(status_code=429)
        report = smoke.Reporter()
        await smoke._rejects_bad_credentials("http://up.example/mcp", report)
        assert report.failures != []
        detail = capsys.readouterr().err
        assert "ACCEPTED a bogus key" not in detail
        assert "429" in detail


class TestArgumentHandling:
    def test_url_and_key_are_required(self):
        with pytest.raises(SystemExit):
            smoke.main(["--url", "http://x/mcp"])
        with pytest.raises(SystemExit):
            smoke.main(["--api-key", "ct_live_x"])

    def test_a_missing_mcp_suffix_is_flagged_but_not_fatal(self, capsys, monkeypatch):
        monkeypatch.setattr(smoke, "_preflight", lambda url, key: "stopped here")
        assert smoke.main(["--url", "http://x/", "--api-key", "ct_live_x"]) == 1
        assert "does not end in /mcp" in capsys.readouterr().err


class TestContentDecoding:
    def test_structured_content_is_preferred(self):
        class _Result:
            structured_content = {"id": "abc"}
            content = []
        assert smoke._content(_Result()) == {"id": "abc"}

    def test_a_single_json_text_block_is_parsed(self):
        class _Block:
            text = '{"id": "abc"}'

        class _Result:
            structured_content = None
            content = [_Block()]
        assert smoke._content(_Result()) == {"id": "abc"}

    def test_non_json_text_is_returned_as_is(self):
        class _Block:
            text = "not json"

        class _Result:
            structured_content = None
            content = [_Block()]
        assert smoke._content(_Result()) == "not json"


def test_run_stops_before_writing_anything_when_preflight_fails(monkeypatch, capsys):
    """A broken endpoint must not leave half-written smoke traces behind."""
    monkeypatch.setattr(smoke, "_preflight", lambda url, key: "server is down")
    args = argparse.Namespace(url="http://x/mcp", api_key="k", other_api_key=None)
    import asyncio
    assert asyncio.run(smoke.run(args)) == 1
    assert "server is down" in capsys.readouterr().err


class TestPassingChecksNeverPrintFailureWording:
    """Reporter.check used ONE string for both outcomes, so a detail written
    to explain a failure was printed verbatim next to [PASS]. The worst of
    them was on the tenant-isolation check that matters most, which
    announced that the other org's commons_overlap "returned our trace" on a
    run where nothing leaked -- an operator running this to gain confidence
    in a fresh deployment would reasonably conclude the opposite."""

    def test_a_pass_prints_the_pass_detail(self, capsys):
        report = smoke.Reporter()
        report.check("isolation holds", True,
                     detail="the other org did not receive it",
                     fail_detail="the other org's commons_overlap returned our trace")
        out = capsys.readouterr().out
        assert "[PASS]" in out
        assert "did not receive it" in out
        assert "returned our trace" not in out

    def test_a_fail_prints_the_fail_detail(self, capsys):
        report = smoke.Reporter()
        report.check("isolation holds", False,
                     detail="the other org did not receive it",
                     fail_detail="the other org's commons_overlap returned our trace")
        captured = capsys.readouterr()
        assert "[FAIL]" in captured.err
        assert "returned our trace" in captured.err
        assert report.failures == ["isolation holds"]

    def test_a_single_detail_still_serves_both(self, capsys):
        """Neutral details ("trust=1.0") are shared by design; only the
        failure-worded ones needed splitting."""
        report = smoke.Reporter()
        report.check("vote_trace updates trust", True, "trust=1.0")
        assert "trust=1.0" in capsys.readouterr().out
        report.check("vote_trace updates trust", False, "trust=0.0")
        assert "trust=0.0" in capsys.readouterr().err


class TestSmokeCleansUpAfterItself:
    """Section 12 tells operators this check is safe to run against
    production, which invites wiring it into a deploy gate -- and every run
    used to permanently add two traces (the original plus its amendment) to
    a real customer org. They are not quarantined, so they come back in
    search_traces results for real agent queries and count against the org's
    plan storage. Measured on a deployment smoked a handful of times: 12 of
    12 traces in the org were this check's own residue."""

    def test_cleanup_is_the_default_and_keep_opts_out(self, capsys):
        with pytest.raises(SystemExit):
            smoke.main(["--help"])
        help_text = capsys.readouterr().out
        assert "--keep" in help_text
        assert "deleted when the run finishes" in help_text

    def test_cleanup_failure_is_reported_loudly_not_swallowed(self, capsys):
        """An operator who is not told cleanup failed has no reason to look,
        and the residue accumulates in a customer's corpus."""
        asyncio.run(smoke._cleanup("http://127.0.0.1:1/mcp", "ct_live_x", "trace-123"))
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "purge-trace trace-123" in err

    def test_cleanup_never_raises_so_it_cannot_mask_the_verdict(self):
        """The checks have already run by then; their verdict is what the
        caller came for."""
        asyncio.run(smoke._cleanup("not-even-a-url", "k", "t"))
