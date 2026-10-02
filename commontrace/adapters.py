"""Reading someone else's trace export without asking them to reshape it."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

GENERIC = "generic"

_MAX_TEXT = 20_000


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()[:_MAX_TEXT]
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)[:_MAX_TEXT]
    except (TypeError, ValueError):
        return str(value)[:_MAX_TEXT]


def _first(row: dict, *names: str) -> object:
    for name in names:
        if name in row and row[name] not in (None, "", [], {}):
            return row[name]
    return None


def _tags(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


_MAX_TITLE = 110


def _excerpt(value: object) -> str:
    if isinstance(value, dict):
        strings = [v for v in value.values() if isinstance(v, str) and v.strip()]
        if len(strings) != 1:
            return ""
        value = strings[0]
    if not isinstance(value, str):
        return ""
    line = value.strip().splitlines()[0].strip() if value.strip() else ""
    return line


def _titled(name: str, subject: object) -> str:
    name = (name or "").strip()
    excerpt = _excerpt(subject)
    if not excerpt:
        return name[:_MAX_TITLE]
    if not name:
        return excerpt[:_MAX_TITLE]
    combined = f"{name}: {excerpt}"
    return combined[:_MAX_TITLE].rstrip()


def _langsmith(row: dict) -> dict:
    inputs = _first(row, "inputs", "input")
    outputs = _first(row, "outputs", "output")
    error = _as_text(_first(row, "error"))
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    metadata = extra.get("metadata") if isinstance(extra.get("metadata"), dict) else {}

    context = _as_text(inputs)
    if error:
        context = (context + "\n\nError:\n" + error).strip()

    flat = {
        "title": _titled(
            _as_text(_first(row, "name", "run_type")) or "LangSmith run", inputs
        ),
        "context": context,
        "solution": _as_text(outputs),
        "tags": _tags(row.get("tags")),
        "id": _as_text(_first(row, "id", "run_id")),
    }
    if "error" in row:
        flat["resolved"] = not error
    tokens = _int_or_none(
        _first(row, "total_tokens") or metadata.get("total_tokens")
    )
    if tokens is not None:
        flat["tokens_used"] = tokens
    return flat


def _langfuse(row: dict) -> dict:
    flat = {
        "title": _titled(
            _as_text(_first(row, "name")) or "Langfuse trace",
            _first(row, "input", "inputs"),
        ),
        "context": _as_text(_first(row, "input", "inputs")),
        "solution": _as_text(_first(row, "output", "outputs")),
        "tags": _tags(row.get("tags")),
        "id": _as_text(_first(row, "id", "traceId", "trace_id")),
    }
    level = str(_first(row, "level") or "").strip().upper()
    if level in ("ERROR", "WARNING"):
        flat["resolved"] = False

    scores = row.get("scores")
    if isinstance(scores, list):
        for score in scores:
            if not isinstance(score, dict):
                continue
            name = str(score.get("name", "")).strip().lower()
            if name not in _SUCCESS_SCORE_NAMES:
                continue
            value = score.get("value")
            if isinstance(value, bool):
                flat["resolved"] = value
            elif isinstance(value, (int, float)):
                flat["resolved"] = value > 0
            elif isinstance(value, str):
                flat["resolved"] = value.strip().lower() in _TRUTHY_STRINGS
            break

    usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    tokens = _int_or_none(_first(usage, "total", "totalTokens", "total_tokens"))
    if tokens is not None:
        flat["tokens_used"] = tokens
    return flat


_SUCCESS_SCORE_NAMES = frozenset({
    "resolved", "success", "successful", "correct", "correctness",
    "pass", "passed", "accuracy",
})

_TRUTHY_STRINGS = frozenset({"true", "yes", "pass", "passed", "success", "correct", "1"})


def _braintrust(row: dict) -> dict:
    output = _as_text(_first(row, "output"))
    expected = _as_text(_first(row, "expected"))
    solution = output
    if expected and expected != output:
        solution = (
            f"{output}\n\nExpected:\n{expected}" if output else f"Expected:\n{expected}"
        )

    span_attrs = (
        row.get("span_attributes")
        if isinstance(row.get("span_attributes"), dict) else {}
    )
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}

    flat = {
        "title": _titled(
            _as_text(
                _first(span_attrs, "name") or _first(metadata, "name")
                or _first(row, "name")
            ) or "Braintrust span",
            _first(row, "input"),
        ),
        "context": _as_text(_first(row, "input")),
        "solution": solution,
        "tags": _tags(row.get("tags") or metadata.get("tags")),
        "id": _as_text(_first(row, "id", "span_id")),
    }

    scores = row.get("scores")
    if isinstance(scores, dict):
        named = {
            str(k).strip().lower(): v for k, v in scores.items()
            if isinstance(v, (int, float, bool))
        }
        for name in _SUCCESS_SCORE_NAMES:
            if name in named:
                value = named[name]
                flat["resolved"] = bool(value) if isinstance(value, bool) else value >= 1.0
                break

    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    tokens = _int_or_none(_first(metrics, "tokens", "total_tokens"))
    if tokens is None:
        prompt = _int_or_none(metrics.get("prompt_tokens")) or 0
        completion = _int_or_none(metrics.get("completion_tokens")) or 0
        tokens = (prompt + completion) or None
    if tokens is not None:
        flat["tokens_used"] = tokens
    return flat


_OTLP_VALUE_KEYS = (
    "stringValue", "intValue", "doubleValue", "boolValue", "arrayValue",
)


def _otlp_value(raw: object) -> object:
    if not isinstance(raw, dict):
        return raw
    for key in _OTLP_VALUE_KEYS:
        if key in raw:
            value = raw[key]
            if key == "arrayValue" and isinstance(value, dict):
                return [_otlp_value(v) for v in value.get("values", [])]
            return value
    return raw


def otel_attributes(row: dict) -> dict:
    """A span's attributes as a flat dict, from either shape exporters emit."""
    raw = row.get("attributes")
    if isinstance(raw, dict):
        return {str(k): _otlp_value(v) for k, v in raw.items()}
    if isinstance(raw, list):
        out = {}
        for item in raw:
            if isinstance(item, dict) and "key" in item:
                out[str(item["key"])] = _otlp_value(item.get("value"))
        return out
    return {}


def _otel(row: dict) -> dict:
    attrs = otel_attributes(row)
    status = row.get("status") if isinstance(row.get("status"), dict) else {}
    status_code = str(
        _first(status, "code") or _first(row, "status_code") or ""
    ).strip().upper()

    context = _as_text(_first(
        attrs,
        "gen_ai.prompt", "gen_ai.input.messages", "gen_ai.request.messages",
        "traceloop.entity.input", "input.value",
    ))
    solution = _as_text(_first(
        attrs,
        "gen_ai.completion", "gen_ai.output.messages", "gen_ai.response.messages",
        "traceloop.entity.output", "output.value",
    ))

    message = _as_text(_first(status, "message"))
    if message:
        context = (context + "\n\nStatus:\n" + message).strip()

    flat = {
        "title": _titled(_as_text(_first(row, "name")) or "OTel span", context),
        "context": context,
        "solution": solution,
        "tags": [
            str(attrs[key]) for key in ("gen_ai.system", "gen_ai.request.model")
            if attrs.get(key)
        ],
        "id": _as_text(_first(row, "spanId", "span_id", "traceId", "trace_id")),
    }
    if "ERROR" in status_code:
        flat["resolved"] = False
    elif status_code.endswith("OK"):
        flat["resolved"] = True

    prompt = _int_or_none(_first(
        attrs, "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens")) or 0
    completion = _int_or_none(_first(
        attrs, "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens")) or 0
    if prompt or completion:
        flat["tokens_used"] = prompt + completion
    occasion = _first(attrs, *OTEL_OCCASION_KEYS)
    if occasion:
        flat["occasion_id"] = _as_text(occasion)
    succeeded = attrs.get(OTEL_OUTCOME_KEY)
    if isinstance(succeeded, bool):
        flat["occasion_succeeded"] = succeeded
    return flat


OTEL_OCCASION_KEYS = ("commontrace.occasion_id", "session.id", "gen_ai.conversation.id")
OTEL_OUTCOME_KEY = "commontrace.occasion.succeeded"


@dataclass(frozen=True)
class Adapter:
    name: str
    describe: str
    normalize: Callable[[dict], dict]


ADAPTERS: dict[str, Adapter] = {
    a.name: a for a in (
        Adapter(
            GENERIC, "flat rows, mapped with --title-field and friends",
            lambda row: row,
        ),
        Adapter(
            "langsmith",
            "LangSmith runs (inputs/outputs/error/tags), as list_runs exports them",
            _langsmith,
        ),
        Adapter(
            "langfuse",
            "Langfuse traces (input/output/scores/level)",
            _langfuse,
        ),
        Adapter(
            "braintrust",
            "Braintrust spans (input/output/expected/scores/metrics)",
            _braintrust,
        ),
        Adapter(
            "otel",
            "OpenTelemetry spans under the GenAI semantic conventions "
            "(OTLP-JSON or flat attributes)",
            _otel,
        ),
    )
}

SOURCES = tuple(ADAPTERS)


def normalize(row: dict, source: str = GENERIC) -> dict:
    try:
        adapter = ADAPTERS[source]
    except KeyError:
        raise ValueError(
            f"unknown import source {source!r}; known sources: "
            + ", ".join(SOURCES)
        ) from None
    if not isinstance(row, dict):
        return {}
    return adapter.normalize(row)
