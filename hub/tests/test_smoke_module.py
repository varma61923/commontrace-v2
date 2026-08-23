"""Tests for hub/smoke.py -- the post-deploy verification tool.

A verification tool that cannot itself fail is worse than none: it reports
green against a broken deployment and someone hands out a key. These tests
cover the reporting and diagnosis logic; the tool's end-to-end behaviour
against a live server is exercised by the `compose-stack` CI job.
"""
import argparse

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
