from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field

from commontrace import llm_cache as llm_cache_mod
from commontrace.circuit_breaker import CircuitBreaker, CircuitOpenError
from commontrace.exceptions import InfrastructureError
from commontrace.retry import call_with_retries, is_retryable
from commontrace.runtime_cache import RuntimeCache
from commontrace.secrets_provider import env_secret

DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = "claude-sonnet-5"

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_MAX_TOKENS = 1536
_TIMEOUT_SECONDS = 60
_SUPPORTED_PROVIDERS = ("anthropic", "openai-compatible", "ollama", "bedrock", "vertex", "local", "gemini")
_CLOUD_PROVIDERS = ("bedrock", "vertex")

_OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434/v1"
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# Thinking models spend output tokens on reasoning before the answer.
GEMINI_MAX_TOKENS = 8192
GEMINI_TIMEOUT_SECONDS = 300

REQUIRED_KEYS = ("rule", "applies_when", "do_not_apply_when", "evidence")

_CIRCUITS = RuntimeCache[CircuitBreaker](max_entries=128, max_bytes=128 * 1024, ttl=3600,
                                       weigh=lambda _key, _value: 1024)
_OVERLOADS = RuntimeCache(max_entries=128, max_bytes=128 * 1024, ttl=3600, weigh=lambda _key, _value: 1024)


def _transient_provider_failure(error: BaseException) -> bool:
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if is_retryable(error, idempotent=True)[0]:
            return True
        response = getattr(error, "response", None)
        if isinstance(response, dict):
            metadata = response.get("ResponseMetadata")
            status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
            if isinstance(status, int) and (status == 429 or 500 <= status < 600):
                return True
        error = error.__cause__
    return False


def _provider_call(cfg: Config, prompt: str, caller) -> tuple[str, dict]:
    from commontrace.overload import OverloadPolicy

    key = (cfg.provider, cfg.model, cfg.base_url, cfg.region, cfg.project, cfg.cache_namespace,
           hashlib.sha256(cfg.api_key.encode("utf-8")).hexdigest())
    overload = _OVERLOADS.get_or_load(key, OverloadPolicy)
    def dispatch():
        overload.admit()
        try:
            value = caller(cfg, prompt)
        except Exception as exc:
            overload.on_error(exc)
            raise
        overload.on_success()
        return value
    if os.environ.get("COMMONTRACE_LLM_CIRCUIT_BREAKER", "1").strip().lower() in ("0", "false", "off", "no"):
        return dispatch()
    circuit = _CIRCUITS.get_or_load(key, CircuitBreaker)
    try:
        return circuit.call(dispatch, transient=_transient_provider_failure)
    except CircuitOpenError as exc:
        raise LLMUnavailable(f"provider temporarily unavailable; retry in {exc.retry_after:.1f}s") from None


class LLMUnavailable(InfrastructureError):
    ...


class LLMDraftRejected(ValueError):
    ...


@dataclass(frozen=True)
class Config:
    provider: str
    model: str
    api_key: str = field(repr=False)
    base_url: str | None = None
    region: str | None = None
    project: str | None = None
    cache_namespace: str | None = None


def load_config() -> Config:
    """Provider settings from the environment."""
    provider = os.environ.get("COMMONTRACE_LLM_PROVIDER", DEFAULT_PROVIDER).strip().lower()
    from commontrace.providers import LLM_CREDENTIALS, LLMS

    if provider not in (*_SUPPORTED_PROVIDERS, *LLMS.names()):
        raise LLMUnavailable(
            f"COMMONTRACE_LLM_PROVIDER={provider!r} is not supported "
            f"(use one of: {', '.join(_SUPPORTED_PROVIDERS)})."
        )
    ollama_alias = provider == "ollama"
    if ollama_alias:
        provider = "openai-compatible"
    try:
        api_key = env_secret("COMMONTRACE_LLM_API_KEY").strip()
    except RuntimeError:
        raise LLMUnavailable("configured LLM API secret could not be resolved") from None
    if provider == "local":
        from commontrace import local_llm

        model = os.environ.get("COMMONTRACE_LLM_MODEL", "").strip() or local_llm.DEFAULT_MODEL
        return Config(provider="local", model=model, api_key="",
                      cache_namespace=os.environ.get("COMMONTRACE_LLM_CACHE_NAMESPACE", "").strip() or None)
    if not api_key and provider not in _CLOUD_PROVIDERS and not ollama_alias and LLM_CREDENTIALS.get(provider, True):
        raise LLMUnavailable(
            "COMMONTRACE_LLM_API_KEY is not set -- no LLM-assisted draft is possible."
        )
    model = os.environ.get("COMMONTRACE_LLM_MODEL", "").strip() or DEFAULT_MODEL
    cache_namespace = os.environ.get("COMMONTRACE_LLM_CACHE_NAMESPACE", "").strip() or None
    if provider in _CLOUD_PROVIDERS:
        model = os.environ.get("COMMONTRACE_LLM_MODEL", "").strip()
        region = os.environ.get("COMMONTRACE_LLM_REGION", "").strip()
        if not region and provider == "bedrock":
            region = os.environ.get("AWS_REGION", "").strip()
        project = os.environ.get("COMMONTRACE_LLM_PROJECT", "").strip() or None
        missing = [name for name, value in (
            ("COMMONTRACE_LLM_MODEL", model), ("COMMONTRACE_LLM_REGION", region),
            *((("COMMONTRACE_LLM_PROJECT", project),) if provider == "vertex" else ())) if not value]
        if missing:
            raise LLMUnavailable(f"COMMONTRACE_LLM_PROVIDER={provider} also needs {', '.join(missing)}.")
        return Config(provider=provider, model=model, api_key="", region=region,
                      project=project, cache_namespace=cache_namespace)
    base_url = os.environ.get("COMMONTRACE_LLM_BASE_URL", "").strip() or None
    if provider == "openai-compatible" and not base_url:
        if ollama_alias:
            base_url = _OLLAMA_DEFAULT_BASE_URL
        else:
            raise LLMUnavailable(
                "COMMONTRACE_LLM_PROVIDER=openai-compatible requires COMMONTRACE_LLM_BASE_URL "
                "(e.g. a local model server, or a provider's OpenAI-compatible endpoint)."
            )
    if base_url and not _is_http_url(base_url):
        raise LLMUnavailable(
            f"COMMONTRACE_LLM_BASE_URL must be an http(s) URL, got {base_url!r}."
        )
    return Config(provider=provider, model=model, api_key=api_key,
                  base_url=base_url, cache_namespace=cache_namespace)


def _is_http_url(url: str) -> bool:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


@dataclass(frozen=True)
class Draft:
    rule: str
    applies_when: str
    do_not_apply_when: str
    evidence: list[str]
    unverifiable_evidence: list[str]
    provenance: dict = field(default_factory=dict)


def _post_json(url: str, headers: dict, payload: dict, *, timeout: float | None = None) -> dict:
    import urllib.error
    import urllib.request

    if not _is_http_url(url):
        raise LLMUnavailable(f"refusing a non-http(s) URL: {url!r}")
    from commontrace import offline

    if offline.enabled() and not offline.is_loopback(url):
        raise LLMUnavailable("offline mode: hosted model providers are disabled (COMMONTRACE_OFFLINE)")
    body = json.dumps(payload).encode("utf-8")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    def read_bounded(response) -> str:
        raw = response.read(8*1024*1024+1)
        if len(raw) > 8*1024*1024:
            raise LLMUnavailable("provider response exceeds 8 MiB")
        return raw.decode("utf-8")

    def _once() -> str:
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with opener.open(request, timeout=timeout or _TIMEOUT_SECONDS) as resp:  # nosec B310 - scheme checked above
                return read_bounded(resp)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise

    # POST lesson drafts are idempotent (same payload → same draft text), so a
    # 5xx may be retried; 429 always is. Non-JSON/4xx surface immediately.
    raw, error = call_with_retries(_once, max_retries=4, idempotent=True)
    if error is not None:
        if isinstance(error, urllib.error.HTTPError):
            raise LLMUnavailable(
                f"Provider returned HTTP {error.code}") from error
        raise LLMUnavailable("Provider request failed") from error
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise LLMUnavailable(f"{url} returned a non-JSON response") from exc


def _call_anthropic(config: Config, prompt: str) -> tuple[str, dict]:
    payload = {
        "model": config.model,
        "max_tokens": _ANTHROPIC_MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": config.api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    data = _post_json(_ANTHROPIC_URL, headers, payload)
    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    usage = data.get("usage") or {}
    return text, {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")}


def gemini_request(config: Config, prompt: str, *, max_tokens: int = GEMINI_MAX_TOKENS,
                   temperature: float = 0.0) -> tuple[str, dict, dict]:
    """(url, headers, payload) for Google AI's generateContent (Gemini and Gemma models)."""
    from urllib.parse import quote

    base = (config.base_url or _GEMINI_BASE_URL).rstrip("/")
    url = f"{base}/models/{quote(config.model, safe='-._')}:generateContent"
    headers = {"x-goog-api-key": config.api_key, "content-type": "application/json"}
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens,
                                    "thinkingConfig": {"includeThoughts": False}}}
    return url, headers, payload


def gemini_parse(data: dict) -> tuple[str, dict]:
    """The answer without thought parts; output tokens include the thinking that was billed."""
    candidates = data.get("candidates") or []
    if not candidates or not isinstance(candidates[0], dict):
        reason = (data.get("promptFeedback") or {}).get("blockReason") or "no candidates"
        raise LLMUnavailable(f"Gemini returned no answer ({reason})")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
    if not text.strip() and candidates[0].get("finishReason") == "MAX_TOKENS":
        raise LLMUnavailable("Gemini spent its whole output budget thinking; raise the output cap")
    usage = data.get("usageMetadata") or {}
    output = (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0)
    return text, {"input_tokens": usage.get("promptTokenCount"), "output_tokens": output,
                  "thinking_tokens": usage.get("thoughtsTokenCount") or 0}


def _call_gemini(config: Config, prompt: str) -> tuple[str, dict]:
    url, headers, payload = gemini_request(config, prompt)
    return gemini_parse(_post_json(url, headers, payload, timeout=GEMINI_TIMEOUT_SECONDS))


def _call_openai_compatible(config: Config, prompt: str) -> tuple[str, dict]:
    base = config.base_url or (_OLLAMA_DEFAULT_BASE_URL if config.provider == "ollama" else None)
    if not base:
        raise LLMUnavailable(
            "COMMONTRACE_LLM_PROVIDER=openai-compatible requires COMMONTRACE_LLM_BASE_URL "
            "(e.g. a local model server, or a provider's OpenAI-compatible endpoint)."
        )
    url = base.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {config.api_key}", "content-type": "application/json"}
    data = _post_json(url, headers, payload)
    choices = data.get("choices") or []
    text = ""
    if choices and isinstance(choices[0], dict):
        text = (choices[0].get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    return text, {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")}


def complete_with_image(
    config: Config, prompt: str, image_bytes: bytes, mime: str,
) -> tuple[str, dict]:
    """Ask the model about an image, returning (text, usage)."""
    import base64

    provider = "openai-compatible" if config.provider == "ollama" else config.provider
    data = base64.b64encode(bytes(image_bytes)).decode("ascii")
    media = (mime or "application/octet-stream").strip() or "application/octet-stream"
    if provider == "anthropic":
        payload = {
            "model": config.model,
            "max_tokens": _ANTHROPIC_MAX_TOKENS,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image", "source": {
                        "type": "base64", "media_type": media, "data": data,
                    }},
                ],
            }],
        }
        headers = {
            "x-api-key": config.api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        result = _post_json(_ANTHROPIC_URL, headers, payload)
        blocks = result.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        usage = result.get("usage") or {}
        return text, {"input_tokens": usage.get("input_tokens"),
                      "output_tokens": usage.get("output_tokens")}
    if provider == "openai-compatible":
        base = config.base_url or _OLLAMA_DEFAULT_BASE_URL
        url = base.rstrip("/") + "/chat/completions"
        payload = {
            "model": config.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:{media};base64,{data}",
                    }},
                ],
            }],
            "temperature": 0,
        }
        headers = {"Authorization": f"Bearer {config.api_key}", "content-type": "application/json"}
        result = _post_json(url, headers, payload)
        choices = result.get("choices") or []
        text = ""
        if choices and isinstance(choices[0], dict):
            text = (choices[0].get("message") or {}).get("content") or ""
        usage = result.get("usage") or {}
        return text, {"input_tokens": usage.get("prompt_tokens"),
                      "output_tokens": usage.get("completion_tokens")}
    raise LLMUnavailable(
        f"provider {config.provider!r} does not support image input "
        "(vision needs 'anthropic' or 'openai-compatible'/'ollama')."
    )


def _sdk_missing(provider: str, package: str) -> LLMUnavailable:
    return LLMUnavailable(
        f"provider {provider!r} needs the optional {package} package: pip install 'commontrace[llm]'")


def _call_local(config: Config, prompt: str) -> tuple[str, dict]:
    from commontrace import local_llm

    return local_llm.complete(config, prompt)


def _call_bedrock(config: Config, prompt: str) -> tuple[str, dict]:
    try:
        import boto3
    except ImportError:
        raise _sdk_missing("bedrock", "boto3") from None
    client = boto3.client("bedrock-runtime", region_name=config.region)
    try:
        data = client.converse(
            modelId=config.model,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": _ANTHROPIC_MAX_TOKENS, "temperature": 0},
        )
    except Exception as exc:  # noqa: BLE001 - botocore's error types are many; all mean "unavailable"
        raise LLMUnavailable(f"bedrock request failed: {type(exc).__name__}: {exc}") from exc
    blocks = ((data.get("output") or {}).get("message") or {}).get("content") or []
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    usage = data.get("usage") or {}
    return text, {"input_tokens": usage.get("inputTokens"), "output_tokens": usage.get("outputTokens")}


def _call_vertex(config: Config, prompt: str) -> tuple[str, dict]:
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise _sdk_missing("vertex", "google-genai") from None
    try:
        client = genai.Client(vertexai=True, project=config.project, location=config.region)
        response = client.models.generate_content(
            model=config.model, contents=prompt,
            config=types.GenerateContentConfig(temperature=0, max_output_tokens=_ANTHROPIC_MAX_TOKENS))
    except Exception as exc:  # noqa: BLE001
        raise LLMUnavailable(f"vertex request failed: {type(exc).__name__}: {exc}") from exc
    usage = getattr(response, "usage_metadata", None)
    return (response.text or ""), {
        "input_tokens": getattr(usage, "prompt_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None)}


def _extract_json_object(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        parsed = json.loads(stripped)
    except ValueError as exc:
        raise LLMDraftRejected(f"model reply was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LLMDraftRejected("model reply was valid JSON but not a JSON object")
    return parsed


def _non_empty_str(parsed: dict, key: str) -> str:
    value = parsed.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LLMDraftRejected(f"'{key}' must be a non-empty string")
    return value.strip()


def complete(prompt: str, config: Config | None = None) -> tuple[str, dict]:
    """One completion from the configured provider: (text, usage).

    When ``COMMONTRACE_LLM_CACHE=1``, provider/account-scoped calls share a
    bounded SQLite cache and concurrent identical calls share one provider call.
    """
    from commontrace.llm_runtime import ACTIVE, PURPOSE

    runtime = ACTIVE.get()
    if runtime is not None:
        return runtime.complete(prompt, purpose=PURPOSE.get(), config=config)
    cfg = config or load_config()
    cache = llm_cache_mod.LLMCache() if llm_cache_mod.enabled() else None
    from commontrace.providers import llm_caller

    caller = llm_caller(cfg.provider, {"anthropic": _call_anthropic, "openai-compatible": _call_openai_compatible,
              "ollama": _call_openai_compatible,
              "bedrock": _call_bedrock, "vertex": _call_vertex, "local": _call_local,
              "gemini": _call_gemini})
    # IAM/ADC identity may change independently of these routing fields. Require
    # an owner-supplied tenant/account namespace before caching cloud SDK calls.
    if cache is None or (cfg.provider in _CLOUD_PROVIDERS and not cfg.cache_namespace):
        return _provider_call(cfg, prompt, caller)
    # Provider, endpoint and cloud routing prevent cross-account/provider reuse.
    # Only a digest of the credential is used; no plaintext enters disk keys.
    namespace = json.dumps([cfg.provider, cfg.base_url, cfg.region, cfg.project, cfg.cache_namespace,
                            hashlib.sha256(cfg.api_key.encode("utf-8")).hexdigest()])
    key = llm_cache_mod.cache_key(cfg.model, prompt, namespace=namespace)

    def compute():
        text, usage = _provider_call(cfg, prompt, caller)
        return {"text": text, "usage": usage}

    value = cache.get_or_compute(key, compute)
    return value["text"], value.get("usage", {})


def draft(
    prompt: str,
    *,
    allowed_evidence_ids: set[str] | None = None,
    config: Config | None = None,
) -> Draft:
    cfg = config or load_config()
    text, usage_raw = complete(prompt, cfg)

    parsed = _extract_json_object(text)
    missing = [k for k in REQUIRED_KEYS if k not in parsed]
    if missing:
        raise LLMDraftRejected(f"model reply is missing required key(s): {', '.join(missing)}")

    rule = _non_empty_str(parsed, "rule")
    applies_when = _non_empty_str(parsed, "applies_when")
    do_not_apply_when = _non_empty_str(parsed, "do_not_apply_when")

    raw_evidence = parsed["evidence"]
    if not isinstance(raw_evidence, list):
        raise LLMDraftRejected("'evidence' must be a JSON array")
    cited = [str(e) for e in raw_evidence]

    if allowed_evidence_ids is None:
        verified, unverifiable = cited, []
    else:
        verified = [e for e in cited if e in allowed_evidence_ids]
        unverifiable = [e for e in cited if e not in allowed_evidence_ids]
        if cited and not verified:
            raise LLMDraftRejected(
                f"model cited evidence {cited!r}, none of which was in the "
                "occasions actually offered -- refusing a draft that cites "
                "nothing verifiable."
            )

    input_tokens, output_tokens = usage_raw.get("input_tokens"), usage_raw.get("output_tokens")
    estimated = input_tokens is None or output_tokens is None
    if estimated:
        input_tokens = input_tokens if input_tokens is not None else max(1, len(prompt) // 4)
        output_tokens = output_tokens if output_tokens is not None else max(1, len(text) // 4)

    return Draft(
        rule=rule,
        applies_when=applies_when,
        do_not_apply_when=do_not_apply_when,
        evidence=verified,
        unverifiable_evidence=unverifiable,
        provenance={
            "provider": cfg.provider,
            "model": cfg.model,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "usage": {
                "input_tokens": int(input_tokens),
                "output_tokens": int(output_tokens),
                "estimated": estimated,
            },
            **({"cost_usd": c} if (c := cost_usd(
                {"input_tokens": int(input_tokens), "output_tokens": int(output_tokens)}, cfg.model)) is not None
               else {}),
        },
    )


def cost_usd(usage: dict, model: str, prices: dict | None = None) -> float | None:
    """What one call cost, from a price table the OWNER supplies."""
    if prices is None:
        path = os.environ.get("COMMONTRACE_LLM_PRICES", "").strip()
        if not path:
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                prices = json.load(fh)
        except (OSError, ValueError):
            return None
    entry = prices.get(model) if isinstance(prices, dict) else None
    if not isinstance(entry, dict):
        return None
    try:
        return round((usage["input_tokens"] * float(entry["input_per_mtok"])
                      + usage["output_tokens"] * float(entry["output_per_mtok"])) / 1_000_000, 6)
    except (KeyError, TypeError, ValueError):
        return None
