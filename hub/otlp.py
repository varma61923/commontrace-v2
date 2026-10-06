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

ACTOR_OTLP = "otlp-ingest"

_MAX_SPANS_PER_REQUEST = 500

_DEFAULT_AGENT_TYPE = "general"


def _json_error(status: int, error: str, detail: str = "") -> JSONResponse:
    body: dict = {"error": error}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status)


def _iter_spans(payload: dict):
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
    """Register `POST /v1/traces`. Call only when OTLP ingest is enabled."""
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
        async with session_scope(session_factory, org_id=authenticated.org_id) as session:
            for span in spans:
                flat = adapters.normalize(span, source="otel")
                span_id = str(flat.get("id") or "")
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
