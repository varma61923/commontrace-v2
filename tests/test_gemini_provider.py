"""Google AI (Gemini/Gemma) provider: thinking is dropped from answers but billed, and only error statuses retry."""
import io
import json
import urllib.error

import pytest

from benchmarks import requests as bench_requests
from benchmarks.cache import CostGuard
from commontrace import llm

PRICES = {"gemma-4-31b-it": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}


def _reply(text="Paris", thought="Let me think.", finish="STOP"):
    parts = ([{"text": thought, "thought": True}] if thought else []) + ([{"text": text}] if text else [])
    return {"candidates": [{"content": {"parts": parts}, "finishReason": finish}],
            "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 3, "thoughtsTokenCount": 40}}


def test_parse_drops_thoughts_and_bills_them():
    text, usage = llm.gemini_parse(_reply())
    assert text == "Paris" and usage == {"input_tokens": 12, "output_tokens": 43, "thinking_tokens": 40}
    with pytest.raises(llm.LLMUnavailable, match="thinking"):
        llm.gemini_parse(_reply(text="", finish="MAX_TOKENS"))
    with pytest.raises(llm.LLMUnavailable, match="no answer"):
        llm.gemini_parse({"promptFeedback": {"blockReason": "SAFETY"}})


def test_request_shape_and_key_requirement(monkeypatch):
    monkeypatch.setenv("COMMONTRACE_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("COMMONTRACE_LLM_MODEL", "gemma-4-31b-it")
    monkeypatch.delenv("COMMONTRACE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("COMMONTRACE_LLM_API_KEY_FILE", raising=False)
    with pytest.raises(llm.LLMUnavailable):
        llm.load_config()
    monkeypatch.setenv("COMMONTRACE_LLM_API_KEY", "k")
    cfg = llm.load_config()
    url, headers, payload = llm.gemini_request(cfg, "hi", max_tokens=64)
    assert url.endswith("/models/gemma-4-31b-it:generateContent") and headers["x-goog-api-key"] == "k"
    assert payload["generationConfig"]["maxOutputTokens"] == 64


class _Opener:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def open(self, request, timeout):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, int):
            raise urllib.error.HTTPError(request.full_url, outcome, "err", {}, io.BytesIO(b"{}"))
        return io.BytesIO(json.dumps(outcome).encode())


def _bounded(monkeypatch, outcomes):
    opener = _Opener(outcomes)
    monkeypatch.setattr(bench_requests.urllib.request, "build_opener", lambda *_a: opener)
    guard = CostGuard(prices=PRICES)
    cfg = llm.Config(provider="gemini", model="gemma-4-31b-it", api_key="k")
    return opener, guard, cfg


def test_bounded_call_retries_only_error_statuses_and_settles_cost(monkeypatch):
    opener, guard, cfg = _bounded(monkeypatch, [500, 429, 503, _reply()])
    text, usage, cost = bench_requests._gemini_complete("q", cfg, guard, output_limit=4096, temperature=0.0,
                                                       sleep=lambda _s: None)
    assert text == "Paris" and opener.calls == 4
    assert cost == pytest.approx((12 * 1.0 + 43 * 2.0) / 1_000_000) and guard.uncertain_calls == 0


def test_other_failures_stop_the_run_with_an_uncertain_charge(monkeypatch):
    opener, guard, cfg = _bounded(monkeypatch, [400])
    with pytest.raises(llm.LLMUnavailable, match="HTTP 400"):
        bench_requests._gemini_complete("q", cfg, guard, output_limit=4096, temperature=0.0, sleep=lambda _s: None)
    assert opener.calls == 1 and guard.uncertain_calls == 1
    opener, guard, cfg = _bounded(monkeypatch, [_reply()])
    with pytest.raises(ValueError, match="bounds"):
        bench_requests._gemini_complete("q", cfg, guard, output_limit=10, temperature=0.0, sleep=lambda _s: None)
