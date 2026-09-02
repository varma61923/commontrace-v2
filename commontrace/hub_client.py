"""The client half of the Local <-> Hub bridge described in
protocol/PROTOCOL.md §5. Talks to a CommonTrace Hub (see hub/) over MCP.

This module is only imported when `commontrace sync` actually needs to talk
to a Hub (i.e. a URL + API key are configured) -- the `mcp` client
dependency is an optional extra (`pip install commontrace[hub-sync]`) so the
core CLI install stays PyYAML-only. Import failures are converted to
HubClientUnavailable with an actionable message rather than a raw
ModuleNotFoundError traceback.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, TypeVar

from commontrace import frontmatter, paths, templates, trace_io

_T = TypeVar("_T")

# Network defaults. Overridable per call; `commontrace sync` exposes them
# as --timeout / --max-attempts.
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 0.5
# Ceiling on any single backoff sleep, including one derived from a
# server-sent Retry-After. A Hub that returns `Retry-After: 3600` must not
# turn `commontrace sync` into an hour-long hang with no output -- the CLI
# gives up and says it was rate limited, which the operator can act on,
# rather than silently blocking.
RETRY_MAX_DELAY_SECONDS = 30.0
# Attempt budget for HTTP 429 specifically, separate from DEFAULT_MAX_ATTEMPTS
# (which covers transport failures). See HubSession.call for why the two are
# counted apart: during a bulk push, being rate limited is the expected
# steady state once the server's burst allowance is spent, not a failure --
# and each of these attempts is a timed wait behind _RateLimitGate rather
# than another request.
RATE_LIMIT_MAX_ATTEMPTS = 10
# Adaptive pacing bounds for _RateLimitGate. The floor is the first spacing
# tried after a refusal; the ceiling stops a pathologically low server limit
# from stretching one batch across hours. The decay is applied per success,
# so a batch recovers its speed in tens of calls rather than instantly
# (which would just re-trip the limiter) or never.
_PACE_MIN_INTERVAL_SECONDS = 0.05
_PACE_MAX_INTERVAL_SECONDS = 2.0
_PACE_DECAY = 0.9

# A status code named AS a status, not any three digits that happen to appear
# in an error string. `"429" in text` (or an unanchored \b\d{3}\b) also matches
# the "500" in "title exceeds 500 chars" and the digits inside a trace id --
# which would classify a permanent, tool-level rejection as a retryable 5xx and
# retry it three times. Only used when no structured status is available.
_STATUS_IN_TEXT_RE = re.compile(
    r"(?:\bHTTP[/ ]?(?:\d\.\d )?|\bstatus(?:[ _]?code)?[ =:]+|\bcode[ =:]+)(4\d{2}|5\d{2})\b",
    re.IGNORECASE,
)

_SECTION_NAMES = r"Rule|Why|How to apply|Counter-examples"
_SECTION_RE = re.compile(
    rf"^##\s*({_SECTION_NAMES})\s*\n(.*?)(?=\n##\s*(?:{_SECTION_NAMES})\s*\n|\Z)",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)


class HubClientUnavailable(RuntimeError):
    """Raised when the optional `mcp` client dependency isn't installed."""


class HubConnectionError(RuntimeError):
    """Raised when the Hub can't be reached or rejects the request."""


class HubConfigurationError(HubConnectionError):
    """The Hub URL itself is unusable (bad scheme, plaintext to a remote
    host, a cloud-metadata address).

    Never retryable: no number of attempts makes a `file://` URL work. Its
    own type so that is decided by what the failure IS, rather than by a
    substring heuristic reading its message.
    """


class _ToolRateLimited(Exception):
    """Internal: the Hub's tool layer refused this call for rate limiting.

    Distinct from an HTTP 429, and easy to miss because it does not look
    like a failure at any transport layer: hub/server.py returns it as a
    perfectly successful MCP result whose PAYLOAD is
    `{"error": "rate_limited", ...}`. So it never reached any retry or
    pacing logic -- every batch caller here read `result.get("error")` and
    recorded a permanent per-file failure for what was actually "come back
    in two seconds".

    Measured: with HTTP-level pacing already working, a 46-trace
    `sync --push-traces` still lost 28 of 46 files to this one, because the
    Hub's write limiter (20/minute by default) refuses a backlog push as a
    matter of course. Raised here so it joins the same rate-limit gate and
    attempt budget the HTTP 429s use.
    """

    def __init__(self, detail: str, retry_after: float | None = None):
        super().__init__(detail)
        self.retry_after = retry_after


class HubToolError(HubConnectionError):
    """The Hub ran the tool and the tool said no (an MCP `is_error` result):
    a schema-invalid trace, an over-length title, a spent entitlement.

    Its own type because it is the one failure here that must NEVER be
    retried under any classification -- it fails identically on every
    attempt, and its message is free text from the server that can contain
    anything, digits included. Left as a plain HubConnectionError, a
    rejection reading "title exceeds 500 chars" was eligible to be read as
    a retryable 5xx by any status-sniffing heuristic.
    """


class HubAuthError(HubConnectionError):
    """The Hub rejected the credential (401/403).

    A subclass rather than a distinct type so every existing
    `except HubConnectionError` call site keeps catching it, but named so
    the message can say "your API key was rejected" instead of the
    "could not reach the Hub" this used to be reported as. Retrying it is
    pointless -- and actively harmful, since the Hub rate-limits auth
    attempts per source address (hub/abuse.py:make_auth_rate_limiter).
    """


class HubRateLimited(HubConnectionError):
    """The Hub returned 429. Carries the server's Retry-After, when it sent
    one, so a caller can pace rather than guess."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


# How many files push_active_lessons/push_captured_traces push at once.
# Each push is one independent network round trip (~150ms typical) against
# a DIFFERENT file with no shared mutable state between them (frontmatter.
# locked() already serializes access to any one file against other local
# processes), so pushing them one-at-a-time serially was pure wasted wall
# time: a 500-file `sync --push`/`--push-traces` spent ~75s just waiting on
# round trips that could run concurrently. Bounded rather than unbounded
# (`asyncio.gather` over everything at once): the Hub's own per-org write
# rate limiter (hub/abuse.py) has a burst ceiling, and firing hundreds of
# requests in one instant risks tripping it and turning pushes that would
# have succeeded serially into 429s -- a small, fixed concurrency keeps the
# speedup without changing whether a sync run succeeds.
_PUSH_CONCURRENCY = 8


@dataclass
class PushResult:
    slug: str
    hub_trace_id: str | None
    quarantined: bool = False
    error: str | None = None
    # True when the lesson was already on the Hub and this run did not
    # re-contribute it. Distinct from an error: nothing went wrong, there
    # was simply nothing to do.
    skipped: bool = False


@dataclass
class PullResult:
    written_paths: list[str] = field(default_factory=list)
    n_found: int = 0
    #: Query terms the Hub did NOT search on, because they appear in too
    #: much of this org's corpus to distinguish one trace from another
    #: (hub/search.py:choose_terms). Carried up so that a pull returning
    #: nothing can say WHY -- "your words are ones nearly every trace
    #: contains" and "your corpus has no match" look identical otherwise,
    #: and only the first one is fixed by rephrasing.
    ignored_terms: list[str] = field(default_factory=list)


def _lesson_sections(body: str) -> dict[str, str]:
    # First occurrence wins -- see commontrace/trace_io.py:_first_wins. This
    # path is worse than the trace one if it gets it wrong: the clobbered
    # `Rule` is what gets contributed to the Hub as solution_text.
    out: dict[str, str] = {}
    for m in _SECTION_RE.finditer(body):
        key = m.group(1).lower()
        if key not in out:
            out[key] = m.group(2).strip()
    return out


def _iter_active_lesson_paths(root: str):
    ldir = paths.lessons_dir(root)
    for p in sorted(glob.glob(os.path.join(ldir, "lesson_*.md"))):
        if os.path.basename(p) == "lesson_template.md":
            continue
        yield p


def _iter_captured_trace_paths(root: str):
    """Every file `commontrace capture` has ever written, in
    paths.traces_dir -- a DIFFERENT directory from paths.lessons_dir above.
    Traces and lessons are deliberately separate local stores (raw
    incident record vs. curated, reusable knowledge distilled from one or
    more traces), so push_active_lessons's iterator over lessons_dir never
    sees these files at all, and vice versa."""
    tdir = paths.traces_dir(root)
    for p in sorted(glob.glob(os.path.join(tdir, "*.md"))):
        # init_cmd.py writes a README.md into every traces_dir. It has no
        # frontmatter delimiter, so trace_io.read() doesn't raise on it
        # (frontmatter.read() treats a file with no leading "---" as valid,
        # empty frontmatter, by design -- see its own docstring) -- it
        # silently returns a near-empty instance instead, which then
        # reached _call_tool as a doomed contribute_trace(title="",
        # context_text="", ...) on every single --push-traces run.
        # commontrace/commands/_traces.py's own trace-directory iterator
        # already excludes this same file for the same reason.
        if os.path.basename(p) == "README.md":
            continue
        # Files pulled FROM the Hub are not locally captured traces, and
        # must never be pushed back TO it. `pull_search_results` writes
        # them as `hub_<slug>_<id>.md` with `hub_trace_id` already set, so
        # `--push-traces` saw each one as "on the Hub but with no recorded
        # fingerprint" and issued an amend_trace against the Hub's own
        # trace -- superseding a record with a round-tripped copy of
        # itself. Reproduced: a store of 46 captured traces became 68 push
        # candidates after one `sync --pull`, and each spurious amend
        # consumed a write-rate-limit token and a plan storage slot for a
        # trace whose content had not changed.
        #
        # The prefix is unambiguous: `capture`/`import` name every file
        # they write `<date>_<slug>_<id>.md`, so `hub_` is only ever
        # written by the pull path.
        if os.path.basename(p).startswith("hub_"):
            continue
        yield p


def _validate_hub_url(hub_url: str) -> None:
    """Reject anything but http(s) before it reaches a transport.

    httpx already refuses to open a `file://` or `ftp://` "connection", so
    nothing is actually exploitable today -- but that safety is incidental to
    the HTTP client's own behavior, not a guarantee this module makes. Left
    unchecked, a bad scheme also wastes the full retry budget (3 attempts,
    exponential backoff) on something that can never succeed, and surfaces as
    an opaque "unhandled errors in a TaskGroup" rather than a clear message.

    Also refuses plaintext `http://` to anything that is not loopback. The
    Authorization: Bearer header carrying the org's API key goes out on
    every call this client makes, and a Hub URL is normally set once in an
    environment variable and then trusted forever -- there is no per-call
    review that would catch a stray `http://` to a real endpoint. Loopback
    is exempt because it is the standard way to develop against a local
    Hub without a certificate, and traffic to it never leaves the host.
    """
    import ipaddress
    from urllib.parse import urlparse

    parsed = urlparse(hub_url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise HubConfigurationError(
            f"refusing to use Hub URL {hub_url!r}: scheme must be http or https, got {scheme or '(none)'!r}"
        )
    hostname = (parsed.hostname or "").lower()
    if scheme == "http" and hostname not in ("localhost", "127.0.0.1", "::1"):
        raise HubConfigurationError(
            f"refusing to use plaintext http:// for remote Hub URL {hub_url!r}: "
            "the API key is sent as a Bearer token on every call. Use https://, "
            "or connect to localhost/127.0.0.1 for local development."
        )
    # Link-local addresses (169.254.0.0/16, fe80::/10) are where AWS/GCP/
    # Azure's cloud metadata service lives (169.254.169.254 -- and GCP's
    # own metadata.google.internal hostname resolves there too), which
    # serves instance credentials over plain HTTP with no auth of its own.
    # COMMONTRACE_HUB_URL is normally an operator-set value trusted for
    # the life of the process, not attacker-controlled per request -- but
    # this client sends the org's Bearer API key on every call it makes to
    # whatever URL is configured, so a Hub URL that got misconfigured or
    # tampered with (a compromised .env, a copy-pasted value from an
    # untrusted source) pointing here would leak that key straight into an
    # SSRF against the host's own cloud credentials. Checked against the
    # literal hostname only, not a DNS resolution -- consistent with the
    # rest of this function, and enough to catch the address actually
    # being configured rather than a same-process TOCTOU DNS-rebind, which
    # is a materially different, harder attack this check does not claim
    # to cover.
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None
    # AWS's IPv6 metadata address is a Unique Local Address (fd00::/8), not
    # link-local (fe80::/10) -- checked by literal value rather than
    # widening the range check, since blocking ULA/RFC1918 space generally
    # would break the legitimate case of a Hub deployed on a private
    # network address, which this project explicitly supports.
    if ip is not None and (ip.is_link_local or str(ip) == "fd00:ec2::254"):
        raise HubConfigurationError(
            f"refusing to use Hub URL {hub_url!r}: {hostname} is a link-local or "
            "cloud-metadata address (e.g. 169.254.169.254, fd00:ec2::254) -- "
            "refusing to send the Hub API key there."
        )


class HttpStatusProbe:
    """Records the last non-2xx HTTP response seen on one httpx client.

    THE MCP SDK THROWS THE STATUS AWAY. A 401 from the Hub's auth
    middleware and a 429 from its rate limiter both surface to this module
    as the same opaque `MCPError: Server returned an error response`, with
    no status code anywhere on the exception or in its message. Measured
    against a live Hub: a rejected API key was indistinguishable from a
    rate limit, so it was retried three times -- straight into the Hub's
    per-source auth-attempt limiter, which is exactly the behaviour the
    retry policy documents as the thing it must not do.

    An httpx response event hook sees the real status before the SDK
    swallows it. This is the only place the distinction survives, so
    classification consults it whenever the exception itself carries
    nothing (see `_is_auth_failure` / `_is_rate_limited`).

    Only error statuses are recorded, and the value is reset before each
    call, so a stale 429 from an earlier request in the same session can
    never be blamed for a later, different failure.
    """

    __slots__ = ("status", "retry_after")

    def __init__(self) -> None:
        self.status: int | None = None
        self.retry_after: float | None = None

    def reset(self) -> None:
        self.status = None
        self.retry_after = None

    async def record(self, response) -> None:
        if response.status_code < 400:
            return
        self.status = response.status_code
        raw = response.headers.get("retry-after")
        if raw:
            try:
                self.retry_after = max(0.0, float(str(raw).strip()))
            except ValueError:
                self.retry_after = None


async def _open_session(hub_url: str, api_key: str, timeout_seconds: float):
    _validate_hub_url(hub_url)
    try:
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:
        raise HubClientUnavailable(
            "the Hub client needs the optional 'mcp' dependency. Install it with "
            "`pip install commontrace[hub-sync]`."
        ) from exc

    probe = HttpStatusProbe()
    # Without an explicit timeout the underlying client waits indefinitely,
    # so a Hub that accepts the connection and then stalls hangs
    # `commontrace sync` forever with no output -- the worst failure mode for
    # a CLI someone may have put in a cron job.
    http_client = httpx.AsyncClient(
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout_seconds,
        event_hooks={"response": [probe.record]},
    )
    # Returned alongside the transport/session so the caller can close it
    # explicitly (httpx.AsyncClient owns a connection pool / open sockets
    # that are never released otherwise -- streamable_http_client wraps it
    # but does not take ownership of its lifecycle).
    return streamable_http_client(hub_url, http_client=http_client), ClientSession, http_client, probe


def _iter_causes(exc: BaseException, _seen: set[int] | None = None):
    """Yield `exc` and every exception reachable from it: the members of an
    ExceptionGroup (recursively), plus `__cause__`/`__context__` chains.

    THIS IS THE FIX FOR A RETRY LAYER THAT NEVER RAN. Everything below
    talks to the Hub through the MCP SDK, which drives its transport from
    inside an `anyio` task group. A task group re-raises whatever its child
    task raised wrapped in an `ExceptionGroup` -- and here, nested two deep:

        ExceptionGroup('unhandled errors in a TaskGroup', [
            ExceptionGroup('unhandled errors in a TaskGroup', [
                <the real httpx.ConnectError / MCPError>])])

    So the exception `_call_tool` actually caught was never an
    `httpx.HTTPStatusError` with a `.response.status_code`, and its string
    form was never "connection refused" -- it was always the literal text
    "unhandled errors in a TaskGroup (1 sub-exception)". Measured against a
    live Hub before this change:

      - a refused connection (the textbook retryable case) was classified
        NOT retryable and tried exactly once;
      - a rejected API key was classified not-retryable only by accident
        (the group's text happens to contain none of the auth markers),
        not by the 401 branch that was written to do it;
      - `--max-attempts` was, in consequence, a no-op flag.

    Flattening the tree first is what lets every classification below run
    against the exception that actually happened.

    `_seen` guards against a cycle in the `__cause__`/`__context__` chain,
    which is legal to construct and would otherwise recurse forever.
    """
    _seen = set() if _seen is None else _seen
    if id(exc) in _seen:
        return
    _seen.add(id(exc))
    yield exc
    for nested in getattr(exc, "exceptions", ()) or ():
        yield from _iter_causes(nested, _seen)
    for link in (exc.__cause__, exc.__context__):
        if link is not None:
            yield from _iter_causes(link, _seen)


def root_cause(exc: BaseException) -> BaseException:
    """The most informative exception inside `exc`.

    Prefers a leaf carrying a real HTTP status, then any leaf that is not
    itself a grouping wrapper, then `exc`. Used for the message the user
    finally reads: "unhandled errors in a TaskGroup (1 sub-exception)"
    names nothing an operator can act on, while the `ConnectError` or the
    401 inside it names the actual problem.
    """
    leaves = [e for e in _iter_causes(exc) if not getattr(e, "exceptions", None)]
    for leaf in leaves:
        if _http_status(leaf) is not None:
            return leaf
    # This module's own wrappers are the least informative leaf available:
    # they restate a diagnosis rather than name the underlying failure.
    for leaf in leaves:
        if not isinstance(leaf, HubConnectionError):
            return leaf
    return leaves[0] if leaves else exc


def _http_status(exc: BaseException) -> int | None:
    """The HTTP status this one exception carries, if any.

    `exc.response.status_code` is httpx's shape. The status-in-the-message
    fallback is deliberately anchored to a word boundary: an unanchored
    `"429" in text` also matches a trace id or a byte count that happens to
    contain those digits.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    match = _STATUS_IN_TEXT_RE.search(str(exc))
    return int(match.group(1)) if match else None


def _retry_after_seconds(exc: BaseException) -> float | None:
    """The server's `Retry-After`, in seconds, if it sent one we can read.

    Only the delta-seconds form is honoured. The HTTP-date form is legal
    too, but parsing it correctly means trusting the client's clock against
    the server's; when it appears, this returns None and the caller falls
    back to its own exponential backoff, which is never wrong, only less
    precise.
    """
    for candidate in _iter_causes(exc):
        # A tool-layer refusal carries the value directly rather than in a
        # header -- see _ToolRateLimited.
        direct = getattr(candidate, "retry_after", None)
        if isinstance(direct, (int, float)) and direct >= 0:
            return float(direct)
        headers = getattr(getattr(candidate, "response", None), "headers", None)
        if headers is None:
            continue
        try:
            raw = headers.get("retry-after")
        except Exception:  # noqa: BLE001 - a header mapping that misbehaves is not our problem
            continue
        if not raw:
            continue
        try:
            seconds = float(str(raw).strip())
        except ValueError:
            continue
        if seconds >= 0:
            return seconds
    return None


def _is_auth_failure(exc: BaseException, observed_status: int | None = None) -> bool:
    """Whether anything in `exc` says the credential itself was rejected.

    `observed_status` is HttpStatusProbe's reading -- see that class for why
    the exception alone cannot answer this through the MCP SDK.
    """
    if observed_status in (401, 403):
        return True
    for candidate in _iter_causes(exc):
        if _http_status(candidate) in (401, 403):
            return True
        text = f"{type(candidate).__name__}: {candidate}".lower()
        if any(m in text for m in ("unauthorized", "revoked", "expired api key", "invalid api key")):
            return True
    return False


def _is_rate_limited(exc: BaseException, observed_status: int | None = None) -> bool:
    if observed_status == 429:
        return True
    for candidate in _iter_causes(exc):
        if isinstance(candidate, _ToolRateLimited):
            return True
        if _http_status(candidate) == 429:
            return True
        if "rate_limited" in str(candidate).lower():
            return True
    return False


def _is_retryable(exc: BaseException, observed_status: int | None = None) -> bool:
    """Retry transport-level failures (connection refused, timeout, 5xx) and
    rate limiting; never a rejected credential.

    A rejected API key or a schema-invalid trace fails identically on every
    attempt, so retrying it just multiplies the delay before the user sees
    the real error -- and retrying a rejected credential against a server
    that rate-limits auth failures per source address actively makes things
    worse.

    429 IS retried, unlike every other 4xx: it is the one client error that
    a later attempt can succeed at, and backing off is the behaviour the
    status code exists to request. Before this, a rate-limited bulk push
    failed permanently on the first 429 -- and reported it as a network
    outage.

    An actual tool-level rejection from the Hub (an MCP `result.is_error`
    response) never reaches this function: _call_tool raises that as
    HubConnectionError and re-raises immediately, bypassing retry entirely.

    Every check runs against the FLATTENED exception tree (`_iter_causes`),
    which is what makes any of this reachable at all through the MCP SDK's
    task-group wrapping -- see that function's docstring.
    """
    if any(isinstance(c, (HubConfigurationError, HubToolError)) for c in _iter_causes(exc)):
        return False
    if _is_auth_failure(exc, observed_status):
        return False
    if _is_rate_limited(exc, observed_status):
        return True
    if isinstance(observed_status, int) and observed_status >= 500:
        return True

    for candidate in _iter_causes(exc):
        status = _http_status(candidate)
        if isinstance(status, int) and status >= 500:
            return True
        # The type name is part of the evidence for a THIRD-PARTY exception
        # (httpx.ConnectError, anyio.EndOfStream) but never for one of this
        # module's own: "HubConnectionError" contains the substring
        # "connect", so including it here made every wrapper this module
        # raises -- a rejected URL scheme included -- look like a retryable
        # transport error to its own classifier.
        if isinstance(candidate, HubConnectionError):
            text = str(candidate).lower()
        else:
            text = f"{type(candidate).__name__}: {candidate}".lower()
        if any(
            marker in text
            # "502"/"503"/"504" are matched as bare substrings, deliberately,
            # and "500" deliberately is not: a message containing 502/503/504
            # is a gateway error in practice, whereas 500 collides with
            # ordinary content ("title exceeds 500 chars") that must never be
            # read as a retryable server error. _http_status handles the cases
            # where a status is stated as a status.
            for marker in (
                "timeout", "connect", "refused", "reset", "temporarily",
                "eof", "broken pipe", "502", "503", "504",
            )
        ):
            return True
    return False


def _tool_rate_limit(payload: dict) -> _ToolRateLimited | None:
    """A tool-layer rate-limit refusal, if that is what this payload is."""
    if not isinstance(payload, dict) or payload.get("error") != "rate_limited":
        return None
    retry_after = payload.get("retry_after")
    try:
        retry_after = float(retry_after) if retry_after is not None else None
    except (TypeError, ValueError):
        retry_after = None
    return _ToolRateLimited(str(payload.get("detail") or "rate_limited"), retry_after)


def _unwrap_result(name: str, result) -> dict:
    """Shape one MCP tool result into a plain dict, or raise."""
    if result.is_error:
        text = "; ".join(getattr(c, "text", str(c)) for c in result.content)
        # An error *from* the tool is an answer, not a transport failure --
        # surfaced immediately, never retried.
        raise HubToolError(f"Hub tool {name!r} returned an error: {text}")
    if result.structured_content is not None:
        return result.structured_content
    # Fallback: some transports only populate .content (text blocks of JSON).
    text = "".join(getattr(c, "text", "") for c in result.content)
    return json.loads(text) if text else {}


def _already_classified(exc: BaseException) -> HubConnectionError | None:
    """An error this module already turned into a specific, human-readable
    failure, found anywhere in `exc`'s tree.

    Needed because the MCP SDK runs its transport inside an anyio task
    group, and a task group re-wraps whatever escapes it -- including a
    HubAuthError this module raised on the way out of the handshake. Left
    unrecognised, that got classified a second time and re-wrapped a second
    time, producing a message with its own full text nested inside itself.
    """
    for candidate in _iter_causes(exc):
        if isinstance(candidate, HubConnectionError):
            return candidate
    return None


def _transport_failure(
    hub_url: str,
    attempts: int,
    exc: BaseException,
    observed_status: int | None = None,
    observed_retry_after: float | None = None,
) -> HubConnectionError:
    """Turn a transport failure into an error that names what went wrong.

    Every failure here used to read:

        could not reach the Hub at <url> after 3 attempt(s):
        unhandled errors in a TaskGroup (1 sub-exception)

    -- for a rejected API key, for a 429, and for a genuine outage alike.
    Two things were wrong with it beyond the missing diagnosis: the cause
    was the MCP SDK's task-group wrapper rather than the real exception
    (see `_iter_causes`), and "after 3 attempt(s)" was the CONFIGURED
    maximum printed unconditionally -- a failure that gave up after one
    attempt still claimed three. An operator reading it went looking for a
    network problem that did not exist.
    """
    already = _already_classified(exc)
    if already is not None:
        return already
    cause = root_cause(exc)
    detail = f"{type(cause).__name__}: {cause}"
    plural = "attempt" if attempts == 1 else "attempts"
    if _is_auth_failure(exc, observed_status):
        return HubAuthError(
            f"the Hub at {hub_url} rejected the API key (invalid, revoked, or expired). "
            f"Check COMMONTRACE_HUB_API_KEY, or issue a new key with "
            f"`python -m hub.manage issue-key <org_id>`. [{detail}]"
        )
    if _is_rate_limited(exc, observed_status):
        retry_after = _retry_after_seconds(exc) or observed_retry_after
        if any(isinstance(c, _ToolRateLimited) for c in _iter_causes(exc)):
            when = f" Retry after {retry_after:.0f}s." if retry_after else ""
            return HubRateLimited(
                f"the Hub at {hub_url} refused this write for rate limiting and was still "
                f"refusing after {attempts} {plural}.{when} This is the Hub's per-org WRITE "
                f"limit (HUB_RATE_LIMIT_PER_MINUTE, 20/min by default), not a network "
                f"problem -- push a smaller batch, retry later, or raise that limit. "
                f"[{detail}]",
                retry_after=retry_after,
            )
        when = f" Retry after {retry_after:.0f}s." if retry_after else ""
        return HubRateLimited(
            f"the Hub at {hub_url} is rate limiting this client (HTTP 429) and was still "
            f"rate limiting after {attempts} {plural}.{when} Reduce concurrency "
            f"(`--concurrency`), retry later, or raise the Hub's "
            f"HUB_READ_RATE_LIMIT_PER_MINUTE / HUB_RATE_LIMIT_PER_MINUTE. [{detail}]",
            retry_after=retry_after,
        )
    return HubConnectionError(
        f"could not reach the Hub at {hub_url} after {attempts} {plural}: {detail}"
    )


class _RateLimitGate:
    """Shared brake for one batch: when the Hub says 429, EVERY worker in
    the batch waits, not just the one that was refused.

    Per-call backoff alone does not fix a stampede. With N workers pushing
    concurrently, one worker sleeping on its own 429 leaves the other N-1
    still firing into a limiter that has already said no, so the bucket
    never refills and each worker independently burns its retry budget.
    Measured on a 46-trace `sync --push-traces` against a default-configured
    Hub: 114 rejected requests and a failed run, with per-call backoff
    already in place.

    One shared "paused until" instant makes the whole batch back off
    together, which is what actually lets the bucket refill. Workers that
    arrive during a pause wait it out rather than each adding a rejection.
    """

    def __init__(self, on_first_pause: Callable[[float], None] | None = None) -> None:
        self._resume_at = 0.0
        self._next_slot = 0.0
        self._min_interval = 0.0
        self._lock = asyncio.Lock()
        # A paced push is a CORRECT slow push, but an unexplained one reads
        # as a hang: a 1,000-trace sync legitimately takes minutes against a
        # Hub's write limit, and the CLI printed nothing at all until it
        # finished. Announced once, not per refusal, so the notice is
        # information rather than a scroll of repeated warnings.
        self._on_first_pause = on_first_pause
        self._announced = False

    async def wait(self) -> None:
        """Acquire this call's turn: serve any batch-wide pause, then take
        the next paced slot.

        A pause alone is not enough, and measuring showed exactly why: when
        it expires, every worker in the batch resumes in the same instant
        and re-exhausts the bucket immediately, so the batch oscillates
        between stampede and pause and never converges. Handing out slots
        `_min_interval` apart -- one shared schedule across all workers --
        is what actually spaces the requests out.
        """
        while True:
            async with self._lock:
                now = asyncio.get_running_loop().time()
                if now >= self._resume_at:
                    slot = max(now, self._next_slot)
                    self._next_slot = slot + self._min_interval
                    delay = slot - now
                    break
                delay = self._resume_at - now
            await asyncio.sleep(min(delay, RETRY_MAX_DELAY_SECONDS))
        if delay > 0:
            await asyncio.sleep(delay)

    def pause(self, seconds: float) -> None:
        """Called on a 429: hold the whole batch, and slow the paced rate.

        Additive-increase/multiplicative-decrease, the standard shape for a
        client that must discover a limit it was never told. The client
        cannot know the Hub's configured rate (it is per-deployment, and
        per-org), so it learns it: widen the spacing on every refusal,
        narrow it on every success, and the batch settles at whatever rate
        the server will actually sustain instead of guessing.
        """
        loop_time = asyncio.get_running_loop().time()
        resume_at = loop_time + min(max(seconds, 0.0), RETRY_MAX_DELAY_SECONDS)
        # Never shorten a pause another worker already set: the longest
        # observed Retry-After is the one the server actually needs.
        self._resume_at = max(self._resume_at, resume_at)
        self._min_interval = min(
            max(self._min_interval * 2.0, _PACE_MIN_INTERVAL_SECONDS), _PACE_MAX_INTERVAL_SECONDS
        )
        # Slots already handed out are in the past relative to the new
        # pause; rebase so spacing resumes from when the pause ends.
        self._next_slot = max(self._next_slot, self._resume_at)
        if not self._announced:
            self._announced = True
            if self._on_first_pause is not None:
                self._on_first_pause(seconds)

    def succeeded(self) -> None:
        """Called on every successful call: relax the spacing back toward
        zero, so a batch that hit one transient limit does not stay
        throttled for its whole remaining run."""
        if self._min_interval:
            self._min_interval *= _PACE_DECAY
            if self._min_interval < _PACE_MIN_INTERVAL_SECONDS / 4:
                self._min_interval = 0.0


class HubSession:
    """One live MCP session, reusable for many tool calls.

    WHY THIS EXISTS. Every `_call_tool` used to stand up a whole MCP
    session of its own -- connect, `initialize`, the initialized
    notification, the actual `tools/call`, then terminate. Measured against
    a live Hub, that is **5 HTTP requests for one logical tool call**, and
    the Hub's rate limiter counts HTTP requests, not tool calls.

    The consequence was not theoretical. `commontrace sync --push-traces`
    on a 46-trace store issued ~230 requests against a Hub whose shipped
    defaults allow a burst of 60 and 300/minute: 62 of 100 logged requests
    came back 429 and **not one trace was pushed**. The documented primary
    workflow could not complete against a default-configured Hub for any
    store bigger than about a dozen traces -- and, because of the retry
    defects fixed above, it reported the failure as "could not reach the
    Hub".

    Reusing one session across a batch drops the cost to roughly two
    requests per call plus a one-time handshake, which is what makes a bulk
    push fit inside the same limits. `_PUSH_CONCURRENCY` still bounds how
    many calls are in flight at once.

    Not thread-safe, and deliberately not reconnect-on-failure: a session
    that dies mid-batch surfaces per-call errors, which every batch caller
    here already records per file rather than aborting on.
    """

    def __init__(
        self,
        hub_url: str,
        api_key: str,
        timeout_seconds: float,
        max_attempts: int,
        probe: "HttpStatusProbe | None" = None,
        gate: "_RateLimitGate | None" = None,
    ):
        self._hub_url = hub_url
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._probe = probe
        self._gate = gate if gate is not None else _RateLimitGate()
        self._session = None

    async def call(self, name: str, arguments: dict[str, Any]) -> dict:
        """One tool call, with retries.

        Rate limiting gets its OWN, larger attempt budget
        (RATE_LIMIT_MAX_ATTEMPTS), counted separately from transport
        failures. The two deserve different budgets: a connection that
        keeps refusing is probably not coming back inside this run, while a
        429 is the server explicitly saying "yes, but later" -- and during
        a bulk push it is the EXPECTED steady state once the burst
        allowance is spent, not an error. Sharing one budget of 3 meant a
        46-trace push failed on traces that only ever needed to wait their
        turn. Each rate-limited attempt is also cheap and well-behaved:
        `_RateLimitGate` has already parked the whole batch until the
        server's own Retry-After elapses, so these are timed waits, not
        extra load.
        """
        last_exc: Exception | None = None
        attempts = 0
        transport_attempts = 0
        rate_limit_attempts = 0
        status = retry_after = None
        while True:
            attempts += 1
            # Reset per attempt so a 429 recorded on an earlier call in this
            # same session can never be blamed for a later, unrelated failure.
            if self._probe is not None:
                self._probe.reset()
            # Respect a pause any worker in this batch is already serving.
            await self._gate.wait()
            try:
                result = _unwrap_result(name, await self._session.call_tool(name, arguments))
                refusal = _tool_rate_limit(result)
                if refusal is not None:
                    raise refusal
            except (HubToolError, HubAuthError):
                raise
            except Exception as exc:  # noqa: BLE001 - transport errors aren't one exception type
                last_exc = exc
                status = self._probe.status if self._probe is not None else None
                retry_after = self._probe.retry_after if self._probe is not None else None
                rate_limited = _is_rate_limited(exc, status)
                if rate_limited:
                    rate_limit_attempts += 1
                    delay = _backoff_delay(rate_limit_attempts, exc, retry_after)
                    # Brake the whole batch, not just this call.
                    self._gate.pause(delay)
                    budget_left = rate_limit_attempts < RATE_LIMIT_MAX_ATTEMPTS
                else:
                    transport_attempts += 1
                    delay = _backoff_delay(transport_attempts, exc, retry_after)
                    budget_left = transport_attempts < self._max_attempts
                if not budget_left or not _is_retryable(exc, status):
                    break
                await asyncio.sleep(delay)
            else:
                self._gate.succeeded()
                return result
        raise _transport_failure(
            self._hub_url, attempts, last_exc, status, retry_after
        ) from last_exc


def _backoff_delay(attempt: int, exc: BaseException, observed_retry_after: float | None = None) -> float:
    """Seconds to wait before retry `attempt` + 1.

    A server-sent `Retry-After` wins over local exponential backoff when
    the Hub sent one: the server knows when its own bucket refills, and
    guessing shorter just burns another request against a limiter that is
    already saying no. Capped either way (RETRY_MAX_DELAY_SECONDS) so a
    hostile or misconfigured value cannot hang the CLI.
    """
    retry_after = _retry_after_seconds(exc)
    if retry_after is None:
        retry_after = observed_retry_after
    if retry_after is not None:
        return min(max(retry_after, 0.0), RETRY_MAX_DELAY_SECONDS)
    return min(RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)), RETRY_MAX_DELAY_SECONDS)


@contextlib.asynccontextmanager
async def open_hub_session(
    hub_url: str,
    api_key: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    on_first_pause: Callable[[float], None] | None = None,
):
    """Hold one MCP session open for a batch of calls. See HubSession."""
    transport_ctx, ClientSession, http_client, probe = await _open_session(
        hub_url, api_key, timeout_seconds
    )
    hub_session = HubSession(
        hub_url, api_key, timeout_seconds, max_attempts, probe,
        gate=_RateLimitGate(on_first_pause),
    )
    try:
        async with transport_ctx as (read, write), ClientSession(read, write) as session:
            try:
                await session.initialize()
            except Exception as exc:  # noqa: BLE001 - the handshake fails the same ways a call does
                # The handshake is where a rejected API key actually shows
                # up: auth runs in middleware ahead of MCP dispatch, so the
                # 401 lands on `initialize`, not on any tool call.
                raise _transport_failure(
                    hub_url, 1, exc, probe.status, probe.retry_after
                ) from exc
            hub_session._session = session
            yield hub_session
    finally:
        # httpx.AsyncClient owns a connection pool / open sockets that
        # streamable_http_client wraps but does not take ownership of;
        # without this they leak until the process hits "Too many open
        # files".
        await http_client.aclose()


async def _call_tool(
    hub_url: str,
    api_key: str,
    name: str,
    arguments: dict[str, Any],
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    session: "HubSession | _LazyHubSession | None" = None,
) -> dict:
    """One tool call. Reuses `session` when a batch caller supplies one,
    otherwise opens a session for this single call (the behaviour every
    one-shot caller in this module wants).

    The retry loop lives in HubSession.call; the whole-session retry here
    covers the case where the failure was the handshake itself, which a
    per-call retry inside a dead session could never recover from.
    """
    if session is not None:
        if isinstance(session, _LazyHubSession):
            session = await session.get()
        return await session.call(name, arguments)

    last_exc: Exception | None = None
    attempts = 0
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        try:
            async with open_hub_session(hub_url, api_key, timeout_seconds, max_attempts=1) as one_shot:
                return await one_shot.call(name, arguments)
        except (HubConfigurationError, HubToolError, HubAuthError, HubClientUnavailable):
            raise
        except HubConnectionError as exc:
            # A tool-level error ("Hub tool X returned an error: ...") is an
            # answer; only a transport failure is worth another attempt.
            if not _is_retryable(exc):
                raise
            last_exc = exc
            if attempt >= max_attempts:
                break
            await asyncio.sleep(_backoff_delay(attempt, exc))
        except Exception as exc:  # noqa: BLE001 - transport errors aren't one exception type
            last_exc = exc
            # An anyio task group can re-wrap a HubAuthError/HubToolError
            # this module already raised; unwrap it before deciding, so a
            # rejected key is still never retried once it has been
            # correctly diagnosed one layer down.
            classified = _already_classified(exc)
            if isinstance(classified, (HubConfigurationError, HubAuthError, HubToolError)):
                raise classified from exc
            # A HubRateLimited that reaches here has ALREADY been through
            # HubSession.call's own rate-limit budget (RATE_LIMIT_MAX_ATTEMPTS,
            # each attempt paced against the server's Retry-After). Retrying
            # it in this outer loop as well would multiply the two budgets
            # together -- up to 30 attempts, and minutes of wall time, for
            # one single-shot call -- when the inner loop has already
            # established that the server is still saying no.
            if isinstance(classified, HubRateLimited):
                raise classified from exc
            if attempt >= max_attempts or not _is_retryable(exc):
                break
            await asyncio.sleep(_backoff_delay(attempt, exc))

    raise _transport_failure(hub_url, attempts, last_exc) from last_exc


def _push_fingerprint(title: str, context_text: str, solution_text: str, tags: list[str]) -> str:
    """Fingerprint of exactly the fields amend_trace can change (title/
    context_text/solution_text/tags -- NOT agent_type, which amend_trace has
    no parameter for and always carries forward from the original
    unchanged, hub/crud.py:amend_trace). Order-independent over tags for the
    same reason hub/crud.py:_contribute_request_hash is: a client may
    reasonably reorder an unordered set between edits without that counting
    as a change worth re-pushing."""
    parts = [title, context_text, solution_text, "\x1f".join(sorted(tags))]
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()


def _amend_idempotency_key(slug: str, fingerprint: str) -> str:
    """Belt and braces alongside this same push's hub_pushed_fingerprint
    guard, for the identical reason the contribute_trace call below passes
    `idempotency_key=f"lesson:{slug}"`: _call_tool retries a transport-level
    failure (timeout, 5xx, connection reset) up to DEFAULT_MAX_ATTEMPTS
    times, and this client cannot distinguish "the amend never arrived"
    from "it landed and the reply was lost" -- without a key, that retry
    calls amend_trace again against the SAME still-unmutated trace_id and
    forks the supersession chain instead of extending it
    (hub/crud.py:amend_trace's own docstring; hub/tests/test_concurrency_audit.py
    TestAmendTraceIdempotency reproduced exactly this).

    Unlike contribute_trace's key, this can't be `f"lesson:{slug}"` alone:
    a lesson can be legitimately amended many times over its life as its
    content actually changes, and each of those is a genuinely different
    logical write that must NOT collide -- reusing one fixed key across
    them would make every edit after the first raise IdempotencyKeyConflict
    against the previous one's hash. Keying on (slug, fingerprint) together
    keeps retries of THIS push (same content, same fingerprint) idempotent
    while still minting a fresh key the moment the content actually
    changes. Hashed rather than concatenated so the result has a fixed,
    small length regardless of how long `slug` is -- Trace.idempotency_key
    is String(128), and a filename-derived slug is not size-bounded the way
    this key needs to be.
    """
    return "lesson-amend:" + hashlib.sha256(f"{slug}\x1e{fingerprint}".encode("utf-8")).hexdigest()


def _write_hub_push_fields(path: str, hub_trace_id: str, fingerprint: str) -> None:
    """Persist the Hub id/fingerprint a push just learned back onto the
    local file. Re-reads under the lock rather than trusting a snapshot
    captured before the (slow, awaited) Hub call that produced
    `hub_trace_id`: another process could have changed a different field on
    this same file (e.g. `lesson approve`/`reject` flipping `status`) while
    that call was in flight, and writing back the pre-call snapshot would
    silently discard that change -- the exact lost-update
    frontmatter.locked() exists to prevent, see its docstring.

    A plain function, not a coroutine: frontmatter.locked() takes a
    blocking OS-level fcntl.flock, and every caller below runs this via
    asyncio.to_thread specifically so that lock contention (e.g. a
    concurrent `lesson approve` on the same file) blocks only the calling
    task's thread, not the whole event loop -- and therefore not every
    other push concurrently in flight in the same bounded-concurrency
    batch (see _PUSH_CONCURRENCY).
    """
    with frontmatter.locked(path):
        fm, body = frontmatter.read(path)
        fm["hub_trace_id"] = hub_trace_id
        fm["hub_pushed_fingerprint"] = fingerprint
        frontmatter.write(path, fm, body)


async def _gather_bounded(
    paths_iter: Iterable[str],
    fn: Callable[[str], Awaitable[_T]],
    concurrency: int = _PUSH_CONCURRENCY,
) -> list[_T]:
    """Run `fn(path)` for every `path` in `paths_iter`, at most
    `concurrency` at once. Shared by push_active_lessons and
    push_captured_traces (see _PUSH_CONCURRENCY's comment for why bounded
    rather than unbounded concurrency)."""
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _bounded(path: str) -> _T:
        async with semaphore:
            return await fn(path)

    return await asyncio.gather(*(_bounded(p) for p in paths_iter))


class _LazyHubSession:
    """The batch's shared MCP session, opened on first actual use.

    Lazy rather than eager for two reasons. The honest one: a batch in which
    every file is already up to date makes no Hub calls at all, and opening
    (and handshaking, and tearing down) a session to discover that is pure
    waste -- `commontrace sync --push` on an unchanged store is the common
    case, not the rare one. The second: establishing the connection at the
    moment of first use keeps the seam at `_call_tool` where every caller,
    and every test, already expects it.

    The double-checked lock matters: `_gather_bounded` runs up to
    `_PUSH_CONCURRENCY` workers, and without it the first several would each
    open a session of their own -- reintroducing exactly the per-call
    session cost this whole change exists to remove.
    """

    def __init__(self, stack, hub_url: str, api_key: str, on_first_pause=None):
        self._stack = stack
        self._hub_url = hub_url
        self._api_key = api_key
        self._on_first_pause = on_first_pause
        self._session: HubSession | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> HubSession:
        if self._session is None:
            async with self._lock:
                if self._session is None:
                    self._session = await self._stack.enter_async_context(
                        open_hub_session(
                            self._hub_url, self._api_key, on_first_pause=self._on_first_pause
                        )
                    )
        return self._session


async def _push_batch(
    hub_url: str,
    api_key: str,
    paths_iter: Iterable[str],
    make_push_one: Callable[["_LazyHubSession | None"], Callable[[str], Awaitable[_T]]],
    concurrency: int = _PUSH_CONCURRENCY,
) -> list[_T]:
    """Push a whole batch over ONE MCP session.

    See HubSession for the measurement behind this: a session per call cost
    5 HTTP requests per logical tool call, which put `sync --push-traces`
    over the Hub's shipped rate limits on any store past about a dozen
    traces and pushed nothing at all.

    If the session cannot be established (Hub down, key rejected), that is
    not a per-file problem -- every file would report the identical error --
    so it propagates to the caller, which is what `commontrace sync`
    already renders as a single "sync failed" line rather than one repeated
    error per file.
    """
    files = list(paths_iter)
    if not files:
        return []

    def _announce(seconds: float) -> None:
        print(
            f"[commontrace] the Hub is rate limiting this push; pacing the remaining "
            f"{len(files)} file(s) to match (first wait ~{max(1, round(seconds))}s). "
            f"This is expected for a large batch -- it will finish, just not instantly.",
            file=sys.stderr,
        )

    async with contextlib.AsyncExitStack() as stack:
        session = _LazyHubSession(stack, hub_url, api_key, on_first_pause=_announce)
        return await _gather_bounded(files, make_push_one(session), concurrency)


async def push_active_lessons(
    hub_url: str, api_key: str, root: str, concurrency: int = _PUSH_CONCURRENCY
) -> list[PushResult]:
    """Push every `status: active` lesson to the Hub via contribute_trace,
    recording the returned id back into the lesson's `hub_trace_id`
    frontmatter field. Mapping matches the one previously documented as
    instructional text in this module's predecessor
    (commontrace/commands/sync_cmd.py):
        contribute_trace(title=lesson.description, context_text=applies_when,
                          solution_text="Rule + How to apply", tags=lesson.tags)

    A lesson already on the Hub (`hub_trace_id` set) is not re-contributed --
    contribute_trace MINTS A NEW TRACE on every call, so without that guard
    each `sync --push` would accumulate one duplicate per lesson per run.
    Instead its current content is fingerprinted (`_push_fingerprint`) and
    compared against the fingerprint recorded at the last successful push
    (`hub_pushed_fingerprint`): unchanged, it's skipped; changed -- a local
    edit to the rule/description/applies-when/tags since the last push --
    it's propagated via amend_trace, so the Hub copy stops silently
    diverging from the local one the moment anyone edits it.
    """
    async def _push_one(path: str, session: "_LazyHubSession | None" = None) -> PushResult | None:
        try:
            fm, body = frontmatter.read(path)
        except Exception as exc:  # noqa: BLE001 - one malformed local file (hand-edited YAML
            # broken, a partial write that somehow survived frontmatter.write's atomic
            # rename) must not abort every OTHER lesson's push. Reproduced: without this,
            # one bad file made the whole `sync --push` raise before pushing anything,
            # including lessons already read and ready to go earlier in the iteration --
            # a single corrupted file silently blocked an entire fleet's lessons from ever
            # reaching the Hub. The slug falls back to the filename since a failed read
            # never got as far as fm.get("name").
            return PushResult(
                slug=os.path.splitext(os.path.basename(path))[0],
                hub_trace_id=None,
                error=f"could not read this file: {type(exc).__name__}: {exc}",
            )
        if fm.get("status") != "active":
            return None
        slug = fm.get("name", os.path.splitext(os.path.basename(path))[0])
        # An active lesson that is still scaffolding must not be published.
        # `lesson approve` now refuses to activate one (and warns loudly on
        # --force), but a lesson can also reach status: active by hand, and
        # the Hub is the one place the mistake stops being local: from
        # there `search_traces` serves it to every agent in the fleet.
        # Reported as an error rather than skipped silently -- the file is
        # something the operator needs to fix, not something to hide.
        unfilled = templates.unfilled_placeholders(fm, body)
        if unfilled:
            return PushResult(
                slug=slug,
                hub_trace_id=None,
                error=(
                    f"not pushed: this active lesson still contains unedited "
                    f"scaffolding in {', '.join(unfilled)}. Fill it in, or set it "
                    f"back to status: review."
                ),
            )

        sections = _lesson_sections(body)
        solution_text = "\n\n".join(
            part for part in (sections.get("rule", ""), sections.get("how to apply", "")) if part
        ) or "(no Rule/How to apply section found in the lesson body)"
        title = fm.get("description") or slug
        context_text = fm.get("applies_when") or ""
        tags = list(fm.get("tags") or []) if isinstance(fm.get("tags"), list) else []
        fingerprint = _push_fingerprint(title, context_text, solution_text, tags)

        existing_hub_id = fm.get("hub_trace_id")
        if existing_hub_id:
            if fm.get("hub_pushed_fingerprint") == fingerprint:
                return PushResult(slug=slug, hub_trace_id=str(existing_hub_id), skipped=True)
            try:
                result = await _call_tool(
                    hub_url,
                    api_key,
                    "amend_trace",
                    {
                        "id": str(existing_hub_id),
                        "title": title,
                        "context_text": context_text,
                        "solution_text": solution_text,
                        "tags": tags,
                        # See _amend_idempotency_key's docstring: without
                        # this, _call_tool's own retry-on-timeout/5xx can
                        # fork the supersession chain the same way an
                        # unkeyed contribute_trace retry used to duplicate
                        # a trace.
                        "idempotency_key": _amend_idempotency_key(slug, fingerprint),
                    },
                    session=session,
                )
            except (HubClientUnavailable, HubConnectionError) as exc:
                return PushResult(slug=slug, hub_trace_id=str(existing_hub_id), error=str(exc))
            if result.get("error"):
                return PushResult(slug=slug, hub_trace_id=str(existing_hub_id), error=result["error"])
            # amend_trace supersedes rather than mutating in place
            # (hub/crud.py:amend_trace), so the id returned here is a NEW
            # trace and hub_trace_id must move forward to it -- the old id
            # is now the head of a chain, not the trace to amend next time.
            amended_id = result.get("id")
            try:
                await asyncio.to_thread(_write_hub_push_fields, path, amended_id, fingerprint)
            except Exception as exc:  # noqa: BLE001 - the Hub write already succeeded; a
                # failure recording it locally (disk full, permission error, the file
                # vanishing under us) must become THIS file's own result, not an
                # exception that escapes _push_one. asyncio.gather in _gather_bounded
                # has no return_exceptions=True: an uncaught exception here would abort
                # every OTHER concurrently in-flight push in this same batch, discarding
                # results already computed for files that already succeeded -- exactly
                # the "one bad file blocks the whole run" failure mode the read-error
                # guard above already exists to prevent, reintroduced on the write side.
                return PushResult(
                    slug=slug,
                    hub_trace_id=amended_id,
                    error=f"pushed to the Hub but failed to record locally: {type(exc).__name__}: {exc}",
                )
            return PushResult(slug=slug, hub_trace_id=amended_id, quarantined=result.get("quarantined", False))

        try:
            result = await _call_tool(
                hub_url,
                api_key,
                "contribute_trace",
                {
                    "title": title,
                    "context_text": context_text,
                    "solution_text": solution_text,
                    "tags": tags,
                    "agent_type": fm.get("agent_type") or "",
                    # Carries WHICH agent produced this, not just what kind.
                    # A Hub plan's agent limit is enforced against distinct
                    # agent_ids, so a fleet that never sends one has its whole
                    # population collapse into a single 'unattributed' agent
                    # and reads as 1 agent however many really run.
                    "agent_id": fm.get("agent_id") or "",
                    # Belt and braces alongside the hub_trace_id guard
                    # above. That guard stops a SECOND run from
                    # re-contributing; this stops THIS run from
                    # double-writing when the response is lost and the
                    # transport retries -- the client cannot distinguish
                    # "never arrived" from "arrived, reply dropped". Keyed
                    # on the lesson slug so a retry of the same lesson
                    # collides deliberately, and the Hub returns the
                    # original trace instead of minting another.
                    "idempotency_key": f"lesson:{slug}",
                },
                session=session,
            )
        except (HubClientUnavailable, HubConnectionError) as exc:
            return PushResult(slug=slug, hub_trace_id=None, error=str(exc))

        if result.get("error"):
            return PushResult(slug=slug, hub_trace_id=None, error=result["error"])

        hub_trace_id = result.get("id")
        try:
            await asyncio.to_thread(_write_hub_push_fields, path, hub_trace_id, fingerprint)
        except Exception as exc:  # noqa: BLE001 - see the amend branch above for why this
            # must return an error result rather than propagate.
            return PushResult(
                slug=slug,
                hub_trace_id=hub_trace_id,
                error=f"pushed to the Hub but failed to record locally: {type(exc).__name__}: {exc}",
            )
        return PushResult(slug=slug, hub_trace_id=hub_trace_id, quarantined=result.get("quarantined", False))

    # Bounded concurrency (see _PUSH_CONCURRENCY / _gather_bounded): each
    # file is pushed independently, so N files no longer means N sequential
    # round trips. asyncio.gather preserves input order in its results
    # regardless of completion order, so this is not just faster but
    # observably identical in ordering to the old sequential loop.
    outcomes = await _push_batch(
        hub_url,
        api_key,
        _iter_active_lesson_paths(root),
        lambda session: (lambda path: _push_one(path, session)),
        concurrency=concurrency,
    )
    return [r for r in outcomes if r is not None]


def _trace_push_fingerprint(
    title: str, context_text: str, solution_text: str, tags: list[str], outcome: dict
) -> str:
    """Like `_push_fingerprint`, but for a captured trace rather than a
    lesson -- and including `outcome`, which a lesson never has but a
    trace's whole reason for existing here is to carry (see
    push_captured_traces's docstring). Re-capturing an occasion to attach
    `--resolved`/`--tokens-used`/etc. after the fact (capture_cmd.py's own
    documented pattern) changes ONLY the outcome dict, and that must count
    as a change worth re-pushing exactly as much as an edited title does --
    omitting it here would make that specific, expected edit silently
    never propagate."""
    parts = [
        title, context_text, solution_text, "\x1f".join(sorted(tags)),
        json.dumps(outcome or {}, sort_keys=True, ensure_ascii=False),
    ]
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()


def _trace_amend_idempotency_key(local_id: str, fingerprint: str) -> str:
    """Same construction and the same reasoning as `_amend_idempotency_key`
    (keyed on (identifier, fingerprint), hashed for a bounded length), but
    its own distinct prefix rather than a shared function: `local_id` here
    is a captured trace's own id (trace_io/templates.trace_frontmatter),
    drawn from a different namespace than a lesson's `name` slug, and nothing
    guarantees the two could never coincide for two files that otherwise
    fingerprint identically. `Trace.idempotency_key` is unique per org
    across every trace regardless of which push path produced it, so a
    shared prefix is a real (if unlikely) cross-push collision this avoids
    for the cost of one extra function."""
    return "trace-amend:" + hashlib.sha256(f"{local_id}\x1e{fingerprint}".encode("utf-8")).hexdigest()


async def push_captured_traces(
    hub_url: str, api_key: str, root: str, concurrency: int = _PUSH_CONCURRENCY
) -> list[PushResult]:
    """Push every locally captured trace (`commontrace capture`) to the Hub
    via contribute_trace/amend_trace, INCLUDING outcome data -- the bridge
    push_active_lessons does not provide, because lessons and traces are
    different local stores (paths.lessons_dir vs paths.traces_dir: curated,
    reusable knowledge vs. a raw incident record) and outcome data
    (--resolved/--escalated/--tokens-used/..., hub/outcomes.py) only ever
    lives on a trace, never a lesson.

    Without this function, hub/crud.py:contribute_trace's `outcome`
    parameter -- added because fleet_outcomes could not otherwise ever
    receive real data from any customer -- was reachable only by an
    agent's own direct MCP tool call. The standard, documented workflow
    (`commontrace capture --resolved ...` then `commontrace sync`) had no
    way to reach it at all: capture_cmd.py writes outcome data to a local
    file, and nothing ever read that file and sent it anywhere.

    Mirrors push_active_lessons's design exactly, fingerprint-and-amend
    included: a trace not yet on the Hub (`hub_trace_id` unset) is
    contributed; one already there is fingerprinted (title/context/
    solution/tags/outcome) and left alone if unchanged, or propagated via
    amend_trace -- whose MERGE semantics for `outcome`
    (hub/crud.py:amend_trace) are exactly what capture_cmd.py's own
    documented "recapture under the same --occasion-id to attach an
    outcome once a task concludes" pattern needs: the second push must add
    `resolved` without erasing whatever the first push already attached.
    """
    async def _push_one(path: str, session: "_LazyHubSession | None" = None) -> PushResult:
        try:
            instance, _body = trace_io.read(path)
        except Exception as exc:  # noqa: BLE001 - see push_active_lessons's identical
            # guard: one malformed local file must not abort every other trace's push.
            # Reproduced live: a single corrupted trace file made this whole function
            # raise before pushing anything, including traces already read earlier in
            # the iteration -- for --push-traces specifically that means a corrupted
            # file silently blocks EVERY OTHER trace's outcome data from ever reaching
            # the Hub, not just its own.
            return PushResult(
                slug=os.path.splitext(os.path.basename(path))[0],
                hub_trace_id=None,
                error=f"could not read this file: {type(exc).__name__}: {exc}",
            )
        local_id = str(instance.get("id") or "")
        slug = local_id or os.path.splitext(os.path.basename(path))[0]
        title = str(instance.get("title") or "")
        context_text = str(instance.get("context_text") or "")
        solution_text = str(instance.get("solution_text") or "")
        tags = list(instance.get("tags") or []) if isinstance(instance.get("tags"), list) else []
        agent_type = str(instance.get("agent_type") or "")
        agent_id = str(instance.get("agent_id") or "")
        outcome = instance.get("outcome") if isinstance(instance.get("outcome"), dict) else {}
        fingerprint = _trace_push_fingerprint(title, context_text, solution_text, tags, outcome)

        existing_hub_id = instance.get("hub_trace_id")
        if existing_hub_id:
            if instance.get("hub_pushed_fingerprint") == fingerprint:
                return PushResult(slug=slug, hub_trace_id=str(existing_hub_id), skipped=True)
            try:
                result = await _call_tool(
                    hub_url,
                    api_key,
                    "amend_trace",
                    {
                        "id": str(existing_hub_id),
                        "title": title,
                        "context_text": context_text,
                        "solution_text": solution_text,
                        "tags": tags,
                        "outcome": outcome,
                        # See _trace_amend_idempotency_key's docstring:
                        # without this, _call_tool's own retry-on-timeout/5xx
                        # can fork the supersession chain exactly like an
                        # unkeyed push_active_lessons amend used to.
                        "idempotency_key": _trace_amend_idempotency_key(slug, fingerprint),
                    },
                    session=session,
                )
            except (HubClientUnavailable, HubConnectionError) as exc:
                return PushResult(slug=slug, hub_trace_id=str(existing_hub_id), error=str(exc))
            if result.get("error"):
                return PushResult(slug=slug, hub_trace_id=str(existing_hub_id), error=result["error"])
            amended_id = result.get("id")
            # _write_hub_push_fields re-reads under the lock rather than
            # reusing `instance`/`fm` captured before the (slow, awaited)
            # Hub call -- see push_active_lessons's identical comment on
            # this exact race.
            try:
                await asyncio.to_thread(_write_hub_push_fields, path, amended_id, fingerprint)
            except Exception as exc:  # noqa: BLE001 - see push_active_lessons's identical
                # guard: the Hub write already succeeded, so a local recording failure
                # must become this file's own result, not an exception that -- via
                # asyncio.gather in _gather_bounded, which has no return_exceptions=True
                # -- would abort every other concurrently in-flight push in this batch.
                return PushResult(
                    slug=slug,
                    hub_trace_id=amended_id,
                    error=f"pushed to the Hub but failed to record locally: {type(exc).__name__}: {exc}",
                )
            return PushResult(slug=slug, hub_trace_id=amended_id, quarantined=result.get("quarantined", False))

        try:
            result = await _call_tool(
                hub_url,
                api_key,
                "contribute_trace",
                {
                    "title": title,
                    "context_text": context_text,
                    "solution_text": solution_text,
                    "tags": tags,
                    "agent_type": agent_type,
                    "agent_id": agent_id,
                    "outcome": outcome,
                    # Keyed on this trace's own local id, like
                    # push_active_lessons's f"lesson:{slug}": a retry of
                    # THIS push (never-yet-contributed, so at most one
                    # logical write is possible per local id in steady
                    # state) must collide deliberately with itself.
                    "idempotency_key": f"trace:{slug}",
                },
                session=session,
            )
        except (HubClientUnavailable, HubConnectionError) as exc:
            return PushResult(slug=slug, hub_trace_id=None, error=str(exc))
        if result.get("error"):
            return PushResult(slug=slug, hub_trace_id=None, error=result["error"])

        new_id = result.get("id")
        try:
            await asyncio.to_thread(_write_hub_push_fields, path, new_id, fingerprint)
        except Exception as exc:  # noqa: BLE001 - see the amend branch above for why this
            # must return an error result rather than propagate.
            return PushResult(
                slug=slug,
                hub_trace_id=new_id,
                error=f"pushed to the Hub but failed to record locally: {type(exc).__name__}: {exc}",
            )
        return PushResult(slug=slug, hub_trace_id=new_id, quarantined=result.get("quarantined", False))

    # Bounded concurrency (see _PUSH_CONCURRENCY / _gather_bounded), over
    # ONE shared MCP session (see _push_batch): same reasoning and same
    # pattern as push_active_lessons above.
    return await _push_batch(
        hub_url,
        api_key,
        _iter_captured_trace_paths(root),
        lambda session: (lambda path: _push_one(path, session)),
        concurrency=concurrency,
    )


async def commons_overlap(
    hub_url: str,
    api_key: str,
    failures: list[dict],
    threshold: float | None = None,
    include_matches: bool = True,
) -> dict:
    """Ask the Hub: of these recurring failures, how many does the
    CommonTrace Knowledge Base already solve?

    `failures` carries MinHash signatures only -- generated locally by
    `commontrace commons sign`. No failure text is sent. See hub/commons.py
    for what the Knowledge Base is and what comes back.
    """
    arguments: dict[str, Any] = {"failures": failures, "include_matches": include_matches}
    if threshold is not None:
        arguments["threshold"] = threshold
    response = await _call_tool(hub_url, api_key, "commons_overlap", arguments)
    if response.get("error"):
        raise HubConnectionError(f"commons_overlap failed: {response['error']}: {response.get('detail', '')}")
    return response


async def commons_search(
    hub_url: str,
    api_key: str,
    query_signature: list[int],
    limit: int | None = None,
    agent_type: str = "",
) -> dict:
    """Ask the Hub what it already knows about ONE failure, ranked.

    Carries a MinHash signature only, exactly like commons_overlap -- the
    failure text stays on this machine. What comes back are ranked
    CANDIDATES with their solutions, not a coverage figure; see
    hub/crud.py:commons_search for why the two are separate tools.
    """
    arguments: dict[str, Any] = {"query_signature": query_signature}
    if limit is not None:
        arguments["limit"] = limit
    if agent_type:
        arguments["agent_type"] = agent_type
    response = await _call_tool(hub_url, api_key, "commons_search", arguments)
    if response.get("error"):
        raise HubConnectionError(f"commons_search failed: {response['error']}: {response.get('detail', '')}")
    return response


async def submit_kb_entry(
    hub_url: str,
    api_key: str,
    title: str,
    context_text: str,
    solution_text: str,
    tags: list[str] | None = None,
    agent_type: str = "",
    rationale: str = "",
    idempotency_key: str | None = None,
) -> dict:
    """Propose a Knowledge Base entry for operator review. Nothing is
    published by this call -- see hub/crud.py:submit_kb_entry for the full
    contract, and `commons_submissions` for checking status afterward.
    """
    arguments: dict[str, Any] = {
        "title": title, "context_text": context_text, "solution_text": solution_text,
        "tags": tags or [], "agent_type": agent_type, "rationale": rationale,
    }
    if idempotency_key is not None:
        arguments["idempotency_key"] = idempotency_key
    response = await _call_tool(hub_url, api_key, "submit_kb_entry", arguments)
    if response.get("error"):
        raise HubConnectionError(f"submit_kb_entry failed: {response['error']}: {response.get('detail', '')}")
    return response


async def list_my_kb_submissions(hub_url: str, api_key: str, limit: int | None = None) -> dict:
    """This org's own Knowledge Base submissions and their review status."""
    arguments: dict[str, Any] = {}
    if limit is not None:
        arguments["limit"] = limit
    response = await _call_tool(hub_url, api_key, "list_my_kb_submissions", arguments)
    if response.get("error"):
        raise HubConnectionError(
            f"list_my_kb_submissions failed: {response['error']}: {response.get('detail', '')}"
        )
    return response


async def account_usage(hub_url: str, api_key: str) -> dict:
    """What this org's plan entitles it to, and what it has used.

    Free to call: reading the meter does not consume a commons query.
    """
    response = await _call_tool(hub_url, api_key, "account_usage", {})
    if response.get("error"):
        raise HubConnectionError(f"account_usage failed: {response['error']}: {response.get('detail', '')}")
    return response


async def fleet_outcomes(hub_url: str, api_key: str, agent_type: str = "") -> dict:
    """Has this fleet's recorded performance changed since its baseline
    window, and does the randomized holdout say the memory caused it?

    Returns both instruments in one response: the before/after comparison
    (observational, and it says so) plus a `causal` section from the
    holdout. Free to call -- it reads this org's own traces only.
    """
    arguments: dict = {}
    if agent_type:
        arguments["agent_type"] = agent_type
    response = await _call_tool(hub_url, api_key, "fleet_outcomes", arguments)
    if response.get("error"):
        raise HubConnectionError(
            f"fleet_outcomes failed: {response['error']}: {response.get('detail', '')}"
        )
    return response


async def holdout_assign(
    hub_url: str, api_key: str, trace_ids: list[str], occasion_id: str
) -> dict:
    """Ask which of these traces to inject on this occasion, and which to
    deliberately withhold.

    Returns {"inject": [...], "withhold": [...]}. **Traces under
    `withhold` must not be used on this occasion** -- using one anyway does
    not fail loudly, it moves that occasion into the treated arm without
    the record saying so, which biases the measured effect toward zero.

    Safe to retry: assignment is a deterministic hash, so a repeat call
    returns the same arms and records nothing new.
    """
    response = await _call_tool(
        hub_url, api_key, "holdout_assign",
        {"trace_ids": list(trace_ids), "occasion_id": occasion_id},
    )
    if response.get("error"):
        raise HubConnectionError(
            f"holdout_assign failed: {response['error']}: {response.get('detail', '')}"
        )
    return response


async def record_occasion_outcome(
    hub_url: str, api_key: str, occasion_id: str, succeeded: bool
) -> dict:
    """Report how an occasion went, resolving every holdout decision made
    for it. Only the first report for an occasion counts."""
    response = await _call_tool(
        hub_url, api_key, "record_occasion_outcome",
        {"occasion_id": occasion_id, "succeeded": bool(succeeded)},
    )
    if response.get("error"):
        raise HubConnectionError(
            f"record_occasion_outcome failed: {response['error']}: "
            f"{response.get('detail', '')}"
        )
    return response


async def delete_trace(hub_url: str, api_key: str, trace_id: str) -> bool:
    """Permanently delete one of this org's own traces, and every trace in
    its amendment chain. Irreversible. Returns False if no such trace
    (including one belonging to another org)."""
    response = await _call_tool(hub_url, api_key, "delete_trace", {"id": trace_id})
    if response.get("error") == "not_found":
        return False
    if response.get("error"):
        raise HubConnectionError(f"delete_trace failed: {response['error']}: {response.get('detail', '')}")
    return bool(response.get("deleted"))


async def request_account_deletion(hub_url: str, api_key: str) -> dict:
    """Start permanently deleting this ENTIRE organization. Deletes
    nothing by itself -- see hub/crud.py:request_org_deletion. Returns
    {"confirmation_token", "confirm_not_before", "expires_at"}.
    """
    response = await _call_tool(hub_url, api_key, "request_account_deletion", {})
    if response.get("error"):
        raise HubConnectionError(
            f"request_account_deletion failed: {response['error']}: {response.get('detail', '')}"
        )
    return response


async def cancel_account_deletion(hub_url: str, api_key: str) -> bool:
    """Cancel a pending request_account_deletion request. Returns False if
    there was no pending request."""
    response = await _call_tool(hub_url, api_key, "cancel_account_deletion", {})
    if response.get("error"):
        raise HubConnectionError(
            f"cancel_account_deletion failed: {response['error']}: {response.get('detail', '')}"
        )
    return bool(response.get("cancelled"))


async def confirm_account_deletion(hub_url: str, api_key: str, confirmation_token: str) -> None:
    """The second call: permanently deletes this organization and
    everything scoped to it. Irreversible. Raises HubConnectionError
    (message carries 'deletion_not_ready') if called too soon, with a
    mismatched/expired token, or with no pending request.
    """
    response = await _call_tool(
        hub_url, api_key, "confirm_account_deletion", {"confirmation_token": confirmation_token}
    )
    if response.get("error"):
        raise HubConnectionError(
            f"confirm_account_deletion failed: {response['error']}: {response.get('detail', '')}"
        )


DEFAULT_MAX_PULL_RESULTS = 2000


async def pull_search_results(
    hub_url: str,
    api_key: str,
    root: str,
    query: str = "",
    tags: list[str] | None = None,
    max_results: int = DEFAULT_MAX_PULL_RESULTS,
) -> PullResult:
    """Pull search_traces results into memory/traces/ as candidate Traces
    awaiting `commontrace lesson new` promotion (protocol/PROTOCOL.md §5).

    Pages through search_traces via `offset`/`has_more` rather than a single
    call -- search_traces caps each response at DEFAULT_SEARCH_LIMIT (50)
    results, so a single call silently pulled only the first page while
    reporting a normal-looking result, with nothing telling the caller more
    was available. Stops when the Hub reports no more results OR
    `max_results` is reached: a configurable safety cap, not a promise that
    every result set is small enough to pull to disk in full.
    """
    traces: list[dict] = []
    ignored_terms: list[str] = []
    offset = 0
    while True:
        response = await _call_tool(
            hub_url, api_key, "search_traces", {"query": query, "tags": tags or [], "offset": offset}
        )
        if response.get("error"):
            raise HubConnectionError(f"search_traces failed: {response['error']}")
        # Identical on every page (it describes the query, not the page), so
        # the first response settles it; read from each anyway rather than
        # special-casing the first iteration.
        raw_ignored = response.get("terms_ignored")
        if isinstance(raw_ignored, list):
            ignored_terms = [str(t) for t in raw_ignored]
        page = response.get("traces", [])
        traces.extend(page)
        if not page or not response.get("has_more") or len(traces) >= max_results:
            break
        offset = int(response.get("offset", offset)) + (int(response.get("limit", 0)) or len(page))
    if len(traces) > max_results:
        traces = traces[:max_results]

    tdir = paths.traces_dir(root)
    os.makedirs(tdir, exist_ok=True)
    tdir_abs = os.path.abspath(tdir)

    written: list[str] = []
    for trace in traces:
        raw_trace_id = str(trace.get("id", "")) if trace.get("id") is not None else ""
        clean_trace_id = re.sub(r"[^A-Za-z0-9_-]", "", raw_trace_id)[:64]
        raw_title = str(trace.get("title") or "trace")
        slug = re.sub(r"[^a-z0-9]+", "-", raw_title.lower()).strip("-")[:60] or "trace"
        filename = f"hub_{slug}_{clean_trace_id[:8]}.md" if clean_trace_id else f"hub_{slug}.md"
        out_path = os.path.abspath(os.path.join(tdir_abs, filename))
        if not (out_path == tdir_abs or out_path.startswith(tdir_abs + os.sep)):
            raise ValueError(f"Path traversal detected in trace id: {raw_trace_id!r}")
        if os.path.exists(out_path):
            continue  # already pulled in a previous sync

        fm = templates.trace_frontmatter(
            clean_trace_id or slug,
            str(trace.get("title") or ""),
            str(trace.get("agent_type") or ""),
            list(trace.get("tags") or []) if isinstance(trace.get("tags"), (list, tuple)) else [],
            str(trace.get("profile") or ""),
            trace.get("outcome") if isinstance(trace.get("outcome"), dict) else None,
        )
        fm["hub_trace_id"] = raw_trace_id
        body = templates.trace_body(trace.get("context_text") or "", trace.get("solution_text") or "")
        frontmatter.write(out_path, fm, body)
        written.append(out_path)

    return PullResult(written_paths=written, n_found=len(traces), ignored_terms=ignored_terms)
