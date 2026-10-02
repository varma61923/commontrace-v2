from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field

DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = "claude-sonnet-5"

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_MAX_TOKENS = 1536
_TIMEOUT_SECONDS = 60
_SUPPORTED_PROVIDERS = ("anthropic", "openai-compatible", "bedrock", "vertex")
_CLOUD_PROVIDERS = ("bedrock", "vertex")

REQUIRED_KEYS = ("rule", "applies_when", "do_not_apply_when", "evidence")


class LLMUnavailable(RuntimeError):
    ...


class LLMDraftRejected(ValueError):
    ...


@dataclass(frozen=True)
class Config:
    provider: str
    model: str
    api_key: str
    base_url: str | None = None
    region: str | None = None
    project: str | None = None


def load_config() -> Config:
    """Provider settings from the environment."""
    provider = os.environ.get("COMMONTRACE_LLM_PROVIDER", DEFAULT_PROVIDER).strip().lower()
    if provider not in _SUPPORTED_PROVIDERS:
        raise LLMUnavailable(
            f"COMMONTRACE_LLM_PROVIDER={provider!r} is not supported "
            f"(use one of: {', '.join(_SUPPORTED_PROVIDERS)})."
        )
    api_key = os.environ.get("COMMONTRACE_LLM_API_KEY", "").strip()
    if not api_key and provider not in _CLOUD_PROVIDERS:
        raise LLMUnavailable(
            "COMMONTRACE_LLM_API_KEY is not set -- no LLM-assisted draft is possible."
        )
    model = os.environ.get("COMMONTRACE_LLM_MODEL", "").strip() or DEFAULT_MODEL
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
        return Config(provider=provider, model=model, api_key="", region=region, project=project)
    base_url = os.environ.get("COMMONTRACE_LLM_BASE_URL", "").strip() or None
    if provider == "openai-compatible" and not base_url:
        raise LLMUnavailable(
            "COMMONTRACE_LLM_PROVIDER=openai-compatible requires COMMONTRACE_LLM_BASE_URL "
            "(e.g. a local model server, or a provider's OpenAI-compatible endpoint)."
        )
    if base_url and not _is_http_url(base_url):
        raise LLMUnavailable(
            f"COMMONTRACE_LLM_BASE_URL must be an http(s) URL, got {base_url!r}."
        )
    return Config(provider=provider, model=model, api_key=api_key, base_url=base_url)


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


def _post_json(url: str, headers: dict, payload: dict) -> dict:
    import urllib.error
    import urllib.request

    if not _is_http_url(url):
        raise LLMUnavailable(f"refusing a non-http(s) URL: {url!r}")
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as resp:  # nosec B310 - scheme checked above
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise LLMUnavailable(f"{url} returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMUnavailable(f"could not reach {url}: {exc}") from exc
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


def _call_openai_compatible(config: Config, prompt: str) -> tuple[str, dict]:
    url = config.base_url.rstrip("/") + "/chat/completions"
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


def _sdk_missing(provider: str, package: str) -> LLMUnavailable:
    return LLMUnavailable(
        f"provider {provider!r} needs the optional {package} package: pip install 'commontrace[llm]'")


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


def draft(
    prompt: str,
    *,
    allowed_evidence_ids: set[str] | None = None,
    config: Config | None = None,
) -> Draft:
    cfg = config or load_config()
    caller = {"anthropic": _call_anthropic, "openai-compatible": _call_openai_compatible,
              "bedrock": _call_bedrock, "vertex": _call_vertex}[cfg.provider]
    text, usage_raw = caller(cfg, prompt)

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
