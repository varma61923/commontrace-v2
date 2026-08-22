"""Unit tests for commontrace/hub_client.py's pure logic (section parsing,
file writing) that don't need a running Hub. hub/tests/test_tenant_isolation.py
and friends cover the server side against a real Postgres; hub/ itself is
outside the scope of this lightweight, PyYAML-only test suite."""
import os

import pytest

from commontrace import frontmatter, hub_client, paths


def test_lesson_sections_extracts_rule_and_how_to_apply():
    body = (
        "## Rule\nAlways check X before Y.\n\n"
        "## Why\nBecause it broke once.\n\n"
        "## How to apply\nRun the checker script first.\n\n"
        "## Counter-examples\nNever, this always applies.\n"
    )
    sections = hub_client._lesson_sections(body)
    assert sections["rule"] == "Always check X before Y."
    assert sections["how to apply"] == "Run the checker script first."
    assert sections["why"] == "Because it broke once."


def test_lesson_sections_handles_empty_body():
    assert hub_client._lesson_sections("") == {}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_iter_active_lesson_paths_skips_template_and_non_active(store):
    from commontrace.cli import main

    assert main(["init", "--agent-type", "code", "--dest", str(store)]) == 0
    ldir = paths.lessons_dir(str(store))

    fm_active = {
        "name": "lesson_a", "description": "d", "tags": [], "agent_type": "code",
        "domain": "testing", "importance": 3, "importance_rationale": "r",
        "importance_history": [], "applies_when": "when", "do_not_apply_when": "never",
        "uses": 0, "last_hit": "NEVER", "source_traces": [], "source_episodes": [],
        "hub_trace_id": None, "status": "active",
    }
    frontmatter.write(os.path.join(ldir, "lesson_a.md"), fm_active, "## Rule\nx\n")

    fm_archived = dict(fm_active, name="lesson_b", status="archived")
    frontmatter.write(os.path.join(ldir, "lesson_b.md"), fm_archived, "## Rule\nx\n")

    paths_found = list(hub_client._iter_active_lesson_paths(str(store)))
    basenames = {os.path.basename(p) for p in paths_found}
    assert "lesson_a.md" in basenames
    assert "lesson_template.md" not in basenames
    # archived lessons are still yielded by the path iterator (status is
    # filtered by the caller, push_active_lessons) -- assert the iterator
    # doesn't silently drop them, which would hide a status-filter bug.
    assert "lesson_b.md" in basenames


class TestHubUrlSchemeGuard:
    """The commit that added path-traversal sanitization to this module
    also claimed to 'enforce http/https-only URLs', but no such check
    existed in the code -- httpx merely refuses non-http(s) "connections"
    on its own, which is safety incidental to the HTTP client, not a
    guarantee this module made. Left unchecked, a bad scheme also burned
    the full retry budget (3 attempts, exponential backoff) on something
    that can never succeed, surfacing as an opaque "unhandled errors in a
    TaskGroup" instead of a clear message."""

    @pytest.mark.parametrize("bad_url", [
        "file:///etc/passwd",
        "ftp://example.com/mcp",
        "javascript://alert(1)",
        "not-a-url-at-all",
        "",
    ])
    def test_non_http_schemes_are_rejected_immediately(self, bad_url):
        with pytest.raises(hub_client.HubConnectionError, match="scheme must be http or https"):
            hub_client._validate_hub_url(bad_url)

    @pytest.mark.parametrize("good_url", [
        "https://hub.example.com/mcp",
        "http://localhost:8420/mcp",
        "http://127.0.0.1:8420/mcp",
        "http://[::1]:8420/mcp",
    ])
    def test_https_and_loopback_http_pass(self, good_url):
        hub_client._validate_hub_url(good_url)  # must not raise

    def test_plaintext_http_to_a_remote_host_is_rejected(self):
        """SEC-03: the Authorization: Bearer header carrying the org's API
        key goes out on every call, and a Hub URL is normally set once and
        trusted forever with no per-call review. Loopback is exempt because
        traffic to it never leaves the host."""
        with pytest.raises(hub_client.HubConnectionError, match="plaintext http"):
            hub_client._validate_hub_url("http://hub.example.com/mcp")

    def test_plaintext_http_to_an_ip_that_is_not_loopback_is_rejected(self):
        with pytest.raises(hub_client.HubConnectionError, match="plaintext http"):
            hub_client._validate_hub_url("http://10.0.0.5:8420/mcp")

    def test_the_rejection_happens_before_any_retry(self, monkeypatch):
        """Fail fast: a bad scheme can never succeed, so it must not consume
        the retry budget or reach the network at all."""
        import asyncio

        calls = []
        monkeypatch.setattr(asyncio, "sleep", lambda *a, **k: calls.append("slept") or asyncio.sleep(0))

        async def go():
            with pytest.raises(hub_client.HubConnectionError):
                await hub_client._call_tool("file:///etc/passwd", "key", "search_traces", {})

        asyncio.run(go())
        assert calls == [], "a bad scheme must not trigger retry backoff"
