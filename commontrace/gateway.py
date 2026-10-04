"""A language-neutral door into the causal loop, for any agent, including robots."""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from commontrace import (
    __version__,
    frontmatter,
    holdout_io,
    injection_guard,
    outcome_detect,
    paths,
    proof,
    retrieval,
    retrieval_io,
)
from commontrace.measure import CausalMemory, HarmWatch

API_VERSION = "1"
REPORT_MIN_INTERVAL = 5.0
REPORT_MAX_AGE = 60.0
MAX_BODY_BYTES = 1 << 20
MAX_ITEMS = 200
MAX_TEXT_CHARS = 20_000
MAX_ID_CHARS = 128
MAX_SIGNALS = 16
EVENTS_NAME = "gateway_events.jsonl"
CONFIG_NAME = "gateway.json"
logger = logging.getLogger("commontrace.gateway")
TOKEN_NAME = "gateway.token"
EVENT_LOG_ROTATE_BYTES = 10 * 1024 * 1024
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")

# Bounded event scan: newest-N events within a trailing byte cap.
EVENTS_TAIL_BYTES = 1_500_000
EVENTS_MAX_EVENTS = 5000
EVENTS_STATUS_LIMIT = 2000

# mtime-keyed active-lesson index cache (module-level, capped).
_ACTIVE_CACHE_MAX = 8
_ACTIVE_CACHE: dict[str, tuple[tuple, list, dict]] = {}
_ACTIVE_CACHE_LOCK = threading.Lock()

# Lesson-body cache keyed by (path, mtime_ns, size) (module-level, capped).
_BODY_CACHE_MAX = 512
_BODY_CACHE: dict[tuple[str, int, int], str] = {}
_BODY_CACHE_LOCK = threading.Lock()


def _cached_active(root: str, reader) -> tuple[list, dict]:
    """Active lessons + term cache, re-parsed only when the listing changes."""
    from commontrace import lesson_cache

    try:
        listing = lesson_cache.listing(root)
    except OSError:
        listing = ()
    key = os.path.abspath(root)
    with _ACTIVE_CACHE_LOCK:
        hit = _ACTIVE_CACHE.get(key)
        if hit is not None and hit[0] == listing:
            _ACTIVE_CACHE[key] = _ACTIVE_CACHE.pop(key)
            return hit[1], hit[2]
    active, term_cache = lesson_cache.load_active_with_terms(root, None, reader=reader)
    with _ACTIVE_CACHE_LOCK:
        _ACTIVE_CACHE[key] = (listing, active, term_cache)
        while len(_ACTIVE_CACHE) > _ACTIVE_CACHE_MAX:
            _ACTIVE_CACHE.pop(next(iter(_ACTIVE_CACHE)))
    return active, term_cache


def _cached_body(path: str) -> str:
    """One lesson body, re-read only when its mtime/size changes."""
    try:
        st = os.stat(path)
        ident = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return frontmatter.read_body(path)
    with _BODY_CACHE_LOCK:
        hit = _BODY_CACHE.get(ident)
        if hit is not None:
            _BODY_CACHE[ident] = _BODY_CACHE.pop(ident)
            return hit
    body = frontmatter.read_body(path)
    with _BODY_CACHE_LOCK:
        _BODY_CACHE[ident] = body
        while len(_BODY_CACHE) > _BODY_CACHE_MAX:
            _BODY_CACHE.pop(next(iter(_BODY_CACHE)))
    return body


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class TransientAuthError(Exception):
    """Raised when upstream authentication or network verification fails transiently (5xx, timeout)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def is_transient_auth_error(exc: BaseException) -> bool:
    """Classify transient upstream errors vs permanent invalid credentials.

    5xx status codes, timeouts, connection blips are transient and should not nuke creds.
    401/403 indicate bad credentials.
    """
    if isinstance(exc, TransientAuthError):
        return True
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in (401, 403):
            return False
        if status in (408, 429) or 500 <= status <= 599:
            return True
    err_str = str(exc).lower()
    transient_markers = (
        "timeout", "timed out", "connection refused",
        "econnreset", "temporarily unavailable", "try again",
    )
    if any(m in err_str for m in transient_markers):
        return True
    if isinstance(exc, (TimeoutError, socket.gaierror, ConnectionError, OSError)):
        return True
    return False


def _bad(message: str, code: str = "bad_request") -> ApiError:
    return ApiError(400, code, message)


@dataclasses.dataclass(frozen=True)
class GatewayConfig:
    env: str | None = None
    protected_prefixes: tuple[str, ...] = ()


def config_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), CONFIG_NAME)


def load_config(root: str) -> GatewayConfig:
    try:
        with open(config_path(root), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return GatewayConfig()
    if not isinstance(raw, dict):
        return GatewayConfig()
    env = raw.get("env")
    prefixes = raw.get("protected_prefixes")
    return GatewayConfig(
        env=env if isinstance(env, str) and env else None,
        protected_prefixes=tuple(p for p in prefixes if isinstance(p, str) and p)
        if isinstance(prefixes, list) else (),
    )


def save_config(root: str, config: GatewayConfig) -> None:
    os.makedirs(paths.memory_dir(root), exist_ok=True)
    tmp = config_path(root) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"env": config.env, "protected_prefixes": list(config.protected_prefixes)}, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, config_path(root))


def merge_config(root: str, *, env: str | None, protect: list[str]) -> GatewayConfig:
    current = load_config(root)
    if env is not None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", env):
            raise ValueError("env must be a lowercase slug such as sim or real")
        if current.env and current.env != env:
            raise ValueError(
                f"this store measures {current.env!r}; refusing to start it as {env!r}. "
                "Use a separate store (--dest) for each environment so they are never pooled.")
    prefixes = tuple(dict.fromkeys((*current.protected_prefixes, *protect)))
    merged = GatewayConfig(env=env or current.env, protected_prefixes=prefixes)
    if merged != current:
        save_config(root, merged)
    return merged


def token_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), TOKEN_NAME)


def load_or_create_token(root: str) -> str:
    """The store's bearer token, created (0600) on first use."""
    try:
        with open(token_path(root), encoding="utf-8") as fh:
            token = fh.read().strip()
        if len(token) >= 24:
            return token
    except OSError as exc:
        logger.debug("No existing token found; creating a new one: %s", exc)
    token = secrets.token_urlsafe(32)
    os.makedirs(paths.memory_dir(root), exist_ok=True)
    fd = os.open(token_path(root), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token


def _text(value, label: str, *, limit: int, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise _bad(f"{label} must be a string")
    if required and not value.strip():
        raise _bad(f"{label} must not be empty")
    if len(value) > limit:
        raise _bad(f"{label} is longer than {limit} characters")
    return value


def _ident(value, label: str) -> str:
    text = _text(value, label, limit=MAX_ID_CHARS)
    if _CONTROL.search(text) or text != text.strip():
        raise _bad(f"{label} must not contain control characters or surrounding whitespace")
    return text


def _agent(req: dict) -> str:
    value = req.get("agent_id")
    if value is None:
        return ""
    if not isinstance(value, str) or not _AGENT_RE.match(value):
        raise _bad("agent_id must be 1-64 characters of letters, digits and . _ : -")
    return value


def _items(value) -> list[dict]:
    if not isinstance(value, list):
        raise _bad("items must be a list")
    if len(value) > MAX_ITEMS:
        raise _bad(f"items is longer than {MAX_ITEMS}")
    seen: set[str] = set()
    out = []
    for n, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise _bad(f"items[{n}] must be an object")
        item_id = _ident(raw.get("id"), f"items[{n}].id")
        if item_id in seen:
            raise _bad(f"items[{n}].id {item_id!r} appears twice")
        seen.add(item_id)
        protected = raw.get("protected", False)
        if not isinstance(protected, bool):
            raise _bad(f"items[{n}].protected must be true or false")
        meta = raw.get("meta")
        if meta is not None and (not isinstance(meta, dict) or len(json.dumps(meta)) > 4096):
            raise _bad(f"items[{n}].meta must be a small object")
        out.append({
            "id": item_id, "text": _text(raw.get("text"), f"items[{n}].text", limit=MAX_TEXT_CHARS,
                                         required=False),
            "protected": protected, **({"meta": meta} if meta else {}),
        })
    return out


def _parse_when(value, label: str) -> datetime.datetime:
    if not isinstance(value, str):
        raise _bad(f"{label} must be an ISO-8601 timestamp string")
    text = value.strip()
    try:
        parsed = datetime.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
    except ValueError:
        raise _bad(f"{label} is not an ISO-8601 timestamp") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


_TIME_ARGS = {"event_at", "started_at", "reversal_at", "now"}
_LIST_TO_SET = {"resolved_statuses", "reopened_statuses"}


def evaluate_signals(signals, combine: str | None) -> bool | None:
    """Run detector calls through `outcome_detect` and combine them. None = undecided."""
    if not isinstance(signals, list) or not signals:
        raise _bad("signals must be a non-empty list")
    if len(signals) > MAX_SIGNALS:
        raise _bad(f"signals is longer than {MAX_SIGNALS}")
    known = {n for n in dir(outcome_detect) if n.startswith("from_") and n not in ("from_all", "from_any")}
    results: list[bool | None] = []
    for n, call in enumerate(signals):
        if not isinstance(call, dict) or not isinstance(call.get("detector"), str):
            raise _bad(f"signals[{n}] needs a detector name")
        name = call["detector"]
        if name not in known:
            raise _bad(f"signals[{n}].detector {name!r} is not one of: {', '.join(sorted(known))}")
        args = call.get("args", {})
        if not isinstance(args, dict):
            raise _bad(f"signals[{n}].args must be an object")
        kwargs = {}
        for key, val in args.items():
            if key in _TIME_ARGS and val is not None:
                val = _parse_when(val, f"signals[{n}].args.{key}")
            elif key in _LIST_TO_SET:
                if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
                    raise _bad(f"signals[{n}].args.{key} must be a list of strings")
                val = set(val)
            elif isinstance(val, (dict, list)):
                raise _bad(f"signals[{n}].args.{key} must be a scalar")
            kwargs[key] = val
        try:
            results.append(getattr(outcome_detect, name)(**kwargs))
        except (TypeError, ValueError) as exc:
            raise _bad(f"signals[{n}] ({name}): {exc}") from None
    rule = combine or ("single" if len(results) == 1 else "all")
    if rule == "single" and len(results) == 1:
        return results[0]
    if rule == "all":
        return outcome_detect.from_all(*results)
    if rule == "any":
        return outcome_detect.from_any(*results)
    raise _bad("combine must be 'all' or 'any'")


@dataclasses.dataclass
class Response:
    status: int
    body: bytes
    content_type: str = "application/json"
    headers: dict = dataclasses.field(default_factory=dict)


def _json(status: int, payload) -> Response:
    return Response(status, (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))


class Gateway:
    def __init__(
        self, root: str, *, token: str | None = None, config: GatewayConfig | None = None,
        durable: bool = True, on_harm: str | None = None, check_every: int = 25,
        allowed_hosts: tuple[str, ...] = (), allow_approval: bool = False,
    ) -> None:
        self.root = os.path.abspath(root)
        self.allow_approval = allow_approval
        self.token = token
        self.config = config if config is not None else load_config(self.root)
        self.durable = durable
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts) | LOOPBACK_HOSTS
        self._watch = HarmWatch(self.root, on_harm, check_every)
        self._events_lock = threading.Lock()
        self._memo_lock = threading.Lock()
        self._memo: dict[str, tuple] = {}
        self.routes: dict[tuple[str, str], tuple[Callable, dict]] = {}
        self._register()


    def _route(self, method: str, path: str, handler: Callable, *, summary: str, auth: bool = True,
               request: dict | None = None, response: str = "object") -> None:
        self.routes[(method, path)] = (handler, {
            "summary": summary, "auth": auth, "request": request, "response": response})

    def _register(self) -> None:
        self._route("GET", "/v1/health", self._health, summary="Liveness.", auth=False)
        self._route("GET", "/v1/capabilities", self._capabilities, summary="Tier and capability matrix.", auth=False)
        self._route("GET", "/v1/whoami", self._whoami, summary="Caller identity and scope.")
        self._route("POST", "/v1/resolve_tag", self._resolve_tag, request={
            "container_tag": "string: container/tenant identifier (alphanumeric, -, _)"
        }, summary="Resolve and validate a container isolation tag.")
        self._route("GET", "/v1/metrics", self._metrics,
                    summary="Request, tool and operation counters and latencies (Prometheus text; ?format=json).")
        self._route("GET", "/v1/openapi.json", self._openapi, summary="This API's schema.", auth=False)
        self._route("GET", "/v1/command-catalog", self._command_catalog,
                    summary="CLI command catalog for the authenticated console.")
        self._route("POST", "/v1/command", self._command, request={
            "command": "one command name from /v1/command-catalog",
            "args": "optional array of command arguments; the gateway store root is implicit",
        }, summary="Run one store-scoped CommonTrace CLI command.")
        self._route("POST", "/v1/recall", self._recall, request={
            "occasion_id": "string, your id for one episode/task/ticket",
            "items": "optional list of {id, text, protected?, meta?}: your candidate memories",
            "query": "optional string; ranks this store's lessons when `items` is absent",
            "top_k": "optional integer for store mode",
            "agent_id": "optional string: which agent or robot",
            "env": "optional string: must equal the store's environment if it has one",
        }, summary="Which memories to deliver on this occasion, which to withhold, which are withdrawn.")
        self._route("POST", "/v1/outcome", self._outcome, request={
            "occasion_id": "string",
            "succeeded": "boolean, or",
            "signals": "list of {detector, args} evaluated three-valued; with `combine`: all|any",
            "agent_id": "optional string",
        }, summary="How the occasion went. Records nothing while the signals are undecided.")
        self._route("POST", "/v1/conversation/add", self._conversation_add, request={
            "space": "string: one user, agent or thread",
            "session": "string: the session these messages belong to",
            "messages": "list of {speaker|role, text|content, at?, id?}",
            "session_at": "optional date the session took place",
        }, summary="Remember messages; relative dates are resolved as they are stored.")
        self._route("POST", "/v1/conversation/recall", self._conversation_recall, request={
            "space": "string", "question": "string",
            "budget": "optional integer: context size in tokens (default 1500)",
            "now": "optional date the question is asked",
            "sessions": "optional list of session ids", "speakers": "optional list of speakers",
            "since": "optional date", "until": "optional date",
        }, summary="The turns that answer a question, as a dated context within a token budget.")
        self._route("GET", "/v1/status", self._status, summary="Experiment and proof progress.")
        self._route("GET", "/v1/memories", self._memories, summary="Each memory's measured verdict.")
        self._route("GET", "/v1/occasions", self._occasions, summary="Recent recalls and outcomes (?limit=).")
        self._route("GET", "/v1/agents", self._agents, summary="Per-agent activity.")
        self._route("GET", "/v1/lessons", self._lessons, summary="Lessons and what is waiting for review (?status=).")
        self._route("GET", "/v1/lesson", self._lesson, summary="One lesson: text, gates, history (?slug=).")
        self._route("POST", "/v1/lesson/edit", self._lesson_edit, request={
            "slug": "a lesson in review", "rule": "optional", "applies_when": "optional",
            "do_not_apply_when": "optional"}, summary="Edit a review draft. Needs --allow-approval.")
        self._route("POST", "/v1/lesson/approve", self._lesson_approve, request={
            "slug": "a lesson in review", "rationale": "optional"},
            summary="Approve a review draft through every gate. Needs --allow-approval.")
        self._route("POST", "/v1/lesson/reject", self._lesson_reject, request={
            "slug": "a lesson in review", "reason": "why"}, summary="Reject a review draft. Needs --allow-approval.")

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None,
        body: bytes | None = None, *, trusted: bool = False,
    ) -> Response:
        from commontrace import telemetry

        headers = headers or {}
        supplied = next((v for k, v in headers.items() if k.lower() == "x-request-id"), "")
        request_id = supplied if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", supplied or "") else \
            telemetry.new_request_id()
        container_tag = next((v for k, v in headers.items() if k.lower() in ("x-container-tag", "container-tag")), "")
        path_label = urlsplit(target).path if (method, urlsplit(target).path) in self.routes else "other"
        with telemetry.bind(request_id=request_id, container_tag=container_tag, surface="gateway"), \
                telemetry.span(f"gateway {method} {path_label}") as handle:
            response = self._handle(method, target, headers, body, trusted=trusted)
            handle.set(status=response.status)
        telemetry.count("commontrace_gateway_requests", method=method, path=path_label, status=response.status)
        response.headers.setdefault("X-Request-Id", request_id)
        if container_tag:
            response.headers.setdefault("X-Container-Tag", container_tag)
        return response

    def _handle(
        self, method: str, target: str, headers: Mapping[str, str], body: bytes | None, *, trusted: bool,
    ) -> Response:
        try:
            split = urlsplit(target)
            path = split.path
            if not trusted and not self._host_ok(headers):
                raise ApiError(403, "bad_host", "the Host header is not allowed")
            if path in ("/", "/index.html", "/ui/app.js", "/ui/app.css", "/ui/favicon.svg") and method == "GET":
                return self._static(path)
            entry = self.routes.get((method, path))
            if entry is None:
                if any(p == path for (_m, p) in self.routes):
                    raise ApiError(405, "method_not_allowed", f"{method} is not allowed on {path}")
                raise ApiError(404, "not_found", f"no such endpoint: {path}")
            handler, spec = entry
            if spec["auth"] and not trusted and not self._authorised(headers):
                raise ApiError(401, "unauthorized", "a valid Authorization: Bearer token is required")
            container_tag = next((
                v for k, v in headers.items()
                if k.lower() in ("x-container-tag", "container-tag")
            ), "")
            if container_tag:
                self._validated_tag(container_tag)
            payload: dict = {}
            if method == "POST":
                payload = self._parse_body(body)
            result = handler(payload, parse_qs(split.query))
            if isinstance(result, Response):
                return result
            return _json(200, result)
        except TransientAuthError as exc:
            return _json(503, {"error": {"code": "transient_auth_error", "message": str(exc)}})
        except ApiError as exc:
            return _json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to a client
            return _json(500, {"error": {"code": "internal", "message": f"{type(exc).__name__}"}})

    def _host_ok(self, headers: Mapping[str, str]) -> bool:
        host = next((v for k, v in headers.items() if k.lower() == "host"), "")
        if not host:
            return True
        try:
            name = urlsplit("//" + host).hostname or ""
        except ValueError:
            return False
        return name.lower() in self.allowed_hosts

    def _authorised(self, headers: Mapping[str, str]) -> bool:
        if not self.token:
            return False
        for key, value in headers.items():
            if key.lower() == "authorization":
                scheme, _, supplied = value.partition(" ")
                return scheme.lower() == "bearer" and hmac.compare_digest(
                    supplied.strip().encode("utf-8"), self.token.encode("utf-8"))
        return False

    @staticmethod
    def _parse_body(body: bytes | None) -> dict:
        if not body:
            raise _bad("a JSON object body is required")
        if len(body) > MAX_BODY_BYTES:
            raise ApiError(413, "too_large", f"body is larger than {MAX_BODY_BYTES} bytes")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise _bad("body is not valid JSON") from None
        if not isinstance(payload, dict):
            raise _bad("body must be a JSON object")
        return payload

    def _static(self, path: str) -> Response:
        name = {"/": "index.html", "/index.html": "index.html", "/ui/app.js": "app.js",
                "/ui/app.css": "app.css", "/ui/favicon.svg": "favicon.svg"}[path]
        types = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8",
                 "css": "text/css; charset=utf-8", "svg": "image/svg+xml"}
        try:
            with open(os.path.join(_UI_DIR, name), "rb") as fh:
                data = fh.read()
        except OSError:
            raise ApiError(404, "no_ui", "the console is not installed in this build") from None
        return Response(200, data, types[name.rsplit(".", 1)[1]], {
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
        })


    def _events_path(self) -> str:
        return os.path.join(paths.memory_dir(self.root), EVENTS_NAME)

    def _log_event(self, event: dict) -> None:
        path = self._events_path()
        event = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds"), **event}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with self._events_lock, frontmatter.locked(path):
                if os.path.isfile(path) and os.path.getsize(path) > EVENT_LOG_ROTATE_BYTES:
                    os.replace(path, path + ".1")
                holdout_io._append_lines(path, [json.dumps(event, separators=(",", ":"))], durable=False)
        except OSError as exc:
            logger.warning("Failed to log gateway event to %s: %s", path, exc)

    def _read_events(self, limit: int = 5000) -> list[dict]:
        """Newest-N events within a trailing byte cap (bounded scan)."""
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = EVENTS_MAX_EVENTS
        limit = max(1, min(limit, EVENTS_MAX_EVENTS))
        path = self._events_path()
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                fh.seek(max(0, size - EVENTS_TAIL_BYTES))
                chunk = fh.read()
        except OSError:
            return []
        lines = chunk.splitlines()
        if size > EVENTS_TAIL_BYTES and lines:
            lines = lines[1:]
        out = []
        for line in lines[-limit:]:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
        return out

    def _events_identity(self) -> tuple | None:
        try:
            st = os.stat(self._events_path())
        except OSError:
            return None
        return (st.st_ino, st.st_size, st.st_mtime_ns)

    def _events_truncated(self) -> bool:
        try:
            return os.path.getsize(self._events_path()) > EVENTS_TAIL_BYTES
        except OSError:
            return False

    def _memoized_flag(self, name: str, compute, extra_key=None) -> tuple[Any, bool]:
        """Like _memoized but also reports whether the value came from the memo."""
        data_key = self._data_key()
        key = data_key if extra_key is None else (data_key, extra_key)
        now = time.monotonic()
        with self._memo_lock:
            hit = self._memo.get(name)
            if hit is not None and (now - hit[1] < REPORT_MIN_INTERVAL
                                    or (hit[0] == key and now - hit[1] < REPORT_MAX_AGE)):
                return hit[2], True
        value = compute()
        with self._memo_lock:
            self._memo[name] = (key, now, value)
        return value, False

    def _memoized_events(self, limit: int) -> tuple[list[dict], bool, bool]:
        """Bounded event scan with 5-60s memo; returns (events, cached, truncated)."""
        try:
            want = max(1, min(int(limit), EVENTS_MAX_EVENTS))
        except (TypeError, ValueError):
            want = EVENTS_MAX_EVENTS
        ident = self._events_identity()
        events, cached = self._memoized_flag(f"events:{want}", lambda: self._read_events(want), extra_key=ident)
        return list(events), cached, self._events_truncated()


    def _command_catalog(self, _body, _query) -> dict:
        from commontrace import ui_commands

        return {
            "commands": ui_commands.catalog(),
            "store": os.path.basename(self.root.rstrip(os.sep)) or "store",
        }

    def _command(self, req: dict, _query) -> dict:
        from commontrace import ui_commands

        command = req.get("command")
        try:
            return ui_commands.run(self.root, command, req.get("args"))
        except ui_commands.UICommandError as exc:
            code = "command_unavailable" if "terminal-only" in str(exc) else "bad_request"
            status = 409 if code == "command_unavailable" else 400
            raise ApiError(status, code, str(exc)) from None

    def _metrics(self, _body, query) -> dict | Response:
        from commontrace import telemetry

        if (query.get("format") or [""])[0] == "json":
            return telemetry.metrics()
        return Response(200, telemetry.prometheus().encode("utf-8"), "text/plain; version=0.0.4")

    def _capability_matrix(self) -> dict[str, Any]:
        retrieval_config = retrieval_io.load_config(self.root)
        has_embed = bool(retrieval_io.parse_embedder(retrieval_config.fusion))
        has_rerank = retrieval_config.rerank != retrieval_io.RERANK_NONE
        provider = os.environ.get("COMMONTRACE_LLM_PROVIDER", "").strip().lower()
        has_key = any(os.environ.get(name, "").strip() for name in (
            "COMMONTRACE_LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY", "GEMINI_API_KEY",
        ))
        local_provider = provider in {"ollama", "local", "llama.cpp", "llamacpp"}
        has_llm = bool(has_key or local_provider)
        if has_llm and has_embed:
            tier = "full"
        elif has_llm:
            tier = "no_embed"
        elif has_embed:
            tier = "no_llm"
        else:
            tier = "lexical"
        return {
            "llm": has_llm,
            "embeddings": has_embed,
            "rerank": has_rerank,
            "tier": tier,
            "degraded_paths": [
                name for name, enabled in (
                    ("lexical_retrieval", True),
                    ("llm_generation", has_llm),
                    ("semantic_retrieval", has_embed),
                    ("cross_encoder_rerank", has_rerank),
                ) if not enabled
            ],
            "rbac": True,
            "container_scoping": True,
            "defense_screen": True,
            "ssrf_guard": True,
        }

    def _health(self, _body, _query) -> dict:
        capabilities = self._capability_matrix()
        return {
            "ok": True,
            "api": API_VERSION,
            "version": __version__,
            "tier": capabilities["tier"],
            "capabilities": capabilities,
        }

    def _capabilities(self, _body, _query) -> dict:
        capabilities = self._capability_matrix()
        return {
            "api": API_VERSION,
            "version": __version__,
            "tier": capabilities["tier"],
            "capabilities": capabilities,
        }

    def _whoami(self, _body, _query) -> dict:
        from commontrace import telemetry

        curr = telemetry.current()
        return {
            "authenticated": bool(self.token is not None),
            "role": "admin" if self.token else "anonymous",
            "token_prefix": (self.token[:8] + "...") if self.token and len(self.token) >= 8 else "",
            "container_tag": curr.get("container_tag", ""),
            "request_id": curr.get("request_id", ""),
        }

    @staticmethod
    def _validated_tag(tag: str) -> str:
        clean_tag = tag.strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", clean_tag):
            raise _bad("container_tag must contain only letters, numbers, '.', '_', or '-' (max 128 chars)")
        return clean_tag

    def _request_scope(self) -> str:
        from commontrace import telemetry

        tag = str(telemetry.current().get("container_tag") or "")
        return f"container:{self._validated_tag(tag)}" if tag else ""

    def _scoped_space(self, space: str) -> str:
        scope = self._request_scope()
        if not scope:
            return space
        scoped = f"{scope}:{space}"
        if len(scoped) <= 128:
            return scoped
        digest = hashlib.sha256(f"{scope}\x1f{space}".encode("utf-8")).hexdigest()[:32]
        return f"container-{digest}"

    def _resolve_tag(self, req: dict, _query) -> dict:
        tag = req.get("container_tag")
        if not tag or not isinstance(tag, str):
            raise _bad("container_tag must be a non-empty string")
        clean_tag = self._validated_tag(tag)
        return {
            "container_tag": clean_tag,
            "scope": f"container:{clean_tag}",
            "valid": True,
        }

    def _check_env(self, req: dict) -> None:
        asked = req.get("env")
        if asked is None:
            return
        if not isinstance(asked, str):
            raise _bad("env must be a string")
        if self.config.env and asked != self.config.env:
            raise ApiError(409, "wrong_environment",
                           f"this store measures {self.config.env!r}, not {asked!r}; "
                           "simulation and reality are never pooled")

    def _protected(self, item: dict) -> bool:
        return bool(item.get("protected")) or any(
            item["id"].startswith(prefix) for prefix in self.config.protected_prefixes)

    def _store_candidates(self, req: dict, *, scope: str = "") -> list[dict]:
        query = _text(req.get("query"), "query", limit=2000)
        top_k = req.get("top_k", 5)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 50:
            raise _bad("top_k must be an integer from 1 to 50")

        def read(path):
            try:
                return frontmatter.read(path)
            except Exception:  # noqa: BLE001 - one unreadable lesson must not stop retrieval
                return None

        active, term_cache = _cached_active(self.root, read)
        if scope:
            from commontrace import lesson_cache

            active = lesson_cache.filter_eligible(active, scope=scope)
            allowed_paths = {path for path, _fm in active}
            term_cache = {path: terms for path, terms in term_cache.items() if path in allowed_paths}
        ranked = retrieval.rank_lessons(query, active, top_k=top_k, term_cache=term_cache)
        projected = dict(active)
        out = []
        for hit in ranked:
            try:
                body = _cached_body(hit.path)
            except frontmatter.FrontmatterError:
                continue
            out.append({"id": hit.slug, "text": body, "protected": bool(projected.get(hit.path, {}).get("core")),
                        "meta": {"description": hit.description, "relevance": round(hit.relevance, 4)}})
        return out

    def _conversation_add(self, req: dict, _query) -> dict:
        from commontrace.conversation import ConversationError, Store

        messages = req.get("messages")
        if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
            raise _bad("messages must be a list of objects with text (or content)")
        if len(messages) > 1000:
            raise _bad("at most 1000 messages per request")
        try:
            space = self._scoped_space(_ident(req.get("space"), "space"))
            with Store(self.root, space) as store:
                return store.add(_ident(req.get("session"), "session"), messages,
                                 session_at=req.get("session_at") or None)
        except ConversationError as exc:
            raise _bad(str(exc)) from None

    def _conversation_recall(self, req: dict, _query) -> dict:
        from commontrace.conversation import ConversationError, Options, Store, recall

        budget = req.get("budget", 1500)
        if not isinstance(budget, int) or isinstance(budget, bool) or not 50 <= budget <= 32_000:
            raise _bad("budget must be an integer number of tokens from 50 to 32000")
        question = _text(req.get("question"), "question", limit=4000)
        lists = {}
        for key in ("sessions", "speakers"):
            value = req.get(key) or []
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise _bad(f"{key} must be a list of strings")
            lists[key] = tuple(value)
        opts = Options(budget=budget, since=req.get("since") or None, until=req.get("until") or None, **lists)
        try:
            space = self._scoped_space(_ident(req.get("space"), "space"))
            with Store(self.root, space, create=False) as store:
                return recall(store, question, now=req.get("now") or None, options=opts).as_dict()
        except ConversationError as exc:
            raise ApiError(404 if "no conversations" in str(exc) else 400, "conversation", str(exc)) from None

    def _recall(self, req: dict, _query) -> dict:
        occasion = _ident(req.get("occasion_id"), "occasion_id")
        agent = _agent(req)
        self._check_env(req)
        mode = "items" if req.get("items") is not None else "store"
        scope = self._request_scope()
        candidates = (
            _items(req["items"])
            if mode == "items"
            else self._store_candidates(req, scope=scope)
        )

        clean, quarantined = [], []
        for item in candidates:
            labels = injection_guard.injection_labels({"body": item["text"]})
            (quarantined if labels else clean).append(
                {"id": item["id"], "reason": "injection screen: " + ", ".join(labels)} if labels else item)
        protected = {i["id"] for i in clean if self._protected(i)}
        by_id = {i["id"]: i for i in clean}

        config = holdout_io.load_config(self.root)
        started = bool(config.started_at) and config.running
        if started:
            memory = CausalMemory(
                lambda _q, **_kw: clean, root=self.root, key=lambda i: i["id"],
                text=lambda i: i["text"] or None, pinned=protected, scorer=f"gateway:{mode}",
                harm_watch=self._watch, durable=self.durable)
            result = memory.recall_detailed("", occasion_id=occasion)
            delivered = [dict(i) for i in result.items]
            withdrawn = result.withdrawn
        else:
            delivered, withdrawn = [dict(i) for i in clean], {}
        out_ids = {i["id"] for i in delivered}
        withheld = [i["id"] for i in clean if i["id"] not in out_ids and i["id"] not in withdrawn]
        response = {
            "occasion_id": occasion, "mode": mode, "deliver": delivered, "withheld": withheld,
            "withdrawn": [{"id": k, "verdict": v.get("verdict"), "effect": v.get("effect")}
                          for k, v in withdrawn.items()],
            "protected": sorted(protected & set(by_id)),
            "quarantined": quarantined,
            "holdout": {"running": started, "rate": config.rate if started else 0.0},
        }
        if self.config.env:
            response["env"] = self.config.env
        if not started:
            response["note"] = ("No experiment has been started, so everything is delivered and nothing "
                                "causal can be measured. Start one on purpose with `commontrace proof "
                                "start` or `commontrace experiment --configure`.")
        self._log_event({"kind": "recall", "occasion_id": occasion, "agent_id": agent,
                         "delivered": len(delivered), "withheld": len(withheld),
                         "withdrawn": len(withdrawn), "protected": len(response["protected"]),
                         "quarantined": len(quarantined)})
        return response

    def _outcome(self, req: dict, _query) -> dict:
        occasion = _ident(req.get("occasion_id"), "occasion_id")
        agent = _agent(req)
        self._check_env(req)
        if "succeeded" in req and "signals" in req:
            raise _bad("give `succeeded` or `signals`, not both")
        if "succeeded" in req:
            succeeded = req["succeeded"]
            if not isinstance(succeeded, bool):
                raise _bad("succeeded must be true or false")
        elif "signals" in req:
            succeeded = evaluate_signals(req["signals"], req.get("combine"))
            if succeeded is None:
                self._log_event({"kind": "undecided", "occasion_id": occasion, "agent_id": agent})
                return {"occasion_id": occasion, "recorded": False, "undecided": True,
                        "reason": "the signals do not settle this occasion yet; nothing was recorded"}
        else:
            raise _bad("give `succeeded` (a boolean) or `signals`")
        try:
            written = holdout_io.record_outcome(self.root, occasion, succeeded, self.durable)
        except holdout_io.ConflictingOutcome as exc:
            raise ApiError(409, "conflicting_outcome", str(exc)) from None
        self._log_event({"kind": "outcome", "occasion_id": occasion, "agent_id": agent,
                         "succeeded": succeeded})
        return {"occasion_id": occasion, "recorded": written, "succeeded": succeeded,
                **({} if written else {"note": "the same answer was already on record"})}

    def _data_key(self) -> tuple:
        key = []
        for path in (holdout_io.holdout_log_path(self.root), holdout_io.outcomes_log_path(self.root),
                     holdout_io.config_path(self.root), proof.state_path(self.root),
                     retrieval_io.config_path(self.root), paths.episodes_dir(self.root),
                     paths.traces_dir(self.root)):
            try:
                st = os.stat(path)
                key.append((st.st_ino, st.st_size, st.st_mtime_ns))
            except OSError:
                key.append(None)
        return tuple(key)

    def _memoized(self, name: str, compute):
        key, now = self._data_key(), time.monotonic()
        with self._memo_lock:
            hit = self._memo.get(name)
            if hit is not None and (now - hit[1] < REPORT_MIN_INTERVAL
                                    or (hit[0] == key and now - hit[1] < REPORT_MAX_AGE)):
                return hit[2]
        value = compute()
        with self._memo_lock:
            self._memo[name] = (key, now, value)
        return value

    def _analysis(self):
        return self._memoized("analysis", self._compute_analysis)

    def _compute_analysis(self):
        state = proof.load_state(self.root)
        rows = proof._current_rows(self.root)
        if not rows:
            return state, rows, None
        mode = proof._mode(state) if state else "sequential"
        vpo = state.get("value_per_occasion") if state else None
        return state, rows, proof.analyse_rows(
            rows, mode, datetime.datetime.now(datetime.timezone.utc).isoformat(), vpo)

    def _status(self, _body, _query) -> dict:
        config = holdout_io.load_config(self.root)
        events, activity_cached, truncated = self._memoized_events(EVENTS_STATUS_LIMIT)
        out: dict = {
            "gateway": {"api": API_VERSION, "version": __version__, "env": self.config.env,
                        "protected_prefixes": list(self.config.protected_prefixes),
                        "durable": self.durable, "harm_policy": retrieval_io.read_harm_policy(self.root),
                        "approval_enabled": self.allow_approval},
            "experiment": {"running": bool(config.started_at) and config.running,
                           "rate": config.rate if config.started_at and config.running else 0.0},
            "activity": {"recalls": sum(1 for e in events if e.get("kind") == "recall"),
                         "outcomes": sum(1 for e in events if e.get("kind") == "outcome"),
                         "window_events": len(events), "truncated": truncated,
                         "cached": activity_cached, "limit": EVENTS_STATUS_LIMIT},
            "proof": None,
            "cached": activity_cached,
        }
        if proof.load_state(self.root):
            try:
                payload, proof_cached = self._memoized_flag(
                    "proof", lambda: dataclasses.asdict(proof.status(self.root)))
                out["proof"] = payload
                out["proof_cached"] = proof_cached
            except proof.ProofError as exc:
                logger.debug("Gateway proof status unavailable: %s", exc)
        else:
            out["proof_cached"] = False
        return out

    def _memories(self, _body, _query) -> dict:
        _state, rows, analysis = self._analysis()
        if analysis is None:
            return {"memories": [], "integrity": None, "note": "no assignments recorded yet"}
        harmful = self._watch.current()
        return {
            "integrity": {"verdict": analysis.report.verdict, "readable": analysis.report.readable},
            "mode": analysis.mode,
            "memories": [{**dataclasses.asdict(e), "withdrawn": e.lesson_slug in harmful}
                         for e in analysis.effects],
            "occasions": len({r.occasion_id for r in rows}),
        }


    def _workbench(self, call):
        from commontrace import workbench

        try:
            return call(workbench)
        except workbench.WorkbenchError as exc:
            raise ApiError(exc.status, exc.code, exc.message) from None

    def _acting(self) -> None:
        if not self.allow_approval:
            raise ApiError(403, "approval_disabled", "start the gateway with --allow-approval to edit, approve or "
                                                       "reject lessons here")

    def _lessons(self, _body, query) -> dict:
        status = (query.get("status") or [None])[0]
        limit_raw = (query.get("limit") or [None])[0]
        offset_raw = (query.get("offset") or [None])[0]
        try:
            limit = None if limit_raw is None else int(limit_raw)
        except (TypeError, ValueError):
            raise _bad("limit must be a positive integer") from None
        try:
            offset = 0 if offset_raw is None else int(offset_raw)
        except (TypeError, ValueError):
            raise _bad("offset must be a non-negative integer") from None
        if limit is not None and (not 1 <= limit <= 1000):
            raise _bad("limit must be between 1 and 1000")
        if offset < 0:
            raise _bad("offset must be a non-negative integer")

        scope = self._request_scope()

        def fetch(w):
            if limit is None and not offset:
                return w.list_lessons(self.root, status, scope=scope)
            return w.list_lessons(self.root, status, limit=limit, offset=offset, scope=scope)

        lessons = self._workbench(fetch)
        if limit is None and not offset:
            total = len(lessons)
        else:
            total = self._workbench(lambda w: w.count_lessons(self.root, status, scope=scope))
        return {"lessons": lessons, "approval_enabled": self.allow_approval,
                "total": total, "limit": limit, "offset": offset}

    def _lesson(self, _body, query) -> dict:
        slug = (query.get("slug") or [""])[0]
        scope = self._request_scope()
        return {**self._workbench(lambda w: w.detail(self.root, slug, scope)), "approval_enabled": self.allow_approval}

    def _lesson_edit(self, body, _query) -> dict:
        self._acting()
        fields = {k: v for k, v in body.items() if k != "slug"}
        scope = self._request_scope()
        return self._workbench(lambda w: w.edit(self.root, body.get("slug", ""), fields, "console", scope))

    def _lesson_approve(self, body, _query) -> dict:
        self._acting()
        scope = self._request_scope()
        return self._workbench(lambda w: w.approve(self.root, body.get("slug", ""), body.get("rationale"),
                                                   "console", scope))

    def _lesson_reject(self, body, _query) -> dict:
        self._acting()
        scope = self._request_scope()
        return self._workbench(lambda w: w.reject(self.root, body.get("slug", ""), body.get("reason", ""), scope))

    @staticmethod
    def _limit(query: dict, default: int, cap: int) -> int:
        try:
            return max(1, min(int(query.get("limit", [default])[0]), cap))
        except (TypeError, ValueError):
            return default

    def _occasions(self, _body, query) -> dict:
        limit = self._limit(query, 50, 500)
        events, cached, truncated = self._memoized_events(limit)
        return {"events": list(reversed(events)), "cached": cached, "truncated": truncated,
                "window_events": len(events), "limit": limit}

    def _agents(self, _body, _query) -> dict:
        ident = self._events_identity()

        def compute():
            agents: dict[str, dict] = {}
            now = time.time()
            events = self._read_events(EVENTS_MAX_EVENTS)
            truncated = self._events_truncated()
            for e in events:
                who = e.get("agent_id") or "(unattributed)"
                a = agents.setdefault(who, {"agent_id": who, "recalls": 0, "outcomes": 0, "succeeded": 0,
                                            "withheld": 0, "protected": 0, "quarantined": 0, "last_seen": ""})
                if e.get("kind") == "recall":
                    a["recalls"] += 1
                    a["withheld"] += int(e.get("withheld", 0))
                    a["protected"] += int(e.get("protected", 0))
                    a["quarantined"] += int(e.get("quarantined", 0))
                elif e.get("kind") == "outcome":
                    a["outcomes"] += 1
                    a["succeeded"] += 1 if e.get("succeeded") else 0
                a["last_seen"] = e.get("at", a["last_seen"])
            rows = []
            for a in agents.values():
                a["success_rate"] = round(a["succeeded"] / a["outcomes"], 4) if a["outcomes"] else None
                try:
                    age = now - datetime.datetime.fromisoformat(a["last_seen"]).timestamp()
                except ValueError:
                    age = None
                a["seconds_since_seen"] = None if age is None else round(max(age, 0.0), 1)
                rows.append(a)
            rows.sort(key=lambda r: r["last_seen"], reverse=True)
            return {"agents": rows, "window": "last 5000 events", "window_events": len(events),
                    "truncated": truncated, "limit": EVENTS_MAX_EVENTS}

        payload, cached = self._memoized_flag("agents", compute, extra_key=ident)
        return {"agents": [dict(a) for a in payload["agents"]], "window": payload["window"],
                "window_events": payload["window_events"], "truncated": payload["truncated"],
                "limit": payload["limit"], "cached": cached}

    def _openapi(self, _body, _query) -> dict:
        paths_doc: dict = {}
        for (method, path), (_h, spec) in sorted(self.routes.items()):
            op: dict = {"summary": spec["summary"], "responses": {"200": {"description": "OK"}}}
            if spec["auth"]:
                op["security"] = [{"bearer": []}]
                op["responses"]["401"] = {"description": "missing or wrong token"}
            if spec["request"]:
                op["requestBody"] = {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "description": "fields: " + "; ".join(
                        f"{k}: {v}" for k, v in spec["request"].items())}}}}
            paths_doc.setdefault(path, {})[method.lower()] = op
        return {
            "openapi": "3.0.3",
            "info": {"title": "CommonTrace gateway", "version": API_VERSION,
                     "description": "Did the memory change how the occasion went? Language-neutral."},
            "paths": paths_doc,
            "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
        }


def make_http_server(gateway: Gateway, host: str, port: int, *, tls: tuple[str, str] | None = None,
                     request_timeout: float = 10.0) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = request_timeout

        def log_message(self, format: str, *args: Any) -> None:
            logger.debug("Gateway HTTP: %s", format % args)

        def handle(self):
            if isinstance(self.connection, ssl.SSLSocket):
                try:
                    self.connection.do_handshake()
                except (ssl.SSLError, OSError):
                    self.close_connection = True
                    return
            super().handle()

        def _send(self, response: Response) -> None:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(response.body)

        def _fail(self, status: int, code: str, message: str) -> None:
            self._send(_json(status, {"error": {"code": code, "message": message}}))
            self.close_connection = True

        def do_GET(self):  # noqa: N802
            self._send(gateway.handle("GET", self.path, dict(self.headers.items())))

        def do_POST(self):  # noqa: N802
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                return self._fail(411, "length_required", "send a Content-Length, not chunked encoding")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._fail(411, "length_required", "Content-Length is required")
            if length < 0 or length > MAX_BODY_BYTES:
                return self._fail(413, "too_large", f"body is larger than {MAX_BODY_BYTES} bytes")
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._fail(415, "unsupported_media_type", "Content-Type must be application/json")
            origin, host_header = self.headers.get("Origin"), self.headers.get("Host", "")
            if origin and urlsplit(origin).netloc != host_header:
                return self._fail(403, "bad_origin", "cross-origin requests are not accepted")
            body = self.rfile.read(length)
            self._send(gateway.handle("POST", self.path, dict(self.headers.items()), body))

        do_PUT = do_DELETE = do_PATCH = lambda self: self._fail(  # noqa: E731
            405, "method_not_allowed", "only GET and POST are used")

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True
        request_queue_size = 64

    server = Server((host, port), Handler)
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(*tls)
        server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    return server


def serve_stdio(gateway: Gateway, stdin, stdout) -> int:
    """One JSON object per line in, one per line out, until EOF."""
    shorthand = {"recall": ("POST", "/v1/recall"), "outcome": ("POST", "/v1/outcome"),
                 "status": ("GET", "/v1/status"), "memories": ("GET", "/v1/memories"),
                 "occasions": ("GET", "/v1/occasions"), "agents": ("GET", "/v1/agents"),
                 "health": ("GET", "/v1/health"),
                 "remember": ("POST", "/v1/conversation/add"), "converse": ("POST", "/v1/conversation/recall"),
                 "conversation_add": ("POST", "/v1/conversation/add"),
                 "conversation_recall": ("POST", "/v1/conversation/recall"),
                 "lessons": ("GET", "/v1/lessons"), "lesson": ("GET", "/v1/lesson"),
                 "lesson_edit": ("POST", "/v1/lesson/edit"), "edit": ("POST", "/v1/lesson/edit"),
                 "lesson_approve": ("POST", "/v1/lesson/approve"), "approve": ("POST", "/v1/lesson/approve"),
                 "lesson_reject": ("POST", "/v1/lesson/reject"), "reject": ("POST", "/v1/lesson/reject"),
                 "proof": ("GET", "/v1/status"), "proof_status": ("GET", "/v1/status")}

    def with_query(base: str, params: dict) -> str:
        clean = {k: v for k, v in params.items() if v is not None}
        if not clean:
            return base
        qs = urlencode(clean, doseq=True)
        return base + ("&" if "?" in base else "?") + qs if qs else base

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        request_id = None
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("a request must be a JSON object")
            request_id = req.get("id")
            if "op" in req:
                if req["op"] not in shorthand:
                    raise ValueError(f"unknown op {req['op']!r}")
                method, base = shorthand[req["op"]]
                params = {k: v for k, v in req.items() if k not in ("op", "id")}
                if method == "GET":
                    path, body = with_query(base, params), {}
                else:
                    path, body = base, params
            else:
                method, path, body = req.get("method", "POST"), req["path"], req.get("body") or {}
                if method == "GET" and isinstance(body, dict) and body:
                    path, body = with_query(path, body), {}
            response = gateway.handle(
                method, path, body=json.dumps(body).encode("utf-8") if method == "POST" else None,
                trusted=True)
            reply = {"id": request_id, "status": response.status, "body": json.loads(response.body)}
        except (ValueError, KeyError) as exc:
            reply = {"id": request_id, "status": 400,
                     "body": {"error": {"code": "bad_request", "message": str(exc)}}}
        stdout.write(json.dumps(reply, separators=(",", ":")) + "\n")
        stdout.flush()
    return 0

