"""Tests for `commontrace sync` -- the CLI wrapper around hub_client's
push_active_lessons/pull_search_results. Every Hub call is monkeypatched
here; the actual push/pull semantics are covered directly in
tests/test_hub_client.py. This file is about the CLI's own
responsibilities: which direction(s) run by default, how results are
reported, and how the two client-side error types are turned into a clean
exit code and message rather than a raw traceback -- none of which had any
test coverage at all before this file (40% line coverage on sync_cmd.py,
essentially the entire body of run() unexercised).
"""
from __future__ import annotations

import argparse

from commontrace import hub_client
from commontrace.commands import sync_cmd


def _args(**over):
    base = dict(
        push=False, pull=False, push_traces=False, query="", tags="",
        hub_url="http://hub.invalid/mcp", hub_api_key="ct_live_test",
        dest="/tmp/commontrace-sync-cmd-test-root",
    )
    base.update(over)
    return argparse.Namespace(**base)


class TestNoHubConfigured:
    def test_prints_the_setup_message_and_returns_0(self, capsys):
        args = _args(hub_url=None, hub_api_key=None)
        assert sync_cmd.run(args) == 0
        assert "sync" in capsys.readouterr().out.lower()

    def test_falls_back_to_environment_variables(self, monkeypatch):
        monkeypatch.setenv("COMMONTRACE_HUB_URL", "http://hub.invalid/mcp")
        monkeypatch.setenv("COMMONTRACE_HUB_API_KEY", "ct_live_env")
        calls = []

        async def fake_push(hub, key, root):
            calls.append((hub, key))
            return []

        async def fake_pull(hub, key, root, query, tags):
            return hub_client.PullResult()

        monkeypatch.setattr(hub_client, "push_active_lessons", fake_push)
        monkeypatch.setattr(hub_client, "pull_search_results", fake_pull)
        args = _args(hub_url=None, hub_api_key=None)
        assert sync_cmd.run(args) == 0
        assert calls == [("http://hub.invalid/mcp", "ct_live_env")]


class TestCliKeyWarning:
    def test_warns_when_the_key_is_passed_on_the_command_line(self, monkeypatch, capsys):
        async def fake_push(hub, key, root):
            return []

        async def fake_pull(hub, key, root, query, tags):
            return hub_client.PullResult()

        monkeypatch.setattr(hub_client, "push_active_lessons", fake_push)
        monkeypatch.setattr(hub_client, "pull_search_results", fake_pull)
        sync_cmd.run(_args())
        assert "visible to other local users" in capsys.readouterr().err

    def test_no_warning_when_the_key_comes_only_from_the_environment(self, monkeypatch, capsys):
        monkeypatch.setenv("COMMONTRACE_HUB_API_KEY", "ct_live_env")

        async def fake_push(hub, key, root):
            return []

        async def fake_pull(hub, key, root, query, tags):
            return hub_client.PullResult()

        monkeypatch.setattr(hub_client, "push_active_lessons", fake_push)
        monkeypatch.setattr(hub_client, "pull_search_results", fake_pull)
        sync_cmd.run(_args(hub_api_key=None))
        assert "visible to other local users" not in capsys.readouterr().err


class TestDirectionSelection:
    """No flag defaults to both directions; either flag alone runs only
    that direction; both together runs both explicitly."""

    def test_default_runs_both_push_and_pull(self, monkeypatch):
        calls = []

        async def fake_push(hub, key, root):
            calls.append("push")
            return []

        async def fake_pull(hub, key, root, query, tags):
            calls.append("pull")
            return hub_client.PullResult()

        monkeypatch.setattr(hub_client, "push_active_lessons", fake_push)
        monkeypatch.setattr(hub_client, "pull_search_results", fake_pull)
        assert sync_cmd.run(_args()) == 0
        assert calls == ["push", "pull"]

    def test_push_only_does_not_pull(self, monkeypatch):
        calls = []

        async def fake_push(hub, key, root):
            calls.append("push")
            return []

        async def explode_pull(*a, **k):
            raise AssertionError("must not pull when --push was given alone")

        monkeypatch.setattr(hub_client, "push_active_lessons", fake_push)
        monkeypatch.setattr(hub_client, "pull_search_results", explode_pull)
        assert sync_cmd.run(_args(push=True)) == 0
        assert calls == ["push"]

    def test_pull_only_does_not_push(self, monkeypatch):
        calls = []

        async def explode_push(*a, **k):
            raise AssertionError("must not push when --pull was given alone")

        async def fake_pull(hub, key, root, query, tags):
            calls.append("pull")
            return hub_client.PullResult()

        monkeypatch.setattr(hub_client, "push_active_lessons", explode_push)
        monkeypatch.setattr(hub_client, "pull_search_results", fake_pull)
        assert sync_cmd.run(_args(pull=True)) == 0
        assert calls == ["pull"]

    def test_both_flags_together_runs_both(self, monkeypatch):
        calls = []

        async def fake_push(hub, key, root):
            calls.append("push")
            return []

        async def fake_pull(hub, key, root, query, tags):
            calls.append("pull")
            return hub_client.PullResult()

        monkeypatch.setattr(hub_client, "push_active_lessons", fake_push)
        monkeypatch.setattr(hub_client, "pull_search_results", fake_pull)
        assert sync_cmd.run(_args(push=True, pull=True)) == 0
        assert calls == ["push", "pull"]


class TestPushReporting:
    def test_reports_ok_error_and_skipped_counts(self, monkeypatch, capsys):
        results = [
            hub_client.PushResult(slug="a", hub_trace_id="t1"),
            hub_client.PushResult(slug="b", hub_trace_id=None, error="boom"),
            hub_client.PushResult(slug="c", hub_trace_id="t3", skipped=True),
            hub_client.PushResult(slug="d", hub_trace_id="t4", quarantined=True),
        ]

        async def fake_push(hub, key, root):
            return results

        monkeypatch.setattr(hub_client, "push_active_lessons", fake_push)
        assert sync_cmd.run(_args(push=True)) == 0
        out = capsys.readouterr()
        assert "2 lesson(s) pushed, 1 already on the Hub, 1 error(s), out of 4" in out.out
        assert "[ERROR] b: boom" in out.err
        assert "c -> already hub_trace_id=t3 (unchanged)" in out.out
        assert "d -> hub_trace_id=t4 (quarantined pending review)" in out.out
        # A plain success (not skipped, not quarantined, no error) carries no
        # extra suffix -- checked so the two conditional tags above are
        # proven conditional, not just present.
        assert "a -> hub_trace_id=t1\n" in out.out


class TestPushTraces:
    """--push-traces is independent of --push/--pull and not run by
    default -- a raw captured trace carries more specific, potentially
    sensitive incident content than a curated lesson, so pushing it is
    opt-in even though lessons already push by default."""

    def test_not_run_by_default(self, monkeypatch):
        async def fake_push(hub, key, root):
            return []

        async def fake_pull(hub, key, root, query, tags):
            return hub_client.PullResult()

        async def explode_push_traces(*a, **k):
            raise AssertionError("must not push traces unless --push-traces was given")

        monkeypatch.setattr(hub_client, "push_active_lessons", fake_push)
        monkeypatch.setattr(hub_client, "pull_search_results", fake_pull)
        monkeypatch.setattr(hub_client, "push_captured_traces", explode_push_traces)
        assert sync_cmd.run(_args()) == 0

    def test_runs_alongside_the_default_push_and_pull_when_given(self, monkeypatch):
        calls = []

        async def fake_push(hub, key, root):
            calls.append("push")
            return []

        async def fake_pull(hub, key, root, query, tags):
            calls.append("pull")
            return hub_client.PullResult()

        async def fake_push_traces(hub, key, root):
            calls.append("push_traces")
            return []

        monkeypatch.setattr(hub_client, "push_active_lessons", fake_push)
        monkeypatch.setattr(hub_client, "pull_search_results", fake_pull)
        monkeypatch.setattr(hub_client, "push_captured_traces", fake_push_traces)
        assert sync_cmd.run(_args(push_traces=True)) == 0
        # Order matters for a human reading the output top to bottom, not
        # for correctness -- but pinning it catches an accidental reorder
        # that would otherwise pass unnoticed.
        assert calls == ["push", "push_traces", "pull"]

    def test_runs_with_push_only_no_pull(self, monkeypatch):
        calls = []

        async def fake_push(hub, key, root):
            calls.append("push")
            return []

        async def explode_pull(*a, **k):
            raise AssertionError("must not pull when --push --push-traces was given without --pull")

        async def fake_push_traces(hub, key, root):
            calls.append("push_traces")
            return []

        monkeypatch.setattr(hub_client, "push_active_lessons", fake_push)
        monkeypatch.setattr(hub_client, "pull_search_results", explode_pull)
        monkeypatch.setattr(hub_client, "push_captured_traces", fake_push_traces)
        assert sync_cmd.run(_args(push=True, push_traces=True)) == 0
        assert calls == ["push", "push_traces"]

    def test_reports_ok_error_and_skipped_counts(self, monkeypatch, capsys):
        results = [
            hub_client.PushResult(slug="a", hub_trace_id="t1"),
            hub_client.PushResult(slug="b", hub_trace_id=None, error="boom"),
            hub_client.PushResult(slug="c", hub_trace_id="t3", skipped=True),
            hub_client.PushResult(slug="d", hub_trace_id="t4", quarantined=True),
        ]

        async def fake_push(hub, key, root):
            return []

        async def fake_pull(hub, key, root, query, tags):
            return hub_client.PullResult()

        async def fake_push_traces(hub, key, root):
            return results

        monkeypatch.setattr(hub_client, "push_active_lessons", fake_push)
        monkeypatch.setattr(hub_client, "pull_search_results", fake_pull)
        monkeypatch.setattr(hub_client, "push_captured_traces", fake_push_traces)
        assert sync_cmd.run(_args(push_traces=True)) == 0
        out = capsys.readouterr()
        assert "2 trace(s) pushed, 1 already on the Hub, 1 error(s), out of 4" in out.out
        assert "[ERROR] b: boom" in out.err
        assert "c -> already hub_trace_id=t3 (unchanged)" in out.out
        assert "d -> hub_trace_id=t4 (quarantined pending review)" in out.out
        assert "a -> hub_trace_id=t1\n" in out.out


class TestPullReporting:
    def test_reports_found_and_written_counts(self, monkeypatch, capsys):
        result = hub_client.PullResult(written_paths=["/tmp/a.md"], n_found=3)

        async def fake_pull(hub, key, root, query, tags):
            return result

        monkeypatch.setattr(hub_client, "pull_search_results", fake_pull)
        assert sync_cmd.run(_args(pull=True)) == 0
        out = capsys.readouterr().out
        assert "3 trace(s) found, 1 new candidate(s)" in out
        assert "wrote /tmp/a.md" in out
        assert "Promote a candidate with `commontrace lesson new`" in out

    def test_ignored_terms_are_reported_even_with_results(self, monkeypatch, capsys):
        result = hub_client.PullResult(written_paths=["/tmp/a.md"], n_found=1, ignored_terms=["the"])

        async def fake_pull(hub, key, root, query, tags):
            return result

        monkeypatch.setattr(hub_client, "pull_search_results", fake_pull)
        sync_cmd.run(_args(pull=True))
        out = capsys.readouterr().out
        assert "Not searched on: the" in out
        assert "Try a more specific word" not in out  # results WERE found

    def test_ignored_terms_with_no_results_suggests_rephrasing(self, monkeypatch, capsys):
        result = hub_client.PullResult(written_paths=[], n_found=0, ignored_terms=["the"])

        async def fake_pull(hub, key, root, query, tags):
            return result

        monkeypatch.setattr(hub_client, "pull_search_results", fake_pull)
        sync_cmd.run(_args(pull=True))
        assert "Try a more specific word" in capsys.readouterr().out

    def test_no_promote_hint_when_nothing_was_written(self, monkeypatch, capsys):
        result = hub_client.PullResult(written_paths=[], n_found=2)

        async def fake_pull(hub, key, root, query, tags):
            return result

        monkeypatch.setattr(hub_client, "pull_search_results", fake_pull)
        sync_cmd.run(_args(pull=True))
        assert "Promote a candidate" not in capsys.readouterr().out

    def test_tags_are_parsed_from_a_comma_separated_string(self, monkeypatch):
        captured = {}

        async def fake_pull(hub, key, root, query, tags):
            captured["tags"] = tags
            return hub_client.PullResult()

        monkeypatch.setattr(hub_client, "pull_search_results", fake_pull)
        sync_cmd.run(_args(pull=True, tags=" a, b ,,c "))
        assert captured["tags"] == ["a", "b", "c"]

    def test_empty_tags_string_is_an_empty_list_not_a_list_with_one_empty_string(self, monkeypatch):
        """"".split(",") is [""], not [] -- a well-known Python gotcha.
        Confirmed handled rather than assumed, since a stray "" tag would
        silently change what search_traces filters on."""
        captured = {}

        async def fake_pull(hub, key, root, query, tags):
            captured["tags"] = tags
            return hub_client.PullResult()

        monkeypatch.setattr(hub_client, "pull_search_results", fake_pull)
        sync_cmd.run(_args(pull=True, tags=""))
        assert captured["tags"] == []


class TestErrorHandling:
    """HubClientUnavailable/HubConnectionError must produce a clean exit 1
    and message, not a raw traceback -- the exact failure mode a CLI
    someone put in a cron job or an agent's own tool loop should never hit."""

    def test_hub_client_unavailable_is_a_clean_exit_1_not_a_traceback(self, monkeypatch, capsys):
        async def fake_push(hub, key, root):
            raise hub_client.HubClientUnavailable("needs the mcp extra")

        monkeypatch.setattr(hub_client, "push_active_lessons", fake_push)
        assert sync_cmd.run(_args(push=True)) == 1
        assert "needs the mcp extra" in capsys.readouterr().err

    def test_hub_connection_error_is_a_clean_exit_1_not_a_traceback(self, monkeypatch, capsys):
        async def fake_pull(hub, key, root, query, tags):
            raise hub_client.HubConnectionError("could not reach the Hub")

        monkeypatch.setattr(hub_client, "pull_search_results", fake_pull)
        assert sync_cmd.run(_args(pull=True)) == 1
        assert "sync failed" in capsys.readouterr().err

    def test_a_push_error_does_not_prevent_the_pull_from_still_running(self, monkeypatch):
        """push and pull are independent legs of one `sync` call (the
        default runs both) -- a HubConnectionError raised INSIDE
        push_active_lessons's own per-lesson handling is caught there and
        turned into a PushResult.error, so it must never abort the pull
        leg that follows. Only a raise ESCAPING push_active_lessons itself
        (not modeled here; see the two tests above for that case) would."""
        pull_calls = []

        async def fake_push(hub, key, root):
            return [hub_client.PushResult(slug="a", hub_trace_id=None, error="boom")]

        async def fake_pull(hub, key, root, query, tags):
            pull_calls.append(1)
            return hub_client.PullResult()

        monkeypatch.setattr(hub_client, "push_active_lessons", fake_push)
        monkeypatch.setattr(hub_client, "pull_search_results", fake_pull)
        assert sync_cmd.run(_args()) == 0
        assert pull_calls == [1]
