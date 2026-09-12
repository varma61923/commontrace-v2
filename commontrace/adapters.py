"""Reading someone else's trace export without asking them to reshape it.

WHY THIS EXISTS
---------------
`commontrace import` could already read JSONL and CSV, with `--title-field`
and friends to say which column is which. That works for a spreadsheet and
does not work for any of the four systems a customer is most likely to be
coming from, because those export NESTED rows: the text lives at
`inputs.input`, `output.choices[0]`, or inside an OTel attribute list, and
no amount of `--context-field` reaches it. The honest description of the
gap was: a customer with two years of LangSmith history had to write a
transform script before this product could read a single one of their
traces, and the cost of that script is exactly the cost of not adopting.

So each adapter here FLATTENS one vendor's export row into the plain
`{title, context, solution, tags, id, ...}` shape `import_data` already
understands. Everything downstream -- streaming, schema validation, the
dry-run count, the skip reasons -- is unchanged and shared, which is the
point: an adapter that had its own write path would be a second importer
with its own bugs and its own idea of what a valid trace is.

FILES, NOT LIVE APIS, AND THAT IS DELIBERATE
--------------------------------------------
These read an export a customer already has on disk. None of them holds an
API key, opens a socket, or knows a vendor's hostname.

That is a product decision, not a shortcut. This product's entire trust
story is that a fleet's experience stays on the fleet's own infrastructure
(see DATA_RETENTION.md and the Knowledge Base boundary in README.md). An
importer that authenticated to a vendor and pulled would mean CommonTrace
holding a third party's credentials and making egress calls on the
customer's behalf, during onboarding, before any trust has been
established. A file is inspectable before it is read -- a security reviewer
can diff exactly what crosses the boundary -- and it works in an air-gapped
environment, where the customers most interested in on-prem memory actually
live.

WHAT AN ADAPTER MAY NOT DO
--------------------------
Invent. Every adapter maps fields that exist and leaves the rest empty, so
a row missing its solution text is SKIPPED with a reason by the shared
path rather than imported with a plausible-looking placeholder. A bulk
import is someone else's data arriving in bulk; a single fabricated field,
repeated ten thousand times, becomes a corpus this product then measures
and bills against.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

#: The shape `commontrace import` accepted before adapters existed: rows are
#: already flat and are passed through untouched.
GENERIC = "generic"

_MAX_TEXT = 20_000


def _as_text(value: object) -> str:
    """Render a vendor field as text an agent can actually read.

    Nested values are rendered as indented JSON rather than `str(dict)`:
    both are lossless, and only one of them is legible in a Markdown trace
    body six months later -- which is the only form this text is ever read
    in.
    """
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
    """The first of these keys the row actually carries.

    Exporters differ on casing and on where a field sits between versions
    (`sessionId` vs `session_id`, `extra.metadata` vs `metadata`), and a
    single hardcoded spelling is how an importer silently reads zero rows
    from a file that is obviously fine when a human looks at it.
    """
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


#: Titles are recommended under ~120 characters (trace.schema.json), and a
#: title is what a curator scans a hundred of at a time.
_MAX_TITLE = 110


def _excerpt(value: object) -> str:
    """A one-line gist of a vendor input, or "" if there isn't one.

    Only PLAIN text is used. A serialised dict makes a title that is
    technically unique and completely unreadable, which is worse than a
    repeated one.
    """
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
    """`<run name>: <what it was about>`, when the input says.

    A vendor's run or span name is the CHAIN's name, not the episode's:
    every row from one pipeline exports as "AgentExecutor". Importing ten
    thousand of those gives a store whose traces are indistinguishable in
    every listing a curator reads, and whose clustering has one fewer
    signal to work with. The name is kept as the prefix because it is real
    provenance; the excerpt is what makes the row findable.
    """
    name = (name or "").strip()
    excerpt = _excerpt(subject)
    if not excerpt:
        return name[:_MAX_TITLE]
    if not name:
        return excerpt[:_MAX_TITLE]
    combined = f"{name}: {excerpt}"
    return combined[:_MAX_TITLE].rstrip()


# --- LangSmith ---------------------------------------------------------------

def _langsmith(row: dict) -> dict:
    """One LangSmith *run*, as `Client.list_runs` serialises it.

    `error` is the outcome signal and is the reason this adapter is worth
    having at all: LangSmith already knows which runs failed, and that is
    precisely the label this product's whole distillation step is looking
    for. A run with an error becomes a trace whose context includes it, so
    `propose_lessons` can cluster on the actual failure text.
    """
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
    # `error` present and non-empty is a failure; `error: null` on a
    # completed run is a success. A run with neither field says nothing,
    # and is left without an outcome rather than assumed to have succeeded
    # -- an unreported occasion is missing data, not a win.
    if "error" in row:
        flat["resolved"] = not error
    tokens = _int_or_none(
        _first(row, "total_tokens") or metadata.get("total_tokens")
    )
    if tokens is not None:
        flat["tokens_used"] = tokens
    return flat


# --- Langfuse ----------------------------------------------------------------

def _langfuse(row: dict) -> dict:
    """One Langfuse *trace*, as its export and `/api/public/traces` return it.

    Langfuse carries outcome in `scores` (a list of named numeric or
    categorical judgements) rather than in an error field. A score named
    for success is read as the outcome; anything else is left alone,
    because guessing which of several custom scorers means "resolved" is
    exactly the judgement that should belong to the person who named them.
    """
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


#: Score names read as "did this occasion go well". Deliberately a short,
#: explicit list rather than a heuristic: a fleet with a scorer called
#: `toxicity` would have every safe answer read as a failure by anything
#: that just took the first numeric score it found.
_SUCCESS_SCORE_NAMES = frozenset({
    "resolved", "success", "successful", "correct", "correctness",
    "pass", "passed", "accuracy",
})

_TRUTHY_STRINGS = frozenset({"true", "yes", "pass", "passed", "success", "correct", "1"})


# --- Braintrust --------------------------------------------------------------

def _braintrust(row: dict) -> dict:
    """One Braintrust span/event row.

    `expected` is carried into the solution text when present. It is the
    field that makes a Braintrust export more useful here than a generic
    one: a row where output and expected disagree is a labelled failure,
    and labelled failures are what this product distils lessons from.
    """
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
                # Braintrust scores are 0..1; 1.0 is a pass and anything
                # short of it is not. A >0 test would read 0.05 as success.
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


# --- OpenTelemetry -----------------------------------------------------------

#: OTLP-JSON renders every attribute value as a single-key wrapper object
#: ({"stringValue": "..."}), so an attribute list has to be unwrapped twice
#: before it is a dict anyone can index.
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
    """A span's attributes as a flat dict, from either shape exporters emit.

    OTLP-JSON gives a LIST of {key, value} pairs; most SDK-side and
    file-exporter dumps give a plain dict. Supporting only one of them means
    reading zero rows from a file that is obviously a span export when a
    human looks at it.
    """
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
    """One span, under the OpenTelemetry GenAI semantic conventions.

    The conventions have moved -- `gen_ai.prompt`/`gen_ai.completion` in
    earlier drafts, `gen_ai.input.messages`/`gen_ai.output.messages` later,
    with `traceloop.entity.input/output` from a widely-used SDK alongside
    them. All are checked, because a customer's collector was configured
    against whichever draft was current when they deployed it, and telling
    them their spans are the wrong vintage is not an onboarding story.
    """
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
    # OTel spells an explicit failure and says nothing about success: UNSET
    # is the default every span carries whether or not anything checked it,
    # so only ERROR and OK are informative. Treating UNSET as a pass would
    # score an entire uninstrumented fleet as 100% resolved.
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
    return flat


# --- registry ----------------------------------------------------------------

@dataclass(frozen=True)
class Adapter:
    name: str
    #: Shown by `--help` and by the import summary, so the operator can see
    #: which shape was actually assumed.
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
    """Flatten one vendor row. An unknown source is an error, never a
    silent pass-through: importing ten thousand rows as `generic` because a
    flag was misspelled produces a store full of traces with no text in
    them and no indication why."""
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
