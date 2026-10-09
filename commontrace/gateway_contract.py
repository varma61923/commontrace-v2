"""The gateway's typed HTTP contract: one entry per route, rendered as OpenAPI 3.0.

Every route the gateway registers has an entry here; `gateway._openapi` refuses
to build a document that leaves one out, and the contract tests call each route
and validate the live response against the schema declared below. The memory
routes take their models from `api_schema`, which also enforces them.

Schemas describe what the handlers accept and return today. Objects stay open
(``additionalProperties``) where the gateway passes through a record from a
module that may add fields; they are closed where the handler rejects unknown
fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field

REF = "#/components/schemas/"


def ref(name: str) -> dict:
    return {"$ref": REF + name}


def string(description: str = "", **extra) -> dict:
    return {"type": "string", **({"description": description} if description else {}), **extra}


def integer(description: str = "", **extra) -> dict:
    return {"type": "integer", **({"description": description} if description else {}), **extra}


def number(description: str = "", **extra) -> dict:
    return {"type": "number", **({"description": description} if description else {}), **extra}


def boolean(description: str = "") -> dict:
    return {"type": "boolean", **({"description": description} if description else {})}


def array(items: dict, description: str = "", **extra) -> dict:
    return {"type": "array", "items": items, **({"description": description} if description else {}), **extra}


def nullable(schema: dict) -> dict:
    return {**schema, "nullable": True}


def obj(properties: dict, required: tuple[str, ...] | list[str] = (), *, closed: bool = False,
        description: str = "") -> dict:
    out: dict = {"type": "object", "properties": properties}
    if required:
        out["required"] = list(required)
    out["additionalProperties"] = not closed
    if description:
        out["description"] = description
    return out


ANY_OBJECT = {"type": "object", "additionalProperties": True}
IDENT = string("1-128 characters, no control characters or surrounding whitespace", minLength=1, maxLength=128)
AGENT_ID = string("which agent or robot", pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
ENV = string("must equal the store's environment when it has one; simulation and reality are never pooled")
TEXT = string(maxLength=20000)
LABELS = array(string(maxLength=256), "scope labels", maxItems=100)
DETECTORS = ["from_csat", "from_event_within_window", "from_human_takeover", "from_no_reversal",
             "from_retry_count", "from_safety_stop", "from_test_exit_code", "from_threshold",
             "from_ticket_transition"]


SCHEMAS: dict[str, dict] = {
    "AgentPlugin": obj({
        "name": string(), "agent_id": string(), "version": string(),
        "skills": array(obj({"name": string(), "path": string(), "content": string()}, ("name", "path", "content"))),
        "api": obj({"version": string(), "auth": string(), "token_env": string()}),
    }, ("name", "agent_id", "version", "skills", "api")),
    "AgentCredential": obj({
        "agent_id": string(), "token": string("shown once; store it as COMMONTRACE_AGENT_TOKEN"),
        "scopes": array(string()), "plugin": ref("AgentPlugin"),
    }, ("agent_id", "token", "scopes", "plugin")),
    "ControlRecordFull": obj({
        "id": string(), "kind": string(), "text": string(), "revision": string(), "recorded_at": string(),
        "actor": string(), "scopes": array(string()), "data": ANY_OBJECT,
        "origin": nullable(obj({"principal": string(), "organization": string(), "authority": string(),
                                "digest": string(), "signature": string("Ed25519 over the record"),
                                "record": ANY_OBJECT})),
    }, ("id", "kind", "text", "revision")),
    "Capabilities": obj({
        "llm": boolean(), "embeddings": boolean(), "rerank": boolean(),
        "tier": string(enum=["full", "no_embed", "no_llm", "lexical"]),
        "degraded_paths": array(string()), "rbac": boolean(), "container_scoping": boolean(),
        "defense_screen": boolean(), "ssrf_guard": boolean(),
    }, ("llm", "embeddings", "rerank", "tier", "degraded_paths")),
    "Effect": obj({
        "lesson_slug": string(), "n_injected": integer(), "n_withheld": integer(),
        "rate_injected": number(), "rate_withheld": number(), "effect": number("injected minus withheld rate"),
        "ci_low": number(), "ci_high": number(), "p_value": nullable(number()), "significant": boolean(),
        "min_detectable_effect": nullable(number()),
        "verdict": string("HELPS, HURTS, NO_DETECTABLE_EFFECT or INSUFFICIENT_DATA style verdict"),
        "note": string(), "withdrawn": boolean("withdrawn from delivery by the harm policy"),
    }, ("lesson_slug", "n_injected", "n_withheld", "effect", "ci_low", "ci_high", "verdict")),
    "LessonChecks": obj({"failed": array(string()), "passes": boolean(),
                         "nearest_active": nullable(obj({"slug": string(), "similarity": number()}))}),
    "LessonSummary": obj({
        "slug": string(), "status": string(), "description": string(), "domain": string(),
        "importance": nullable(integer()), "drafted_by_model": boolean(), "source_traces": integer(),
        "revises": nullable(string()), "revision": string("send back as expected_revision"),
        "checks": ref("LessonChecks"),
    }, ("slug", "status", "revision")),
    "LessonDetail": {"allOf": [ref("LessonSummary"), obj({
        "applies_when": string(), "do_not_apply_when": string(), "rule": string(), "body": string(),
        "body_truncated": boolean(), "provenance": nullable(ANY_OBJECT),
        "history": array(obj({"at": string(), "from": nullable(string()), "to": nullable(string()),
                              "actor": string(), "reason": string()})),
        "separation_of_duties": nullable(ANY_OBJECT), "approval_enabled": boolean(),
    }, ("rule", "history"))]},
    "RecallItem": obj({"id": IDENT, "text": string(maxLength=20000),
                       "protected": boolean("always delivered, never randomized"),
                       "meta": obj({}, description="a small object (at most 4 KiB as JSON) echoed back")},
                      ("id",)),
    "Signal": obj({"detector": string(enum=DETECTORS), "args": obj({}, description=(
        "scalar keyword arguments; event_at/started_at/reversal_at/now are ISO-8601, "
        "resolved_statuses/reopened_statuses are string lists"))}, ("detector",)),
    "EpisodeMedia": obj({
        "kind": string(enum=["image", "audio", "video"]),
        "mime": string(enum=["image/png", "image/jpeg", "audio/wav", "audio/mpeg", "audio/ogg",
                             "video/mp4", "video/webm"]),
        "data_b64": string("base64, 1 byte to 2 MiB decoded", format="byte"),
        "caption": string(maxLength=1000),
    }, ("kind", "mime", "data_b64"), closed=False),
    "StoredMedia": obj({"kind": string(), "mime": string(), "bytes": integer(), "sha256": string(),
                        "caption": string(), "width": integer(), "height": integer()},
                       ("kind", "mime", "bytes", "sha256")),
    "Message": obj({"speaker": nullable(string()), "role": nullable(string()), "name": nullable(string()),
                    "text": nullable(string()), "content": nullable(string()),
                    "at": nullable(string("ISO date or timestamp")),
                    "id": {"description": "idempotency id: string or number"}}),
    "ExecutiveMemory": obj({
        "memory": string(), "verdict": string(), "effect": number(), "ci": array(number(), minItems=2, maxItems=2),
        "n": array(integer(), "[injected, withheld]", minItems=2, maxItems=2), "tokens": nullable(integer()),
        "withdrawn": boolean(), "lift_per_1k_tokens": nullable(number()),
        "occasions_improved_low": number("HELPS only: ci_low x injected occasions"),
        "failures_avoided_per_1k_if_withdrawn": number("HURTS only, at the conservative bound"),
    }, ("memory", "verdict", "effect", "ci", "n", "withdrawn")),
    "Listing": ANY_OBJECT,
}


def _list_response(key: str, item: dict, **extra) -> dict:
    return obj({key: array(item), **extra}, (key,))


EVENTS_WINDOW = {"window_events": integer(), "truncated": boolean("older events were not scanned"),
                 "limit": integer(), "cached": boolean("served from the 5-60 s report memo")}


@dataclass(frozen=True)
class Operation:
    operation_id: str
    tag: str
    request: dict | None = None
    response: dict = field(default_factory=lambda: ANY_OBJECT)
    query: tuple[tuple[str, dict, bool, str], ...] = ()
    errors: tuple[int, ...] = ()
    description: str = ""
    content_type: str = "application/json"


def _control(op: str, request: dict, errors=(400, 403)) -> Operation:
    return Operation("control_" + op.replace("-", "_"), "Memory control", request, ref("ControlRecordFull"),
                     errors=errors)


OPERATIONS: dict[tuple[str, str], Operation] = {
    # -- agents --------------------------------------------------------------------------------------
    ("POST", "/v1/agent/enroll"): Operation(
        "agent_enroll", "Agents", obj({}, closed=True, description="must be the empty object {}"),
        ref("AgentCredential"), errors=(400, 403, 429),
        description="Off unless the gateway runs with self-signup; capped per day and in total."),
    ("POST", "/v1/agent/claim"): Operation(
        "agent_claim", "Agents", obj({"agent_id": string(), "owner": string()}, ("agent_id", "owner")),
        obj({"agent_id": string(), "claimed_by": string()}, ("agent_id", "claimed_by")), errors=(400,)),
    ("POST", "/v1/agent/rotate"): Operation(
        "agent_rotate", "Agents", obj({"agent_id": string()}, ("agent_id",)), ref("AgentCredential"),
        errors=(400,)),
    ("POST", "/v1/agent/signup"): Operation(
        "agent_signup", "Agents", obj({"agent_id": AGENT_ID, "scopes": LABELS}, ("agent_id",)),
        ref("AgentCredential"), errors=(400,)),
    ("POST", "/v1/agent/plugin"): Operation(
        "agent_plugin", "Agents", obj({"agent_id": string("ignored for agent keys, which name themselves")}),
        ref("AgentPlugin"), errors=(400,)),
    ("POST", "/v1/agent/heartbeat"): Operation(
        "agent_heartbeat", "Agents", obj({"agent_id": string("ignored for agent keys")}),
        obj({"agent_id": string(), "heartbeat_at": string(), "refreshes_queued": integer()},
            ("agent_id", "heartbeat_at", "refreshes_queued")), errors=(400,)),
    # -- governed memory -----------------------------------------------------------------------------
    ("GET", "/v1/palace"): Operation(
        "palace", "Memory control", None, obj({
            "models": array(ref("ControlRecordFull")), "foresight": array(ref("ControlRecordFull")),
            "directives": array(ref("ControlRecordFull")), "suggestions": array(ref("ControlRecordFull")),
            "needs_attention": obj({"proposals": integer(), "jobs": obj({}, description="job counts by state")}),
            "agents": array(ANY_OBJECT, "registered agents, without key hashes"),
        }, ("models", "foresight", "directives", "suggestions", "needs_attention", "agents")), errors=(403,)),
    ("POST", "/v1/control/directive"): _control("directive", obj({
        "text": string(minLength=1), "deny_tools": array(string()), "required_tags": array(string()),
        "context": LABELS}, ("text",))),
    ("POST", "/v1/control/question"): _control("question", obj({
        "text": string("the standing question", minLength=1), "context": LABELS,
        "budget": integer(minimum=0, maximum=100000, default=600),
        "refresh_seconds": integer(minimum=1, maximum=31536000, default=3600)}, ("text",)), errors=(400,)),
    ("POST", "/v1/control/refresh"): Operation(
        "control_refresh", "Memory control", obj({"id": IDENT}, ("id",)),
        obj({"job_id": string()}, ("job_id",)), errors=(400,)),
    ("POST", "/v1/control/reject-proposal"): _control("reject-proposal", obj({
        "id": string(), "expected_revision": string(), "reason": string()}, ("id", "expected_revision"))),
    ("POST", "/v1/control/review-foresight"): _control("review-foresight", obj({
        "id": string(), "expected_revision": string(), "approve": boolean()},
        ("id", "expected_revision", "approve"))),
    ("POST", "/v1/control/skill-review"): _control("skill-review", obj({
        "id": string(), "expected_revision": string(), "verdict": string(), "evidence": ANY_OBJECT},
        ("id", "expected_revision", "verdict"))),
    # -- service -------------------------------------------------------------------------------------
    ("GET", "/v1/health"): Operation("health", "Service", None, obj({
        "ok": boolean(), "api": string(), "version": string(), "tier": string(),
        "capabilities": ref("Capabilities")}, ("ok", "api", "version", "tier", "capabilities"))),
    ("GET", "/v1/health/live"): Operation("health_live", "Service", None, obj({
        "ok": boolean(), "status": string(enum=["live"]), "version": string()}, ("ok", "status", "version"))),
    ("GET", "/v1/health/ready"): Operation("health_ready", "Service", None, obj({
        "ok": boolean(), "status": string(enum=["ready", "not_ready"]), "version": string(),
        "checks": obj({"store": string(enum=["ok", "missing", "not_writable"]),
                       "schemas": string(enum=["ok", "unavailable"]),
                       "disk": string(enum=["ok", "low", "unknown"])}, ("store", "schemas", "disk"))},
        ("ok", "status", "checks", "version")), errors=(503,),
        description="503 with the same body when not ready; names causes, never paths."),
    ("GET", "/v1/capabilities"): Operation("capabilities", "Service", None, obj({
        "api": string(), "version": string(), "tier": string(), "capabilities": ref("Capabilities")},
        ("api", "version", "tier", "capabilities"))),
    ("GET", "/v1/whoami"): Operation("whoami", "Service", None, obj({
        "authenticated": boolean(), "role": string(enum=["admin", "anonymous"]),
        "token_prefix": string(), "container_tag": string(), "request_id": string()},
        ("authenticated", "role", "request_id"))),
    ("POST", "/v1/resolve_tag"): Operation(
        "resolve_tag", "Service", obj({"container_tag": string(pattern=r"^[A-Za-z0-9._-]{1,128}$")},
                                      ("container_tag",)),
        obj({"container_tag": string(), "scope": string(), "valid": boolean()}, ("container_tag", "scope", "valid")),
        errors=(400,)),
    ("GET", "/v1/metrics"): Operation(
        "metrics", "Service", None, obj({
            "counters": array(obj({"name": string(), "labels": ANY_OBJECT, "value": number()})),
            "histograms": array(obj({"name": string(), "labels": ANY_OBJECT, "count": integer(),
                                     "sum_ms": number(), "p50_ms": number(), "p95_ms": number()}))}),
        query=(("format", string(enum=["json"]), False, "json for this body; Prometheus text otherwise"),),
        description="Prometheus text exposition (text/plain; version=0.0.4) unless ?format=json."),
    ("GET", "/v1/openapi.json"): Operation("openapi", "Service", None, ANY_OBJECT,
                                           description="This document."),
    ("GET", "/v1/docs"): Operation("swagger_ui", "Service", None, string(), content_type="text/html"),
    ("POST", "/v1/connectors/github/push"): Operation(
        "github_push", "Connectors", ANY_OBJECT, ANY_OBJECT, errors=(400, 403, 404),
        description="GitHub push webhook. Authenticated by X-Hub-Signature-256 (HMAC-SHA256 of the raw "
                    "body with the configured secret), with X-GitHub-Delivery and X-GitHub-Event."),
    ("GET", "/v1/command-catalog"): Operation("command_catalog", "Console", None, obj({
        "commands": array(obj({"name": string(), "group": string(), "description": string(),
                               "runnable": boolean(), "reason": string(), "example": array(string())})),
        "store": string(), "approval_enabled": boolean(), "scoped": boolean(),
        "execution_policy": obj({"read_only": boolean(), "scoped_commands": string(),
                                 "lesson_review": string(), "force_overrides": string()})},
        ("commands", "approval_enabled", "execution_policy"))),
    ("POST", "/v1/command"): Operation(
        "command_run", "Console", obj({"command": string("a name from /v1/command-catalog"),
                                       "args": array(string(), "the store root is implicit")}, ("command",)),
        obj({"command": string(), "args": array(string()), "exit_code": integer(), "ok": boolean(),
             "stdout": string(), "stderr": string(), "truncated": boolean(), "elapsed_ms": number()},
            ("command", "exit_code", "ok", "stdout", "stderr")), errors=(400, 403, 409)),
    # -- retrieval and measurement -------------------------------------------------------------------
    ("POST", "/v1/explore"): Operation(
        "explore", "Retrieval", obj({
            "question": string(minLength=1, maxLength=2000),
            "channels": array(string(enum=["lessons", "facts", "conversations"]), minItems=1, maxItems=3,
                              uniqueItems=True, default=["lessons", "facts"]),
            "space": string("required for the conversations channel"),
            "budget": integer(minimum=50, maximum=8000, default=1500),
            "evidence_budget": integer(minimum=0, maximum=2000, default=512),
            "fact_scorer": string(enum=["overlap-v1", "bm25-v1"], default="overlap-v1"),
            "as_of": string("ISO date or timestamp", maxLength=64)}, ("question",)),
        obj({"question": string(), "as_of": nullable(string()), "budget": integer(), "tokens": integer(),
             "items": array(obj({"channel": string(), "id": string(), "text": string(), "score": number(),
                                 "fused": number(), "at": nullable(string()), "tokens": integer(),
                                 "truncated": boolean(), "provenance": ANY_OBJECT})),
             "considered": ANY_OBJECT, "errors": ANY_OBJECT, "assessment": ANY_OBJECT, "context": string(),
             "fact_scorer": string(), "elapsed_ms": number(), "read_only": boolean(), "scope": string(),
             "answer_accuracy_measured": boolean()}, ("question", "items", "context", "read_only")),
        errors=(400,), description="Records no occasion and no outcome."),
    ("POST", "/v1/recall"): Operation(
        "recall", "Measurement", obj({
            "occasion_id": IDENT, "items": array(ref("RecallItem"), "your candidate memories", maxItems=200),
            "query": string("ranks this store's lessons when items is absent", maxLength=2000),
            "top_k": integer(minimum=1, maximum=50, default=5), "agent_id": AGENT_ID, "env": ENV},
            ("occasion_id",)),
        obj({"occasion_id": string(), "mode": string(enum=["items", "store"]),
             "deliver": array(ref("RecallItem")), "withheld": array(string("randomized holdout")),
             "withdrawn": array(obj({"id": string(), "verdict": nullable(string()),
                                     "effect": nullable(number())})),
             "protected": array(string()),
             "quarantined": array(obj({"id": string(), "reason": string()})),
             "holdout": obj({"running": boolean(), "rate": number()}, ("running", "rate")),
             "env": string(), "note": string()},
            ("occasion_id", "mode", "deliver", "withheld", "withdrawn", "protected", "quarantined", "holdout")),
        errors=(400, 409)),
    ("POST", "/v1/outcome"): Operation(
        "outcome", "Measurement", obj({
            "occasion_id": IDENT, "succeeded": boolean("give this or signals, not both"),
            "signals": array(ref("Signal"), "evaluated three-valued", minItems=1, maxItems=16),
            "combine": string(enum=["all", "any"]), "agent_id": AGENT_ID, "env": ENV}, ("occasion_id",)),
        obj({"occasion_id": string(), "recorded": boolean(), "succeeded": boolean(),
             "undecided": boolean(), "reason": string(), "note": string()}, ("occasion_id", "recorded")),
        errors=(400, 409), description="Records nothing while signals are undecided; a different answer "
                                       "for a recorded occasion is a 409."),
    ("POST", "/v1/episode"): Operation(
        "episode", "Measurement", obj({
            "occasion_id": IDENT, "summary": string(minLength=1, maxLength=20000),
            "sensors": obj({}, description="name -> number, boolean or text up to 128 chars; at most 64; "
                                           "names match ^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,63}$"),
            "media": array(ref("EpisodeMedia"), maxItems=8), "succeeded": boolean(), "agent_id": AGENT_ID,
            "env": ENV}, ("occasion_id", "summary")),
        obj({"episode_id": string(), "occasion_id": string(), "media": array(ref("StoredMedia")),
             "sensors": integer("readings stored"), "env": nullable(string())},
            ("episode_id", "occasion_id", "media", "sensors")), errors=(400, 409)),
    ("POST", "/v1/conversation/add"): Operation(
        "conversation_add", "Conversations", obj({
            "space": IDENT, "session": IDENT, "messages": array(ref("Message"), maxItems=1000),
            "session_at": nullable(string("date the session took place"))}, ("space", "session", "messages")),
        obj({"space": string(), "session": string(), "added": integer(), "skipped": integer(),
             "secrets_redacted": integer()}, ("added", "skipped")), errors=(400,)),
    ("POST", "/v1/conversation/recall"): Operation(
        "conversation_recall", "Conversations", obj({
            "space": IDENT, "question": string(minLength=1, maxLength=4000),
            "budget": integer("context size in tokens", minimum=50, maximum=32000, default=1500),
            "now": nullable(string("date the question is asked")), "sessions": array(string()),
            "speakers": array(string()), "since": nullable(string()), "until": nullable(string()),
            "context_strategy": string(enum=["legacy", "coverage-v1"], default="legacy"),
            "adaptive_budget": boolean("treat budget as a floor sized by the question's shape")},
            ("space", "question")),
        obj({"question": string(), "context": string(), "tokens": integer(), "turns": array(integer()),
             "window": nullable(ANY_OBJECT), "explain": ANY_OBJECT}, ("question", "context", "tokens", "turns")),
        errors=(400, 404)),
    ("GET", "/v1/status"): Operation("status", "Measurement", None, obj({
        "gateway": ANY_OBJECT, "experiment": obj({"running": boolean(), "rate": number()}),
        "activity": ANY_OBJECT, "proof": nullable(ANY_OBJECT), "cached": boolean(), "proof_cached": boolean()},
        ("gateway", "experiment", "activity", "proof"))),
    ("GET", "/v1/memories"): Operation("memories", "Measurement", None, obj({
        "memories": array(ref("Effect")), "integrity": nullable(obj({"verdict": string(), "readable": boolean()})),
        "mode": string(), "occasions": integer(), "note": string()}, ("memories", "integrity"))),
    ("GET", "/v1/occasions"): Operation(
        "occasions", "Measurement", None, obj({"events": array(ANY_OBJECT, "newest first"), **EVENTS_WINDOW},
                                              ("events", "limit")),
        query=(("limit", integer(minimum=1, maximum=500, default=50), False, "events to return"),)),
    ("GET", "/v1/agents"): Operation("agents", "Measurement", None, obj({
        "agents": array(obj({"agent_id": string(), "recalls": integer(), "outcomes": integer(),
                             "succeeded": integer(), "withheld": integer(), "protected": integer(),
                             "quarantined": integer(), "last_seen": string(),
                             "success_rate": nullable(number()), "seconds_since_seen": nullable(number())})),
        "window": string(), **EVENTS_WINDOW}, ("agents",))),
    # -- lesson review -------------------------------------------------------------------------------
    ("GET", "/v1/lessons"): Operation(
        "lessons", "Lessons", None, obj({"lessons": array(ref("LessonSummary")), "approval_enabled": boolean(),
                                         "total": integer(), "limit": nullable(integer()), "offset": integer()},
                                        ("lessons", "total", "offset")),
        query=(("status", string(enum=["review", "active", "archived", "draft"]), False, "filter"),
               ("limit", integer(minimum=1, maximum=1000), False, "page size"),
               ("offset", integer(minimum=0, default=0), False, "page start")), errors=(400,)),
    ("GET", "/v1/lesson"): Operation(
        "lesson", "Lessons", None, ref("LessonDetail"),
        query=(("slug", string(), True, "the lesson"),), errors=(404,)),
    ("POST", "/v1/lesson/edit"): Operation(
        "lesson_edit", "Lessons", obj({"slug": string(), "expected_revision": string(),
                                       "rule": string(), "applies_when": string(), "do_not_apply_when": string(),
                                       "description": string()}, ("slug",)),
        ref("LessonDetail"), errors=(400, 403, 404, 409), description="Needs --allow-approval."),
    ("POST", "/v1/lesson/approve"): Operation(
        "lesson_approve", "Lessons", obj({"slug": string(), "rationale": string(), "expected_revision": string()},
                                         ("slug",)),
        obj({"slug": string(), "status": string(enum=["active"])}, ("slug", "status")), errors=(400, 403, 404, 409),
        description="Runs every approval gate. Needs --allow-approval."),
    ("POST", "/v1/lesson/reject"): Operation(
        "lesson_reject", "Lessons", obj({"slug": string(), "reason": string(minLength=1),
                                         "expected_revision": string()}, ("slug", "reason")),
        obj({"slug": string(), "status": string(enum=["archived"])}, ("slug", "status")),
        errors=(400, 403, 404, 409), description="Needs --allow-approval."),
    # -- learning ledger -----------------------------------------------------------------------------
    ("GET", "/v1/ledger/executive"): Operation("ledger_executive", "Ledger", None, obj({
        "memories": array(ref("ExecutiveMemory")),
        "frontier": array(obj({"memory": string(), "cumulative_tokens": integer(), "cumulative_effect": number()})),
        "proven_occasions_improved": number(), "proven_value": nullable(number()),
        "value_per_occasion": nullable(number()), "harmful": integer(), "harmful_withdrawn": integer(),
        "basis": string()}, ("memories", "frontier", "proven_occasions_improved", "harmful", "basis"))),
    ("GET", "/v1/ledger/releases"): Operation(
        "ledger_releases", "Ledger", None, {"oneOf": [
            obj({"releases": array(obj({"id": string(), "parent": nullable(string()), "created_at": string(),
                                        "actor": string(), "reason": string(), "lessons": integer()}))},
                ("releases",)),
            obj({"from": nullable(string()), "to": string(), "added": array(string()), "removed": array(string()),
                 "changed": array(obj({"lesson": string(), "from": string(), "to": string(),
                                       "diff": nullable(string("unified diff when both texts are recoverable"))}))},
                ("to", "added", "removed", "changed"))]},
        query=(("to", string(), False, "a release id: return its diff instead of the list"),
               ("from", string(), False, "diff against this release instead of the parent")), errors=(404,)),
    ("GET", "/v1/ledger/design"): Operation(
        "ledger_design", "Ledger", None, obj({
            "baseline": number(), "effect": number(), "rate": number(), "power": number(), "n_per_arm": integer(),
            "occasions_needed": integer(), "rate_for_budget": nullable(number()), "verdict": string(),
            "command": string("the command that configures this experiment"), "days_needed": number()},
            ("n_per_arm", "occasions_needed", "verdict", "command")),
        query=(("baseline", number(default=0.5), False, "current success rate"),
               ("effect", number(default=0.1), False, "smallest effect worth detecting"),
               ("rate", number(default=0.1), False, "holdout rate"),
               ("power", number(default=0.8), False, "statistical power"),
               ("daily", number(), False, "occasions per day, to estimate duration"),
               ("budget", integer(), False, "occasions available")), errors=(400,)),
    ("GET", "/v1/ledger/forensics"): Operation(
        "ledger_forensics", "Ledger", None, obj({
            "occasion": string(), "found": boolean(), "outcome": nullable(boolean()),
            "assignments": array(obj({"memory": string(), "injected": boolean(), "rank": nullable(integer()),
                                      "revision": nullable(string()), "relevance": nullable(number()),
                                      "at": nullable(string())})),
            "recall_receipts": array(ANY_OBJECT), "incident_reports": array(ANY_OBJECT)},
            ("occasion", "found", "assignments", "recall_receipts", "incident_reports")),
        query=(("occasion", IDENT, True, "the occasion id"),), errors=(400,)),
    ("GET", "/v1/ledger/digest"): Operation(
        "ledger_digest", "Ledger", None, obj({
            "since": string(), "until": string(), "markdown": string(),
            "counts": obj({"occasions": integer(), "helps": integer(), "hurts": integer(), "approved": integer(),
                           "drafted": integer(), "releases": integer()})}, ("since", "until", "markdown", "counts")),
        query=(("days", integer(minimum=1, maximum=366, default=7), False, "period length"),), errors=(400,)),
    # -- marketplace ---------------------------------------------------------------------------------
    ("GET", "/v1/market/listings"): Operation(
        "market_listings", "Marketplace", None, {"oneOf": [
            obj({"listings": array(ANY_OBJECT, "listing summaries")}, ("listings",)),
            obj({"listing": ref("Listing")}, ("listing",))]},
        query=(("q", string(maxLength=500), False, "words to match"),
               ("id", string(), False, "return this one full listing")), errors=(404,)),
    ("POST", "/v1/market/listings"): Operation(
        "market_add", "Marketplace", obj({"listing": ref("Listing")}, ("listing",)),
        obj({"id": string(), "summary": ANY_OBJECT}, ("id", "summary")), errors=(400, 422),
        description="The listing must verify against this store's trusted publisher keys; at most 256 KiB."),
    ("POST", "/v1/market/install"): Operation(
        "market_install", "Marketplace", obj({"id": string(), "accept_licence": string()}, ("id", "accept_licence")),
        ANY_OBJECT, errors=(403, 404, 422), description="Installs at status=review. Needs --allow-approval."),
}

# Served outside the JSON route table but part of the same HTTP surface.
EXTRA_PATHS: dict[str, dict] = {
    "/": {"get": {"operationId": "console", "tags": ["Console"], "summary": "The no-build operator console.",
                  "responses": {"200": {"description": "HTML", "content": {"text/html": {"schema": string()}}}}}},
    "/.well-known/oauth-protected-resource": {"get": {
        "operationId": "oauth_protected_resource", "tags": ["Service"],
        "summary": "RFC 9728 protected-resource metadata when an OAuth 2.1 issuer is configured.",
        "responses": {"200": {"description": "metadata", "content": {"application/json": {"schema": ANY_OBJECT}}},
                      "404": {"description": "OAuth is not configured"}}}},
}


def memory_operation(operation: str) -> Operation:
    return Operation("memory_" + operation.replace("-", "_"), "Memory")


def components() -> dict:
    return dict(SCHEMAS)
