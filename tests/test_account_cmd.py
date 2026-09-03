"""Tests for `commontrace account` -- the client half of self-service
deletion. Every Hub call is monkeypatched here; the actual deletion
semantics (org-scoping, the grace period, token matching) are covered
against a real Postgres instance in hub/tests/test_self_service_deletion.py.
This file is about the CLI's own responsibilities: the confirmation gate
on irreversible actions, and reporting the Hub's response accurately.
"""
from __future__ import annotations

import argparse

from commontrace.commands import account_cmd


def _args(**over):
    base = dict(hub_url="http://hub.invalid/mcp", hub_api_key="ct_live_test", yes=False)
    base.update(over)
    return argparse.Namespace(**base)


class TestDeleteTraceConfirmationGate:
    def test_refuses_without_yes_when_stdin_is_not_a_tty(self, capsys, monkeypatch):
        monkeypatch.setattr(account_cmd.sys.stdin, "isatty", lambda: False)
        called = []
        monkeypatch.setattr(account_cmd.hub_client, "delete_trace", lambda *a: called.append(a))
        assert account_cmd.run_delete_trace(_args(trace_id="t1")) == 1
        assert "refusing" in capsys.readouterr().err.lower()
        assert called == [], "must not have contacted the Hub at all"

    def test_yes_flag_bypasses_the_prompt(self, capsys, monkeypatch):
        async def fake_delete(hub, key, trace_id):
            return True

        monkeypatch.setattr(account_cmd.hub_client, "delete_trace", fake_delete)
        assert account_cmd.run_delete_trace(_args(trace_id="t1", yes=True)) == 0
        assert "permanently deleted" in capsys.readouterr().out.lower()

    def test_typing_yes_at_the_prompt_proceeds(self, capsys, monkeypatch):
        monkeypatch.setattr(account_cmd.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt: "yes")

        async def fake_delete(hub, key, trace_id):
            return True

        monkeypatch.setattr(account_cmd.hub_client, "delete_trace", fake_delete)
        assert account_cmd.run_delete_trace(_args(trace_id="t1")) == 0

    def test_typing_anything_else_refuses(self, capsys, monkeypatch):
        monkeypatch.setattr(account_cmd.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        called = []
        monkeypatch.setattr(account_cmd.hub_client, "delete_trace", lambda *a: called.append(a))
        assert account_cmd.run_delete_trace(_args(trace_id="t1")) == 1
        assert called == []

    def test_a_not_found_response_is_reported_cleanly(self, capsys, monkeypatch):
        async def fake_delete(hub, key, trace_id):
            return False

        monkeypatch.setattr(account_cmd.hub_client, "delete_trace", fake_delete)
        assert account_cmd.run_delete_trace(_args(trace_id="t1", yes=True)) == 1
        assert "no such trace" in capsys.readouterr().err.lower()

    def test_a_hub_error_is_reported_not_raised(self, capsys, monkeypatch):
        async def fake_delete(hub, key, trace_id):
            raise account_cmd.hub_client.HubConnectionError("delete_trace failed: internal_error")

        monkeypatch.setattr(account_cmd.hub_client, "delete_trace", fake_delete)
        assert account_cmd.run_delete_trace(_args(trace_id="t1", yes=True)) == 1
        assert "internal_error" in capsys.readouterr().err


class TestRequestDeletion:
    def test_needs_no_confirmation_and_reports_the_token(self, capsys, monkeypatch):
        async def fake_request(hub, key):
            return {
                "confirmation_token": "ctd_abc123",
                "confirm_not_before": "2026-01-01T00:05:00+00:00",
                "expires_at": "2026-01-02T00:00:00+00:00",
            }

        monkeypatch.setattr(account_cmd.hub_client, "request_account_deletion", fake_request)
        assert account_cmd.run_request_deletion(_args()) == 0
        out = capsys.readouterr().out
        assert "ctd_abc123" in out
        assert "NOTHING has been deleted yet" in out
        assert "cancel-deletion" in out

    def test_a_hub_error_is_reported_not_raised(self, capsys, monkeypatch):
        async def fake_request(hub, key):
            raise account_cmd.hub_client.HubConnectionError("boom")

        monkeypatch.setattr(account_cmd.hub_client, "request_account_deletion", fake_request)
        assert account_cmd.run_request_deletion(_args()) == 1
        assert "boom" in capsys.readouterr().err


class TestCancelDeletion:
    def test_reports_when_something_was_cancelled(self, capsys, monkeypatch):
        async def fake_cancel(hub, key):
            return True

        monkeypatch.setattr(account_cmd.hub_client, "cancel_account_deletion", fake_cancel)
        assert account_cmd.run_cancel_deletion(_args()) == 0
        assert "cancelled" in capsys.readouterr().out.lower()

    def test_reports_when_nothing_was_pending(self, capsys, monkeypatch):
        async def fake_cancel(hub, key):
            return False

        monkeypatch.setattr(account_cmd.hub_client, "cancel_account_deletion", fake_cancel)
        assert account_cmd.run_cancel_deletion(_args()) == 0
        assert "no pending" in capsys.readouterr().out.lower()


class TestConfirmDeletionConfirmationGate:
    def test_refuses_without_yes_when_stdin_is_not_a_tty(self, capsys, monkeypatch):
        monkeypatch.setattr(account_cmd.sys.stdin, "isatty", lambda: False)
        called = []
        monkeypatch.setattr(
            account_cmd.hub_client, "confirm_account_deletion", lambda *a: called.append(a)
        )
        assert account_cmd.run_confirm_deletion(_args(confirmation_token="tok")) == 1
        assert "refusing" in capsys.readouterr().err.lower()
        assert called == []

    def test_yes_flag_bypasses_the_prompt_and_deletes(self, capsys, monkeypatch):
        async def fake_confirm(hub, key, token):
            return None

        monkeypatch.setattr(account_cmd.hub_client, "confirm_account_deletion", fake_confirm)
        assert account_cmd.run_confirm_deletion(_args(confirmation_token="tok", yes=True)) == 0
        assert "permanently deleted" in capsys.readouterr().out.lower()

    def test_a_deletion_not_ready_error_is_reported_not_raised(self, capsys, monkeypatch):
        async def fake_confirm(hub, key, token):
            raise account_cmd.hub_client.HubConnectionError(
                "confirm_account_deletion failed: deletion_not_ready: too soon"
            )

        monkeypatch.setattr(account_cmd.hub_client, "confirm_account_deletion", fake_confirm)
        assert account_cmd.run_confirm_deletion(_args(confirmation_token="tok", yes=True)) == 1
        assert "deletion_not_ready" in capsys.readouterr().err
