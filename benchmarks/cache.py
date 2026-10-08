"""Disk cache and cost accounting for benchmark answer generation and judging.

Caches LLM completions keyed by configuration, generation settings and prompt.
Allows interrupted benchmark runs to resume without re-running or re-paying for calls.
Enforces --max-cost USD budget limits before starting.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from dataclasses import asdict, is_dataclass
from typing import Any, Callable

# Default pricing in USD per million tokens (input / output)
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "gpt-4o": {"input_per_mtok": 2.50, "output_per_mtok": 10.00},
    "gpt-4o-2024-08-06": {"input_per_mtok": 2.50, "output_per_mtok": 10.00},
    "gpt-4o-mini": {"input_per_mtok": 0.15, "output_per_mtok": 0.60},
    "gpt-4o-mini-2024-07-18": {"input_per_mtok": 0.15, "output_per_mtok": 0.60},
    "gpt-4.1-mini": {"input_per_mtok": 0.15, "output_per_mtok": 0.60},
    "claude-sonnet-5": {"input_per_mtok": 3.00, "output_per_mtok": 15.00},
    "claude-3-5-sonnet": {"input_per_mtok": 3.00, "output_per_mtok": 15.00},
    "claude-3-5-haiku": {"input_per_mtok": 0.80, "output_per_mtok": 4.00},
}


def prompt_hash(prompt: str) -> str:
    """Deterministic sha256 hex digest of the prompt text."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def completion_binding(config: object, *, generation: dict | None = None) -> dict:
    """Opaque identity for provider/account routing and request implementation.

    Never persist configuration values, credentials, or credential-bearing URLs.
    Old unbound responses cannot satisfy a configuration-bound lookup.
    """
    from commontrace import llm

    fields = asdict(config) if is_dataclass(config) else dict(vars(config))
    if fields.get("provider") in ("bedrock", "vertex") and not fields.get("cache_namespace"):
        raise ValueError("benchmark caching with ambient cloud credentials requires COMMONTRACE_LLM_CACHE_NAMESPACE")
    credential = str(fields.pop("api_key", ""))
    fields["credential_sha256"] = prompt_hash(credential)
    with open(llm.__file__, "rb") as source:
        implementation = hashlib.sha256(source.read()).hexdigest()
    payload = {"schema": 2, "configuration": fields, "generation": generation or {},
               "completion_source_sha256": implementation}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                       allow_nan=False).encode()).hexdigest()
    return {"schema": 2, "identity_sha256": digest}


def get_price(model: str, prices: dict | None = None, *, require_known: bool = False) -> tuple[float, float]:
    """Return (input_per_mtok, output_per_mtok) for a model."""
    if prices is None:
        path = os.environ.get("COMMONTRACE_LLM_PRICES", "").strip()
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    prices = json.load(fh)
            except Exception:
                prices = None
    if isinstance(prices, dict) and model in prices:
        entry = prices[model]
        rates = float(entry["input_per_mtok"]), float(entry["output_per_mtok"])
        if any(not math.isfinite(rate) or rate < 0 for rate in rates):
            raise ValueError("model prices must be finite and nonnegative")
        return rates
    # Fallback to default prices (match longest prefix first)
    for prefix in sorted(DEFAULT_PRICES.keys(), key=len, reverse=True):
        if model.lower().startswith(prefix.lower()):
            pr = DEFAULT_PRICES[prefix]
            return pr["input_per_mtok"], pr["output_per_mtok"]
    if require_known:
        raise ValueError(f"no benchmark price configured for model {model!r}")
    return 0.0, 0.0


def compute_cost_usd(usage: dict, model: str, prices: dict | None = None) -> float:
    """Compute cost in USD given usage dict with input_tokens and output_tokens."""
    in_tok = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    out_tok = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    in_price, out_price = get_price(model, prices)
    return round((in_tok * in_price + out_tok * out_price) / 1_000_000, 6)


class BenchmarkCache:
    """Persistent SQLite-backed cache for LLM responses."""

    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, mode=0o700, exist_ok=True)
        self.db_path = os.path.join(cache_dir, "llm_cache.sqlite3")
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS completions_v2 (
                    binding_sha256 TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    response TEXT NOT NULL,
                    usage_json TEXT NOT NULL,
                    cost_usd REAL NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (binding_sha256, model, prompt_hash)
                )
                """
            )
        os.chmod(self.db_path, 0o600)

    def get(self, model: str, prompt: str, *, binding: dict | None = None) -> tuple[str, dict, float] | None:
        """Lookup cached completion. Returns (response, usage, cost_usd) or None."""
        p_hash = prompt_hash(prompt)
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT response, usage_json, cost_usd FROM completions_v2 "
                "WHERE binding_sha256 = ? AND model = ? AND prompt_hash = ?",
                (self._binding_hash(binding), model, p_hash),
            )
            row = cur.fetchone()
            if row:
                resp, usage_str, cost = row
                try:
                    usage = json.loads(usage_str)
                except Exception:
                    usage = {}
                return resp, usage, cost
        return None

    @staticmethod
    def _binding_hash(binding: dict | None) -> str:
        return prompt_hash(json.dumps(binding if binding is not None else {"unbound": True, "schema": 2},
                                      sort_keys=True, separators=(",", ":"), allow_nan=False))

    def put(self, model: str, prompt: str, response: str, usage: dict, cost_usd: float | None = None,
            *, binding: dict | None = None) -> None:
        """Store completion in cache."""
        p_hash = prompt_hash(prompt)
        if cost_usd is None:
            cost_usd = compute_cost_usd(usage, model)
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO completions_v2
                (binding_sha256, model, prompt_hash, prompt, response, usage_json, cost_usd, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (self._binding_hash(binding), model, p_hash, prompt, response, json.dumps(usage), cost_usd, time.time()),
            )

    def complete_cached(
        self,
        model: str,
        prompt: str,
        complete_fn: Callable[[str, str], tuple[str, dict]],
        *, binding: dict | None = None,
    ) -> tuple[str, dict, float, bool]:
        """Get from cache or invoke complete_fn(prompt, model).

        Returns: (response_text, usage, cost_usd, is_cached)
        """
        hit = self.get(model, prompt, binding=binding)
        if hit is not None:
            resp, usage, cost = hit
            return resp, usage, cost, True

        resp, usage = complete_fn(prompt, model)
        cost = compute_cost_usd(usage, model)
        self.put(model, prompt, resp, usage, cost, binding=binding)
        return resp, usage, cost, False

    def stats(self) -> dict[str, Any]:
        """Return total cached entries and total cost."""
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*), coalesce(sum(cost_usd), 0.0) FROM completions_v2")
            count, total_cost = cur.fetchone()
            return {"entries": count, "total_cost_usd": round(total_cost, 4)}


class CostGuard:
    """Reserve each bounded request before dispatch; uncertain calls stop the run."""

    def __init__(self, max_cost_usd: float | None = None, prices: dict | None = None):
        if max_cost_usd is not None and (not math.isfinite(max_cost_usd) or max_cost_usd < 0):
            raise ValueError("maximum cost must be finite and nonnegative")
        self.max_cost_usd = max_cost_usd
        self.prices = prices
        self.total_cost_usd: float = 0.0
        self.call_count: int = 0
        self.cached_count: int = 0
        self.reserved_cost_usd = 0.0
        self.uncertain_calls = 0
        self.cached_cost_usd = 0.0
        self._lock = threading.RLock()

    def reserve_call(self, model: str, input_upper: int, output_limit: int) -> float:
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0
               for value in (input_upper, output_limit)):
            raise ValueError("token limits must be nonnegative integers")
        rates = get_price(model, self.prices, require_known=True)
        upper_cost = (input_upper * rates[0] + output_limit * rates[1]) / 1_000_000
        with self._lock:
            if self.uncertain_calls:
                raise RuntimeError("benchmark stopped after an uncertain provider charge")
            if self.max_cost_usd is not None and (
                    self.total_cost_usd + self.reserved_cost_usd + upper_cost > self.max_cost_usd):
                raise RuntimeError("next bounded completion exceeds remaining --max-cost budget")
            self.reserved_cost_usd += upper_cost
        return upper_cost

    def settle_call(self, reservation: float, cost: float) -> None:
        with self._lock:
            if not math.isfinite(cost) or cost < 0 or cost > reservation + 1e-12:
                raise ValueError("provider cost exceeds its reserved bound")
            self.reserved_cost_usd = max(0.0, self.reserved_cost_usd - reservation)
            self.record_call(cost)

    def mark_uncertain(self) -> None:
        with self._lock:
            self.uncertain_calls += 1

    def snapshot(self) -> dict:
        return {"current_run_spend_usd": self.total_cost_usd, "reserved_cost_usd": self.reserved_cost_usd,
                "historical_cached_cost_usd": self.cached_cost_usd, "calls": self.call_count,
                "cache_hits": self.cached_count, "uncertain_calls": self.uncertain_calls,
                "max_cost_usd": self.max_cost_usd}

    def record_call(self, cost_usd: float, is_cached: bool = False) -> None:
        if not math.isfinite(cost_usd) or cost_usd < 0:
            raise ValueError("reported cost must be finite and nonnegative")
        self.call_count += 1
        if is_cached:
            self.cached_count += 1
            self.cached_cost_usd += cost_usd
        else:
            self.total_cost_usd += cost_usd

    def check_estimate(
        self,
        num_questions: int,
        answer_model: str,
        judge_model: str,
        avg_context_tokens: int = 2500,
        expected_output_tokens: int = 150,
        judge_calls_per_question: int = 1,
    ) -> dict[str, Any]:
        """Estimate tokens and cost for an upcoming benchmark run.

        Raises RuntimeError if estimated cost exceeds max_cost_usd.
        """
        ans_in_p, ans_out_p = get_price(answer_model, self.prices)
        judge_in_p, judge_out_p = get_price(judge_model, self.prices)

        # Answer estimation
        ans_in_tok = num_questions * avg_context_tokens
        ans_out_tok = num_questions * expected_output_tokens
        ans_cost = (ans_in_tok * ans_in_p + ans_out_tok * ans_out_p) / 1_000_000

        # Judge estimation
        judge_in_tok = num_questions * judge_calls_per_question * (expected_output_tokens + 400)
        judge_out_tok = num_questions * judge_calls_per_question * 50
        judge_cost = (judge_in_tok * judge_in_p + judge_out_tok * judge_out_p) / 1_000_000

        total_cost = round(ans_cost + judge_cost, 4)
        total_tokens = ans_in_tok + ans_out_tok + judge_in_tok + judge_out_tok
        total_calls = num_questions * (1 + judge_calls_per_question)

        estimate = {
            "num_questions": num_questions,
            "answer_model": answer_model,
            "judge_model": judge_model,
            "total_calls": total_calls,
            "total_tokens": total_tokens,
            "estimated_cost_usd": total_cost,
            "answer_cost_usd": round(ans_cost, 4),
            "judge_cost_usd": round(judge_cost, 4),
        }

        if self.max_cost_usd is not None and total_cost > self.max_cost_usd:
            raise RuntimeError(
                f"Estimated cost ${total_cost:.2f} exceeds limit --max-cost ${self.max_cost_usd:.2f} "
                f"for {num_questions} questions ({total_calls} calls, ~{total_tokens} tokens). "
                f"Increase --max-cost or reduce question limit to proceed."
            )

        return estimate
