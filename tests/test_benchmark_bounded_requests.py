"""Actual HTTP contracts for benchmark caps, retries and multi-call accounting."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from benchmarks.cache import BenchmarkCache, CostGuard
from benchmarks.conversation_bench import grade_answer
from benchmarks.requests import bounded_complete
from commontrace import llm


@pytest.fixture
def provider():
    calls, replies = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append({"method": "GET", "authorization": self.headers.get("Authorization")})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            status, response = replies.pop(0) if replies else (200, {
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            })
            self.send_response(status)
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = llm.Config("openai-compatible", "fixture", "local-only", base_url=f"http://127.0.0.1:{server.server_port}")
    try:
        yield config, calls, replies
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


PRICES = {"fixture": {"input_per_mtok": 0, "output_per_mtok": 1_000_000}}


def test_output_cap_is_sent_and_remaining_budget_refuses_before_dispatch(provider):
    config, calls, _ = provider
    guard = CostGuard(2, PRICES)
    answer, usage, cost = bounded_complete("query", config, guard, output_limit=2)
    assert answer == "answer" and usage["output_tokens"] == 1 and cost == 1
    assert calls[0]["max_tokens"] == 2 and calls[0]["temperature"] == 0
    assert guard.reserved_cost_usd == 0
    with pytest.raises(RuntimeError, match="remaining"):
        bounded_complete("second", config, guard, output_limit=2)
    assert len(calls) == 1


def test_full_history_is_reserved_not_the_nominal_retrieval_budget(provider):
    config, calls, _ = provider
    guard = CostGuard(.01, {"fixture": {"input_per_mtok": 1, "output_per_mtok": 1}})
    with pytest.raises(RuntimeError, match="remaining"):
        bounded_complete("long history " * 10000, config, guard)
    assert calls == []


@pytest.mark.parametrize("reply", [
    (429, {"error": "retry later"}),
    (200, {"choices": [{"message": {"content": "answer"}}]}),
    (200, {"choices": [{"message": {"content": "answer"}}],
           "usage": {"prompt_tokens": 2, "completion_tokens": 99}}),
])
def test_failed_or_unbounded_usage_retains_charge_and_never_retries(provider, reply):
    config, calls, replies = provider
    replies.append(reply)
    guard = CostGuard(10, PRICES)
    with pytest.raises((llm.LLMUnavailable, ValueError)):
        bounded_complete("query", config, guard, output_limit=2)
    assert len(calls) == 1 and guard.uncertain_calls == 1 and guard.reserved_cost_usd == 2
    with pytest.raises(RuntimeError, match="uncertain"):
        bounded_complete("next", config, guard, output_limit=2)
    assert len(calls) == 1


def test_unknown_price_cannot_be_reported_as_free(provider):
    config, calls, _ = provider
    with pytest.raises(ValueError, match="no benchmark price"):
        bounded_complete("query", config, CostGuard(10))
    assert calls == []


def test_redirect_cannot_forward_credentials_or_create_a_second_request(provider):
    config, calls, _ = provider
    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", config.base_url + "/stolen")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    guarded = llm.Config(config.provider, config.model, "must-not-leave-origin",
                         base_url=f"http://127.0.0.1:{server.server_port}")
    guard = CostGuard(10, PRICES)
    try:
        with pytest.raises(llm.LLMUnavailable):
            bounded_complete("query", guarded, guard, output_limit=2)
        assert not calls and guard.uncertain_calls == 1
        assert guard.reserved_cost_usd == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_every_rubric_call_uses_same_live_guard(provider, monkeypatch):
    from benchmarks import conversation_bench as bench

    config, calls, _ = provider
    monkeypatch.setattr(bench, "_get_llm_config", lambda model: config)

    class Rubric:
        name = profile = "fixture"

        def grade(self, question, answer, *, complete_fn, model):
            for i in range(3):
                complete_fn(f"rubric {i}")
            return {"correct": True, "score": 1}

    guard = CostGuard(4, PRICES)
    with pytest.raises(RuntimeError, match="remaining"):
        grade_answer({"question": "q", "answer": "a"}, "context", None, "fixture", "fixture",
                     Rubric(), None, guard, output_limit=2)
    assert len(calls) == 3 and guard.total_cost_usd == 3


def test_cache_hit_has_zero_current_spend_and_separate_historical_cost(provider, monkeypatch, tmp_path):
    from benchmarks import conversation_bench as bench

    config, calls, _ = provider
    monkeypatch.setattr(bench, "_get_llm_config", lambda model: config)

    class Rubric:
        name = profile = "fixture"

        def grade(self, question, answer, *, complete_fn, model):
            complete_fn("rubric")
            return {"correct": True, "score": 1}

    cache = BenchmarkCache(str(tmp_path))
    first = grade_answer({"question": "q", "answer": "a"}, "context", None, "fixture", "fixture",
                         Rubric(), cache, CostGuard(10, PRICES), output_limit=2)
    guard = CostGuard(0, PRICES)
    second = grade_answer({"question": "q", "answer": "a"}, "context", None, "fixture", "fixture",
                          Rubric(), cache, guard, output_limit=2)
    assert len(calls) == 2 and first["answer_cost_usd"] == first["judge_cost_usd"] == 1
    assert second["answer_cost_usd"] == second["judge_cost_usd"] == 0
    assert second["historical_cached_cost_usd"] == 2
    assert guard.snapshot()["cache_hits"] == 2 and guard.total_cost_usd == 0
