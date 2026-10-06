"""Tests for Session Ledger: cost and token tracking with per-model attribution."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from commontrace import session_ledger

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "commontrace.cli", *argv],
        capture_output=True, text=True, cwd=REPO_ROOT, check=False,
    )


@pytest.fixture
def store(tmp_path):
    root = str(tmp_path / "fleet")
    assert cli("init", "--dest", root, "--agent-type", "coding").returncode == 0
    return root


def test_session_ledger_usage(store):
    e1 = session_ledger.record_usage(
        store, "session_test", "gpt-4o",
        prompt_tokens=2000, completion_tokens=500,
        provider="openai", occasion="qa",
    )
    assert e1.prompt_tokens == 2000
    assert e1.completion_tokens == 500
    assert e1.total_tokens == 2500
    assert e1.cost_usd > 0

    e2 = session_ledger.record_usage(
        store, "session_test", "claude-sonnet-5",
        prompt_tokens=1000, completion_tokens=100,
        provider="anthropic", occasion="summarize",
    )
    assert e2.total_tokens == 1100

    # Summary
    summary = session_ledger.session_summary(store, "session_test")
    assert summary["session_id"] == "session_test"
    assert summary["call_count"] == 2
    assert summary["prompt_tokens"] == 3000
    assert summary["completion_tokens"] == 600
    assert summary["total_tokens"] == 3600
    assert "gpt-4o" in summary["by_model"]
    assert "claude-sonnet-5" in summary["by_model"]

    # Global summary
    overall = session_ledger.overall_ledger_summary(store)
    assert overall["call_count"] == 2
    assert overall["total_tokens"] == 3600


def test_session_ledger_cli_commands(store):
    res_rec = cli("session-ledger", "record", "sess_cli_1",
                  "--model", "gpt-4.1-mini", "--prompt-tokens", "1000", "--completion-tokens", "200",
                  "--occasion", "benchmark", "--dest", store)
    assert res_rec.returncode == 0
    assert "Recorded 1,200 tokens" in res_rec.stdout

    res_show = cli("session-ledger", "show", "sess_cli_1", "--dest", store)
    assert res_show.returncode == 0
    assert "sess_cli_1" in res_show.stdout
    assert "gpt-4.1-mini" in res_show.stdout

    res_sum = cli("session-ledger", "summary", "--dest", store)
    assert res_sum.returncode == 0
    assert "Global Session Ledger Summary" in res_sum.stdout
