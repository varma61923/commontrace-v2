"""Bounded text-only benchmark requests, with explicit caps and no hidden retries.

The reservation uses a UTF-8 byte upper bound plus 1024 framing tokens. Providers
must honor the output cap and report usage inside these bounds. Breach or absent
usage invalidates the run and retains the reservation; this is not a guarantee
against a provider billing outside the declared contract or configured prices.
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from functools import lru_cache

from benchmarks.cache import CostGuard, completion_binding, get_price
from unimem import llm


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@lru_cache(maxsize=8)
def _encoding(name: str):
    if not name.startswith("tiktoken:"):
        raise ValueError("tokenizer must be tiktoken:<encoding-name>")
    try:
        import tiktoken
    except ImportError:
        raise ValueError("the optional tiktoken package is required for exact text token counts") from None
    return tiktoken.get_encoding(name.split(":", 1)[1])


def text_tokens(text: str, tokenizer: str) -> int:
    return len(_encoding(tokenizer).encode(text, disallowed_special=()))


def text_counter_binding(tokenizer: str) -> dict:
    """Pin the loaded tokenizer's vocabulary, regex and special-token mapping."""
    import hashlib
    import importlib.metadata

    encoding = _encoding(tokenizer)
    digest = hashlib.sha256()
    digest.update(json.dumps({"pattern": encoding._pat_str, "special": encoding._special_tokens},
                             sort_keys=True).encode("utf-8"))
    for word, rank in sorted(encoding._mergeable_ranks.items()):
        digest.update(len(word).to_bytes(8, "big") + word + rank.to_bytes(8, "big"))
    return {"name": tokenizer, "package_version": importlib.metadata.version("tiktoken"),
            "encoding_sha256": digest.hexdigest(), "disallowed_special": []}


def benchmark_binding(config: llm.Config, *, output_limit: int = 1536) -> dict:
    import hashlib

    with open(__file__, "rb") as source:
        implementation = hashlib.sha256(source.read()).hexdigest()
    return completion_binding(config, generation={"max_tokens": output_limit, "temperature": 0.0,
                                                 "retries": 0, "requests_sha256": implementation})


def _local_complete(prompt: str, config: llm.Config, guard: CostGuard, *, output_limit: int):
    """An in-process model: no charge, but the output cap and usage bounds still hold."""
    from unimem import local_llm

    answer, usage = local_llm.complete(config, prompt, max_new_tokens=output_limit)
    if usage["output_tokens"] > output_limit:
        raise ValueError("local model exceeded the declared output limit")
    guard.record_call(0.0)
    return answer, usage, 0.0


GEMINI_RETRY_STATUSES = (429, 500, 503)


class _Refused(llm.LLMUnavailable):
    """The provider answered with an HTTP error: it produced no completion and bills nothing."""


class InputTokenWindow:
    """A process-wide sliding one-minute allowance of provider input tokens.

    Free-tier quotas are counted in input tokens per minute (16,000 for Gemma 4 on
    the Gemini Developer API). Concurrent graders that each discover the quota
    through a 429 retry in lockstep; reserving the estimate first spaces them out.
    A request larger than the whole allowance still runs once the window is empty.
    """

    def __init__(self, per_minute: int, *, window_s: float = 60.0, clock=None, sleep=None):
        import threading
        import time

        if not isinstance(per_minute, int) or isinstance(per_minute, bool) or per_minute <= 0:
            raise ValueError("input tokens per minute must be a positive integer")
        self.per_minute, self.window_s = per_minute, window_s
        self._clock, self._sleep = clock or time.monotonic, sleep or time.sleep
        self._lock = threading.Lock()
        self._entries: list[list[float]] = []

    def acquire(self, tokens: int) -> list[float]:
        while True:
            with self._lock:
                now = self._clock()
                self._entries = [e for e in self._entries if e[0] > now - self.window_s]
                used = sum(e[1] for e in self._entries)
                if not self._entries or used + tokens <= self.per_minute:
                    entry = [now, float(tokens)]
                    self._entries.append(entry)
                    return entry
                wait = self._entries[0][0] + self.window_s - now
            self._sleep(max(0.05, wait))

    def settle(self, entry: list[float], tokens: int) -> None:
        """Replace an estimate with the provider-reported count."""
        with self._lock:
            entry[1] = float(tokens)


_WINDOWS: dict[int, InputTokenWindow] = {}


def input_token_window() -> InputTokenWindow | None:
    import os

    raw = os.environ.get("UNIMEM_BENCH_INPUT_TPM", "").strip()
    if not raw:
        return None
    if not raw.isdigit() or int(raw) <= 0:
        raise ValueError("UNIMEM_BENCH_INPUT_TPM must be a positive integer")
    return _WINDOWS.setdefault(int(raw), InputTokenWindow(int(raw)))


def _retry_delay(body: bytes) -> float | None:
    """Seconds from a google.rpc.RetryInfo detail (e.g. "22.9s"), if the error carries one."""
    try:
        details = json.loads(body).get("error", {}).get("details", [])
        for detail in details:
            if str(detail.get("@type", "")).endswith("google.rpc.RetryInfo"):
                return max(0.0, float(str(detail["retryDelay"]).rstrip("s")))
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    return None


def _gemini_complete(prompt: str, config: llm.Config, guard: CostGuard, *, output_limit: int,
                     temperature: float, attempts: int = 20, sleep=None) -> tuple[str, dict, float]:
    """Google AI generateContent, bounded like every benchmark call.

    Retries are limited to HTTP 429/500/503. A request the provider answered
    with an HTTP error produced no completion and is not billed, so its
    reservation is released and later questions may still run; a timeout or
    dropped connection leaves the charge uncertain and stops the run.
    """
    import time

    sleep = sleep or time.sleep
    url, headers, payload = llm.gemini_request(config, prompt, max_tokens=output_limit, temperature=temperature)
    if not llm._is_http_url(url):
        raise ValueError("benchmark endpoint must use HTTP(S)")
    input_upper = len(prompt.encode("utf-8")) + 1024
    reservation = guard.reserve_call(config.model, input_upper, output_limit)
    window = input_token_window()
    entry = None
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        for attempt in range(attempts):
            if window is not None:
                # Gemma averages ~3.8 characters per token on English chat; reserve a
                # little more and correct it from the reported usage below.
                entry = window.acquire(math.ceil(len(prompt) / 3.5) + 16)
            request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
            try:
                with opener.open(request, timeout=llm.GEMINI_TIMEOUT_SECONDS) as response:  # nosec B310
                    data = json.load(response)
                break
            except urllib.error.HTTPError as exc:
                status = exc.code
                if entry is not None:
                    window.settle(entry, 0)  # a refused request consumed no quota
                try:
                    hint = _retry_delay(exc.read(65536))
                finally:
                    exc.close()
                if status not in GEMINI_RETRY_STATUSES or attempt == attempts - 1:
                    raise _Refused(f"bounded provider request refused (HTTP {status})") from None
                # A quota 429 says how long to wait; otherwise back off exponentially.
                sleep(min(120.0, hint + 1.0) if hint is not None else min(60.0, 2.0 * 2 ** attempt))
            except (OSError, ValueError):
                raise llm.LLMUnavailable("bounded provider request failed") from None
        answer, usage = llm.gemini_parse(data)
        input_tokens, output_tokens = usage["input_tokens"], usage["output_tokens"]
        if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in (input_tokens, output_tokens)):
            raise ValueError("provider must return text and complete integer usage")
        if entry is not None:
            window.settle(entry, input_tokens)
        if input_tokens > input_upper or output_tokens > output_limit:
            raise ValueError("provider usage exceeds the declared request bounds")
        rates = get_price(config.model, guard.prices, require_known=True)
        cost = (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000
        guard.settle_call(reservation, cost)
        return answer, usage, cost
    except _Refused as exc:
        guard.release_call(reservation)
        raise llm.LLMUnavailable(f"{exc}; nothing was charged") from None
    except Exception as exc:
        guard.mark_uncertain(reservation)
        if isinstance(exc, llm.LLMUnavailable):
            raise llm.LLMUnavailable(f"{exc}; charge remains uncertain") from None
        raise


def bounded_complete(prompt: str, config: llm.Config, guard: CostGuard, *, output_limit: int = 1536,
                     temperature: float = 0.0) -> tuple[str, dict, float]:
    """One attempt; a failure/unknown charge refuses all subsequent dispatches."""
    if not isinstance(output_limit, int) or isinstance(output_limit, bool) or not 1 <= output_limit <= 65536:
        raise ValueError("output limit must be in 1..65536")
    if config.provider == "local":
        return _local_complete(prompt, config, guard, output_limit=output_limit)
    if config.provider == "gemini":
        return _gemini_complete(prompt, config, guard, output_limit=output_limit, temperature=temperature)
    if config.provider not in ("anthropic", "openai-compatible", "ollama"):
        raise ValueError("bounded benchmark requests support Anthropic and OpenAI-compatible HTTP providers")
    input_upper = len(prompt.encode("utf-8")) + 1024
    reservation = guard.reserve_call(config.model, input_upper, output_limit)
    try:
        if config.provider == "anthropic":
            url = llm._ANTHROPIC_URL
            headers = {"x-api-key": config.api_key, "anthropic-version": llm._ANTHROPIC_VERSION,
                       "content-type": "application/json"}
        else:
            if not config.base_url:
                raise ValueError("bounded benchmark requests require an explicit base URL")
            url = config.base_url.rstrip("/") + "/chat/completions"
            headers = {"Authorization": "Bearer " + config.api_key, "content-type": "application/json"}
        if not llm._is_http_url(url):
            raise ValueError("benchmark endpoint must use HTTP(S)")
        payload = {"model": config.model, "max_tokens": output_limit, "temperature": temperature,
                   "messages": [{"role": "user", "content": prompt}]}
        request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            # The normal opener retains environment proxy/CA settings, but
            # redirects cannot move credentials or create a hidden second call.
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=60) as response:  # nosec B310 - scheme checked above
                data = json.load(response)
        except (OSError, ValueError):
            raise llm.LLMUnavailable("bounded provider request failed") from None
        usage = data.get("usage") or {}
        if config.provider == "anthropic":
            answer = "".join(block.get("text", "") for block in data.get("content", []))
            input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
        else:
            answer = data["choices"][0]["message"]["content"]
            input_tokens, output_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if not isinstance(answer, str) or any(not isinstance(value, int) or isinstance(value, bool) or value < 0
                                              for value in (input_tokens, output_tokens)):
            raise ValueError("provider must return text and complete integer usage")
        if input_tokens > input_upper or output_tokens > output_limit:
            raise ValueError("provider usage exceeds the declared request bounds")
        rates = get_price(config.model, guard.prices, require_known=True)
        cost = (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000
        guard.settle_call(reservation, cost)
        return answer, {"input_tokens": input_tokens, "output_tokens": output_tokens}, cost
    except Exception as exc:
        guard.mark_uncertain(reservation)
        if isinstance(exc, llm.LLMUnavailable):
            raise llm.LLMUnavailable("benchmark provider request failed; no retries; charge remains uncertain") from None
        raise
