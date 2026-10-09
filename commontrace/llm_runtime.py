"""Per-prompt routing with explicit call/token/cost budgets and measured usage."""
from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from commontrace import llm, session_ledger

ACTIVE: ContextVar["LLMRuntime | None"] = ContextVar("commontrace_llm_runtime", default=None)
PURPOSE: ContextVar[str] = ContextVar("commontrace_llm_purpose", default="default")


@contextmanager
def purpose(name: str):
    token = PURPOSE.set(name)
    try:
        yield
    finally:
        PURPOSE.reset(token)


class BudgetExceeded(llm.LLMUnavailable):
    pass


@dataclass(frozen=True)
class Budget:
    calls: int = 20
    tokens: int = 50000
    cost_usd: float | None = None
    seconds: float = 120.0

    def __post_init__(self):
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0
               for value in (self.calls, self.tokens)) or not math.isfinite(self.seconds) or self.seconds <= 0 or (
                self.cost_usd is not None
                and (not math.isfinite(self.cost_usd) or self.cost_usd < 0)):
            raise ValueError("budgets must be finite and nonnegative")


class LLMRuntime:
    """Routing is supplied as Config objects; credentials never enter manifests.

    Calls are capped before dispatch. Tokens and cost stop the next call when
    reported usage reaches the cap; providers can overshoot on the last call.
    """
    def __init__(self, root: str, session_id: str, *, routes: dict[str, llm.Config] | None = None,
                 budget: Budget | None = None, complete=None):
        self.root, self.session_id = root, session_id
        self.routes = dict(routes or {})
        self.budget = budget or Budget()
        self.caller = complete or llm.complete
        self.calls = self.tokens = 0
        self.cost = 0.0
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
            self.tokens += input_tokens + output_tokens
            cost = llm.cost_usd({"input_tokens": input_tokens, "output_tokens": output_tokens}, cfg.model)
            if cost is None:
                # Unpriced providers cannot silently evade a monetary budget.
                self.unpriced = True
            elif cost is not None:
                self.cost += cost
            session_ledger.record_usage(self.root, self.session_id, cfg.model, input_tokens,
                                        output_tokens, provider=cfg.provider, cost_usd=cost if cost is not None else 0,
                                        occasion=purpose, metadata={"cost_known": cost is not None})
            return text, usage

    def manifest(self) -> dict:
        return {"routes": {key: {"provider": cfg.provider, "model": cfg.model}
                           for key, cfg in self.routes.items()},
                "calls": self.calls, "failed_calls": self.failed, "tokens": self.tokens,
                "known_cost_usd": self.cost, "unpriced_calls": self.unpriced,
                "accounting_unknown": self.accounting_unknown,
                "elapsed_seconds": time.monotonic() - self.started}
