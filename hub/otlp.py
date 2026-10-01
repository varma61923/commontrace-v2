"""OTLP/HTTP trace ingest: `POST /v1/traces` accepts a live OpenTelemetry
SDK or Collector pointed directly at this Hub, so a completed GenAI span
becomes a Trace the moment it exports -- no client-side exporter code
running inside the caller's own process.

WHY THIS EXISTS, AND HOW IT DIFFERS FROM WHAT ALREADY SHIPS
--------------------------------------------------------------
`commontrace/otel_exporter.py`'s `CommonTraceSpanExporter` already does
"a completed GenAI span becomes a Trace, live" -- but into the LOCAL
store, from inside the same process that produced the span, via a real
`opentelemetry.sdk.trace.export.SpanExporter` the caller attaches to their
own `TracerProvider`. This module is the Hub-side counterpart: an
application that already has ANY OTLP/HTTP-speaking exporter configured
(the OTel SDK's own, a Collector's `otlphttp` exporter, a vendor SDK that
re-exports over OTLP) needs only its endpoint pointed at this Hub, plus an
`X-API-Key` header, to reach the SAME Trace object -- multi-tenant, over
the network, no CommonTrace-specific code in the exporting process at
all.

NOT A SECOND PARSER
--------------------
Every span here is normalized with `commontrace/adapters.py`'s
`normalize(span, source="otel")` -- the EXACT function `commontrace
import --source otel` and `CommonTraceSpanExporter` both already call.
Both drafts of "vintage" the GenAI semantic conventions have shipped under
(`gen_ai.prompt`/`gen_ai.completion`, then `gen_ai.input.messages`/
`gen_ai.output.messages`) and a widely-used SDK's own attribute names
(`traceloop.entity.input/output`) are all handled there, once; this module
would otherwise need to track the same drift a second time.

OTLP-JSON ONLY, NOT PROTOBUF -- STATED, NOT HIDDEN
-----------------------------------------------------
The full OTLP/HTTP spec accepts either `application/json` or
`application/x-protobuf`. Only JSON is accepted here. Parsing the
protobuf wire format needs the generated `ExportTraceServiceRequest`
message classes (the `opentelemetry-proto` package), which is not among
this Hub's declared dependencies (`hub/requirements.txt`) and would need
its own scrutiny before being added -- a wrong field-number assumption in
a hand-rolled decoder is a worse failure than a request format this
module explicitly does not claim to accept. Every mainstream OTLP
exporter (the Python/JS/Go SDKs' own OTLP/HTTP exporters, an OTel
Collector's `otlphttp` exporter) supports an `encoding: json` /
`OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/json` setting; a caller using
protobuf gets a clear 415, not a silent partial parse.

JOINING SPANS TO HOLDOUT OCCASIONS
-----------------------------------
A span that carries an occasion id (`commontrace.occasion_id`, OpenInference's
`session.id` or GenAI `gen_ai.conversation.id`) AND an explicit boolean
`commontrace.occasion.succeeded` closes that occasion's holdout observations in
both arms. The span's own status is never used for this: it describes one call,
not the task, and treating UNSET/OK as success would score an uninstrumented fleet
as winning.

AUTHENTICATION AND LIMITS
--------------------------
`X-API-Key`, verified with `auth.verify_api_key` and requiring the
`write` scope -- the identical mechanism `hub/rest.py`'s `/api/v1/traces`
uses, not a second implementation, so a key valid for one is valid for
the other under the same rules. Body size is bounded globally by
`BodySizeLimitMiddleware` (every route, not re-implemented here); span
COUNT per request is bounded separately (`_MAX_SPANS_PER_REQUEST`) because
a body just under the byte limit can still carry thousands of tiny spans,
each one a real write. Each span goes through the SAME per-org write-rate
bucket and entitlement check `crud.contribute_trace` already enforces for
every other ingestion path, and is deduplicated by its own OTel span id as
an idempotency key, so a batch an exporter retries after a partial network
failure does not create duplicate Traces for the spans that already landed.
"""
from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from hub import auth, crud, scopes
from hub.abuse import RateLimited, RateLimiter, TraceRejected, make_named_limiter, rate_limit_key
from hub.config import HubConfig
from hub.db import session_scope
from hub.plans import EntitlementExceeded

logger = logging.getLogger("commontrace.hub.otlp")

OTLP_TRACES_PATH = "/v1/traces"

#: Audit actor for anything arriving here, distinct from every other
#: ingestion path's own actor (hub/rest.py's ACTOR_REST_API, the MCP
#: path's per-key actor) -- the same "where did this trace come from"
#: reasoning hub/admin.py's operator-console actor already documents.
ACTOR_OTLP = "otlp-ingest"

#: A hard ceiling on spans per request, independent of the global body-size
#: limit: a batch under the byte cap can still carry an unreasonable
#: number of tiny spans, each a real database write. 500 is generous for
#: any single export batch a real SDK/Collector actually sends (the OTel
#: SDK's own default `BatchSpanProcessor` caps an export batch at 512).
_MAX_SPANS_PER_REQUEST = 500

_DEFAULT_AGENT_TYPE = "general"


def _json_error(status: int, error: str, detail: str = "") -> JSONResponse:
    body: dict = {"error": error}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status)


def _iter_spans(payload: dict):
    """Every span in an OTLP-JSON `ExportTraceServiceRequest` body,
    flattened out of `resourceSpans[].scopeSpans[].spans[]`. Resource- and
    scope-level attributes are NOT merged into each span's own -- matching
    exactly what `commontrace import --source otel` already does for the
    same OTLP-JSON shape, not a richer merge invented only for this path.
    """
    for resource_span in payload.get("resourceSpans") or []:
        if not isinstance(resource_span, dict):
            continue
        for scope_span in resource_span.get("scopeSpans") or []:
            if not isinstance(scope_span, dict):
                continue
            for span in scope_span.get("spans") or []:
                if isinstance(span, dict):
                    yield span


def add_otlp_routes(
    app,
    session_factory,
    *,
    config: HubConfig,
    rate_limiter: RateLimiter,
    trusted_proxy_hops: int = 0,
) -> None:
    """Register `POST /v1/traces`. Call only when OTLP ingest is enabled.

    `rate_limiter` is the SAME write-bucket limiter every other ingestion
    surface shares (see hub/rest.py's `add_rest_routes` for why one shared
    budget, not one per surface, is the point).
    """
    from commontrace import adapters

    auth_limiter = make_named_limiter(
        config, config.auth_attempts_per_minute, config.auth_attempts_burst, "otlp_auth"
    )

    async def ingest(request: Request) -> Response:
        allowed, retry_after = await auth_limiter.check(
            rate_limit_key(request, trusted_proxy_hops)
        )
        if not allowed:
            return JSONResponse(
                {"error": "rate_limited", "detail": "too many requests"},
                status_code=429,
                headers={"Retry-After": str(max(1, int(retry_after) + 1))},
            )
        raw_key = request.headers.get("X-API-Key", "")
        if not raw_key:
            return _json_error(401, "unauthorized", "X-API-Key header is required")
        async with session_scope(session_factory) as session:
            authenticated = await auth.verify_api_key(session, raw_key)
        if authenticated is None:
            return _json_error(401, "unauthorized", "that key was not accepted")
        if not scopes.satisfies(authenticated.scopes, scopes.SCOPE_WRITE):
            return _json_error(403, "forbidden", "this key does not carry the 'write' scope")

        content_type = request.headers.get("content-type", "")
        if content_type and "json" not in content_type:
            # Explicit 415 rather than attempting to parse: see this
            # module's docstring on why protobuf is not accepted.
            return _json_error(
                415, "unsupported_media_type",
                "only OTLP/JSON (Content-Type: application/json) is accepted here",
            )

        try:
            payload = json.loads(await request.body())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return _json_error(400, "bad_request", "body must be OTLP-JSON")
        if not isinstance(payload, dict):
            return _json_error(400, "bad_request", "body must be a JSON object")

        spans = list(_iter_spans(payload))
        if not spans:
            return JSONResponse({"accepted": 0, "skipped": 0, "errors": []})
        if len(spans) > _MAX_SPANS_PER_REQUEST:
            return _json_error(
                413, "payload_too_large",
                f"{len(spans)} spans in one request exceeds the limit of {_MAX_SPANS_PER_REQUEST}",
            )

        accepted = 0
        skipped = 0
        occasions_resolved = 0
        errors: list[dict] = []
        async with session_scope(session_factory) as session:
            for span in spans:
                flat = adapters.normalize(span, source="otel")
                span_id = str(flat.get("id") or "")
                # A span with no context/solution text at all carries
                # nothing this store's Trace schema requires -- skipped,
                # not stored as an empty Trace and not counted as an
                # error, matching commontrace/otel_exporter.py's own
                # "a span with no GenAI content is skipped" rule for the
                # identical shape.
                # An explicit occasion outcome on the span closes that occasion's
                # holdout observations, whether or not the span has text of its own
                # (the "task finished" span usually has none). First report wins,
                # so an exporter retry cannot flip it.
                if flat.get("occasion_id") and isinstance(flat.get("occasion_succeeded"), bool):
                    try:
                        done = await crud.record_occasion_outcome(
                            session, authenticated.org_id, flat["occasion_id"],
                            flat["occasion_succeeded"], actor=ACTOR_OTLP)
                        occasions_resolved += done["observations_resolved"]
                    except ValueError as exc:
                        errors.append({"span_id": span_id, "error": str(exc)})
                if not flat.get("context") and not flat.get("solution"):
                    skipped += 1
                    continue
                outcome = {"resolved": flat["resolved"]} if "resolved" in flat else None
                try:
                    await crud.contribute_trace(
                        session, authenticated.org_id, config, rate_limiter,
                        title=flat.get("title") or "OTel span",
                        context_text=flat.get("context", ""),
                        solution_text=flat.get("solution", ""),
                        tags=list(flat.get("tags") or []),
                        agent_type=_DEFAULT_AGENT_TYPE,
                        profile="otel",
                        outcome=outcome,
                        actor=ACTOR_OTLP,
                        idempotency_key=span_id or None,
                    )
                    accepted += 1
                except TraceRejected as exc:
                    errors.append({"span_id": span_id, "error": str(exc)})
                except crud.IdempotencyKeyConflict as exc:
                    errors.append({"span_id": span_id, "error": str(exc)})
                except EntitlementExceeded as exc:
                    # Every remaining span in this batch will hit the same
                    # cap -- reported once, not once per remaining span.
                    errors.append({"span_id": span_id, "error": str(exc)})
                    break
                except RateLimited as exc:
                    errors.append({
                        "span_id": span_id,
                        "error": f"rate_limited: {exc}",
                    })
                    break

        status = 200 if not errors else 207
        return JSONResponse(
            {"accepted": accepted, "skipped": skipped, "occasions_resolved": occasions_resolved,
             "errors": errors}, status_code=status,
        )

    app.add_route(OTLP_TRACES_PATH, ingest, methods=["POST"])
