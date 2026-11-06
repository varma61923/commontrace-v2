from __future__ import annotations

import asyncio
import json

import pytest

from unimem import llm, session_ledger
from unimem.commands import session_ledger_cmd
from unimem.llm_runtime import ACTIVE, Budget, LLMRuntime


@pytest.mark.parametrize("error", [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit(1)])
def test_interrupted_dispatch_stops_subsequent_accounting_sensitive_calls(tmp_path, error):
    calls = []

    def interrupted(*_args, **_kwargs):
        calls.append(True)
        raise error

    runtime = LLMRuntime(
        str(tmp_path), "cancel", routes={"default": llm.Config("ollama", "m", "")}, complete=interrupted
    )
    with runtime.scope():
        with pytest.raises(type(error)) as caught:
            runtime.complete("question")
        assert ACTIVE.get() is runtime
    assert caught.value is error
    assert runtime.manifest()["accounting_unknown"] and runtime.manifest()["failed_calls"] == 1
    with pytest.raises(llm.LLMUnavailable, match="budget exhausted"):
        runtime.complete("question")
    assert calls == [True]


def test_failed_usage_persistence_preserves_error_and_stops_further_provider_spend(tmp_path, monkeypatch):
    calls = []
    failure = OSError("journal unavailable")

    def completed(*_args, **_kwargs):
        calls.append(True)
        return "answer", {"prompt_tokens": 10, "completion_tokens": 5}

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(session_ledger, "record_usage", fail)
    monkeypatch.setattr(llm, "cost_usd", lambda *_args: 0.125)
    runtime = LLMRuntime(
        str(tmp_path), "persist", routes={"default": llm.Config("ollama", "m", "")}, complete=completed
    )
    with pytest.raises(OSError) as caught:
        runtime.complete("question")
    assert caught.value is failure
    assert runtime.cost == 0.125 and runtime.tokens == 15
    assert runtime.manifest()["accounting_unknown"] and runtime.failed == 1
    with pytest.raises(llm.LLMUnavailable, match="budget exhausted"):
        runtime.complete("next question")
    assert calls == [True]


@pytest.mark.parametrize("price", [float("nan"), float("inf"), -1, True, 10**400])
def test_invalid_prices_remain_unknown_and_cannot_bypass_a_cost_budget(tmp_path, monkeypatch, price):
    table = tmp_path / "prices.json"
    table.write_text(json.dumps({"m": {"input_per_mtok": price, "output_per_mtok": 1}}))
    monkeypatch.setenv("UNIMEM_LLM_PRICES", str(table))
    calls = []

    def complete(*_args, **_kwargs):
        calls.append(True)
        return "answer", {"prompt_tokens": 10, "completion_tokens": 5}

    runtime = LLMRuntime(
        str(tmp_path),
        "prices",
        routes={"default": llm.Config("ollama", "m", "")},
        budget=Budget(cost_usd=1),
        complete=complete,
    )
    assert runtime.complete("question")[0] == "answer"
    assert runtime.unpriced and runtime.cost == 0
    with pytest.raises(llm.LLMUnavailable, match="budget exhausted"):
        runtime.complete("next question")
    assert calls == [True]


def test_cached_completion_is_historical_usage_not_fresh_spend(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIMEM_LLM_CACHE", "1")
    monkeypatch.setenv("UNIMEM_LLM_CACHE_PATH", str(tmp_path / "llm-cache.db"))
    calls = []

    def provider(_config, _prompt, _caller):
        calls.append(1)
        return "answer", {"prompt_tokens": 10, "completion_tokens": 5}

    monkeypatch.setattr(llm, "_provider_call", provider)
    monkeypatch.setattr(llm, "cost_usd", lambda _usage, _model: 0.125)
    route = llm.Config("ollama", "priced-local-model", "")
    runtime = LLMRuntime(
        str(tmp_path / "memory"),
        "cached-session",
        routes={"default": route},
        budget=Budget(calls=3, tokens=100, cost_usd=0.2),
    )

    assert runtime.complete("same prompt")[0] == "answer"
    monkeypatch.setattr(llm, "cost_usd", lambda _usage, _model: 0.9)
    assert runtime.complete("same prompt")[0] == "answer"

    summary = session_ledger.session_summary(str(tmp_path / "memory"), "cached-session")
    assert calls == [1]
    assert runtime.cost == 0.125
    assert runtime.cached_calls == 1
    assert summary["total_cost_usd"] == 0.125
    assert summary["historical_cached_cost_usd"] == 0.125
    assert summary["cache_hit_count"] == 1
    assert summary["cached_tokens"] == 15


def test_cached_completion_without_historical_price_keeps_current_spend_known(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIMEM_LLM_CACHE", "1")
    monkeypatch.setenv("UNIMEM_LLM_CACHE_PATH", str(tmp_path / "llm-cache.db"))
    monkeypatch.setattr(llm, "_provider_call", lambda *_args: ("answer", {"prompt_tokens": 10, "completion_tokens": 5}))
    monkeypatch.setattr(llm, "cost_usd", lambda _usage, _model: None)
    route = llm.Config("ollama", "unpriced-local-model", "")
    llm.complete("same prompt", route)
    runtime = LLMRuntime(
        str(tmp_path),
        "cached-unpriced-session",
        routes={"default": route},
        budget=Budget(calls=3, tokens=100, cost_usd=1),
    )

    runtime.complete("same prompt")

    summary = session_ledger.session_summary(str(tmp_path), "cached-unpriced-session")
    assert summary["total_cost_usd"] == 0
    assert summary["unpriced_calls"] == 0
    assert summary["cache_hit_count"] == 1
    assert summary["cached_unpriced_calls"] == 1
    assert summary["historical_cached_cost_usd"] is None
    assert runtime.unpriced_calls == 0 and runtime.cached_unpriced_calls == 1


def test_unpriced_runtime_calls_make_spend_unknown_and_cli_stays_readable(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(llm, "cost_usd", lambda _usage, _model: None)
    route = llm.Config("ollama", "unpriced-local-model", "")
    runtime = LLMRuntime(
        str(tmp_path),
        "unpriced-session",
        routes={"default": route},
        budget=Budget(calls=2, tokens=100),
        complete=lambda _prompt, **_kwargs: ("answer", {"prompt_tokens": 10, "completion_tokens": 5}),
    )

    runtime.complete("question")
    summary = session_ledger.session_summary(str(tmp_path), "unpriced-session")
    overall = session_ledger.overall_ledger_summary(str(tmp_path))
    assert summary["total_cost_usd"] is None
    assert summary["known_cost_usd"] == 0
    assert summary["unpriced_calls"] == 1
    assert overall["total_cost_usd"] is None and overall["unpriced_calls"] == 1
    assert runtime.manifest()["unpriced_calls"] == 1

    assert (
        session_ledger_cmd.run_show(
            type("Args", (), {"dest": str(tmp_path), "session_id": "unpriced-session", "json": False})()
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Unknown" in output and "1 unpriced call" in output
