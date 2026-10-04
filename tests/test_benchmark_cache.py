"""Unit tests for benchmark disk cache and cost guard."""
import pytest

from benchmarks.cache import BenchmarkCache, CostGuard, compute_cost_usd, get_price, prompt_hash


def test_prompt_hash():
    p1 = "What is the capital of France?"
    p2 = "What is the capital of France?"
    p3 = "What is the capital of Germany?"
    assert prompt_hash(p1) == prompt_hash(p2)
    assert prompt_hash(p1) != prompt_hash(p3)
    assert len(prompt_hash(p1)) == 64


def test_get_price_defaults():
    in_p, out_p = get_price("gpt-4o")
    assert in_p == 2.50
    assert out_p == 10.00

    in_p, out_p = get_price("gpt-4o-mini-2024-07-18")
    assert in_p == 0.15
    assert out_p == 0.60

    in_p, out_p = get_price("unknown-model-xyz")
    assert in_p == 0.0
    assert out_p == 0.0


def test_compute_cost_usd():
    usage = {"input_tokens": 1000, "output_tokens": 200}
    # gpt-4o: 1000 * 2.50 / 1e6 + 200 * 10.00 / 1e6 = 0.0025 + 0.002 = 0.0045
    cost = compute_cost_usd(usage, "gpt-4o")
    assert cost == 0.0045


def test_benchmark_cache_put_get(tmp_path):
    cache = BenchmarkCache(str(tmp_path / "cache"))
    assert cache.get("gpt-4o", "test prompt") is None

    cache.put("gpt-4o", "test prompt", "Paris", {"input_tokens": 10, "output_tokens": 5}, cost_usd=0.0001)

    hit = cache.get("gpt-4o", "test prompt")
    assert hit is not None
    resp, usage, cost = hit
    assert resp == "Paris"
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 5
    assert cost == 0.0001


def test_benchmark_cache_complete_cached(tmp_path):
    cache = BenchmarkCache(str(tmp_path / "cache"))
    call_count = 0

    def fake_complete(prompt: str, model: str) -> tuple[str, dict]:
        nonlocal call_count
        call_count += 1
        return f"Answer to {prompt}", {"input_tokens": 20, "output_tokens": 10}

    # First call: miss
    resp1, usage1, cost1, cached1 = cache.complete_cached("gpt-4o", "q1", fake_complete)
    assert not cached1
    assert call_count == 1
    assert resp1 == "Answer to q1"

    # Second call: hit
    resp2, usage2, cost2, cached2 = cache.complete_cached("gpt-4o", "q1", fake_complete)
    assert cached2
    assert call_count == 1
    assert resp2 == "Answer to q1"

    # Persistence check with new cache instance
    cache2 = BenchmarkCache(str(tmp_path / "cache"))
    resp3, usage3, cost3, cached3 = cache2.complete_cached("gpt-4o", "q1", fake_complete)
    assert cached3
    assert call_count == 1
    assert resp3 == "Answer to q1"

    stats = cache2.stats()
    assert stats["entries"] == 1
    assert stats["total_cost_usd"] > 0


def test_cost_guard_budget():
    guard = CostGuard(max_cost_usd=5.0)

    # 10 questions with gpt-4o should be under $5
    est = guard.check_estimate(10, "gpt-4o", "gpt-4o")
    assert est["num_questions"] == 10
    assert est["estimated_cost_usd"] < 5.0

    # 10,000 questions with gpt-4o should exceed $5
    with pytest.raises(RuntimeError) as exc_info:
        guard.check_estimate(10000, "gpt-4o", "gpt-4o")
    assert "exceeds limit --max-cost" in str(exc_info.value)


def test_cost_guard_recording():
    guard = CostGuard(max_cost_usd=10.0)
    guard.record_call(0.005, is_cached=False)
    guard.record_call(0.005, is_cached=True)
    assert guard.call_count == 2
    assert guard.cached_count == 1
    assert guard.total_cost_usd == 0.005
