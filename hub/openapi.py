"""The Hub's OpenAPI 3.1.0 description, generated from its live route table.

Every operation is declared once, in `_operations()`, keyed by
`(METHOD, path)`. `document()` builds the Hub exactly as `hub.server.build_app`
does, with every optional surface switched on, walks the resulting route table
and joins each live route to its declaration. It refuses to build when a live
route has no declaration (so a new route cannot ship undocumented) and when a
declaration names a route that no longer exists (so a removed route cannot
linger in the contract). `scripts/export_hub_openapi.py` writes the result to
`hub/openapi.json`, and CI fails when that file is stale.

Every request and response shape below was read off the handler that serves
it. A shape that is genuinely the caller's or a vendor's own (a webhook
payload, an MCP tool's result) is declared with `additionalProperties: true`
and says so.
"""
from __future__ import annotations

import json
import logging
import re

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route

from commontrace import __version__
from hub import alerts, crud, events, plans, rbac, retention, scopes

OPENAPI_VERSION = "3.1.0"
DOCUMENT_PATH = "/api/v1/openapi.json"

_DUMMY_SECRET = "openapi-reference-build-" + "x" * 24


class OpenAPIDriftError(RuntimeError):
    """The live route table and the declarations in this module disagree."""


# ---------------------------------------------------------------------------
# Small builders
# ---------------------------------------------------------------------------


def _schema_ref(name: str) -> dict:
    return {"$ref": "#/components/schemas/" + name}


def _response_ref(name: str) -> dict:
    return {"$ref": "#/components/responses/" + name}


def _string(description: str = "", **extra) -> dict:
    out = {"type": "string", **extra}
    if description:
        out["description"] = description
    return out


def _integer(description: str = "", **extra) -> dict:
    out = {"type": "integer", **extra}
    if description:
        out["description"] = description
    return out


def _number(description: str = "", **extra) -> dict:
    out = {"type": "number", **extra}
    if description:
        out["description"] = description
    return out


def _boolean(description: str = "") -> dict:
    out: dict = {"type": "boolean"}
    if description:
        out["description"] = description
    return out


def _array(items: dict, description: str = "", **extra) -> dict:
    out = {"type": "array", "items": items, **extra}
    if description:
        out["description"] = description
    return out


def _object(properties: dict, required: tuple = (), *, additional: bool = False, description: str = "") -> dict:
    out: dict = {"type": "object", "properties": properties, "additionalProperties": additional}
    if required:
        out["required"] = list(required)
    if description:
        out["description"] = description
    return out


def _free_object(description: str) -> dict:
    return {"type": "object", "additionalProperties": True, "description": description}


def _query(name: str, schema: dict, description: str, *, required: bool = False) -> dict:
    return {"name": name, "in": "query", "required": required, "description": description, "schema": schema}


def _header(name: str, schema: dict, description: str, *, required: bool = False) -> dict:
    return {"name": name, "in": "header", "required": required, "description": description, "schema": schema}


def _json_body(schema: dict, description: str = "", *, media: tuple = ("application/json",)) -> dict:
    out: dict = {"required": True, "content": {m: {"schema": schema} for m in media}}
    if description:
        out["description"] = description
    return out


def _form_body(properties: dict, required: tuple = (), description: str = "") -> dict:
    out: dict = {
        "required": bool(required),
        "content": {"application/x-www-form-urlencoded": {"schema": _object(properties, required, additional=True)}},
    }
    if description:
        out["description"] = description
    return out


def _json(description: str, schema: dict, *, media: str = "application/json", headers: dict | None = None) -> dict:
    out: dict = {"description": description, "content": {media: {"schema": schema}}}
    if headers:
        out["headers"] = headers
    return out


def _html(description: str) -> dict:
    return {"description": description, "content": {"text/html": {"schema": {"type": "string"}}}}


def _text(description: str) -> dict:
    return {"description": description, "content": {"text/plain": {"schema": {"type": "string"}}}}


def _see_other(description: str) -> dict:
    return {"description": description, "headers": {"Location": {"$ref": "#/components/headers/Location"}}}


def _error(description: str) -> dict:
    return _json(description, _schema_ref("Error"))


def _op(
    operation_id: str,
    tag: str,
    summary: str,
    *,
    security: list,
    responses: dict,
    description: str = "",
    parameters: tuple = (),
    body: dict | None = None,
) -> dict:
    out: dict = {
        "operationId": operation_id,
        "tags": [tag],
        "summary": summary,
        "security": security,
        "responses": dict(responses),
    }
    if description:
        out["description"] = description
    if parameters:
        out["parameters"] = list(parameters)
    if body is not None:
        out["requestBody"] = body
    return out


# ---------------------------------------------------------------------------
# Security requirements
# ---------------------------------------------------------------------------

PUBLIC: list = []
API_KEY = [{"apiKey": []}]
ADMIN = [{"adminBasic": []}]
CONSOLE = [{"consoleSession": []}]
SCIM = [{"scimBearer": []}]
MCP = [{"mcpBearer": []}]
METRICS = [{"metricsBearer": []}, {}]
STRIPE = [{"stripeSignature": []}]
CONNECTOR = [
    {"githubSignature": []},
    {"greenhouseSignature": []},
    {"intercomSignature": []},
    {"zendeskSignature": [], "zendeskSignatureTimestamp": []},
]

_SECURITY_SCHEMES = {
    "apiKey": {
        "type": "apiKey", "in": "header", "name": "X-API-Key",
        "description": "An org API key (`ct_...`). Each route also requires a scope on that key "
                       "(read, write); a key without it gets 403.",
    },
    "mcpBearer": {
        "type": "http", "scheme": "bearer",
        "description": "`Authorization: Bearer <api-key>` (an org API key), or an OIDC identity token "
                       "when the deployment configures an identity provider. Checked by "
                       "ApiKeyAuthMiddleware before MCP dispatch.",
    },
    "adminBasic": {
        "type": "http", "scheme": "basic",
        "description": "HTTP Basic: any username, the operator admin token (HUB_ADMIN_TOKEN) as the "
                       "password. Form posts additionally carry a per-action `csrf` token the pages embed.",
    },
    "consoleSession": {
        "type": "apiKey", "in": "cookie", "name": "ct_console",
        "description": "Signed session cookie set by POST /app/signin (HttpOnly, SameSite=Strict, "
                       "Path=/app, 8 hours). It names the API key used to sign in; revoking that key "
                       "ends the session.",
    },
    "scimBearer": {
        "type": "http", "scheme": "bearer",
        "description": "`Authorization: Bearer <api-key>` for a key carrying the `scim` scope.",
    },
    "metricsBearer": {
        "type": "http", "scheme": "bearer",
        "description": "HUB_METRICS_TOKEN. Required only when the deployment sets one.",
    },
    "stripeSignature": {
        "type": "apiKey", "in": "header", "name": "Stripe-Signature",
        "description": "Stripe's webhook signature over the raw body, checked against HUB_STRIPE_WEBHOOK_SECRET.",
    },
    "githubSignature": {
        "type": "apiKey", "in": "header", "name": "X-Hub-Signature-256",
        "description": "GitHub connectors: HMAC-SHA256 of the raw body with the connector's secret.",
    },
    "greenhouseSignature": {
        "type": "apiKey", "in": "header", "name": "Signature",
        "description": "Greenhouse connectors: HMAC-SHA256 of the raw body with the connector's secret.",
    },
    "intercomSignature": {
        "type": "apiKey", "in": "header", "name": "X-Hub-Signature",
        "description": "Intercom connectors: HMAC-SHA1 of the raw body with the connector's secret.",
    },
    "zendeskSignature": {
        "type": "apiKey", "in": "header", "name": "X-Zendesk-Webhook-Signature",
        "description": "Zendesk connectors: signature over the timestamp and raw body.",
    },
    "zendeskSignatureTimestamp": {
        "type": "apiKey", "in": "header", "name": "X-Zendesk-Webhook-Signature-Timestamp",
        "description": "Zendesk connectors: the signed timestamp; outside the replay window is refused.",
    },
}

# ---------------------------------------------------------------------------
# Reusable shapes
# ---------------------------------------------------------------------------

_ISO_MOMENT = "ISO 8601 date or date-time (a trailing Z is accepted)."
_PREVIEW = f"Brief preview: at most {crud.BRIEF_PREVIEW_CHARS} characters, ending in an ellipsis when cut."


def _schemas() -> dict:
    roles = list(rbac.ROLES)
    signed = _object({
        "record": _free_object("The signed record."),
        "principal": _string("Signing principal id."),
        "organization": _string("The principal's organization."),
        "authority": _string("The authority the principal signs with."),
        "digest": _string("SHA-256 hex digest of the canonical record bytes.", pattern="^[0-9a-f]{64}$"),
        "algorithm": _string("Signature algorithm (`ed25519`, or absent for HMAC).", enum=["ed25519", "hmac-sha256"]),
        "signature": _string("Base64 Ed25519 signature, or HMAC hex."),
    }, ("record", "principal", "organization", "authority", "digest", "signature"), additional=True,
        description="A signed receipt (commontrace/origin.py `sign`).")
    listing_summary = _object({
        "id": _string("Listing id: the signed listing's digest."),
        "name": _string(),
        "description": _string(),
        "tags": _array(_string(), maxItems=50),
        "publisher": _string("Publisher organization."),
        "publisher_id": _string("Publisher principal id."),
        "lift": _number("Pooled lift effect."),
        "lift_ci": _array(_number(), "Pooled lift interval [ci_low, ci_high].", minItems=2, maxItems=2),
        "organizations": _integer("Independent organizations behind the lift certificate."),
        "licence": _string("Licence id."),
        "price": {"oneOf": [
            _object({"amount_usd": _number(minimum=0, maximum=1_000_000),
                     "per": _string(enum=["install", "month", "year"])}, ("amount_usd", "per")),
            {"type": "null"},
        ]},
        "status": _string("`listed` or `withdrawn`.", enum=["listed", "withdrawn"]),
        "listed_at": {"type": ["string", "null"], "description": "ISO 8601 time the Hub accepted the listing."},
    }, ("id", "name", "description", "tags", "publisher", "publisher_id", "lift", "lift_ci", "organizations",
        "licence", "price", "status", "listed_at"))
    trace_brief = _object({
        "id": _string("Trace id (UUID).", format="uuid"),
        "title": _string(),
        "context_text": _string(_PREVIEW),
        "solution_text": _string(_PREVIEW),
        "tags": _array(_string()),
        "agent_type": _string(),
        "created_at": _string("ISO 8601, UTC, Z-suffixed.", format="date-time"),
        "trust": _number(),
        "quarantined": _boolean(),
        "brief": {"const": True},
        "contributor_name": _string("The contributor, or empty."),
        "agent_id": _string(),
        "profile": _string(),
        "scopes": _array(_string()),
        "valid_from": _string(_ISO_MOMENT),
        "valid_until": _string(_ISO_MOMENT),
        "extensions": _free_object("Protocol extensions stored with the trace."),
        "watch_condition": _string(),
        "review_after": _string(),
        "supersedes_trace_id": _string(),
        "superseded_by_trace_id": _string(),
        "superseded_at": _string(),
        "contributor": _string(),
        "retrievals": _integer(),
        "depth": _integer(),
        "votes": _array(_object({
            "vote_type": _string(enum=list(crud.VALID_VOTE_TYPES)),
            "feedback_tag": _string(),
            "feedback_text": _string(),
        }, ("vote_type", "feedback_tag", "feedback_text"))),
        "related": _array(_object({"relationship": _string(), "trace_id": _string()}, ("relationship", "trace_id"))),
        "outcome": _free_object("The recorded outcome object (e.g. `resolved`)."),
        "shared_with_commons": _boolean(),
        "quarantine_reason": _string(),
        "evidence": _object({"verdict": _string()}, ("verdict",), additional=True,
                            description="Causal evidence for this trace, present while an experiment has data."),
    }, ("id", "title", "context_text", "solution_text", "tags", "agent_type", "created_at", "trust",
        "quarantined", "brief", "contributor_name"),
        description="A brief search hit. Optional fields appear only when non-empty.")
    tags_field = {"oneOf": [_array(_string()), _string("Comma-separated.")]}
    scim_meta = _object({"resourceType": _string(), "created": _string(format="date-time")},
                        ("resourceType", "created"))
    scim_members = _array(_object({"value": _string("User id."), "display": _string()}, ("value",), additional=True))
    patch_op = _object({
        "op": _string("Case-insensitive.", enum=["add", "replace", "remove", "Add", "Replace", "Remove"]),
        "path": _string("`active` / `displayName` / `members` / `members[value eq \"<id>\"]`, or empty."),
        "value": {"description": "The new value; any JSON type."},
    }, ("op",), additional=True)
    return {
        "Error": _object({
            "error": _string("Machine-readable code, e.g. `bad_request`, `unauthorized`, `forbidden`, "
                             "`not_found`, `conflict`, `rate_limited`, `payload_too_large`."),
            "detail": _string("Human-readable explanation; omitted when there is nothing to add."),
        }, ("error",)),
        "RateLimitedError": _object({
            "error": {"const": "rate_limited"},
            "detail": _string(),
            "retry_after": _integer("Seconds to wait (also sent as Retry-After). Present on MCP 429s.", minimum=1),
        }, ("error",)),
        "ScimError": _object({
            "schemas": _array({"const": "urn:ietf:params:scim:api:messages:2.0:Error"}),
            "detail": _string(),
            "status": _string("The HTTP status, as a string."),
            "scimType": _string(enum=["invalidValue", "invalidFilter", "uniqueness"]),
        }, ("schemas", "detail", "status")),
        "JsonRpcError": _object({
            "jsonrpc": {"const": "2.0"},
            "id": {"type": ["string", "integer", "null"]},
            "error": _object({"code": _integer(), "message": _string(), "data": {}}, ("code", "message")),
        }, ("jsonrpc", "error"), description="A JSON-RPC 2.0 error object, as the MCP transport returns it."),
        "JsonRpcRequest": _object({
            "jsonrpc": {"const": "2.0"},
            "id": {"type": ["string", "integer"]},
            "method": _string("e.g. `initialize`, `tools/list`, `tools/call`, `ping`."),
            "params": _free_object("Method parameters; for `tools/call`, `{name, arguments}`."),
        }, ("jsonrpc", "id", "method")),
        "JsonRpcNotification": _object({
            "jsonrpc": {"const": "2.0"},
            "method": _string("e.g. `notifications/initialized`."),
            "params": _free_object("Notification parameters."),
        }, ("jsonrpc", "method")),
        "JsonRpcResponse": _object({
            "jsonrpc": {"const": "2.0"},
            "id": {"type": ["string", "integer"]},
            "result": _free_object("Method result. A Hub tool failure is a successful JSON-RPC result whose "
                                   "content carries `{error, detail}`, not an HTTP error."),
        }, ("jsonrpc", "id", "result")),
        "JsonRpcMessage": {"anyOf": [
            _schema_ref("JsonRpcRequest"), _schema_ref("JsonRpcNotification"),
            _schema_ref("JsonRpcResponse"), _schema_ref("JsonRpcError"),
        ]},
        "ContributeTraceRequest": _object({
            "title": _string("Required, non-blank. At most HUB_MAX_TITLE_CHARS (default 500) characters."),
            "context_text": _string("Required, non-blank. At most HUB_MAX_TEXT_CHARS (default 20000)."),
            "solution_text": _string("Required, non-blank. At most HUB_MAX_TEXT_CHARS (default 20000)."),
            "tags": {**tags_field, "description": "At most HUB_MAX_TAGS (default 20), each at most "
                                                  "HUB_MAX_TAG_CHARS (default 64) characters."},
            "agent_type": _string("Defaults to `general`."),
            "agent_id": _string("Counts toward the plan's agent limit.", maxLength=128),
            "idempotency_key": _string("Retrying with the same key and payload replays the first result; "
                                       "the same key with a different payload is 409.", maxLength=128),
            "scopes": {**tags_field, "description": f"Visibility scopes: at most {crud.MAX_SCOPES}, each at most "
                                                    f"{crud.MAX_SCOPE_CHARS} characters."},
            "valid_from": _string(_ISO_MOMENT),
            "valid_until": _string(_ISO_MOMENT + " Must be after valid_from."),
        }, ("title", "context_text", "solution_text"), additional=True,
            description="Unknown keys are ignored. The serialized trace must fit HUB_MAX_TRACE_BYTES (default 65536)."),
        "ContributeTraceResponse": _object({
            "id": _string("The new trace's id (or the original's, on an idempotent replay).", format="uuid"),
            "quarantined": _boolean("Held for operator review instead of being served."),
            "quarantine_reason": _string(),
            "possible_duplicates": _array(_string(format="uuid"), "Ids of near-duplicate traces already stored."),
            "auto_proposed_to_commons": _boolean("Proposed to the Knowledge Base (org opt-in). Absent on a replay."),
        }, ("id", "quarantined", "quarantine_reason", "possible_duplicates")),
        "SearchTracesRequest": _object({
            "q": _string("Required, non-blank query text."),
            "limit": _integer(f"Clamped to 1..{50}; default 3.", minimum=1, maximum=50, default=3),
            "scope": _string("Only traces in this scope or unscoped ones.", maxLength=crud.MAX_SCOPE_CHARS),
            "as_of": _string("Only traces valid at this moment. " + _ISO_MOMENT),
        }, ("q",), additional=True),
        "SearchTracesResponse": _object({"results": _array(_schema_ref("TraceBrief"))}, ("results",)),
        "TraceBrief": trace_brief,
        "CreateKeyRequest": _object({
            "email": _string("Truncated to 200 characters. Its local part names the org when display_name is absent."),
            "display_name": _string("Organization name. Truncated to 200 characters."),
        }, additional=True, description="At least one of display_name or email is required."),
        "CreateKeyResponse": _object({
            "api_key": _string("The raw API key. Shown once; store it now."),
            "org_id": _string("The new organization's id.", format="uuid"),
        }, ("api_key", "org_id")),
        "PublisherCard": _object({
            "id": _string("Publisher principal id.", pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"),
            "organization": _string(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"),
            "role": {"const": "publisher"},
            "authority": {"const": "market-publisher"},
            "algorithm": {"const": "ed25519"},
            "public_key": _string("Base64 32-byte Ed25519 public key.", contentEncoding="base64"),
        }, ("id", "organization", "role", "authority", "algorithm", "public_key")),
        "RegisterPublisherRequest": _object({
            "card": _object({
                "id": _string(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"),
                "organization": _string(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"),
                "public_key": _string("Base64 32-byte Ed25519 public key.", contentEncoding="base64"),
            }, ("id", "organization", "public_key"), additional=True),
        }, ("card",)),
        "SignedReceipt": signed,
        "SignedListing": {
            "allOf": [signed],
            "description": "A publisher-signed lesson listing (commontrace/market_listing.py). `record` holds "
                           "`kind` (`lesson-listing`), `schema_version` (1), `lesson` {frontmatter, body}, "
                           "`lesson_sha256`, `licence` {id, terms, price?, outcome_share?}, `certificate` (a "
                           "referee-signed replicated-lift certificate) and `listed_at`.",
        },
        "UploadListingRequest": _object({"listing": _schema_ref("SignedListing")}, ("listing",),
                                        description="At most 256 KiB + 4 KiB, or 400."),
        "ListingStatus": _object({"id": _string(), "status": _string(enum=["listed", "withdrawn"])}, ("id", "status")),
        "ListingSummary": listing_summary,
        "ListingSearchResponse": _object({"listings": _array(_schema_ref("ListingSummary"))}, ("listings",)),
        "ListingDetail": _object({"listing": _schema_ref("SignedListing"), "summary": _schema_ref("ListingSummary")},
                                 ("listing", "summary")),
        "OtlpExportRequest": _object({
            "resourceSpans": _array(_object({
                "resource": _free_object("OTLP resource; ignored."),
                "scopeSpans": _array(_object({
                    "scope": _free_object("Instrumentation scope; ignored."),
                    "spans": _array(_schema_ref("OtlpSpan")),
                }, additional=True)),
            }, additional=True)),
        }, additional=True, description="OTLP/JSON ExportTraceServiceRequest. At most 500 spans per request."),
        "OtlpSpan": _object({
            "name": _string("Becomes the trace title."),
            "spanId": _string("Idempotency key for the stored trace (else traceId)."),
            "traceId": _string(),
            "attributes": {
                "description": "OTLP KeyValue list, or a flat object. Read: gen_ai.prompt / gen_ai.input.messages / "
                               "gen_ai.request.messages / traceloop.entity.input / input.value (context); "
                               "gen_ai.completion / gen_ai.output.messages / gen_ai.response.messages / "
                               "traceloop.entity.output / output.value (solution); gen_ai.system and "
                               "gen_ai.request.model (tags); commontrace.occasion_id / session.id / "
                               "gen_ai.conversation.id with commontrace.occasion.succeeded (occasion outcome).",
                "oneOf": [
                    _array(_object({"key": _string(), "value": _free_object("OTLP AnyValue.")}, ("key",),
                                   additional=True)),
                    _free_object("Flat attribute map."),
                ],
            },
            "status": _object({"code": {"type": ["string", "integer"]}, "message": _string()}, additional=True),
        }, additional=True, description="A span with neither context nor solution text is skipped."),
        "OtlpExportResponse": _object({
            "accepted": _integer(minimum=0),
            "skipped": _integer("Spans with no context or solution text.", minimum=0),
            "occasions_resolved": _integer("Absent when the request held no spans.", minimum=0),
            "errors": _array(_object({"span_id": _string(), "error": _string()}, ("span_id", "error"))),
        }, ("accepted", "skipped", "errors")),
        "ConnectorResult": _object({
            "status": _string(enum=["ok", "dry-run", "duplicate", "unusable"]),
            "delivery": _string("The vendor's delivery id (replay protection)."),
            "detail": _string("Why the payload was unusable."),
            "signals": _array(_object({
                "kind": _string(enum=["candidate_success", "success", "failure", "reversal"]),
                "occasion_id": _string(),
                "event": _string(),
                "action": _string("What the Hub did with the signal."),
                "resolved": _integer("Observations resolved (live connectors only)."),
            }, ("kind", "occasion_id", "event", "action"))),
        }, ("status",)),
        "ConnectorError": _object({"error": _string()}, ("error",)),
        "HealthStatus": _object({"status": {"const": "ok"}}, ("status",)),
        "Readiness": _object({
            "status": _string(enum=["ready", "not_ready", "rate_limited"]),
            "database": _string(enum=["ok", "unreachable"]),
            "retry_after": _integer(minimum=1),
        }, ("status",)),
        "MetricsStatus": _object({
            "status": _string(enum=["unauthorized", "rate_limited"]), "retry_after": _integer(minimum=1),
        }, ("status",)),
        "Disclosure": _object({
            "data_region": _string("Or `not disclosed by this deployment's operator`."),
            "region_enforced_for_pinned_orgs": _boolean(),
            "operator_legal_name": _string(),
            "operator_support_contact": _string(),
            "note": _string("States that the values are self-reported, not attested."),
        }, ("data_region", "region_enforced_for_pinned_orgs", "operator_legal_name",
            "operator_support_contact", "note")),
        "StripeEvent": _object({
            "id": _string("Event id; a repeated id is acknowledged and ignored."),
            "type": _string("e.g. checkout.session.completed, customer.subscription.updated/deleted."),
            "data": _object({"object": _free_object("The Stripe object the event is about.")}, additional=True),
        }, additional=True, description="A Stripe webhook event; the Hub reads only the fields listed."),
        "ScimUser": _object({
            "schemas": _array({"const": "urn:ietf:params:scim:schemas:core:2.0:User"}),
            "id": _string(format="uuid"),
            "userName": _string("The user's email."),
            "displayName": _string(),
            "active": _boolean(),
            "meta": scim_meta,
        }, ("schemas", "id", "userName", "displayName", "active", "meta")),
        "ScimUserInput": _object({
            "userName": _string("Required on create; unique within the org."),
            "displayName": _string(),
            "active": {"oneOf": [_boolean(), _string(enum=["true", "false", "1", "0"])]},
        }, additional=True,
            description="Provisioned users get the `viewer` role. PUT reads only displayName and active."),
        "ScimGroup": _object({
            "schemas": _array({"const": "urn:ietf:params:scim:schemas:core:2.0:Group"}),
            "id": _string(format="uuid"),
            "displayName": _string(),
            "members": scim_members,
            "meta": scim_meta,
        }, ("schemas", "id", "displayName", "members", "meta")),
        "ScimGroupInput": _object({
            "displayName": _string("Required on create; unique within the org."),
            "externalId": _string("Stored on create."),
            "members": _array(_object({"value": _string("User id in this org.")}, ("value",), additional=True)),
        }, additional=True, description="Members that are not users of this org are dropped."),
        "ScimPatchOp": _object({
            "schemas": _array(_string()),
            "Operations": _array(patch_op),
            "operations": _array(patch_op, "Lower-case alias accepted."),
            "active": {"description": "Users only: a bare `active` with no Operations."},
        }, additional=True),
        "ScimUserList": _object({
            "schemas": _array({"const": "urn:ietf:params:scim:api:messages:2.0:ListResponse"}),
            "totalResults": _integer(minimum=0),
            "startIndex": _integer(minimum=1),
            "itemsPerPage": _integer(minimum=0),
            "Resources": _array(_schema_ref("ScimUser")),
        }, ("schemas", "totalResults", "startIndex", "itemsPerPage", "Resources")),
        "ScimGroupList": _object({
            "schemas": _array({"const": "urn:ietf:params:scim:api:messages:2.0:ListResponse"}),
            "totalResults": _integer(minimum=0),
            "startIndex": _integer(minimum=1),
            "itemsPerPage": _integer(minimum=0),
            "Resources": _array(_schema_ref("ScimGroup")),
        }, ("schemas", "totalResults", "startIndex", "itemsPerPage", "Resources")),
        "ScimServiceProviderConfig": _object({
            "schemas": _array(_string()),
            "patch": _free_object("`{supported: true}`"),
            "bulk": _free_object("`{supported: false}`"),
            "filter": _free_object("`{supported: true, maxResults: 200}`"),
            "changePassword": _free_object("`{supported: false}`"),
            "sort": _free_object("`{supported: false}`"),
            "etag": _free_object("`{supported: false}`"),
            "authenticationSchemes": _array(_free_object("An authentication scheme.")),
        }, ("schemas", "patch", "bulk", "filter", "changePassword", "sort", "etag", "authenticationSchemes")),
        "ScimDiscoveryList": _object({
            "schemas": _array(_string()),
            "totalResults": _integer(),
            "Resources": _array(_free_object("A ResourceType or Schema resource.")),
        }, ("schemas", "totalResults", "Resources")),
        "OpenAPIDocument": _free_object("This OpenAPI 3.1.0 document."),
        "Roles": _string("An RBAC role.", enum=roles),
    }


_SHARED_RESPONSES = {
    "Overloaded": _json("Load shed: too many concurrent requests on this replica.", _schema_ref("Error"),
                        headers={"Retry-After": {"$ref": "#/components/headers/RetryAfter"}}),
    "Timeout": _json("The request exceeded HUB_REQUEST_TIMEOUT_SECONDS and was cancelled.", _schema_ref("Error")),
    "PayloadTooLarge": _json("The body exceeds HUB_MAX_REQUEST_BODY_BYTES (default 1 MiB).", _schema_ref("Error")),
    "Unauthorized": _error("Missing or rejected X-API-Key."),
    "Forbidden": _error("The key lacks the scope this route requires."),
    "RateLimited": _json("Too many requests from this address or org.", _schema_ref("RateLimitedError"),
                         headers={"Retry-After": {"$ref": "#/components/headers/RetryAfter"}}),
    "AdminUnauthorized": {
        "description": "Missing or wrong admin credentials.",
        "headers": {"WWW-Authenticate": {"description": "`Basic realm=...`", "schema": {"type": "string"}}},
        "content": {"text/plain": {"schema": {"type": "string"}}},
    },
    "AdminForbidden": _text("Cross-site request, or a missing/invalid per-action `csrf` token."),
    "AdminRateLimited": {
        "description": "Too many admin requests from this address.",
        "headers": {"Retry-After": {"$ref": "#/components/headers/RetryAfter"}},
        "content": {"text/plain": {"schema": {"type": "string"}}},
    },
    "AdminDone": _see_other("Done; redirects to the affected page with a signed flash message (`done`, `sig`)."),
    "CrossOriginRefused": _text("A state-changing request from another origin's page."),
    "MalformedId": _text("A `*_id` path segment is not a UUID."),
    "SignInRedirect": _see_other("No valid console session: redirect to /app/signin."),
    "ScimBadRequest": _json("Invalid JSON, filter or paging parameters.", _schema_ref("ScimError"),
                            media="application/scim+json"),
    "ScimUnauthorized": _json("Missing, invalid, or not `scim`-scoped bearer token.", _schema_ref("ScimError"),
                              media="application/scim+json"),
    "ScimNotFound": _json("No such resource in the token's organization.", _schema_ref("ScimError"),
                          media="application/scim+json"),
    "ScimRateLimited": {
        "description": "Too many SCIM auth attempts from this address.",
        "headers": {"Retry-After": {"$ref": "#/components/headers/RetryAfter"}},
        "content": {"application/scim+json": {"schema": _schema_ref("ScimError")}},
    },
}

_HEADERS = {
    "RetryAfter": {"description": "Seconds to wait before retrying.", "schema": {"type": "integer", "minimum": 1}},
    "Location": {"description": "Where the browser goes next.", "schema": {"type": "string"}},
    "McpSessionId": {"description": "The MCP session id to send on every later request.",
                     "schema": {"type": "string"}},
}

_TAGS = [
    ("MCP", "The MCP streamable-HTTP endpoint (path HUB_STREAMABLE_HTTP_PATH, default /mcp). Tools are "
            "discovered with `tools/list`; their arguments are described by the server itself."),
    ("REST", "The /api/v1 REST API (HUB_REST_API_ENABLED) the CommonTrace client plugin speaks. X-API-Key."),
    ("Marketplace", "The lesson marketplace under /api/v1/market (HUB_REST_API_ENABLED). Uploads are verified "
                    "against HUB_MARKET_REFEREES_FILE."),
    ("Telemetry", "Client beacons under /api/v1/telemetry (HUB_REST_API_ENABLED). Authenticated, body ignored."),
    ("Signup", "Self-serve account creation (HUB_SIGNUP_ENABLED; POST /api/v1/keys also needs the REST API)."),
    ("Console", "The customer console under /app (HUB_CONSOLE_SECRET): HTML pages and form posts."),
    ("Admin", "The operator console under /admin (HUB_ADMIN_TOKEN): HTML pages and form posts. Each form "
              "names its target in a body field (`org_id`, `user_id`, `key_id`, `trace_id`, `hold_id`, "
              "`submission_id`, `digest` or `target`) together with a matching `csrf` token; that field, not "
              "the URL, selects what is changed."),
    ("Billing", "The Stripe webhook (HUB_STRIPE_WEBHOOK_SECRET)."),
    ("OTLP", "OpenTelemetry trace ingest (HUB_OTLP_INGEST_ENABLED), OTLP/JSON only."),
    ("Connectors", "System-of-record webhooks that record outcomes (HUB_CONNECTORS_ENABLED)."),
    ("SCIM", "SCIM 2.0 user and group provisioning (always served)."),
    ("Health", "Liveness, readiness, metrics and public discovery documents (always served)."),
]

_PATH_PARAMS = {
    "org_id": "Organization id (UUID).",
    "user_id": "User id (UUID).",
    "group_id": "SCIM group id (UUID).",
    "key_id": "API key id (UUID).",
    "rule_id": "Alert rule id (UUID).",
    "endpoint_id": "Webhook endpoint id (UUID).",
    "token": "A signed, expiring proof-share token issued from the console.",
    "principal_id": "Publisher principal id.",
    "listing_id": "Listing id (the signed listing's digest).",
    "connector_id": "Connector id (UUID). An unknown id answers 401, indistinguishable from a bad signature.",
}


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _csrf_form(target: str, fields: dict | None = None, required: tuple = (), description: str = "") -> dict:
    props = {
        target: _string("The object this action applies to (authoritative over the URL)."),
        "csrf": _string("Per-action token embedded in the page that renders this form."),
        **(fields or {}),
    }
    return _form_body(props, (target, "csrf", *required), description)


def _admin_post(operation_id: str, summary: str, target: str, *, fields: dict | None = None,
                required: tuple = (), description: str = "", ok: dict | None = None,
                extra: dict | None = None, redirects: bool = True) -> dict:
    responses = {
        "401": _response_ref("AdminUnauthorized"),
        "403": _response_ref("AdminForbidden"),
        "429": _response_ref("AdminRateLimited"),
    }
    if redirects:
        responses["303"] = _response_ref("AdminDone")
    if ok:
        responses.update(ok)
    if extra:
        responses.update(extra)
    return _op(operation_id, "Admin", summary, security=ADMIN, description=description,
               body=_csrf_form(target, fields, required), responses=responses)


def _admin_page(operation_id: str, summary: str, parameters: tuple = (), description: str = "") -> dict:
    return _op(operation_id, "Admin", summary, security=ADMIN, parameters=parameters, description=description,
               responses={
                   "200": _html("The page."),
                   "401": _response_ref("AdminUnauthorized"),
                   "429": _response_ref("AdminRateLimited"),
               })


_FLASH_PARAMS = (
    _query("done", _string(maxLength=500), "Flash message from the previous action."),
    _query("sig", _string(), "Signature over `done`; an unsigned message is not shown."),
)


def _console_page(operation_id: str, summary: str, parameters: tuple = (), description: str = "") -> dict:
    return _op(operation_id, "Console", summary, security=CONSOLE, parameters=parameters, description=description,
               responses={"200": _html("The page."), "303": _response_ref("SignInRedirect")})


def _console_post(operation_id: str, summary: str, *, fields: dict | None = None, required: tuple = (),
                  page: str = "", redirect: str = "", has_id: bool = False, description: str = "") -> dict:
    responses: dict = {"403": _response_ref("CrossOriginRefused")}
    if redirect:
        responses["303"] = _see_other(redirect)
    else:
        responses["303"] = _response_ref("SignInRedirect")
    if page:
        responses["200"] = _html(page)
    if has_id:
        responses["404"] = _response_ref("MalformedId")
    body = _form_body(fields, required) if fields else None
    return _op(operation_id, "Console", summary, security=CONSOLE, body=body, description=description,
               responses=responses)


def _api(operation_id: str, tag: str, summary: str, *, ok: dict, errors: tuple = (), body: dict | None = None,
         parameters: tuple = (), description: str = "", security: list | None = None) -> dict:
    responses = dict(ok)
    for code in errors:
        responses[code] = {
            "401": _response_ref("Unauthorized"),
            "403": _response_ref("Forbidden"),
            "429": _response_ref("RateLimited"),
        }.get(code) or _error({
            "400": "Malformed body or invalid field.",
            "402": "The plan's entitlement is exhausted.",
            "404": "Not found.",
            "409": "Conflict.",
            "413": "Too many items in one request.",
            "415": "Unsupported Content-Type.",
            "422": "The listing did not verify.",
            "503": "The marketplace trusts no referee on this Hub.",
        }[code])
    return _op(operation_id, tag, summary, security=API_KEY if security is None else security, body=body,
               parameters=parameters, description=description, responses=responses)


def _scim(operation_id: str, summary: str, *, ok: dict, extra: tuple = (), body: dict | None = None,
          parameters: tuple = (), description: str = "") -> dict:
    responses = dict(ok)
    responses["401"] = _response_ref("ScimUnauthorized")
    responses["429"] = _response_ref("ScimRateLimited")
    for code in extra:
        responses[code] = {
            "400": _response_ref("ScimBadRequest"),
            "404": _response_ref("ScimNotFound"),
            "409": _json("A user or group with that name already exists.", _schema_ref("ScimError"),
                         media="application/scim+json"),
        }[code]
    return _op(operation_id, "SCIM", summary, security=SCIM, body=body, parameters=parameters,
               description=description, responses=responses)


def _scim_json(description: str, schema: str) -> dict:
    return _json(description, _schema_ref(schema), media="application/scim+json")


def _scim_body(schema: str, description: str = "") -> dict:
    return _json_body(_schema_ref(schema), description, media=("application/scim+json", "application/json"))


def _operations() -> dict[tuple[str, str], dict]:
    roles = list(rbac.ROLES)
    key_scopes = _array(_string(enum=list(scopes.ALL_SCOPES)), "Repeat the field once per scope.", minItems=1)
    retention_kinds = list(retention.KINDS)
    mcp_session = _header("Mcp-Session-Id", _string(), "The session id the server returned from `initialize`. "
                                                       "Required on every request after it.")
    mcp_version = _header("MCP-Protocol-Version", _string(), "The negotiated MCP protocol version.")
    mcp_errors = {
        "400": _json("Malformed JSON-RPC, a missing Mcp-Session-Id, or an invalid Content-Type.",
                     _schema_ref("JsonRpcError")),
        "401": _error("Missing or rejected bearer credential (ApiKeyAuthMiddleware)."),
        "403": _text("Origin not allowed (DNS-rebinding protection when the Hub binds to localhost)."),
        "404": _json("Unknown, expired or terminated MCP session.", _schema_ref("JsonRpcError")),
        "421": _text("Host not allowed (DNS-rebinding protection when the Hub binds to localhost)."),
        "429": _response_ref("RateLimited"),
    }
    ops = {
        # --- MCP -----------------------------------------------------------
        ("POST", "/mcp"): _op(
            "mcpPost", "MCP", "Send one JSON-RPC 2.0 message",
            security=MCP, parameters=(
                _header("Accept", _string(), "Must list both application/json and text/event-stream.", required=True),
                mcp_session, mcp_version,
            ),
            body=_json_body(_schema_ref("JsonRpcMessage"), "A JSON-RPC request, notification or response."),
            description="Requests are answered on a server-sent-event stream carrying JSON-RPC messages; "
                        "`initialize` returns the Mcp-Session-Id header. Notifications and responses are "
                        "accepted with 202.",
            responses={
                "200": {
                    "description": "The response stream for this request.",
                    "headers": {"Mcp-Session-Id": {"$ref": "#/components/headers/McpSessionId"}},
                    "content": {
                        "text/event-stream": {"schema": _string("SSE events whose `data` is a JSON-RPC message.")},
                        "application/json": {"schema": _schema_ref("JsonRpcMessage")},
                    },
                },
                "202": {"description": "Notification or response accepted; no body."},
                **mcp_errors,
                "406": _json("Accept does not list both required media types.", _schema_ref("JsonRpcError")),
                "415": _json("Content-Type is not application/json.", _schema_ref("JsonRpcError")),
            },
        ),
        ("GET", "/mcp"): _op(
            "mcpStream", "MCP", "Open the server-to-client event stream",
            security=MCP, parameters=(
                _header("Accept", _string(), "Must list text/event-stream.", required=True),
                mcp_session, mcp_version,
                _header("Last-Event-ID", _string(), "Resume after this event, when the server keeps events."),
            ),
            responses={
                "200": {"description": "Server-sent events, each a JSON-RPC message.",
                        "content": {"text/event-stream": {"schema": _string()}}},
                **mcp_errors,
                "406": _json("Accept does not list text/event-stream.", _schema_ref("JsonRpcError")),
                "409": _json("A stream is already open for this session.", _schema_ref("JsonRpcError")),
            },
        ),
        ("DELETE", "/mcp"): _op(
            "mcpEndSession", "MCP", "Terminate an MCP session",
            security=MCP, parameters=(_header("Mcp-Session-Id", _string(), "The session to end.", required=True),
                                      mcp_version),
            description="DELETE is exempt from the per-org read rate limit.",
            responses={"200": {"description": "Session terminated; no body."}, **mcp_errors,
                       "405": _json("This session cannot be terminated.", _schema_ref("JsonRpcError"))},
        ),

        # --- REST ------------------------------------------------------------
        ("POST", "/api/v1/traces"): _api(
            "contributeTrace", "REST", "Contribute a trace",
            description="Needs the `write` scope. Suspicious content is stored quarantined, not refused.",
            body=_json_body(_schema_ref("ContributeTraceRequest")),
            ok={"201": _json("Stored (or replayed for a repeated idempotency_key).",
                             _schema_ref("ContributeTraceResponse"))},
            errors=("400", "401", "402", "403", "409", "429"),
        ),
        ("POST", "/api/v1/traces/search"): _api(
            "searchTraces", "REST", "Search your organization's traces",
            description="Needs the `read` scope. Quarantined and superseded traces are never returned.",
            body=_json_body(_schema_ref("SearchTracesRequest")),
            ok={"200": _json("Brief hits, best first.", _schema_ref("SearchTracesResponse"))},
            errors=("400", "401", "403", "429"),
        ),
        ("POST", "/api/v1/keys"): _api(
            "createAccountKey", "Signup", "Create an organization and its first API key",
            security=PUBLIC,
            description="Unauthenticated; limited to about one signup per minute per address. Registered only "
                        "when both the REST API and signup are enabled.",
            body=_json_body(_schema_ref("CreateKeyRequest")),
            ok={"201": _json("Created. Sent with Cache-Control: no-store.", _schema_ref("CreateKeyResponse"))},
            errors=("400", "429"),
        ),
        **{
            ("POST", f"/api/v1/telemetry/{beacon}"): _api(
                f"telemetry{beacon.capitalize()}", "Telemetry", f"Record a client `{beacon}` beacon",
                description="Any valid API key; the body is not read.",
                ok={"204": {"description": "Accepted."}},
                errors=("401", "429"),
            )
            for beacon in ("install", "ping", "triggers")
        },

        # --- Marketplace -----------------------------------------------------
        ("POST", "/api/v1/market/publishers"): _api(
            "registerPublisher", "Marketplace", "Register or update your publisher key",
            description="Needs `write`. Re-registering your own id replaces its key.",
            body=_json_body(_schema_ref("RegisterPublisherRequest")),
            ok={"201": _json("The stored publisher card.", _schema_ref("PublisherCard"))},
            errors=("400", "401", "403", "409", "429"),
        ),
        ("GET", "/api/v1/market/publishers/{principal_id}"): _api(
            "getPublisher", "Marketplace", "Read a publisher card",
            description="Needs `read`. Publisher cards are visible to every org.",
            ok={"200": _json("The publisher card.", _schema_ref("PublisherCard"))},
            errors=("401", "403", "404", "429"),
        ),
        ("POST", "/api/v1/market/listings"): _api(
            "uploadListing", "Marketplace", "Upload a signed listing",
            description="Needs `write`. Accepted only if every buyer-side check passes: both signatures, the "
                        "lesson digest, the content screen, the licence, and replicated positive lift across "
                        "two or more organizations.",
            body=_json_body(_schema_ref("UploadListingRequest")),
            ok={"201": _json("Listed.", _schema_ref("ListingStatus")),
                "200": _json("Already listed; its current status.", _schema_ref("ListingStatus"))},
            errors=("400", "401", "403", "422", "429", "503"),
        ),
        ("GET", "/api/v1/market/listings"): _api(
            "searchListings", "Marketplace", "Browse listed lessons",
            description="Needs `read`. Ordered by the lift interval's lower bound, then recency.",
            parameters=(
                _query("q", _string(maxLength=200), "Words matched against name and description (first 8 used)."),
                _query("limit", _integer(minimum=1, maximum=100, default=20), "Clamped to 1..100."),
            ),
            ok={"200": _json("Matching listings.", _schema_ref("ListingSearchResponse"))},
            errors=("401", "403", "429"),
        ),
        ("GET", "/api/v1/market/listings/{listing_id}"): _api(
            "getListing", "Marketplace", "Read one listing",
            description="Needs `read`. A withdrawn listing is visible only to its owner.",
            ok={"200": _json("The signed listing and its summary.", _schema_ref("ListingDetail"))},
            errors=("401", "403", "404", "429"),
        ),
        ("DELETE", "/api/v1/market/listings/{listing_id}"): _api(
            "withdrawListing", "Marketplace", "Withdraw one of your listings",
            description="Needs `write`. Idempotent.",
            ok={"200": _json("The listing's status after the call.", _schema_ref("ListingStatus"))},
            errors=("401", "403", "404", "429"),
        ),

        # --- OTLP ------------------------------------------------------------
        ("POST", "/v1/traces"): _api(
            "otlpIngest", "OTLP", "Ingest OTLP/JSON spans as traces",
            description="Needs `write`. Each span becomes a trace (profile `otel`, spanId as idempotency key); "
                        "a span carrying an occasion id and outcome also records that outcome. Protobuf is not "
                        "accepted. Ingest stops at the first entitlement or rate-limit refusal.",
            body=_json_body(_schema_ref("OtlpExportRequest")),
            ok={"200": _json("Every span processed.", _schema_ref("OtlpExportResponse")),
                "207": _json("Some spans failed; see `errors`.", _schema_ref("OtlpExportResponse"))},
            errors=("400", "401", "403", "413", "415", "429"),
        ),

        # --- Connectors ------------------------------------------------------
        ("POST", "/connectors/{connector_id}/events"): _op(
            "connectorEvent", "Connectors", "Deliver a vendor webhook to a connector",
            security=CONNECTOR,
            description="The connector's provider (zendesk, github, greenhouse, intercom) decides which "
                        "signature header applies. A dry-run connector reports what it would record.",
            parameters=(
                _header("X-GitHub-Event", _string(), "GitHub: the event type."),
                _header("X-GitHub-Delivery", _string(), "GitHub: delivery id (required for GitHub)."),
                _header("Greenhouse-Event-ID", _string(maxLength=128), "Greenhouse: delivery id."),
            ),
            body=_json_body(_free_object("The vendor's own webhook payload (a JSON object).")),
            responses={
                "200": _json("Processed, a duplicate delivery, or an unusable payload.",
                             _schema_ref("ConnectorResult")),
                "400": _json("Payload is not a JSON object, or carries no usable delivery id.",
                             _schema_ref("ConnectorError")),
                "401": _json("Bad, stale or missing signature, or an unknown/disabled connector.",
                             _schema_ref("ConnectorError")),
                "429": _json("Too many deliveries from this address.", _schema_ref("ConnectorError"),
                             headers={"Retry-After": {"$ref": "#/components/headers/RetryAfter"}}),
            },
        ),

        # --- Billing ---------------------------------------------------------
        ("POST", "/billing/webhook"): _op(
            "stripeWebhook", "Billing", "Receive a Stripe webhook event",
            security=STRIPE,
            body=_json_body(_schema_ref("StripeEvent")),
            responses={"200": _text("`ok`: applied, ignored, or already processed."),
                       "400": _text("`invalid signature` or `invalid payload`.")},
        ),

        # --- Health ----------------------------------------------------------
        ("GET", "/healthz"): _op(
            "healthz", "Health", "Liveness", security=PUBLIC,
            responses={"200": _json("The process is up.", _schema_ref("HealthStatus"))},
        ),
        ("GET", "/readyz"): _op(
            "readyz", "Health", "Readiness (database reachable)", security=PUBLIC,
            responses={
                "200": _json("Ready.", _schema_ref("Readiness")),
                "429": _json("Too many probes from this address.", _schema_ref("Readiness"),
                             headers={"Retry-After": {"$ref": "#/components/headers/RetryAfter"}}),
                "503": _json("The database is unreachable.", _schema_ref("Readiness")),
            },
        ),
        ("GET", "/metrics"): _op(
            "metrics", "Health", "Prometheus metrics", security=METRICS,
            description="Bearer HUB_METRICS_TOKEN when one is configured; open otherwise.",
            responses={
                "200": {"description": "Prometheus text exposition format 0.0.4.",
                        "content": {"text/plain": {"schema": {"type": "string"}}}},
                "401": _json("Wrong or missing metrics token.", _schema_ref("MetricsStatus")),
                "429": _json("Too many scrapes from this address.", _schema_ref("MetricsStatus"),
                             headers={"Retry-After": {"$ref": "#/components/headers/RetryAfter"}}),
            },
        ),
        ("GET", "/disclosure"): _op(
            "disclosure", "Health", "Operator's self-reported deployment disclosure", security=PUBLIC,
            description="Exempt from HUB_IP_ALLOWLIST so outside reviewers can read it.",
            responses={"200": _json("The disclosure.", _schema_ref("Disclosure"))},
        ),
        ("GET", DOCUMENT_PATH): _op(
            "openapiDocument", "Health", "This OpenAPI document", security=PUBLIC,
            description="Describes every route the Hub can serve; a route whose surface is disabled on this "
                        "deployment answers 404.",
            responses={"200": _json("The OpenAPI 3.1.0 document.", _schema_ref("OpenAPIDocument"))},
        ),

        # --- SCIM ------------------------------------------------------------
        ("GET", "/scim/v2/ServiceProviderConfig"): _op(
            "scimServiceProviderConfig", "SCIM", "SCIM service provider configuration", security=PUBLIC,
            responses={"200": _scim_json("Capabilities.", "ScimServiceProviderConfig")},
        ),
        ("GET", "/scim/v2/ResourceTypes"): _op(
            "scimResourceTypes", "SCIM", "SCIM resource types", security=PUBLIC,
            responses={"200": _scim_json("User and Group.", "ScimDiscoveryList")},
        ),
        ("GET", "/scim/v2/Schemas"): _op(
            "scimSchemas", "SCIM", "SCIM schemas", security=PUBLIC,
            responses={"200": _scim_json("The User and Group schemas.", "ScimDiscoveryList")},
        ),
        ("GET", "/scim/v2/Users"): _scim(
            "scimListUsers", "List users",
            parameters=(
                _query("filter", _string(), 'Only `userName eq "<value>"` is supported.'),
                _query("startIndex", _integer(default=1), "1-based; values below 1 count as 1."),
                _query("count", _integer(default=100, minimum=0, maximum=200), "Clamped to 0..200."),
            ),
            ok={"200": _scim_json("A page of users.", "ScimUserList")}, extra=("400",),
        ),
        ("POST", "/scim/v2/Users"): _scim(
            "scimCreateUser", "Provision a user", body=_scim_body("ScimUserInput"),
            ok={"201": _scim_json("Created.", "ScimUser")}, extra=("400", "404", "409"),
        ),
        ("GET", "/scim/v2/Users/{user_id}"): _scim(
            "scimGetUser", "Read a user", ok={"200": _scim_json("The user.", "ScimUser")}, extra=("404",),
        ),
        ("PUT", "/scim/v2/Users/{user_id}"): _scim(
            "scimReplaceUser", "Replace a user's displayName and active state", body=_scim_body("ScimUserInput"),
            ok={"200": _scim_json("The user.", "ScimUser")}, extra=("400", "404"),
        ),
        ("PATCH", "/scim/v2/Users/{user_id}"): _scim(
            "scimPatchUser", "Activate or deactivate a user", body=_scim_body("ScimPatchOp"),
            description="Only `active` is patchable; other operations are ignored.",
            ok={"200": _scim_json("The user.", "ScimUser")}, extra=("400", "404"),
        ),
        ("DELETE", "/scim/v2/Users/{user_id}"): _scim(
            "scimDeactivateUser", "Deactivate a user (never deletes)",
            ok={"204": {"description": "Deactivated."}}, extra=("404",),
        ),
        ("GET", "/scim/v2/Groups"): _scim(
            "scimListGroups", "List groups",
            parameters=(
                _query("filter", _string(), 'Only `displayName eq "<value>"` is supported.'),
                _query("startIndex", _integer(default=1), "1-based; values below 1 count as 1."),
                _query("count", _integer(default=100, minimum=0, maximum=200), "Clamped to 0..200."),
            ),
            ok={"200": _scim_json("A page of groups.", "ScimGroupList")}, extra=("400",),
        ),
        ("POST", "/scim/v2/Groups"): _scim(
            "scimCreateGroup", "Create a group", body=_scim_body("ScimGroupInput"),
            ok={"201": _scim_json("Created.", "ScimGroup")}, extra=("400", "404", "409"),
        ),
        ("GET", "/scim/v2/Groups/{group_id}"): _scim(
            "scimGetGroup", "Read a group", ok={"200": _scim_json("The group.", "ScimGroup")}, extra=("404",),
        ),
        ("PUT", "/scim/v2/Groups/{group_id}"): _scim(
            "scimReplaceGroup", "Replace a group's name and members", body=_scim_body("ScimGroupInput"),
            ok={"200": _scim_json("The group.", "ScimGroup")}, extra=("400", "404"),
        ),
        ("PATCH", "/scim/v2/Groups/{group_id}"): _scim(
            "scimPatchGroup", "Rename a group or change its members", body=_scim_body("ScimPatchOp"),
            ok={"200": _scim_json("The group.", "ScimGroup")}, extra=("400", "404"),
        ),
        ("DELETE", "/scim/v2/Groups/{group_id}"): _scim(
            "scimDeleteGroup", "Delete a group", ok={"204": {"description": "Deleted."}}, extra=("404",),
        ),

        # --- Signup ----------------------------------------------------------
        ("GET", "/signup"): _op(
            "signupPage", "Signup", "Account creation form", security=PUBLIC,
            responses={"200": _html("The form.")},
        ),
        ("POST", "/signup"): _op(
            "signup", "Signup", "Create an organization from the browser", security=PUBLIC,
            description="About one signup per minute per address. Every outcome, including a refusal, is a "
                        "200 page; success shows the API key once.",
            body=_form_body({
                "org_name": _string("Organization name.", minLength=2, maxLength=200),
                "website": _string("Honeypot: must be left empty.", maxLength=0),
            }, ("org_name",)),
            responses={"200": _html("The created key, or the form with an error."),
                       "403": _response_ref("CrossOriginRefused")},
        ),

        # --- Console ---------------------------------------------------------
        ("GET", "/app/signin"): _op(
            "consoleSigninPage", "Console", "Sign-in form", security=PUBLIC,
            responses={"200": _html("The form."), "303": _see_other("Already signed in: to /app.")},
        ),
        ("POST", "/app/signin"): _op(
            "consoleSignin", "Console", "Sign in with an API key", security=PUBLIC,
            body=_form_body({"api_key": _string("An org API key; checked once, never stored in the cookie.")},
                            ("api_key",)),
            responses={
                "303": {"description": "Signed in: sets the ct_console cookie and redirects to /app.",
                        "headers": {"Location": {"$ref": "#/components/headers/Location"},
                                    "Set-Cookie": {"description": "ct_console session cookie.",
                                                   "schema": {"type": "string"}}}},
                "200": _html("The form with an error (rejected key, or too many attempts)."),
                "403": _response_ref("CrossOriginRefused"),
            },
        ),
        ("GET", "/app/signout"): _op(
            "consoleSignoutPage", "Console", "Sign-out confirmation", security=CONSOLE,
            responses={"200": _html("A confirmation form."), "303": _response_ref("SignInRedirect")},
        ),
        ("POST", "/app/signout"): _op(
            "consoleSignout", "Console", "Sign out", security=PUBLIC,
            responses={"303": _see_other("Clears the session cookie and redirects to /app/signin."),
                       "403": _response_ref("CrossOriginRefused")},
        ),
        ("GET", "/app"): _console_page("consoleOverview", "Fleet overview"),
        ("GET", "/app/proof"): _console_page(
            "consoleProof", "Proof of value",
            parameters=(
                _query("per_occasion", _number(), "Value per occasion used to price the measured lift."),
                _query("done", _string(enum=["revoked"]), "Flash key from the previous action."),
            ),
        ),
        ("POST", "/app/proof/share"): _console_post(
            "consoleProofShare", "Mint a read-only share link for the proof page",
            page="The proof page showing the new link (valid 14 days).",
        ),
        ("POST", "/app/proof/share/revoke"): _console_post(
            "consoleProofShareRevoke", "Revoke every share link issued so far",
            redirect="Revoked (admin keys): to /app/proof?done=revoked; or to sign-in.",
            page="Non-admin keys: the proof page, unchanged.",
        ),
        ("GET", "/app/proof/shared/{token}"): _op(
            "consoleProofShared", "Console", "Shared, read-only proof page", security=PUBLIC,
            description="The token in the path is the credential.",
            responses={
                "200": _html("The shared report."),
                "404": _html("Invalid, expired or revoked link."),
                "429": {"description": "This report is being viewed too often.",
                        "headers": {"Retry-After": {"$ref": "#/components/headers/RetryAfter"}},
                        "content": {"text/html": {"schema": {"type": "string"}}}},
            },
        ),
        ("GET", "/app/proof/assignments.csv"): _op(
            "consoleAssignmentsCsv", "Console", "Download holdout assignments as CSV", security=CONSOLE,
            responses={"200": {"description": "Attachment `commontrace-assignments.csv`.",
                               "content": {"text/csv": {"schema": {"type": "string"}}}},
                       "303": _response_ref("SignInRedirect")},
        ),
        ("POST", "/app/proof/experiment/start"): _console_post(
            "consoleExperimentStart", "Start a holdout experiment (admin keys)",
            fields={
                "rate": _number("Holdout rate.", exclusiveMinimum=0, exclusiveMaximum=1),
                "outcome": _string("Outcome measured; defaults to `resolved`."),
                "notes": _string(),
            }, required=("rate",),
            redirect="Started: to /app/proof; or to sign-in.",
            page="The proof page with an error (bad rate, already running, or not an admin key).",
        ),
        ("POST", "/app/proof/experiment/stop"): _console_post(
            "consoleExperimentStop", "Stop the running experiment (admin keys)",
            redirect="Stopped: to /app/proof; or to sign-in.",
            page="The proof page with an error.",
        ),
        ("POST", "/app/billing/checkout"): _console_post(
            "consoleBillingCheckout", "Start a Stripe Checkout for a plan",
            fields={"plan": _string(enum=list(plans.BILLABLE_PLANS))}, required=("plan",),
            redirect="To Stripe Checkout, back to /app (nothing to do, or ?billing_error=1), or to sign-in.",
        ),
        ("POST", "/app/billing/portal"): _console_post(
            "consoleBillingPortal", "Open the Stripe billing portal",
            redirect="To the Stripe portal, back to /app, or to sign-in.",
        ),
        ("GET", "/app/memory"): _console_page(
            "consoleMemory", "Browse and search stored traces",
            parameters=(
                _query("q", _string(maxLength=500), "Search text."),
                _query("offset", _integer(minimum=0, maximum=100_000, default=0), "Clamped to 0..100000."),
            ),
        ),
        ("GET", "/app/kb"): _console_page(
            "consoleKnowledgeBase", "Knowledge Base: browse, propose, vote",
            parameters=(
                _query("tag", _string(maxLength=64), "Browse one tag."),
                _query("offset", _integer(minimum=0, maximum=100_000, default=0), "Clamped to 0..100000."),
                _query("done", _string(enum=["proposed", "auto_on", "auto_off", "voted", "voted_uncounted"]),
                       "Flash key from the previous action."),
            ),
        ),
        ("POST", "/app/kb/submit"): _console_post(
            "consoleKbSubmit", "Propose a Knowledge Base entry (admin keys)",
            fields={
                "title": _string(), "context_text": _string(), "solution_text": _string(),
                "tags": _string("Comma-separated."),
                "rationale": _string(maxLength=300),
            }, required=("title", "context_text", "solution_text"),
            redirect="Proposed: to /app/kb?done=proposed; or to sign-in.",
            page="The Knowledge Base page with an error.",
        ),
        ("POST", "/app/kb/vote"): _console_post(
            "consoleKbVote", "Vote on a Knowledge Base entry (write keys)",
            fields={
                "vote": _string(enum=list(crud.VALID_VOTE_TYPES)),
                "trace_id": _string(format="uuid"),
                "feedback_tag": _string(enum=list(crud.VALID_FEEDBACK_TAGS)),
            }, required=("vote", "trace_id"),
            redirect="Voted: to /app/kb?done=voted (or voted_uncounted); or to sign-in.",
            page="The Knowledge Base page with an error.",
        ),
        ("POST", "/app/kb/auto-contribute"): _console_post(
            "consoleKbAutoContribute", "Turn automatic Knowledge Base contribution on or off (admin keys)",
            fields={"enabled": _string("`1` turns it on; anything else turns it off.")},
            redirect="To /app/kb?done=auto_on|auto_off; or to sign-in.",
            page="The Knowledge Base page with an error.",
        ),
        ("GET", "/app/users"): _console_page("consoleUsers", "Users and roles"),
        ("POST", "/app/users/create"): _console_post(
            "consoleUsersCreate", "Add a user (admin keys)",
            fields={"email": _string(), "role": _string(enum=roles), "display_name": _string()},
            required=("email", "role"),
            redirect="To /app/users; or to sign-in.", page="Non-admin keys: the page with an error.",
        ),
        ("POST", "/app/users/{user_id}/role"): _console_post(
            "consoleUsersSetRole", "Change a user's role (admin keys)",
            fields={"role": _string(enum=roles)}, required=("role",), has_id=True,
            redirect="To /app/users; or to sign-in.", page="The page with an error.",
        ),
        ("POST", "/app/users/{user_id}/disable"): _console_post(
            "consoleUsersDisable", "Disable a user (admin keys)", has_id=True,
            redirect="To /app/users; or to sign-in.", page="The page with an error.",
        ),
        ("POST", "/app/users/{user_id}/enable"): _console_post(
            "consoleUsersEnable", "Re-enable a user (admin keys)", has_id=True,
            redirect="To /app/users; or to sign-in.", page="The page with an error.",
        ),
        ("GET", "/app/keys"): _console_page("consoleKeys", "API keys"),
        ("POST", "/app/keys/issue"): _console_post(
            "consoleKeysIssue", "Issue an API key (admin keys)",
            fields={"scopes": key_scopes,
                    "expires_days": _integer("Days until expiry; empty means never.", minimum=1)},
            required=("scopes",), page="The keys page, showing the new key once.",
        ),
        ("POST", "/app/keys/{key_id}/rotate"): _console_post(
            "consoleKeysRotate", "Rotate an API key (admin keys)", has_id=True,
            page="The keys page, showing the replacement key once.",
        ),
        ("POST", "/app/keys/{key_id}/revoke"): _console_post(
            "consoleKeysRevoke", "Revoke an API key (admin keys)", has_id=True,
            redirect="To /app/keys; or to sign-in.",
        ),
        ("GET", "/app/alerts"): _console_page("consoleAlerts", "Alert rules"),
        ("POST", "/app/alerts/create"): _console_post(
            "consoleAlertsCreate", "Create an alert rule (admin keys)",
            fields={
                "metric": _string(enum=list(alerts.METRICS)),
                "comparator": _string(enum=list(alerts.COMPARATORS)),
                "threshold": _number(),
                "cooldown_minutes": _integer(f"Defaults to {alerts.DEFAULT_COOLDOWN_MINUTES}."),
            }, required=("metric", "comparator", "threshold"),
            redirect="To /app/alerts; or to sign-in.", page="The page with an error.",
        ),
        ("POST", "/app/alerts/{rule_id}/delete"): _console_post(
            "consoleAlertsDelete", "Delete an alert rule (admin keys)", has_id=True,
            redirect="To /app/alerts; or to sign-in.",
        ),
        ("POST", "/app/alerts/generate-report"): _console_post(
            "consoleAlertsGenerateReport", "Generate a usage report now (admin keys)",
            page="The alerts page with the report, or an error.",
        ),
        ("GET", "/app/webhooks"): _console_page("consoleWebhooks", "Outbound webhooks"),
        ("POST", "/app/webhooks/create"): _console_post(
            "consoleWebhooksCreate", "Add a webhook endpoint (admin keys)",
            fields={"url": _string("HTTPS endpoint URL."),
                    "events": _array(_string(enum=list(events.EVENT_TYPES)),
                                     "Repeat once per event type; none means every type.")},
            required=("url",), page="The webhooks page, showing the signing secret once, or an error.",
        ),
        ("POST", "/app/webhooks/{endpoint_id}/rotate"): _console_post(
            "consoleWebhooksRotate", "Rotate a webhook signing secret (admin keys)", has_id=True,
            page="The webhooks page, showing the new secret once.",
        ),
        ("POST", "/app/webhooks/{endpoint_id}/disable"): _console_post(
            "consoleWebhooksDisable", "Disable a webhook endpoint (admin keys)", has_id=True,
            redirect="To /app/webhooks; or to sign-in.",
        ),
        ("GET", "/app/audit"): _console_page(
            "consoleAudit", "Audit log",
            parameters=(_query("offset", _integer(minimum=0, maximum=100_000, default=0), "Clamped to 0..100000."),),
        ),

        # --- Admin -----------------------------------------------------------
        ("GET", "/admin"): _admin_page("adminOverview", "Operator overview", _FLASH_PARAMS),
        ("POST", "/admin/create-org"): _admin_post(
            "adminCreateOrg", "Create an organization", "target",
            fields={"name": _string("Organization name.")}, required=("name",),
        ),
        ("POST", "/admin/generate-encryption-key"): _admin_post(
            "adminGenerateEncryptionKey", "Generate a HUB_ENCRYPTION_KEY value", "target",
            ok={"200": _html("The overview, showing a fresh key once. Nothing is stored.")}, redirects=False,
        ),
        ("GET", "/admin/org/{org_id}"): _admin_page(
            "adminOrgDetail", "One organization",
            _FLASH_PARAMS + (
                _query("subject_id", _string(maxLength=200), "Show traces tagged with this data subject."),
                _query("preview_retention", _string(), "Any value: preview the retention plan and its digest."),
            ),
            description="An unknown id renders a 'No such organization' page with status 200.",
        ),
        ("POST", "/admin/org/{org_id}/quarantine/release"): _admin_post(
            "adminQuarantineRelease", "Release a trace from quarantine", "trace_id",
        ),
        ("POST", "/admin/org/{org_id}/legal-hold/place"): _admin_post(
            "adminLegalHoldPlace", "Place a legal hold", "org_id",
            fields={"reason": _string(maxLength=500),
                    "object_type": _string("Limit to one object type; empty holds everything."),
                    "target_id": _string("Limit to one object.")},
        ),
        ("POST", "/admin/org/{org_id}/legal-hold/release"): _admin_post(
            "adminLegalHoldRelease", "Release a legal hold", "hold_id",
            fields={"reason": _string(maxLength=200)},
        ),
        ("POST", "/admin/org/{org_id}/retention/set"): _admin_post(
            "adminRetentionSet", "Set a retention policy", "org_id",
            fields={"object_type": _string(enum=retention_kinds),
                    "status": _string(f"Defaults to `{retention.STATUS_ANY}`."),
                    "days": _integer("Maximum age in days; each type has a floor.", minimum=1)},
            required=("object_type", "days"),
        ),
        ("POST", "/admin/org/{org_id}/retention/clear"): _admin_post(
            "adminRetentionClear", "Clear a retention policy", "target",
            fields={"org_id": _string(format="uuid"), "object_type": _string(enum=retention_kinds),
                    "status": _string(f"Defaults to `{retention.STATUS_ANY}`.")},
            required=("org_id", "object_type"),
        ),
        ("POST", "/admin/org/{org_id}/set-plan"): _admin_post(
            "adminSetPlan", "Change an organization's plan", "org_id",
            fields={"plan_name": _string(enum=list(plans.PLANS))}, required=("plan_name",),
        ),
        ("POST", "/admin/org/{org_id}/users/create"): _admin_post(
            "adminUsersCreate", "Add a user", "org_id",
            fields={"email": _string(), "role": _string(enum=roles)}, required=("email", "role"),
        ),
        ("POST", "/admin/org/{org_id}/users/set-role"): _admin_post(
            "adminUsersSetRole", "Change a user's role", "user_id",
            fields={"role": _string(enum=roles)}, required=("role",),
        ),
        ("POST", "/admin/org/{org_id}/users/disable"): _admin_post("adminUsersDisable", "Disable a user", "user_id"),
        ("POST", "/admin/org/{org_id}/users/enable"): _admin_post("adminUsersEnable", "Re-enable a user", "user_id"),
        ("POST", "/admin/org/{org_id}/users/link-sso"): _admin_post(
            "adminUsersLinkSso", "Link an SSO identity to a user", "user_id",
            fields={"issuer": _string(), "external_subject": _string()}, required=("issuer", "external_subject"),
        ),
        ("POST", "/admin/org/{org_id}/users/unlink-sso"): _admin_post(
            "adminUsersUnlinkSso", "Unlink a user's SSO identity", "user_id",
        ),
        ("POST", "/admin/org/{org_id}/tag-subjects"): _admin_post(
            "adminTagSubjects", "Tag a trace with data-subject ids", "org_id",
            fields={"trace_id": _string(format="uuid"), "subject_ids": _string("Comma-separated.")},
            required=("trace_id",),
        ),
        ("POST", "/admin/org/{org_id}/amend-trace"): _admin_post(
            "adminAmendTrace", "Amend a trace (stores a superseding trace)", "org_id",
            fields={"trace_id": _string(format="uuid"), "title": _string(), "context_text": _string(),
                    "solution_text": _string(), "tags_csv": _string("Comma-separated tags.")},
            required=("trace_id",),
        ),
        ("POST", "/admin/org/{org_id}/keys/issue"): _admin_post(
            "adminKeysIssue", "Issue an API key", "org_id",
            fields={"scopes": key_scopes,
                    "expires_days": _integer("Days until expiry; empty means never.", minimum=1)},
            ok={"200": _html("The organization page, showing the new key once (or asking for a scope).")},
            redirects=False,
        ),
        ("POST", "/admin/org/{org_id}/keys/rotate"): _admin_post(
            "adminKeysRotate", "Rotate an API key", "key_id",
            ok={"200": _html("The organization page, showing the replacement key once.")},
        ),
        ("POST", "/admin/org/{org_id}/keys/revoke"): _admin_post("adminKeysRevoke", "Revoke an API key", "key_id"),
        ("POST", "/admin/org/{org_id}/retention/apply"): _admin_post(
            "adminRetentionApply", "Apply a previewed retention plan", "digest",
            fields={"confirm_digest": _string("Must equal `digest`.")}, required=("confirm_digest",),
            description="Acts on the organization in the URL. Refused if the store moved since the preview.",
        ),
        ("POST", "/admin/org/{org_id}/purge-trace"): _admin_post(
            "adminPurgeTrace", "Permanently delete one trace", "org_id",
            fields={"trace_id": _string(format="uuid"), "confirm_trace_id": _string("Must equal trace_id.")},
            required=("trace_id", "confirm_trace_id"),
        ),
        ("POST", "/admin/org/{org_id}/purge"): _admin_post(
            "adminPurgeOrg", "Permanently delete an organization", "org_id",
            fields={"confirm_name": _string("Must equal the organization's name.")}, required=("confirm_name",),
        ),
        ("POST", "/admin/org/{org_id}/purge-subject-traces"): _admin_post(
            "adminPurgeSubjectTraces", "Permanently delete every trace tagged with a data subject", "org_id",
            fields={"subject_id": _string(), "confirm_subject_id": _string("Must equal subject_id.")},
            required=("subject_id", "confirm_subject_id"),
        ),
        ("GET", "/admin/kb"): _admin_page("adminKnowledgeBase", "Knowledge Base review queue", _FLASH_PARAMS),
        ("POST", "/admin/kb/review"): _admin_post(
            "adminKbReview", "Approve or reject a Knowledge Base proposal", "submission_id",
            fields={"decision": _string(enum=["approve", "reject"]), "reason": _string(maxLength=200)},
            required=("decision",),
            ok={"200": _html("HUB_COMMONS_ENABLED=false: an explanatory page.")},
            extra={"400": _text("Unknown decision."),
                   "409": _text("Approval refused: HUB_OPERATOR_ORG_ID is not set.")},
        ),
        ("POST", "/admin/kb/retract"): _admin_post(
            "adminKbRetract", "Withdraw a published Knowledge Base entry", "trace_id",
            fields={"reason": _string(maxLength=200)},
            ok={"200": _html("HUB_COMMONS_ENABLED=false: an explanatory page.")},
        ),
        ("POST", "/admin/kb/restore"): _admin_post(
            "adminKbRestore", "Restore a withdrawn Knowledge Base entry", "trace_id",
            ok={"200": _html("HUB_COMMONS_ENABLED=false: an explanatory page.")},
        ),
    }
    return ops


# ---------------------------------------------------------------------------
# Route table walk
# ---------------------------------------------------------------------------

_CONVERTER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}")
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


def _openapi_path(path: str) -> str:
    return _CONVERTER.sub(r"{\1}", path)


def path_parameters(path: str) -> list[str]:
    return _CONVERTER.findall(path)


def live_operations(app, declared: dict[tuple[str, str], dict] | None = None) -> set[tuple[str, str]]:
    """Every (METHOD, path) the app routes, HEAD excluded.

    A route that accepts every method (an ASGI app such as the MCP transport)
    contributes the methods declared for its path; it must have at least one.
    """
    declared = _operations() if declared is None else declared
    found: set[tuple[str, str]] = set()

    def walk(routes, prefix: str) -> None:
        for route in routes:
            if isinstance(route, Mount) and route.routes:
                walk(route.routes, prefix + route.path)
                continue
            if not isinstance(route, (Route, Mount)):
                raise OpenAPIDriftError(f"unsupported route type {type(route).__name__} at {prefix}")
            path = _openapi_path(prefix + route.path)
            methods = getattr(route, "methods", None)
            if methods is None:
                methods = {method for method, declared_path in declared if declared_path == path}
                if not methods:
                    raise OpenAPIDriftError(f"{path} accepts any method but declares none in hub/openapi.py")
            found.update((method, path) for method in methods if method != "HEAD")

    walk(app.routes, "")
    return found


class _DropRecord(logging.Filter):
    def __init__(self, prefix: str) -> None:
        super().__init__()
        self._prefix = prefix

    def filter(self, record: logging.LogRecord) -> bool:
        return not str(record.msg).startswith(self._prefix)


def reference_config():
    """A HubConfig with every optional surface switched on. Never connects anywhere."""
    from hub.config import HubConfig

    return HubConfig(
        database_url="postgresql+asyncpg://openapi:openapi@localhost/openapi",
        admin_token=_DUMMY_SECRET,
        console_secret=_DUMMY_SECRET,
        stripe_webhook_secret=_DUMMY_SECRET,
        metrics_token=_DUMMY_SECRET,
        signup_enabled=True,
        rest_api_enabled=True,
        otlp_ingest_enabled=True,
        connectors_enabled=True,
        commons_enabled=True,
    )


def reference_app():
    """`build_app(reference_config(), None)`, without disturbing this process.

    build_app sets process-wide auth state (the deployment's data region and
    the auth cache policy). This may run inside a live Hub, the first time
    the document is served, so that state is put back exactly as it was: a
    reference build must never switch off a running deployment's region
    enforcement.
    """
    from hub import auth
    from hub.server import build_app

    saved = (auth._DEPLOYMENT_REGION, auth._AUTH_CACHE_TTL, auth._AUTH_CACHE_REQUIRES_LISTENER)
    server_logger = logging.getLogger("commontrace.hub")
    quiet = _DropRecord("rate limiting is in-process")
    server_logger.addFilter(quiet)
    try:
        return build_app(reference_config(), None)
    finally:
        server_logger.removeFilter(quiet)
        auth._DEPLOYMENT_REGION, auth._AUTH_CACHE_TTL, auth._AUTH_CACHE_REQUIRES_LISTENER = saved


def document(app=None) -> dict:
    """The OpenAPI 3.1.0 document for `app` (default: the full reference Hub).

    Raises OpenAPIDriftError when a live route is undeclared, or a declared
    route is not live.
    """
    declared = _operations()
    live = live_operations(reference_app() if app is None else app, declared)
    undocumented = sorted(live - set(declared))
    stale = sorted(set(declared) - live)
    if undocumented or stale:
        lines = [f"  undocumented: {m} {p}" for m, p in undocumented]
        lines += [f"  declared but not served: {m} {p}" for m, p in stale]
        raise OpenAPIDriftError(
            "hub/openapi.py and the Hub's route table disagree:\n" + "\n".join(lines)
            + "\nDeclare each new route in hub/openapi.py `_operations()` and remove declarations for "
              "removed routes, then run `python scripts/export_hub_openapi.py`."
        )

    paths: dict[str, dict] = {}
    for method, path in sorted(live, key=lambda item: (item[1], item[0])):
        operation = dict(declared[(method, path)])
        names = path_parameters(path)
        if names:
            operation["parameters"] = [
                {"name": name, "in": "path", "required": True,
                 "description": _PATH_PARAMS.get(name, ""), "schema": {"type": "string"}}
                for name in names
            ] + list(operation.get("parameters", ()))
        responses = dict(operation["responses"])
        if method in _BODY_METHODS:
            responses.setdefault("413", _response_ref("PayloadTooLarge"))
        responses.setdefault("503", _response_ref("Overloaded"))
        responses.setdefault("504", _response_ref("Timeout"))
        operation["responses"] = responses
        paths.setdefault(path, {})[method.lower()] = operation

    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": "CommonTrace Hub",
            "version": __version__,
            "summary": "Every HTTP route the CommonTrace Hub serves.",
            "description": (
                "Generated from the Hub's live route table by hub/openapi.py. Optional surfaces are "
                "registered only when their setting is on (see each tag); a disabled surface answers 404. "
                "Every route can also answer 503 (load shed) and 504 (timeout); every body-carrying route "
                "413. When HUB_IP_ALLOWLIST is set, sources outside it get 403 everywhere except /healthz, "
                "/readyz and /disclosure."
            ),
            "license": {"name": "MIT", "identifier": "MIT"},
        },
        "tags": [{"name": name, "description": text} for name, text in _TAGS],
        "paths": paths,
        "components": {
            "schemas": _schemas(),
            "responses": _SHARED_RESPONSES,
            "headers": _HEADERS,
            "securitySchemes": _SECURITY_SCHEMES,
        },
    }


def render(doc: dict) -> str:
    """The canonical serialization, as checked in at hub/openapi.json."""
    return json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def add_openapi_route(app) -> None:
    """Serve the document, unauthenticated, at DOCUMENT_PATH. Built once, on first request."""
    cached: dict[str, bytes] = {}

    async def openapi_document(request: Request) -> Response:
        body = cached.get("body")
        if body is None:
            body = cached["body"] = render(document()).encode("utf-8")
        return Response(body, media_type="application/json", headers={"Cache-Control": "public, max-age=300"})

    app.add_route(DOCUMENT_PATH, openapi_document, methods=["GET"])


__all__ = [
    "DOCUMENT_PATH",
    "OPENAPI_VERSION",
    "OpenAPIDriftError",
    "add_openapi_route",
    "document",
    "live_operations",
    "path_parameters",
    "reference_app",
    "reference_config",
    "render",
]
