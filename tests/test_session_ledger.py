"""Tests for Session Ledger: cost and token tracking with per-model attribution."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from unimem import session_ledger

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "unimem.cli", *argv],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


@pytest.fixture
def store(tmp_path):
    root = str(tmp_path / "fleet")
    assert cli("init", "--dest", root, "--agent-type", "coding").returncode == 0
    return root


def test_session_ledger_usage(store):
    e1 = session_ledger.record_usage(
        store,
        "session_test",
        "gpt-4o",
        prompt_tokens=2000,
        completion_tokens=500,
        provider="openai",
        occasion="qa",
    )
    assert e1.prompt_tokens == 2000
    assert e1.completion_tokens == 500
    assert e1.total_tokens == 2500
    assert e1.cost_usd > 0

    e2 = session_ledger.record_usage(
        store,
        "session_test",
        "claude-sonnet-5",
        prompt_tokens=1000,
        completion_tokens=100,
        provider="anthropic",
        occasion="summarize",
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
    assert summary["by_occasion"]["qa"]["prompt_tokens"] == 2000
    assert summary["by_occasion"]["qa"]["completion_tokens"] == 500
    assert summary["by_occasion"]["qa"]["total_tokens"] == 2500

    # Global summary
    overall = session_ledger.overall_ledger_summary(store)
    assert overall["call_count"] == 2
    assert overall["total_tokens"] == 3600


def test_session_ledger_cli_commands(store):
    res_rec = cli(
        "session-ledger",
        "record",
        "sess_cli_1",
        "--model",
        "gpt-4.1-mini",
        "--prompt-tokens",
        "1000",
        "--completion-tokens",
        "200",
        "--occasion",
        "benchmark",
        "--dest",
        store,
    )
    assert res_rec.returncode == 0
    assert "Recorded 1,200 tokens" in res_rec.stdout

    res_show = cli("session-ledger", "show", "sess_cli_1", "--dest", store)
    assert res_show.returncode == 0
    assert "sess_cli_1" in res_show.stdout
    assert "gpt-4.1-mini" in res_show.stdout

    res_sum = cli("session-ledger", "summary", "--dest", store)
    assert res_sum.returncode == 0
    assert "Global Session Ledger Summary" in res_sum.stdout


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-4o", 2.5),
        ("gpt-4o-mini", 0.15),
        (" OpenAI/GPT-4o-mini ", 0.15),
        ("openai:gpt-4o-mini-2024-07-18", 0.15),
        ("gpt-4o-2024-08-06", 2.5),
        ("gpt-4-turbo-preview", 10.0),
        ("claude-3-5-sonnet-latest", 3.0),
        ("us.anthropic.claude-3-5-sonnet-20241022-v2:0", 3.0),
        ("gpt", 1.0),
        ("mini", 1.0),
        ("prefix-gpt-4o", 1.0),
        ("gpt-4o-miniature", 1.0),
        ("", 1.0),
        ("unpriced-model", 1.0),
    ],
)
def test_cost_estimation_matches_specific_models_and_bounded_aliases(model, expected):
    assert session_ledger.estimate_cost(model, 1_000_000, 0) == expected


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), -float("inf"), -0.01, True, 10**400])
def test_invalid_explicit_cost_cannot_enter_the_ledger(tmp_path, cost):
    with pytest.raises(ValueError, match="finite nonnegative"):
        session_ledger.record_usage(str(tmp_path), "session", "gpt-4o", 20, 10, cost_usd=cost)
    assert not (tmp_path / "memory" / "session_ledger.jsonl").exists()


def test_invalid_cli_cost_refuses_before_recording(store):
    result = cli(
        "session-ledger",
        "record",
        "bad-cost",
        "--model",
        "gpt-4o",
        "--prompt-tokens",
        "20",
        "--completion-tokens",
        "10",
        "--cost",
        "nan",
        "--dest",
        store,
    )
    assert result.returncode == 1
    assert "finite nonnegative" in result.stderr
    assert session_ledger.session_summary(store, "bad-cost")["call_count"] == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cost_usd", float("nan")),
        ("cost_usd", "1.20"),
        ("cost_usd", -1.0),
        ("prompt_tokens", True),
        ("completion_tokens", "10"),
        ("total_tokens", 999),
        ("metadata", {"cache_hit": True, "historical_cost_usd": float("inf")}),
    ],
)
def test_corrupt_accounting_rows_fail_closed_without_rewriting_history(tmp_path, field, value):
    entry = session_ledger.record_usage(str(tmp_path), "session", "gpt-4o", 20, 10)
    row = entry.to_dict()
    row[field] = value
    path = tmp_path / "memory" / "session_ledger.jsonl"
    raw = json.dumps(row) + "\n"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt session ledger entry"):
        session_ledger.session_summary(str(tmp_path), "session")
    with pytest.raises(ValueError, match="corrupt session ledger entry"):
        session_ledger.overall_ledger_summary(str(tmp_path))
    assert session_ledger.session_summary(str(tmp_path), "other")["call_count"] == 0
    assert path.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), -0.01, True, "1.20", 10**400])
def test_invalid_known_historical_cost_is_rejected_before_append(tmp_path, cost):
    with pytest.raises(ValueError, match="historical_cost_usd must be a finite nonnegative"):
        session_ledger.record_usage(
            str(tmp_path),
            "session",
            "gpt-4o",
            20,
            10,
            metadata={"cache_hit": True, "historical_cost_usd": cost},
        )
    assert not (tmp_path / "memory" / "session_ledger.jsonl").exists()


def test_aggregate_cost_overflow_raises_instead_of_returning_infinite_spend(tmp_path):
    for _ in range(2):
        session_ledger.record_usage(str(tmp_path), "session", "gpt-4o", 20, 10, cost_usd=1e308)
    with pytest.raises(ValueError, match="total cost must be a finite nonnegative"):
        session_ledger.session_summary(str(tmp_path), "session")


@pytest.mark.parametrize("limit", [0, -1])
def test_nonpositive_listing_limits_return_no_entries(tmp_path, limit):
    session_ledger.record_usage(str(tmp_path), "session", "gpt-4o", 2, 1)
    assert session_ledger.list_session_entries(str(tmp_path), limit=limit) == []


@pytest.mark.parametrize("global_summary,count", [(False, 10_001), (True, 100_001)])
def test_summaries_include_entries_beyond_former_silent_caps(tmp_path, global_summary, count):
    entry = session_ledger.record_usage(str(tmp_path), "session", "gpt-4o", 2, 1, cost_usd=0.01)
    path = tmp_path / "memory" / "session_ledger.jsonl"
    row = entry.to_dict()
    with path.open("w", encoding="utf-8") as journal:
        for i in range(count):
            row["id"] = f"entry-{i}"
            # Date filtering must also reach older rows beyond a listing cap.
            row["timestamp"] = "2026-01-01" if i == 0 else "2026-02-01"
            journal.write(json.dumps(row) + "\n")
    summary = (
        session_ledger.overall_ledger_summary(str(tmp_path))
        if global_summary
        else session_ledger.session_summary(str(tmp_path), "session")
    )
    assert summary["call_count"] == count
    assert summary["total_tokens"] == count * 3
    assert summary["total_cost_usd"] == pytest.approx(count * 0.01)
    if global_summary:
        historical = session_ledger.overall_ledger_summary(str(tmp_path), until="2026-01-31")
        assert historical["call_count"] == 1
    recent = session_ledger.list_session_entries(str(tmp_path), limit=2)
    assert [r.id for r in recent] == [f"entry-{count - 1}", f"entry-{count - 2}"]


@pytest.mark.parametrize("field,value", [("model", []), ("timestamp", 42), ("session_id", {}), ("metadata", [])])
def test_malformed_ledger_fields_fail_with_a_corruption_error(tmp_path, field, value):
    row = session_ledger.record_usage(str(tmp_path), "session", "gpt-4o", 2, 1).to_dict()
    row[field] = value
    path = tmp_path / "memory" / "session_ledger.jsonl"
    raw = json.dumps(row) + "\n"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt session ledger entry"):
        session_ledger.overall_ledger_summary(str(tmp_path))
    assert path.read_text(encoding="utf-8") == raw


def test_summary_closes_stream_when_a_row_is_corrupt(tmp_path, monkeypatch):
    row = session_ledger.record_usage(str(tmp_path), "session", "gpt-4o", 2, 1).to_dict()
    row["cost_usd"] = "invalid"
    closed = []

    def rows(_path):
        try:
            yield row
        finally:
            closed.append(True)

    monkeypatch.setattr(session_ledger._jsonl, "iter_rows", rows)
    with pytest.raises(ValueError, match="corrupt session ledger entry"):
        session_ledger.session_summary(str(tmp_path), "session")
    assert closed == [True]


def test_listing_limit_preserves_newest_order_and_validates_only_selected_rows(tmp_path):
    valid = session_ledger.record_usage(str(tmp_path), "session", "gpt-4o", 2, 1).to_dict()
    corrupt = {**valid, "id": "old", "cost_usd": "invalid"}
    path = tmp_path / "memory" / "session_ledger.jsonl"
    path.write_text(json.dumps(corrupt) + "\n" + json.dumps(valid) + "\n", encoding="utf-8")
    assert [e.id for e in session_ledger.list_session_entries(str(tmp_path), limit=1)] == [valid["id"]]
    with pytest.raises(ValueError, match="corrupt session ledger entry"):
        session_ledger.session_summary(str(tmp_path), "session")
