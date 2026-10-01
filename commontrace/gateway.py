"""A language-neutral door into the causal loop, for any agent, including robots.

An LLM agent can use `commontrace serve` (MCP). A robot's perception stack, a
C++ planner, a Rust controller, a shell script or a ROS node cannot, and should
not have to embed Python to get the one thing this product is for: *did the
memory I gave this episode change how it went?* The gateway is that door: plain
JSON over HTTP (`commontrace gateway`) or one JSON object per line over stdio
(`commontrace gateway --stdio`), with one dispatcher behind both.

    POST /v1/recall    {occasion_id, items:[{id,text,protected?}]}   -> deliver / withheld / withdrawn
    POST /v1/outcome   {occasion_id, succeeded} | {occasion_id, signals:[...]}
    GET  /v1/status    /v1/memories   /v1/occasions   /v1/agents   /v1/openapi.json

MEMORY-AGNOSTIC. The caller brings its candidate memories (`items`) from whatever
store it uses and the gateway only randomizes, withdraws what is measured to hurt
and records outcomes, exactly as `CausalMemory` does for a Python caller. Without
`items` it ranks this store's own active lessons.

WHAT A PHYSICAL SYSTEM NEEDS THAT A CHATBOT DOES NOT
  * A memory that is a SAFETY CONSTRAINT must never be withheld as a control. An
    item with `protected: true`, or whose id starts with a protected prefix
    (stored with the store, so every client obeys it), is always delivered, never
    randomized, never withdrawn and never counted.
  * Simulation must not be pooled with reality. A store measures ONE environment
    (`--env sim|real|...`); a request naming another is refused.
  * A control loop cannot wait on an `fsync`. `--relaxed-durability` skips it: a
    power loss can drop the last few log lines (a missing observation, never a
    wrong one).
  * Outcomes arrive as measurements, not booleans. `signals` evaluates the same
    detectors `commontrace.outcome_detect` has, three-valued: an undecided signal
    records nothing.

SECURITY. Every `/v1` call but health and the schema needs `Authorization: Bearer`.
The Host header must be a loopback name or one the operator allowed (DNS
rebinding), bodies are capped, the HTTP server times out slow clients, and every
memory text is screened for injection before it can be delivered
(commontrace/injection_guard.py). Not TLS: bind to loopback or terminate TLS in
front of it (or pass --tls-cert/--tls-key).
"""
from __future__ import annotations

import dataclasses
import datetime
import hmac
import json
import os
import re
import secrets
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

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
MAX_BODY_BYTES = 1 << 20
MAX_ITEMS = 200
MAX_TEXT_CHARS = 20_000
MAX_ID_CHARS = 128
MAX_SIGNALS = 16
EVENTS_NAME = "gateway_events.jsonl"
CONFIG_NAME = "gateway.json"
TOKEN_NAME = "gateway.token"
EVENT_LOG_ROTATE_BYTES = 10 * 1024 * 1024
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _bad(message: str, code: str = "bad_request") -> ApiError:
    return ApiError(400, code, message)


# --- Store-held policy ----------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class GatewayConfig:
    #: The one environment this store measures ("sim", "real", ...), or None.
    env: str | None = None
    #: Item ids starting with any of these are safety-protected, for every client.
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
    """Fold command-line choices into the store's policy. An environment, once set, is
    not changed here: a store that measured simulation must not start measuring
    hardware under the same salt."""
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
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    os.makedirs(paths.memory_dir(root), exist_ok=True)
    fd = os.open(token_path(root), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token


# --- Request validation ----------------------------------------------------------------


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
    """Run detector calls through `outcome_detect` and combine them. None = undecided.

    Detectors are looked up by name from the module's own `from_*` functions, so a
    new detector is available here the moment it exists, and an unknown name is a
    400 -- never an attribute lookup on whatever string a client sends.
    """
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


# --- The gateway -------------------------------------------------------------------------


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
        allowed_hosts: tuple[str, ...] = (),
    ) -> None:
        self.root = os.path.abspath(root)
        self.token = token
        self.config = config if config is not None else load_config(self.root)
        self.durable = durable
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts) | LOOPBACK_HOSTS
        self._watch = HarmWatch(self.root, on_harm, check_every)
        self._events_lock = threading.Lock()
        self.routes: dict[tuple[str, str], tuple[Callable, dict]] = {}
        self._register()

    # -- routing -------------------------------------------------------------------------

    def _route(self, method: str, path: str, handler: Callable, *, summary: str, auth: bool = True,
               request: dict | None = None, response: str = "object") -> None:
        self.routes[(method, path)] = (handler, {
            "summary": summary, "auth": auth, "request": request, "response": response})

    def _register(self) -> None:
        self._route("GET", "/v1/health", self._health, summary="Liveness.", auth=False)
        self._route("GET", "/v1/openapi.json", self._openapi, summary="This API's schema.", auth=False)
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
        self._route("GET", "/v1/status", self._status, summary="Experiment and proof progress.")
        self._route("GET", "/v1/memories", self._memories, summary="Each memory's measured verdict.")
        self._route("GET", "/v1/occasions", self._occasions, summary="Recent recalls and outcomes (?limit=).")
        self._route("GET", "/v1/agents", self._agents, summary="Per-agent activity.")

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None,
        body: bytes | None = None, *, trusted: bool = False,
    ) -> Response:
        """One request, from any transport. `trusted` skips the token (stdio: the caller
        already owns the process) but never the validation."""
        headers = headers or {}
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
            payload: dict = {}
            if method == "POST":
                payload = self._parse_body(body)
            result = handler(payload, parse_qs(split.query))
            if isinstance(result, Response):
                return result
            return _json(200, result)
        except ApiError as exc:
            return _json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to a client
            return _json(500, {"error": {"code": "internal", "message": f"{type(exc).__name__}"}})

    def _host_ok(self, headers: Mapping[str, str]) -> bool:
        """DNS-rebinding defence: a page on evil.example resolving to 127.0.0.1 still
        sends `Host: evil.example`, which is not a name this gateway answers to."""
        host = next((v for k, v in headers.items() if k.lower() == "host"), "")
        if not host:
            return True  # HTTP/1.0 without Host: nothing to rebind
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

    # -- events (the console's view; never part of the measurement) ----------------------

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
        except OSError:
            pass  # a log the console reads must never break a control loop

    def _read_events(self, limit: int = 5000) -> list[dict]:
        path = self._events_path()
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                fh.seek(max(0, size - 1_500_000))
                chunk = fh.read()
        except OSError:
            return []
        lines = chunk.splitlines()
        if size > 1_500_000 and lines:
            lines = lines[1:]  # the first line is probably cut
        out = []
        for line in lines[-limit:]:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
        return out

    # -- handlers ----------------------------------------------------------------------------

    def _health(self, _body, _query) -> dict:
        return {"ok": True, "api": API_VERSION, "version": __version__}

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

    def _store_candidates(self, req: dict) -> list[dict]:
        query = _text(req.get("query"), "query", limit=2000)
        top_k = req.get("top_k", 5)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 50:
            raise _bad("top_k must be an integer from 1 to 50")

        def read(path):
            try:
                return frontmatter.read(path)
            except Exception:  # noqa: BLE001 - one unreadable lesson must not stop retrieval
                return None

        from commontrace import lesson_cache

        active = lesson_cache.load_active(self.root, None, reader=read)
        ranked = retrieval.rank_lessons(query, active, top_k=top_k)
        out = []
        for hit in ranked:
            parsed = read(hit.path)
            if parsed is None:
                continue
            fm, body = parsed
            out.append({"id": hit.slug, "text": body, "protected": bool(fm.get("core")),
                        "meta": {"description": hit.description, "relevance": round(hit.relevance, 4)}})
        return out

    def _recall(self, req: dict, _query) -> dict:
        occasion = _ident(req.get("occasion_id"), "occasion_id")
        agent = _agent(req)
        self._check_env(req)
        mode = "items" if req.get("items") is not None else "store"
        candidates = _items(req["items"]) if mode == "items" else self._store_candidates(req)

        clean, quarantined = [], []
        for item in candidates:
            labels = injection_guard.injection_labels({"body": item["text"]})
            (quarantined if labels else clean).append(
                {"id": item["id"], "reason": "injection screen: " + ", ".join(labels)} if labels else item)
        protected = {i["id"] for i in clean if self._protected(i)}
        by_id = {i["id"]: i for i in clean}

        config = holdout_io.load_config(self.root)
        # Nothing is withheld until an experiment has been STARTED on purpose. A store
        # with no experiment config defaults to a 10% holdout for the chatbot case, but
        # withholding a memory from hardware nobody chose to measure is a decision, not
        # a default.
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
                         "withdrawn": len(withdrawn), "protected": len(response["protected"])})
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

    def _analysis(self):
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
        events = self._read_events(2000)
        out: dict = {
            "gateway": {"api": API_VERSION, "version": __version__, "env": self.config.env,
                        "protected_prefixes": list(self.config.protected_prefixes),
                        "durable": self.durable, "harm_policy": retrieval_io.read_harm_policy(self.root)},
            "experiment": {"running": bool(config.started_at) and config.running,
                           "rate": config.rate if config.started_at and config.running else 0.0},
            "activity": {"recalls": sum(1 for e in events if e.get("kind") == "recall"),
                         "outcomes": sum(1 for e in events if e.get("kind") == "outcome"),
                         "window_events": len(events)},
            "proof": None,
        }
        if proof.load_state(self.root):
            try:
                out["proof"] = dataclasses.asdict(proof.status(self.root))
            except proof.ProofError:
                pass
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

    @staticmethod
    def _limit(query: dict, default: int, cap: int) -> int:
        try:
            return max(1, min(int(query.get("limit", [default])[0]), cap))
        except (TypeError, ValueError):
            return default

    def _occasions(self, _body, query) -> dict:
        limit = self._limit(query, 50, 500)
        return {"events": list(reversed(self._read_events(limit)))}

    def _agents(self, _body, _query) -> dict:
        agents: dict[str, dict] = {}
        now = time.time()
        for e in self._read_events(5000):
            who = e.get("agent_id") or "(unattributed)"
            a = agents.setdefault(who, {"agent_id": who, "recalls": 0, "outcomes": 0, "succeeded": 0,
                                        "withheld": 0, "protected": 0, "last_seen": ""})
            if e.get("kind") == "recall":
                a["recalls"] += 1
                a["withheld"] += int(e.get("withheld", 0))
                a["protected"] += int(e.get("protected", 0))
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
        return {"agents": rows, "window": "last 5000 events"}

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


# --- Transports --------------------------------------------------------------------------


def make_http_server(gateway: Gateway, host: str, port: int, *, tls: tuple[str, str] | None = None,
                     request_timeout: float = 10.0) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = request_timeout  # a slow client cannot hold a thread for ever

        def log_message(self, *args, **kwargs):  # silence the default stderr access log
            pass

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
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def serve_stdio(gateway: Gateway, stdin, stdout) -> int:
    """One JSON object per line in, one per line out, until EOF.

    Request: {"id": any, "method": "POST", "path": "/v1/recall", "body": {...}} or the
    shorthand {"id": any, "op": "recall", ...fields}. Response:
    {"id": same, "status": 200, "body": {...}}. The process that spawned this is
    trusted, so no token is needed; validation is unchanged.
    """
    shorthand = {"recall": ("POST", "/v1/recall"), "outcome": ("POST", "/v1/outcome"),
                 "status": ("GET", "/v1/status"), "memories": ("GET", "/v1/memories"),
                 "occasions": ("GET", "/v1/occasions"), "agents": ("GET", "/v1/agents"),
                 "health": ("GET", "/v1/health")}
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
                method, path = shorthand[req["op"]]
                body = {k: v for k, v in req.items() if k not in ("op", "id")}
            else:
                method, path, body = req.get("method", "POST"), req["path"], req.get("body") or {}
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

