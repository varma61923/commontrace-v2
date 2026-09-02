"""Regression tests for the deployment-readiness pass.

Every test here corresponds to a defect reproduced against a live Hub and a
real store before it was fixed, so what is pinned is the behaviour an
operator or customer would actually hit -- not the shape of the patch.

The groups:
  1. The Hub client's retry layer, which never ran (ExceptionGroup wrapping).
  2. Failures reported as the wrong thing (a rejected key as a network outage).
  3. Rate limiting: recognised, paced, and reported as itself.
  4. One MCP session per batch instead of one per call.
  5. Unedited scaffolding never becoming an active, injected, published lesson.
  6. Config that refuses to start rather than misbehaving later.
"""
from __future__ import annotations

import asyncio

import pytest

from commontrace import hub_client, templates

# ExceptionGroup is a builtin only from 3.11; this project supports 3.10, and
# CI runs the suite on it. The backport (a transitive dependency of anyio, so
# present wherever the `mcp` extra is) is preferred when the builtin is
# missing, and the last resort is a local stand-in implementing the one
# attribute `_iter_causes` actually reads -- which is the contract under test.
try:  # pragma: no cover - which branch runs depends only on the interpreter
    _ExcGroup = ExceptionGroup  # type: ignore[name-defined]  # noqa: F821
except NameError:  # pragma: no cover
    try:
        from exceptiongroup import ExceptionGroup as _ExcGroup  # type: ignore[no-redef]
    except ImportError:
        class _ExcGroup(Exception):  # type: ignore[no-redef]
            def __init__(self, message, exceptions):
                super().__init__(message)
                self.exceptions = list(exceptions)


# --- 1. The retry layer that never ran ---------------------------------


class TestExceptionGroupUnwrapping:
    """The MCP SDK drives its transport inside an anyio task group, so every
    transport error arrived wrapped -- often twice -- in an ExceptionGroup
    whose only text is "unhandled errors in a TaskGroup". Classification ran
    against that wrapper, so it never saw the real exception."""

    def _wrapped(self, exc):
        # The exact double-nesting observed from the MCP client.
        return _ExcGroup("unhandled errors in a TaskGroup",
                         [_ExcGroup("unhandled errors in a TaskGroup", [exc])])

    def test_a_refused_connection_inside_a_task_group_is_retryable(self):
        """Measured before the fix: a genuine connection refusal -- the
        textbook retryable case -- was classified NOT retryable and tried
        exactly once, while the error still claimed three attempts."""
        assert hub_client._is_retryable(self._wrapped(ConnectionRefusedError("refused"))) is True

    def test_a_timeout_inside_a_task_group_is_retryable(self):
        assert hub_client._is_retryable(self._wrapped(TimeoutError("read timeout"))) is True

    def test_root_cause_names_the_real_exception(self):
        cause = hub_client.root_cause(self._wrapped(ConnectionRefusedError("all attempts failed")))
        assert isinstance(cause, ConnectionRefusedError)
        assert "TaskGroup" not in str(cause)

    def test_a_cyclic_cause_chain_terminates(self):
        """__cause__ cycles are constructible; walking them must not hang."""
        a, b = RuntimeError("a"), RuntimeError("b")
        a.__cause__ = b
        b.__cause__ = a
        assert hub_client._is_retryable(a) is False  # terminates, and is unrecognised


class TestStatusDetectionDoesNotFireOnIncidentalDigits:
    def test_a_rejection_mentioning_500_chars_is_not_a_server_error(self):
        """The Hub rejects an over-length title with "title exceeds 500
        chars". Read as an HTTP 500 that would be retried three times."""
        assert hub_client._http_status(Exception("title exceeds 500 chars (612)")) is None

    def test_a_trace_id_is_not_a_status_code(self):
        assert hub_client._http_status(Exception("no trace with id 93228973-4446-43d1")) is None

    @pytest.mark.parametrize("text,expected", [
        ("Server returned HTTP 503", 503),
        ("status code: 429", 429),
        ("status=401", 401),
    ])
    def test_a_status_stated_as_a_status_is_read(self, text, expected):
        assert hub_client._http_status(Exception(text)) == expected


class TestThisModulesOwnWrappersAreNotMistakenForTransportErrors:
    def test_a_bad_url_is_not_retryable(self):
        """"HubConnectionError" contains the substring "connect", so matching
        on the exception's class name made every wrapper this module raises
        -- a rejected URL scheme included -- look retryable to its own
        classifier."""
        exc = hub_client.HubConfigurationError("refusing to use Hub URL 'file:///x': scheme must be http")
        assert hub_client._is_retryable(exc) is False

    def test_a_tool_rejection_is_never_retryable(self):
        assert hub_client._is_retryable(hub_client.HubToolError("title exceeds 500 chars")) is False


# --- 2. Failures reported as what they are ------------------------------


class TestFailuresAreReportedAsThemselves:
    def test_a_rejected_key_is_an_auth_error_not_a_network_error(self):
        exc = hub_client._transport_failure(
            "http://hub/mcp", 1, RuntimeError("Server returned an error response"), observed_status=401
        )
        assert isinstance(exc, hub_client.HubAuthError)
        assert "rejected the API key" in str(exc)
        assert "could not reach" not in str(exc)

    def test_a_rate_limit_is_reported_as_a_rate_limit(self):
        exc = hub_client._transport_failure(
            "http://hub/mcp", 3, RuntimeError("boom"), observed_status=429, observed_retry_after=2.0
        )
        assert isinstance(exc, hub_client.HubRateLimited)
        assert exc.retry_after == 2.0

    def test_the_attempt_count_is_the_real_one(self):
        """The message printed the CONFIGURED maximum unconditionally, so a
        failure that gave up after one attempt still claimed three."""
        assert "after 1 attempt:" in str(
            hub_client._transport_failure("http://hub/mcp", 1, ConnectionRefusedError("refused"))
        )
        assert "after 3 attempts:" in str(
            hub_client._transport_failure("http://hub/mcp", 3, ConnectionRefusedError("refused"))
        )

    def test_an_already_classified_error_is_not_wrapped_twice(self):
        inner = hub_client.HubAuthError("the Hub rejected the API key (invalid, revoked, or expired).")
        outer = _ExcGroup("unhandled errors in a TaskGroup", [inner])
        assert hub_client._transport_failure("http://hub/mcp", 1, outer) is inner


# --- 3. Rate limiting: recognised, paced, reported ----------------------


class TestRateLimitHandling:
    def test_429_is_retryable_unlike_every_other_4xx(self):
        """It is the one client error a later attempt can succeed at. Before
        this, a rate-limited bulk push failed permanently on the first 429."""
        assert hub_client._is_retryable(RuntimeError("boom"), observed_status=429) is True
        assert hub_client._is_retryable(RuntimeError("boom"), observed_status=401) is False

    def test_a_tool_level_rate_limit_is_recognised(self):
        """The Hub's per-org WRITE limit comes back as a successful MCP
        result whose payload is {"error": "rate_limited"} -- so it reached no
        retry or pacing logic at all and became a permanent per-file error."""
        refusal = hub_client._tool_rate_limit({"error": "rate_limited", "detail": "x", "retry_after": 2})
        assert refusal is not None
        assert refusal.retry_after == 2.0
        assert hub_client._is_rate_limited(refusal) is True

    def test_an_ordinary_tool_error_is_not_a_rate_limit(self):
        assert hub_client._tool_rate_limit({"error": "invalid_request", "detail": "bad title"}) is None
        assert hub_client._tool_rate_limit({"id": "abc"}) is None

    def test_a_servers_retry_after_wins_over_local_backoff(self):
        refusal = hub_client._ToolRateLimited("rate_limited", retry_after=7.0)
        assert hub_client._backoff_delay(1, refusal) == 7.0

    def test_backoff_is_capped_so_a_hostile_retry_after_cannot_hang_the_cli(self):
        refusal = hub_client._ToolRateLimited("rate_limited", retry_after=99999.0)
        assert hub_client._backoff_delay(1, refusal) == hub_client.RETRY_MAX_DELAY_SECONDS


class TestRateLimitGatePacesTheWholeBatch:
    def test_a_pause_from_one_worker_holds_every_worker(self):
        """Per-call backoff alone does not fix a stampede: the other workers
        keep firing into a limiter that has already said no, so the bucket
        never refills."""
        async def go():
            gate = hub_client._RateLimitGate()
            gate.pause(0.2)
            loop = asyncio.get_running_loop()
            started = loop.time()
            await asyncio.gather(*(gate.wait() for _ in range(4)))
            return loop.time() - started

        assert asyncio.run(go()) >= 0.19

    def test_spacing_widens_on_refusal_and_relaxes_on_success(self):
        async def go():
            gate = hub_client._RateLimitGate()
            assert gate._min_interval == 0.0
            gate.pause(0.0)
            first = gate._min_interval
            assert first > 0.0
            gate.pause(0.0)
            assert gate._min_interval > first          # additive increase
            widened = gate._min_interval
            for _ in range(5):
                gate.succeeded()
            assert gate._min_interval < widened        # multiplicative decrease
        asyncio.run(go())

    def test_the_first_pause_is_announced_once(self):
        """A paced push is a correct slow push, but an unexplained one reads
        as a hang -- and a notice per refusal would be its own noise."""
        async def go():
            seen = []
            gate = hub_client._RateLimitGate(on_first_pause=seen.append)
            gate.pause(1.0)
            gate.pause(1.0)
            gate.pause(1.0)
            return seen
        assert len(asyncio.run(go())) == 1


# --- 5. Unedited scaffolding is never an active lesson ------------------


class TestUnfilledPlaceholders:
    """An all-placeholder lesson used to be approvable, valid, retrievable,
    counted as coverage, and publishable to the Hub -- reproduced end to end
    on a real store."""

    def test_distills_todo_scaffolding_is_detected(self):
        found = templates.unfilled_placeholders(
            {"applies_when": "TODO: precise activation condition",
             "do_not_apply_when": "TODO: explicit counter-condition"},
            "## Rule\nTODO: derive the actionable rule from the traces below.\n",
        )
        assert "applies_when" in found and "do_not_apply_when" in found and "## Rule" in found

    def test_lesson_news_bracket_scaffolding_is_detected(self):
        assert templates.unfilled_placeholders({}, templates.lesson_body())

    def test_a_written_lesson_is_clean(self):
        assert templates.unfilled_placeholders(
            {"applies_when": "A refund retry returns HTTP 409",
             "do_not_apply_when": "The charge was never authorized"},
            "## Rule\nReuse the original idempotency key.\n\n## Why\nSix incidents.\n",
        ) == []

    def test_prose_that_merely_mentions_todo_is_not_scaffolding(self):
        """A code-review lesson about TODO comments is a real lesson."""
        assert templates.unfilled_placeholders(
            {"applies_when": "the diff adds a TODO: comment with no owner"},
            "## Rule\nReject a TODO: comment that names no owner.\n",
        ) == []

    def test_prose_containing_brackets_is_not_scaffolding(self):
        assert templates.unfilled_placeholders(
            {}, "## Rule\nPrefer arr[0] over arr.first() in hot loops.\n"
        ) == []


class TestScaffoldingNeverBecomesAnActiveLesson:
    """The end-to-end chain, at the CLI. Before these guards, on a real
    store: `distill` wrote an all-"TODO:" candidate, `approve` activated it,
    `validate` called it "1/1 lessons valid", `query` returned it as the top
    hit, `taxonomy` reported its source pattern as "covered", `pilot`
    reported "Gaps: 0", and `sync --push` published it to the whole fleet."""

    def _candidate(self, store):
        from commontrace.cli import main
        main(["init", "--agent-type", "support", "--dest", str(store)])
        for i in range(3):
            main(["capture", "--title", f"Gateway 409 refund {i}",
                  "--context", f"payment gateway 409 on refund retry {i}",
                  "--solution", f"reuse idempotency key {i}",
                  "--tags", "refunds", "--agent-type", "support", "--dest", str(store)])
        main(["distill", "--min-cluster", "2", "--dest", str(store)])
        lessons = [
            p for p in (store / "memory" / "lessons").glob("lesson_candidate_*.md")
            if p.name != "lesson_template.md"
        ]
        assert lessons, "distill should have written a candidate"
        return lessons[0].stem[len("lesson_"):]

    def test_approve_refuses_a_lesson_that_is_still_scaffolding(self, tmp_path, capsys):
        from commontrace.cli import main
        slug = self._candidate(tmp_path)
        capsys.readouterr()
        assert main(["lesson", "approve", slug, "--dest", str(tmp_path)]) == 1
        assert "unedited scaffolding" in capsys.readouterr().err

    def test_force_approves_but_says_so(self, tmp_path, capsys):
        from commontrace.cli import main
        slug = self._candidate(tmp_path)
        capsys.readouterr()
        assert main(["lesson", "approve", slug, "--force", "--dest", str(tmp_path)]) == 0
        assert "warning" in capsys.readouterr().err

    def test_validate_fails_an_active_lesson_that_is_still_scaffolding(self, tmp_path):
        from commontrace.cli import main
        slug = self._candidate(tmp_path)
        main(["lesson", "approve", slug, "--force", "--dest", str(tmp_path)])
        assert main(["lesson", "validate", "--dest", str(tmp_path)]) == 1

    def test_validate_leaves_a_review_candidate_alone(self, tmp_path):
        """A `status: review` candidate is SUPPOSED to carry placeholders --
        that is what distill writes and what a human is asked to fill in."""
        from commontrace.cli import main
        self._candidate(tmp_path)
        assert main(["lesson", "validate", "--dest", str(tmp_path)]) == 0

    def test_taxonomy_does_not_count_scaffolding_as_coverage(self, tmp_path, capsys):
        """This is the number a customer reads to decide what still needs
        work; counting an empty lesson reported "Gaps: 0" on a real gap."""
        from commontrace.cli import main
        slug = self._candidate(tmp_path)
        main(["lesson", "approve", slug, "--force", "--dest", str(tmp_path)])
        capsys.readouterr()
        main(["taxonomy", "--dest", str(tmp_path)])
        out = capsys.readouterr().out
        assert "Already covered by an active lesson: **0**" in out
        assert "Gaps (recurring, no lesson yet): **1**" in out

    def test_lesson_new_scaffolds_at_review_not_active(self, tmp_path):
        """`lesson new` wrote status: active over a body that is entirely
        template text, so a lesson was live -- retrievable, counted, and
        publishable -- before a word of it had been written."""
        from commontrace import frontmatter
        from commontrace.cli import main
        main(["init", "--agent-type", "code", "--dest", str(tmp_path)])
        main(["lesson", "new", "--slug", "fresh", "--description", "d",
              "--agent-type", "code", "--domain", "testing", "--dest", str(tmp_path)])
        fm, _ = frontmatter.read(str(tmp_path / "memory" / "lessons" / "lesson_fresh.md"))
        assert fm["status"] == "review"

    def test_push_refuses_to_publish_scaffolding(self, tmp_path):
        """The Hub is where the mistake stops being local: from there
        search_traces serves it to every agent in the fleet."""
        from commontrace import frontmatter
        from commontrace.cli import main
        slug = self._candidate(tmp_path)
        main(["lesson", "approve", slug, "--force", "--dest", str(tmp_path)])
        path = tmp_path / "memory" / "lessons" / f"lesson_{slug}.md"
        fm, _ = frontmatter.read(str(path))
        assert fm["status"] == "active"

        async def explode(*a, **kw):  # must never be reached
            raise AssertionError("scaffolding must not be sent to the Hub")

        import commontrace.hub_client as hc
        original, hc._call_tool = hc._call_tool, explode
        try:
            results = asyncio.run(hc.push_active_lessons("https://hub.example/mcp", "k", str(tmp_path)))
        finally:
            hc._call_tool = original
        assert len(results) == 1
        assert "unedited scaffolding" in (results[0].error or "")
        assert results[0].hub_trace_id is None


class TestPulledTracesAreNeverPushedBack:
    """`sync --pull` writes the Hub's own traces into the local traces dir
    as `hub_<slug>_<id>.md` with `hub_trace_id` already set. `--push-traces`
    then saw each as "on the Hub, no recorded fingerprint" and amended the
    Hub's trace with a round-tripped copy of itself -- reproduced: a store of
    46 captured traces became 68 push candidates after one pull."""

    def test_a_pulled_trace_is_not_a_push_candidate(self, tmp_path):
        import os

        from commontrace import hub_client, paths

        tdir = paths.traces_dir(str(tmp_path))
        os.makedirs(tdir)
        for name in ("README.md", "2026-01-01_local_abc12345.md", "hub_pulled_def67890.md"):
            with open(os.path.join(tdir, name), "w", encoding="utf-8") as fh:
                fh.write("---\nid: x\n---\n\n## Context\nc\n\n## Solution\ns\n")

        candidates = sorted(
            os.path.basename(p) for p in hub_client._iter_captured_trace_paths(str(tmp_path))
        )
        assert candidates == ["2026-01-01_local_abc12345.md"]

    def test_locally_captured_traces_are_still_pushed(self, tmp_path):
        """The exclusion is by the `hub_` prefix the pull path writes; every
        name `capture`/`import` produces starts with a date."""
        from commontrace import hub_client
        from commontrace.cli import main

        main(["init", "--agent-type", "support", "--dest", str(tmp_path)])
        main(["capture", "--title", "Real capture", "--context", "c", "--solution", "s",
              "--agent-type", "support", "--dest", str(tmp_path)])
        assert len(list(hub_client._iter_captured_trace_paths(str(tmp_path)))) == 1


class TestRetryBudgetsCannotMultiply:
    """HubSession.call has its own rate-limit budget, and _call_tool wraps it
    in a second retry loop for the single-shot case. Left unbounded the two
    multiply -- up to 30 attempts and minutes of wall time for one call --
    when the inner loop has already established the server is still saying
    no."""

    class _AlwaysRateLimited:
        def __init__(self):
            self.calls = 0

        async def call_tool(self, name, args):
            self.calls += 1

            class Result:
                is_error = False
                structured_content = {"error": "rate_limited", "detail": "nope", "retry_after": 0}
                content: list = []

            return Result()

    def test_a_permanently_rate_limited_call_stops_at_one_budget(self):
        async def go():
            session = hub_client.HubSession("http://hub/mcp", "k", 5.0, hub_client.DEFAULT_MAX_ATTEMPTS)
            fake = self._AlwaysRateLimited()
            session._session = fake
            with pytest.raises(hub_client.HubRateLimited):
                await session.call("account_usage", {})
            return fake.calls

        assert asyncio.run(go()) == hub_client.RATE_LIMIT_MAX_ATTEMPTS

    def test_a_tool_level_refusal_is_surfaced_as_a_rate_limit_not_an_outage(self):
        """The Hub's per-org WRITE limit is returned inside a successful MCP
        response, so it used to become a permanent per-file error reading
        "could not reach the Hub"."""
        async def go():
            session = hub_client.HubSession("http://hub/mcp", "k", 5.0, 1)
            session._session = self._AlwaysRateLimited()
            try:
                await session.call("contribute_trace", {})
            except hub_client.HubRateLimited as exc:
                return str(exc)
            raise AssertionError("expected HubRateLimited")

        message = asyncio.run(go())
        assert "WRITE limit" in message
        assert "could not reach" not in message
