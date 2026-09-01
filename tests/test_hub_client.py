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

    def test_amend_carries_an_idempotency_key_so_a_retry_cannot_fork_the_chain(self, store, monkeypatch):
        """_call_tool retries transport-level failures (timeout, 5xx,
        connection reset) up to DEFAULT_MAX_ATTEMPTS times, and this client
        cannot tell "never arrived" from "arrived, reply lost" -- exactly
        the scenario contribute_trace's idempotency_key already exists to
        make safe. amend_trace needed the same protection (found by
        auditing this call site after adding idempotency_key support to
        hub/crud.py:amend_trace itself) or the client's own retry loop
        could still fork the supersession chain despite the server-side
        fix, simply by never asking for it."""
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
            return {"id": "trace-2", "quarantined": False}

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        asyncio.run(hub_client.push_active_lessons("http://localhost:8420/mcp", "key", str(store)))

        key = calls[0][1].get("idempotency_key")
        assert key, "amend_trace call carried no idempotency_key at all"
        assert len(key) <= 128, "must fit Trace.idempotency_key's String(128) column"

        new_fingerprint = hub_client._push_fingerprint("new desc", "when", "new rule", [])
        assert key == hub_client._amend_idempotency_key("lesson_a", new_fingerprint)

    def test_two_genuinely_different_edits_get_different_idempotency_keys(self):
        """The reason this can't reuse contribute_trace's f"lesson:{slug}"
        pattern unmodified: a lesson can be legitimately amended many times
        as its content actually changes, and each edit is a different
        logical write that must NOT collide -- a fixed per-lesson key would
        make every edit after the first raise IdempotencyKeyConflict
        against the previous one's stored request_hash."""
        fp1 = hub_client._push_fingerprint("desc v1", "when", "rule v1", [])
        fp2 = hub_client._push_fingerprint("desc v2", "when", "rule v2", [])
        assert hub_client._amend_idempotency_key("lesson_a", fp1) != hub_client._amend_idempotency_key(
            "lesson_a", fp2
        )

    def test_the_same_edit_retried_gets_the_same_idempotency_key(self):
        """The property that actually matters: _call_tool retrying the
        SAME push attempt (same content, same fingerprint) must produce the
        identical key both times, or the retry protection does nothing."""
        fp = hub_client._push_fingerprint("desc", "when", "rule", ["a", "b"])
        assert hub_client._amend_idempotency_key("lesson_a", fp) == hub_client._amend_idempotency_key(
            "lesson_a", fp
        )

    def test_a_malformed_lesson_file_does_not_abort_the_others(self, store, monkeypatch):
        """Reproduced before this fix: frontmatter.read(path) raising on one
        corrupted lesson file (broken YAML from a hand-edit, a partial
        write) propagated straight out of push_active_lessons, aborting
        the WHOLE run -- every other lesson in the same directory, valid
        and ready to push, never got pushed either. One bad file silently
        blocked an entire fleet's lessons."""
        import asyncio

        ldir = paths.lessons_dir(str(store))
        _write_active_lesson(ldir, "lesson_good", "desc", "when", "do the thing")
        with open(os.path.join(ldir, "lesson_bad.md"), "w") as fh:
            fh.write("---\nname: [unclosed list\n---\nbroken\n")

        async def fake_call_tool(hub_url, api_key, name, arguments, **kw):
            return {"id": "trace-1", "quarantined": False}

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        results = asyncio.run(hub_client.push_active_lessons("http://localhost:8420/mcp", "key", str(store)))

        by_slug = {r.slug: r for r in results}
        assert by_slug["lesson_good"].hub_trace_id == "trace-1"
        assert by_slug["lesson_good"].error is None
        assert by_slug["lesson_bad"].hub_trace_id is None
        assert by_slug["lesson_bad"].error is not None

    def test_pushes_run_concurrently_but_bounded(self, store, monkeypatch):
        """Same fix, same reasoning as push_captured_traces's identical
        test: N independent lessons used to mean N sequential round trips.
        Tracking actual concurrent in-flight calls (not wall-clock time,
        which is flaky under CI load) proves both that calls now overlap
        and that the overlap stays bounded rather than firing every call
        at once and risking the Hub's write rate limiter."""
        import asyncio

        ldir = paths.lessons_dir(str(store))
        n = 20
        for i in range(n):
            _write_active_lesson(ldir, f"lesson_{i}", f"desc {i}", "when", "do the thing")

        in_flight = 0
        max_in_flight = 0

        async def fake_call_tool(hub_url, api_key, name, arguments, **kw):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return {"id": f"hub-{arguments['title']}", "quarantined": False}

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        results = asyncio.run(hub_client.push_active_lessons("http://localhost:8420/mcp", "key", str(store)))

        assert len(results) == n
        assert max_in_flight > 1, "pushes ran strictly sequentially -- the concurrency fix regressed"
        assert max_in_flight <= hub_client._PUSH_CONCURRENCY, (
            f"unbounded concurrency: {max_in_flight} calls in flight at once, "
            f"expected at most {hub_client._PUSH_CONCURRENCY}"
        )


def _write_captured_trace(
    tdir, filename, trace_id, title, context, solution,
    agent_type="code", agent_id="", tags=None, outcome=None, extra=None,
):
    from commontrace import templates

    os.makedirs(tdir, exist_ok=True)
    fm = templates.trace_frontmatter(trace_id, title, agent_type, tags or [], outcome=outcome, agent_id=agent_id)
    if extra:
        fm.update(extra)
    body = templates.trace_body(context, solution)
    frontmatter.write(os.path.join(tdir, filename), fm, body)


class TestPushCapturedTraces:
    """push_captured_traces is the bridge push_active_lessons does not
    provide: lessons and traces are different local stores, and outcome
    data (--resolved/--tokens-used/..., hub/outcomes.py) only ever lives on
    a trace. Without this, the documented `commontrace capture` + `sync`
    workflow had no way to ever get outcome data to the Hub, even after
    hub/crud.py:contribute_trace grew an `outcome` parameter to accept it."""

    def test_first_push_contributes_with_outcome_and_stamps_a_fingerprint(self, store, monkeypatch):
        import asyncio

        tdir = paths.traces_dir(str(store))
        _write_captured_trace(
            tdir, "t1.md", "occasion-1", "title", "ctx", "sol",
            outcome={"resolved": True, "tokens_used": 500},
        )

        calls = []

        async def fake_call_tool(hub_url, api_key, name, arguments, **kw):
            calls.append((name, arguments))
            assert name == "contribute_trace"
            assert arguments["outcome"] == {"resolved": True, "tokens_used": 500}
            return {"id": "hub-trace-1", "quarantined": False}

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        results = asyncio.run(hub_client.push_captured_traces("http://localhost:8420/mcp", "key", str(store)))

        assert len(calls) == 1
        assert results[0].hub_trace_id == "hub-trace-1"
        assert results[0].skipped is False
        fm, _ = frontmatter.read(os.path.join(tdir, "t1.md"))
        assert fm["hub_trace_id"] == "hub-trace-1"
        assert fm["hub_pushed_fingerprint"]

    def test_a_trace_with_no_outcome_yet_pushes_an_empty_outcome(self, store, monkeypatch):
        import asyncio

        tdir = paths.traces_dir(str(store))
        _write_captured_trace(tdir, "t1.md", "occasion-1", "title", "ctx", "sol")

        async def fake_call_tool(hub_url, api_key, name, arguments, **kw):
            assert arguments["outcome"] == {}
            return {"id": "hub-trace-1", "quarantined": False}

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        asyncio.run(hub_client.push_captured_traces("http://localhost:8420/mcp", "key", str(store)))

    def test_the_traces_dir_readme_is_never_pushed(self, store, monkeypatch):
        """init_cmd.py writes a README.md into every traces_dir. It has no
        frontmatter delimiter, so trace_io.read() doesn't raise on it --
        it silently returns a near-empty instance instead -- and without
        excluding it explicitly, that reached _call_tool as a doomed
        contribute_trace(title="", context_text="", ...) on every single
        --push-traces run, alongside whatever real traces existed."""
        import asyncio

        tdir = paths.traces_dir(str(store))
        os.makedirs(tdir, exist_ok=True)
        with open(os.path.join(tdir, "README.md"), "w", encoding="utf-8") as fh:
            fh.write("# Traces\n\nRaw captured incidents live here.\n")
        _write_captured_trace(tdir, "t1.md", "occasion-1", "title", "ctx", "sol")

        calls = []

        async def fake_call_tool(hub_url, api_key, name, arguments, **kw):
            calls.append((name, arguments))
            return {"id": "hub-trace-1", "quarantined": False}

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        results = asyncio.run(hub_client.push_captured_traces("http://localhost:8420/mcp", "key", str(store)))

        assert len(calls) == 1, f"README.md must never reach _call_tool: {calls}"
        assert len(results) == 1
        assert results[0].hub_trace_id == "hub-trace-1"

    def test_pushes_run_concurrently_but_bounded(self, store, monkeypatch):
        """20 independent files used to mean 20 sequential network round
        trips (~150ms each in practice -> ~3s for just this many, ~75s for
        a real 500-file sync). Tracking the actual number of calls
        in-flight at once -- rather than asserting on wall-clock time,
        which is flaky under CI load -- proves both halves of the fix:
        more than one call in flight at a time (not still sequential), and
        never more than _PUSH_CONCURRENCY at once (bounded, not
        `asyncio.gather` over everything unbounded -- see hub_client.py's
        own comment on why: tripping the Hub's write rate limiter would
        turn pushes that succeed serially into 429s)."""
        import asyncio

        tdir = paths.traces_dir(str(store))
        n = 20
        for i in range(n):
            _write_captured_trace(tdir, f"t{i}.md", f"occasion-{i}", f"title {i}", "ctx", "sol")

        in_flight = 0
        max_in_flight = 0

        async def fake_call_tool(hub_url, api_key, name, arguments, **kw):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return {"id": f"hub-{arguments['title']}", "quarantined": False}

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        results = asyncio.run(hub_client.push_captured_traces("http://localhost:8420/mcp", "key", str(store)))

        assert len(results) == n
        assert max_in_flight > 1, "pushes ran strictly sequentially -- the concurrency fix regressed"
        assert max_in_flight <= hub_client._PUSH_CONCURRENCY, (
            f"unbounded concurrency: {max_in_flight} calls in flight at once, "
            f"expected at most {hub_client._PUSH_CONCURRENCY}"
        )

    def test_unchanged_trace_is_skipped_without_a_second_call(self, store, monkeypatch):
        import asyncio

        tdir = paths.traces_dir(str(store))
        fingerprint = hub_client._trace_push_fingerprint("title", "ctx", "sol", [], {"resolved": True})
        _write_captured_trace(
            tdir, "t1.md", "occasion-1", "title", "ctx", "sol", outcome={"resolved": True},
            extra={"hub_trace_id": "hub-trace-1", "hub_pushed_fingerprint": fingerprint},
        )

        async def explode(*a, **k):
            raise AssertionError("must not call the Hub for an unchanged, already-pushed trace")

        monkeypatch.setattr(hub_client, "_call_tool", explode)
        results = asyncio.run(hub_client.push_captured_traces("http://localhost:8420/mcp", "key", str(store)))

        assert results[0].skipped is True
        assert results[0].hub_trace_id == "hub-trace-1"

    def test_a_recapture_that_only_attaches_an_outcome_is_propagated_via_amend(self, store, monkeypatch):
        """The exact scenario capture_cmd.py documents: `--occasion-id`
        pins the trace id, and a later recapture attaches --resolved/etc.
        without changing title/context/solution at all. This must still be
        detected as a change worth pushing."""
        import asyncio

        tdir = paths.traces_dir(str(store))
        stale_fingerprint = hub_client._trace_push_fingerprint("title", "ctx", "sol", [], {})
        _write_captured_trace(
            tdir, "t1.md", "occasion-1", "title", "ctx", "sol", outcome={"resolved": True},
            extra={"hub_trace_id": "hub-trace-1", "hub_pushed_fingerprint": stale_fingerprint},
        )

        calls = []

        async def fake_call_tool(hub_url, api_key, name, arguments, **kw):
            calls.append((name, arguments))
            assert name == "amend_trace"
            assert arguments["id"] == "hub-trace-1"
            assert arguments["outcome"] == {"resolved": True}
            return {"id": "hub-trace-2", "quarantined": False}

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        results = asyncio.run(hub_client.push_captured_traces("http://localhost:8420/mcp", "key", str(store)))

        assert len(calls) == 1
        assert results[0].hub_trace_id == "hub-trace-2"
        fm, _ = frontmatter.read(os.path.join(tdir, "t1.md"))
        assert fm["hub_trace_id"] == "hub-trace-2"
        assert fm["hub_pushed_fingerprint"] != stale_fingerprint

    def test_amend_carries_its_own_idempotency_key_distinct_from_lesson_amends(self, store, monkeypatch):
        import asyncio

        tdir = paths.traces_dir(str(store))
        stale_fingerprint = hub_client._trace_push_fingerprint("title", "ctx", "sol", [], {})
        _write_captured_trace(
            tdir, "t1.md", "occasion-1", "title", "ctx", "sol", outcome={"resolved": True},
            extra={"hub_trace_id": "hub-trace-1", "hub_pushed_fingerprint": stale_fingerprint},
        )

        calls = []

        async def fake_call_tool(hub_url, api_key, name, arguments, **kw):
            calls.append((name, arguments))
            return {"id": "hub-trace-2", "quarantined": False}

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        asyncio.run(hub_client.push_captured_traces("http://localhost:8420/mcp", "key", str(store)))

        key = calls[0][1].get("idempotency_key")
        assert key, "amend_trace call carried no idempotency_key at all"
        assert len(key) <= 128
        assert key.startswith("trace-amend:")

        new_fingerprint = hub_client._trace_push_fingerprint("title", "ctx", "sol", [], {"resolved": True})
        assert key == hub_client._trace_amend_idempotency_key("occasion-1", new_fingerprint)

    def test_two_pushes_with_different_outcomes_get_different_fingerprints(self):
        """The property that makes the fingerprint comparison actually
        catch an outcome-only edit: two otherwise-identical traces with
        different outcome dicts must not fingerprint the same."""
        fp1 = hub_client._trace_push_fingerprint("t", "c", "s", [], {"resolved": True})
        fp2 = hub_client._trace_push_fingerprint("t", "c", "s", [], {"resolved": False})
        assert fp1 != fp2

    def test_an_error_from_the_hub_is_reported_without_updating_the_local_file(self, store, monkeypatch):
        import asyncio

        tdir = paths.traces_dir(str(store))
        _write_captured_trace(tdir, "t1.md", "occasion-1", "title", "ctx", "sol")

        async def fake_call_tool(hub_url, api_key, name, arguments, **kw):
            return {"error": "entitlement_exceeded", "detail": "over plan limit"}

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        results = asyncio.run(hub_client.push_captured_traces("http://localhost:8420/mcp", "key", str(store)))

        assert results[0].error == "entitlement_exceeded"
        assert results[0].hub_trace_id is None
        fm, _ = frontmatter.read(os.path.join(tdir, "t1.md"))
        assert fm.get("hub_trace_id") is None

    def test_a_malformed_trace_file_does_not_abort_the_others(self, store, monkeypatch):
        """Same guard as push_active_lessons's identical fix, and arguably
        higher-stakes here: one corrupted trace file used to abort the
        whole push before this fix, which for --push-traces specifically
        means every other trace's outcome data -- the entire reason this
        function exists -- silently never reaches the Hub either."""
        import asyncio

        tdir = paths.traces_dir(str(store))
        _write_captured_trace(
            tdir, "good.md", "occasion-good", "good title", "ctx", "sol", outcome={"resolved": True},
        )
        with open(os.path.join(tdir, "bad.md"), "w") as fh:
            fh.write("---\ntitle: [unclosed list\n---\nbroken\n")

        async def fake_call_tool(hub_url, api_key, name, arguments, **kw):
            return {"id": "hub-trace-1", "quarantined": False}

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        results = asyncio.run(hub_client.push_captured_traces("http://localhost:8420/mcp", "key", str(store)))

        by_slug = {r.slug: r for r in results}
        assert by_slug["occasion-good"].hub_trace_id == "hub-trace-1"
        assert by_slug["occasion-good"].error is None
        assert by_slug["bad"].hub_trace_id is None
        assert by_slug["bad"].error is not None


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
