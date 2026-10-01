"""Optional, explicitly-invoked LLM calls that DRAFT lesson content -- never
write it directly. `commontrace distill --draft`, `lesson suggest-revision
--draft` and `lesson suggest-rewrite` are the only callers, and every draft
they produce still lands at `status: review` and is refused activation by
the SAME gate any other draft is: the scaffolding check, the content-safety
scan, the redundancy check and separation-of-duties
(commontrace/commands/lesson_cmd.py:run_approve). Nothing in this module
bypasses that gate; it only makes a draft better than the "TODO: ..."
placeholder a curator gets without it.

WHY THIS ADDS NO DEPENDENCY, OPTIONAL OR OTHERWISE
----------------------------------------------------
Every provider this module supports exposes a plain JSON-over-HTTPS chat
endpoint, and stdlib `urllib.request` is enough to call one. That is a
stronger version of what an optional `[llm]` extra would give: there is
nothing to install, so "the core install stays PyYAML-only" (AGENTS.md)
holds trivially. Importing this module costs nothing; only calling
`draft()` reaches the network, and only when a caller passed `--draft`.

PROVIDERS
---------
- `anthropic` (default): the Messages API, called directly.
- `openai-compatible`: the `/chat/completions` shape, against ANY base URL
  that speaks it -- OpenAI itself, or a local model server (Ollama, vLLM,
  LM Studio) reachable without a live API key or egress.

Bedrock and Vertex's own signed-request APIs are deliberately NOT
implemented here: both need per-provider request signing (SigV4, a
service-account JWT) that cannot be verified against a real account from
this codebase, and a signing implementation subtly wrong is a worse failure
than not having one. Both providers document an OpenAI-compatible endpoint
of their own -- a customer on either is one `COMMONTRACE_LLM_BASE_URL` away
from using this module, not blocked on a rewrite here.

WHAT A DRAFT CARRIES, AND WHAT IT IS REFUSED FOR
--------------------------------------------------
The model is given only the evidence the CALLER already computed --
distill.py's trace variants, or reliability.py's hit/miss occasions -- and
is asked for a strict JSON object. A reply that is not valid JSON, is not a
JSON object, or is missing a required key is refused outright: no partial
acceptance, no best-effort text extraction. `evidence` citations are
checked against the occasion/trace ids the caller actually offered; any
citation outside that set cannot be verified and is dropped rather than
trusted, and a draft that cited nothing verifiable at all is refused
entirely, on the same reasoning `commontrace/adapters.py` refuses to invent
a field a vendor export doesn't have. Every accepted draft's provenance --
provider, model, a SHA-256 of the exact prompt sent, and token usage from
the provider's own response (or a clearly labelled estimate when a
provider's reply carries none) -- travels with it so a disputed draft is
checkable without re-sending anything.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field

DEFAULT_PROVIDER = "anthropic"
# The master prompt this module implements against says "default to the
# latest Claude model" -- Sonnet 5 is that model as of this writing. An
# operator on a newer model sets COMMONTRACE_LLM_MODEL; this default is a
# starting point, not a claim that it will always be current.
DEFAULT_MODEL = "claude-sonnet-5"

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_MAX_TOKENS = 1536
_TIMEOUT_SECONDS = 60
_SUPPORTED_PROVIDERS = ("anthropic", "openai-compatible")

REQUIRED_KEYS = ("rule", "applies_when", "do_not_apply_when", "evidence")


class LLMUnavailable(RuntimeError):
    """No provider is configured, or it could not be reached. Every caller
    of `draft()` catches this and falls back to the existing non-LLM
    scaffold -- this is an expected, common outcome (no API key set), not
    a bug."""


class LLMDraftRejected(ValueError):
    """The provider responded, but not with a complete, strict JSON draft.
    Callers fall back to the existing scaffold exactly as they do for
    LLMUnavailable; the distinction exists so a caller CAN report "the
    model answered but the answer was unusable" differently from "no model
    was configured", if it chooses to."""


@dataclass(frozen=True)
class Config:
    provider: str
    model: str
    api_key: str
    base_url: str | None = None


def load_config() -> Config:
    """Provider settings from the environment.

    Raises LLMUnavailable rather than returning a Config with an empty key:
    every caller handles that exception by falling back to the template
    scaffold, so "no key set" reads as "no LLM-assisted draft is possible"
    at the point `--draft` was actually requested, not as a confusing 401
    three network calls later.
    """
    api_key = os.environ.get("COMMONTRACE_LLM_API_KEY", "").strip()
    if not api_key:
        raise LLMUnavailable(
            "COMMONTRACE_LLM_API_KEY is not set -- no LLM-assisted draft is possible."
        )
    provider = os.environ.get("COMMONTRACE_LLM_PROVIDER", DEFAULT_PROVIDER).strip().lower()
    if provider not in _SUPPORTED_PROVIDERS:
        raise LLMUnavailable(
            f"COMMONTRACE_LLM_PROVIDER={provider!r} is not supported "
            f"(use one of: {', '.join(_SUPPORTED_PROVIDERS)})."
        )
    model = os.environ.get("COMMONTRACE_LLM_MODEL", "").strip() or DEFAULT_MODEL
    base_url = os.environ.get("COMMONTRACE_LLM_BASE_URL", "").strip() or None
    if provider == "openai-compatible" and not base_url:
        raise LLMUnavailable(
            "COMMONTRACE_LLM_PROVIDER=openai-compatible requires COMMONTRACE_LLM_BASE_URL "
            "(e.g. a local model server, or a provider's OpenAI-compatible endpoint)."
        )
    return Config(provider=provider, model=model, api_key=api_key, base_url=base_url)


@dataclass(frozen=True)
class Draft:
    rule: str
    applies_when: str
    do_not_apply_when: str
    #: Evidence ids the model cited AND the caller could verify. Never
    #: includes a citation outside what the caller offered -- see this
    #: module's docstring.
    evidence: list[str]
    #: Citations the model produced that were NOT in the caller's allowed
    #: set. Kept (not silently discarded) so a curator reviewing the draft
    #: can see the model referenced something it did not actually have.
    unverifiable_evidence: list[str]
    provenance: dict = field(default_factory=dict)


def _post_json(url: str, headers: dict, payload: dict) -> dict:
    # Imported here, not at module level: urllib.request pulls in `ssl`
    # (~a few ms, but tests/test_cli.py:test_a_command_imports_only_what_it_uses
    # enforces that building the parser for an unrelated command -- capture,
    # query, lesson without --draft -- never pays for a network stack it
    # will not use). Only actually calling draft() should reach the network;
    # importing this module must stay free.
    import urllib.error
    import urllib.request

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as resp:
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


def _extract_json_object(text: str) -> dict:
    """The model's reply, parsed as ONE JSON object.

    Tolerates a fenced code block (```` ```json ... ``` ````) around it --
    the single most common way a chat model wraps structured output despite
    being asked not to -- but nothing looser: text before/after the object,
    more than one object, or a non-object top level all raise
    LLMDraftRejected rather than being guessed at.
    """
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
    """Call the configured provider with `prompt` and parse a strict JSON
    draft from the reply.

    `prompt` is built entirely by the CALLER -- this function never
    constructs one -- so what evidence the model was shown is always
    visible at the call site rather than hidden in this module.

    `allowed_evidence_ids`, when given, is the exact set of occasion/trace
    ids the prompt actually cited; a model reply's `evidence` entries are
    split into `Draft.evidence` (verifiable) and
    `Draft.unverifiable_evidence` (not in the set) rather than trusted
    wholesale. If the model cited at least one id and NONE of them verify,
    that is treated as fabrication rather than a partial match, and the
    whole draft is refused.

    Raises LLMUnavailable if no provider is configured or reachable, and
    LLMDraftRejected if the reply is not a usable draft. Neither is
    swallowed here -- every caller decides how to fall back.
    """
    cfg = config or load_config()
    if cfg.provider == "anthropic":
        text, usage_raw = _call_anthropic(cfg, prompt)
    else:
        text, usage_raw = _call_openai_compatible(cfg, prompt)

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
        # A crude, clearly-labelled fallback for a provider whose response
        # carries no usage block -- never presented as measured.
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
        },
    )
