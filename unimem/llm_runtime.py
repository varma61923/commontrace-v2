"""Per-prompt routing with explicit call/token/cost budgets and measured usage."""
from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace

from unimem import llm, session_ledger
from unimem.configuration import Budget

ACTIVE: ContextVar["LLMRuntime | None"] = ContextVar("unimem_llm_runtime", default=None)
PURPOSE: ContextVar[str] = ContextVar("unimem_llm_purpose", default="default")


@contextmanager
def purpose(name: str):
    token = PURPOSE.set(name)
    try:
        yield
    finally:
        PURPOSE.reset(token)


class BudgetExceeded(llm.LLMUnavailable):
    pass




class LLMRuntime:
    """Routing is supplied as Config objects; credentials never enter manifests.

    Calls are capped before dispatch. Tokens and cost stop the next call when
    reported usage reaches the cap; providers can overshoot on the last call.
    """
    def __init__(self, root: str, session_id: str, *, routes: dict[str, llm.Config] | None = None,
                 budget: Budget | None = None, complete=None):
        self.root, self.session_id = root, session_id
        from unimem import configuration
        configured = configuration.current() or configuration.load(root)
        self.model_routes = configured['model_routes']
        self.routes = dict(routes or {})
        self.selected_routes: dict[str, dict[str, str]] = {}
        self.budget = budget or Budget(**configured['llm_budget'])
        self.caller = complete or llm.complete
        self.calls = self.tokens = 0
        self.cost = 0.0
        self.cached_calls = self.cached_tokens = 0
        self.cached_historical_cost = 0.0
        self.cached_unpriced_calls = 0
        self.unpriced_calls = 0
        self.failed = 0
        self.unpriced = False
        self.accounting_unknown = False
        self.started = time.monotonic()
        self._lock = threading.Lock()

    @contextmanager
    def scope(self):
        token = ACTIVE.set(self)
        try:
            yield self
        finally:
            ACTIVE.reset(token)

    def complete(self, prompt: str, *, purpose: str = "default", config: llm.Config | None = None) -> tuple[str, dict]:
        with self._lock:
            if self.accounting_unknown or self.calls >= self.budget.calls or self.tokens >= self.budget.tokens or (
                    self.budget.cost_usd is not None and (self.unpriced or self.cost >= self.budget.cost_usd)) or (
                    time.monotonic() - self.started >= self.budget.seconds):
                raise BudgetExceeded("LLM budget exhausted")
            cfg = self.routes.get(purpose, self.routes.get("default")) or config or llm.load_config()
            if purpose not in self.routes and purpose in self.model_routes:
                cfg = replace(cfg, model=self.model_routes[purpose])
            # Keep the actual provider/model decision in the run manifest.
            # This is especially useful when model_routes comes from project
            # config and the base provider config itself contains credentials.
            self.selected_routes[purpose] = {"provider": cfg.provider, "model": cfg.model}
            self.calls += 1
            token = ACTIVE.set(None)
            try:
                text, usage = self.caller(prompt, config=cfg)
            except Exception:
                self.failed += 1
                self.accounting_unknown = self.unpriced = True
                raise
            finally:
                ACTIVE.reset(token)
            input_tokens = usage.get("prompt_tokens", usage.get("input_tokens")) if isinstance(usage, dict) else None
            output_tokens = (usage.get("completion_tokens", usage.get("output_tokens"))
                             if isinstance(usage, dict) else None)
            if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in (input_tokens, output_tokens)):
                self.accounting_unknown = self.unpriced = True
                self.failed += 1
                raise llm.LLMUnavailable("provider usage is missing or invalid; further dispatch stopped")
            cache_hit = isinstance(usage, dict) and usage.get("_unimem_cache_hit") is True
            self.tokens += input_tokens + output_tokens
            if cache_hit:
                cached_cost = usage.get("_unimem_cached_cost_usd")
                cost = (float(cached_cost) if isinstance(cached_cost, (float, int))
                        and not isinstance(cached_cost, bool) and math.isfinite(cached_cost)
                        and cached_cost >= 0 else None)
            else:
                cost = llm.cost_usd({"input_tokens": input_tokens, "output_tokens": output_tokens}, cfg.model)
            if cache_hit:
                self.cached_calls += 1
                self.cached_tokens += input_tokens + output_tokens
                if cost is None:
                    self.cached_unpriced_calls += 1
                else:
                    self.cached_historical_cost += cost
            elif cost is None:
                # Unpriced provider calls cannot silently evade a monetary budget.
                self.unpriced = True
                self.unpriced_calls += 1
            else:
                self.cost += cost
            ledger_cost = 0.0 if cache_hit or cost is None else cost
            session_ledger.record_usage(self.root, self.session_id, cfg.model, input_tokens,
                                        output_tokens, provider=cfg.provider, cost_usd=ledger_cost,
                                        occasion=purpose,
                                        metadata={"cost_known": cache_hit or cost is not None,
                                                  "cache_hit": cache_hit,
                                                  "historical_cost_known": cost is not None if cache_hit else None,
                                                  "historical_cost_usd": (
                                                      cost if cache_hit and cost is not None else None)})
            return text, usage

    def manifest(self) -> dict:
        return {"routes": {key: {"provider": cfg.provider, "model": cfg.model}
                           for key, cfg in self.routes.items()},
                "configured_model_routes": dict(self.model_routes),
                "selected_routes": {key: dict(value) for key, value in self.selected_routes.items()},
                "calls": self.calls, "failed_calls": self.failed, "tokens": self.tokens,
                "known_cost_usd": self.cost, "unpriced_calls": self.unpriced_calls,
                "cached_calls": self.cached_calls, "cached_tokens": self.cached_tokens,
                "historical_cached_cost_usd": self.cached_historical_cost,
                "cached_unpriced_calls": self.cached_unpriced_calls,
                "accounting_unknown": self.accounting_unknown,
                "elapsed_seconds": time.monotonic() - self.started}
