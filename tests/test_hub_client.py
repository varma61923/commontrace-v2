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


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeHTTPStatusError(Exception):
    """Shaped like httpx.HTTPStatusError (an `exc.response.status_code`
    attribute) without requiring httpx to be installed to run this test."""

    def __init__(self, status_code):
        super().__init__(f"HTTP error {status_code}")
        self.response = _FakeResponse(status_code)


class TestIsRetryable:
    """_is_retryable used to classify every exception purely by matching
    substrings in str(exc) -- fragile in both directions: a genuinely
    transient error whose message happens to contain "invalid" or "403"
    (a URL, a nested upstream error, ...) is misclassified as permanent,
    and a permanent error whose message doesn't happen to contain any
    listed marker falls through to the retryable-substring check. A real
    HTTP status code, when the exception carries one, is authoritative and
    checked first."""

    def test_a_structured_401_is_not_retried_even_with_a_confusing_message(self):
        exc = _FakeHTTPStatusError(401)
        exc.args = ("this response is definitely not invalid, all good",)
        assert hub_client._is_retryable(exc) is False

    def test_a_structured_403_is_not_retried(self):
        assert hub_client._is_retryable(_FakeHTTPStatusError(403)) is False

    def test_a_structured_503_is_retried_even_without_a_recognized_word(self):
        exc = _FakeHTTPStatusError(503)
        exc.args = ("the server said no",)
        assert hub_client._is_retryable(exc) is True

    def test_falls_back_to_substring_matching_when_no_status_code_present(self):
        assert hub_client._is_retryable(Exception("connection refused")) is True
        assert hub_client._is_retryable(Exception("401 unauthorized")) is False


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

    @pytest.mark.parametrize("metadata_url", [
        "https://169.254.169.254/latest/meta-data/",
        "https://169.254.170.2/v2/credentials/",  # ECS task metadata, same /16
        "https://[fd00:ec2::254]/latest/meta-data/",  # AWS IPv6 metadata (a ULA, not link-local)
        "https://[fe80::1]/",
    ])
    def test_link_local_and_cloud_metadata_addresses_are_rejected(self, metadata_url):
        """169.254.0.0/16 (IPv4 link-local) is where AWS/GCP/Azure's
        instance-metadata service lives -- it serves credentials over plain
        HTTP with no auth of its own. A Hub URL that got misconfigured or
        tampered with pointing here would leak the org's Bearer API key
        straight into an SSRF against the host's own cloud credentials."""
        with pytest.raises(hub_client.HubConnectionError, match="link-local|cloud-metadata"):
            hub_client._validate_hub_url(metadata_url)

    def test_plaintext_http_to_a_metadata_address_is_also_rejected(self):
        """Caught by the plaintext-http-to-non-loopback check before ever
        reaching the link-local check -- still rejected, just for the
        earlier-triggered reason. Blocked either way."""
        with pytest.raises(hub_client.HubConnectionError, match="plaintext http"):
            hub_client._validate_hub_url("http://169.254.169.254/latest/meta-data/")

    @pytest.mark.parametrize("private_url", [
        "https://10.0.0.5/mcp",
        "https://192.168.1.50/mcp",
        "https://172.16.0.1/mcp",
    ])
    def test_ordinary_private_network_addresses_still_pass(self, private_url):
        """Must not overreach into blocking RFC1918 space generally -- a
        Hub deployed on a private network address is the documented,
        supported case, not an attack."""
        hub_client._validate_hub_url(private_url)  # must not raise

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


def _write_active_lesson(ldir, name, description, applies_when, rule, tags=None, extra=None):
    fm = {
        "name": name, "description": description, "tags": tags or [], "agent_type": "code",
        "domain": "testing", "importance": 3, "importance_rationale": "r",
        "importance_history": [], "applies_when": applies_when, "do_not_apply_when": "never",
        "uses": 0, "last_hit": "NEVER", "source_traces": [], "source_episodes": [],
        "hub_trace_id": None, "status": "active",
    }
    if extra:
        fm.update(extra)
    frontmatter.write(os.path.join(ldir, f"{name}.md"), fm, f"## Rule\n{rule}\n")


class TestPushPropagatesEdits:
    """push_active_lessons used to skip ANY lesson that already had a
    hub_trace_id, forever -- a local edit to an already-pushed lesson's
    rule/description/applies-when/tags never reached the Hub again, so the
    two copies silently diverged the moment anyone edited a promoted
    lesson. It now fingerprints the pushed fields and calls amend_trace
    when they've changed since the last push."""

    def test_first_push_contributes_and_stamps_a_fingerprint(self, store, monkeypatch):
        import asyncio

        ldir = paths.lessons_dir(str(store))
        _write_active_lesson(ldir, "lesson_a", "desc", "when", "do the thing")

        calls = []

        async def fake_call_tool(hub_url, api_key, name, arguments, **kw):
            calls.append((name, arguments))
            assert name == "contribute_trace"
            return {"id": "trace-1", "quarantined": False}

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        results = asyncio.run(hub_client.push_active_lessons("http://localhost:8420/mcp", "key", str(store)))

        assert len(calls) == 1
        assert results[0].hub_trace_id == "trace-1"
        assert results[0].skipped is False
        fm, _ = frontmatter.read(os.path.join(ldir, "lesson_a.md"))
        assert fm["hub_trace_id"] == "trace-1"
        assert fm["hub_pushed_fingerprint"]

    def test_unchanged_lesson_is_skipped_without_a_second_call(self, store, monkeypatch):
        import asyncio

        ldir = paths.lessons_dir(str(store))
        fingerprint = hub_client._push_fingerprint("desc", "when", "do the thing", [])
        _write_active_lesson(
            ldir, "lesson_a", "desc", "when", "do the thing",
            extra={"hub_trace_id": "trace-1", "hub_pushed_fingerprint": fingerprint},
        )

        async def explode(*a, **k):
            raise AssertionError("must not call the Hub for an unchanged, already-pushed lesson")

        monkeypatch.setattr(hub_client, "_call_tool", explode)
        results = asyncio.run(hub_client.push_active_lessons("http://localhost:8420/mcp", "key", str(store)))

        assert results[0].skipped is True
        assert results[0].hub_trace_id == "trace-1"

    def test_edited_lesson_is_propagated_via_amend_trace(self, store, monkeypatch):
        import asyncio

        ldir = paths.lessons_dir(str(store))
        stale_fingerprint = hub_client._push_fingerprint("old desc", "when", "old rule", [])
        _write_active_lesson(
            ldir, "lesson_a", "new desc", "when", "new rule",
            extra={"hub_trace_id": "trace-1", "hub_pushed_fingerprint": stale_fingerprint},
        )

        calls = []

        async def fake_call_tool(hub_url, api_key, name, arguments, **kw):
            calls.append((name, arguments))
            assert name == "amend_trace"
            assert arguments["id"] == "trace-1"
            assert arguments["title"] == "new desc"
            return {"id": "trace-2", "quarantined": False}

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        results = asyncio.run(hub_client.push_active_lessons("http://localhost:8420/mcp", "key", str(store)))

        assert len(calls) == 1
        assert calls[0][0] == "amend_trace"
        assert results[0].hub_trace_id == "trace-2"
        # hub_trace_id must move forward to the amended (superseding) id --
        # amend_trace never mutates the original in place.
        fm, _ = frontmatter.read(os.path.join(ldir, "lesson_a.md"))
        assert fm["hub_trace_id"] == "trace-2"
        assert fm["hub_pushed_fingerprint"] != stale_fingerprint

    def test_never_pushed_lesson_with_no_stored_fingerprint_but_a_hub_id_still_amends(
        self, store, monkeypatch
    ):
        """A lesson pushed before this fix has hub_trace_id set but no
        hub_pushed_fingerprint at all -- must be treated as "possibly
        changed" (propagate) rather than crashing on a missing key or being
        silently skipped forever."""
        import asyncio

        ldir = paths.lessons_dir(str(store))
        _write_active_lesson(ldir, "lesson_a", "desc", "when", "rule", extra={"hub_trace_id": "trace-1"})

        async def fake_call_tool(hub_url, api_key, name, arguments, **kw):
            assert name == "amend_trace"
            return {"id": "trace-2", "quarantined": False}

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        results = asyncio.run(hub_client.push_active_lessons("http://localhost:8420/mcp", "key", str(store)))
        assert results[0].hub_trace_id == "trace-2"


class TestPullPaginatesAllResults:
    """pull_search_results used to call search_traces exactly once --
    search_traces caps a single response at 50 results, so a Hub with more
    than one page of matches silently returned only the first page with no
    indication anything was left out."""

    def test_pages_until_has_more_is_false(self, store, monkeypatch):
        import asyncio

        pages = [
            {"traces": [{"id": f"t{i}", "title": f"trace {i}"} for i in range(50)],
             "limit": 50, "offset": 0, "has_more": True},
            {"traces": [{"id": f"t{i}", "title": f"trace {i}"} for i in range(50, 75)],
             "limit": 50, "offset": 50, "has_more": False},
        ]
        calls = []

        async def fake_call_tool(hub_url, api_key, name, arguments, **kw):
            calls.append(arguments["offset"])
            return pages[len(calls) - 1]

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        result = asyncio.run(
            hub_client.pull_search_results("http://localhost:8420/mcp", "key", str(store))
        )

        assert calls == [0, 50]
        assert result.n_found == 75
        assert len(result.written_paths) == 75

    def test_stops_at_max_results_even_if_more_pages_remain(self, store, monkeypatch):
        import asyncio

        async def fake_call_tool(hub_url, api_key, name, arguments, **kw):
            offset = arguments["offset"]
            return {
                "traces": [{"id": f"t{offset + i}", "title": f"trace {offset + i}"} for i in range(50)],
                "limit": 50, "offset": offset, "has_more": True,
            }

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        result = asyncio.run(
            hub_client.pull_search_results(
                "http://localhost:8420/mcp", "key", str(store), max_results=120
            )
        )
        assert result.n_found == 120


class TestPullSurfacesTermsTheHubDidNotSearchOn:
    """`terms_ignored` is why an empty pull is readable.

    The Hub drops query terms that appear in too much of the org's corpus
    to distinguish one trace from another (hub/search.py:choose_terms). A
    client that discards that field turns two different situations -- "your
    corpus has no answer" and "the words you used are in nearly every trace
    you have" -- into the same silent empty result, and only the second one
    is fixed by rephrasing.
    """

    def test_ignored_terms_are_carried_up(self, store, monkeypatch):
        import asyncio

        async def fake_call_tool(hub_url, api_key, name, arguments, **kw):
            return {"traces": [], "limit": 50, "offset": 0, "has_more": False,
                    "terms": ["retri", "timeout"], "terms_ignored": ["retri", "timeout"]}

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        result = asyncio.run(
            hub_client.pull_search_results("http://localhost:8420/mcp", "key", str(store), query="retry timeout")
        )
        assert result.n_found == 0
        assert result.ignored_terms == ["retri", "timeout"]

    def test_an_older_hub_without_the_field_is_not_an_error(self, store, monkeypatch):
        """The client is versioned separately from the Hub it talks to, so a
        missing key must read as 'nothing was ignored', never as a crash."""
        import asyncio

        async def fake_call_tool(hub_url, api_key, name, arguments, **kw):
            return {"traces": [{"id": "t1", "title": "trace one"}],
                    "limit": 50, "offset": 0, "has_more": False}

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        result = asyncio.run(
            hub_client.pull_search_results("http://localhost:8420/mcp", "key", str(store))
        )
        assert result.ignored_terms == []
        assert result.n_found == 1
