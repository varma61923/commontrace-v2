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
import glob
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from commontrace import frontmatter, paths, templates, trace_io

# Network defaults. Overridable per call; `commontrace sync` exposes them
# as --timeout / --max-attempts.
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 0.5

_SECTION_NAMES = r"Rule|Why|How to apply|Counter-examples"
_SECTION_RE = re.compile(
    rf"^##\s*({_SECTION_NAMES})\s*\n(.*?)(?=\n##\s*(?:{_SECTION_NAMES})\s*\n|\Z)",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)


class HubClientUnavailable(RuntimeError):
    """Raised when the optional `mcp` client dependency isn't installed."""


class HubConnectionError(RuntimeError):
    """Raised when the Hub can't be reached or rejects the request."""


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
        raise HubConnectionError(
            f"refusing to use Hub URL {hub_url!r}: scheme must be http or https, got {scheme or '(none)'!r}"
        )
    hostname = (parsed.hostname or "").lower()
    if scheme == "http" and hostname not in ("localhost", "127.0.0.1", "::1"):
        raise HubConnectionError(
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
        raise HubConnectionError(
            f"refusing to use Hub URL {hub_url!r}: {hostname} is a link-local or "
            "cloud-metadata address (e.g. 169.254.169.254, fd00:ec2::254) -- "
            "refusing to send the Hub API key there."
        )


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

    # Without an explicit timeout the underlying client waits indefinitely,
    # so a Hub that accepts the connection and then stalls hangs
    # `commontrace sync` forever with no output -- the worst failure mode for
    # a CLI someone may have put in a cron job.
    http_client = httpx.AsyncClient(
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout_seconds,
    )
    # Returned alongside the transport/session so the caller can close it
    # explicitly (httpx.AsyncClient owns a connection pool / open sockets
    # that are never released otherwise -- streamable_http_client wraps it
    # but does not take ownership of its lifecycle).
    return streamable_http_client(hub_url, http_client=http_client), ClientSession, http_client


def _is_retryable(exc: Exception) -> bool:
    """Retry transport-level failures (connection refused, timeout, 5xx),
    never application-level ones.

    A rejected API key or a schema-invalid trace fails identically on every
    attempt, so retrying it just multiplies the delay before the user sees
    the real error -- and retrying a rejected credential against a server
    that may be rate-limiting auth failures actively makes things worse.

    An actual tool-level rejection from the Hub (an MCP `result.is_error`
    response, e.g. "rejected the API key") never reaches this function at
    all -- _call_tool raises that as HubConnectionError and re-raises it
    immediately, bypassing retry entirely. This function only judges
    transport-layer exceptions that got here some other way, so it checks
    for a real HTTP status code first (httpx.HTTPStatusError carries one on
    `exc.response.status_code`) and only falls back to matching substrings
    in the exception's string form when no structured status is available --
    a legitimate transient error whose message happens to contain "invalid"
    or "403" (in a URL, a nested error, ...) would otherwise be
    misclassified as non-retryable by the substring check alone.
    """
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status_code, int):
        if status_code in (401, 403):
            return False
        if status_code >= 500:
            return True

    text = f"{type(exc).__name__}: {exc}".lower()
    if any(marker in text for marker in ("401", "unauthorized", "invalid", "revoked", "expired", "403")):
        return False
    return any(
        marker in text
        for marker in ("timeout", "connect", "refused", "reset", "temporarily", "502", "503", "504", "eof")
    )


async def _call_tool(
    hub_url: str,
    api_key: str,
    name: str,
    arguments: dict[str, Any],
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict:
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        transport_ctx, ClientSession, http_client = await _open_session(hub_url, api_key, timeout_seconds)
        try:
            async with transport_ctx as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
                if result.is_error:
                    text = "; ".join(getattr(c, "text", str(c)) for c in result.content)
                    # An error *from* the tool is an answer, not a transport
                    # failure -- surfaced immediately, never retried.
                    raise HubConnectionError(f"Hub tool {name!r} returned an error: {text}")
                if result.structured_content is not None:
                    return result.structured_content
                # Fallback: some transports only populate .content (text blocks of JSON).
                text = "".join(getattr(c, "text", "") for c in result.content)
                return json.loads(text) if text else {}
        except HubConnectionError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport errors aren't one exception type
            last_exc = exc
            if attempt >= max_attempts or not _is_retryable(exc):
                break
            # Exponential backoff: 0.5s, 1s, 2s, ...
            await asyncio.sleep(RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
        finally:
            # Every attempt opens a fresh AsyncClient (a fresh connection
            # pool / socket); without this it is never released, and a
            # long-running loop or repeated `commontrace sync` invocations
            # leak file descriptors until the process hits "Too many open
            # files".
            await http_client.aclose()

    raise HubConnectionError(
        f"could not reach the Hub at {hub_url} after {max_attempts} attempt(s): {last_exc}"
    ) from last_exc


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


async def push_active_lessons(hub_url: str, api_key: str, root: str) -> list[PushResult]:
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
    results: list[PushResult] = []
    for path in _iter_active_lesson_paths(root):
        fm, body = frontmatter.read(path)
        if fm.get("status") != "active":
            continue
        slug = fm.get("name", os.path.splitext(os.path.basename(path))[0])

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
                results.append(PushResult(slug=slug, hub_trace_id=str(existing_hub_id), skipped=True))
                continue
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
                )
            except (HubClientUnavailable, HubConnectionError) as exc:
                results.append(PushResult(slug=slug, hub_trace_id=str(existing_hub_id), error=str(exc)))
                continue
            if result.get("error"):
                results.append(PushResult(slug=slug, hub_trace_id=str(existing_hub_id), error=result["error"]))
                continue
            # amend_trace supersedes rather than mutating in place
            # (hub/crud.py:amend_trace), so the id returned here is a NEW
            # trace and hub_trace_id must move forward to it -- the old id
            # is now the head of a chain, not the trace to amend next time.
            amended_id = result.get("id")
            with frontmatter.locked(path):
                fm, body = frontmatter.read(path)
                fm["hub_trace_id"] = amended_id
                fm["hub_pushed_fingerprint"] = fingerprint
                frontmatter.write(path, fm, body)
            results.append(
                PushResult(slug=slug, hub_trace_id=amended_id, quarantined=result.get("quarantined", False))
            )
            continue

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
            )
        except (HubClientUnavailable, HubConnectionError) as exc:
            results.append(PushResult(slug=slug, hub_trace_id=None, error=str(exc)))
            continue

        if result.get("error"):
            results.append(PushResult(slug=slug, hub_trace_id=None, error=result["error"]))
            continue

        hub_trace_id = result.get("id")
        # Re-read under the lock rather than reusing the `fm` captured
        # before the (slow, awaited) Hub call above: another process could
        # have changed a different field on this same file (e.g. `lesson
        # approve`/`reject` flipping `status`) while this push was in
        # flight, and writing back the pre-call snapshot would silently
        # discard that change -- the exact lost-update frontmatter.locked()
        # exists to prevent, see its docstring.
        with frontmatter.locked(path):
            fm, body = frontmatter.read(path)
            fm["hub_trace_id"] = hub_trace_id
            fm["hub_pushed_fingerprint"] = fingerprint
            frontmatter.write(path, fm, body)
        results.append(PushResult(slug=slug, hub_trace_id=hub_trace_id, quarantined=result.get("quarantined", False)))
    return results


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


async def push_captured_traces(hub_url: str, api_key: str, root: str) -> list[PushResult]:
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
    results: list[PushResult] = []
    for path in _iter_captured_trace_paths(root):
        instance, _body = trace_io.read(path)
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
                results.append(PushResult(slug=slug, hub_trace_id=str(existing_hub_id), skipped=True))
                continue
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
                )
            except (HubClientUnavailable, HubConnectionError) as exc:
                results.append(PushResult(slug=slug, hub_trace_id=str(existing_hub_id), error=str(exc)))
                continue
            if result.get("error"):
                results.append(PushResult(slug=slug, hub_trace_id=str(existing_hub_id), error=result["error"]))
                continue
            amended_id = result.get("id")
            # Re-read under the lock rather than reusing `instance`/`fm`
            # captured before the (slow, awaited) Hub call -- see
            # push_active_lessons's identical comment on this exact race.
            with frontmatter.locked(path):
                fm, body = frontmatter.read(path)
                fm["hub_trace_id"] = amended_id
                fm["hub_pushed_fingerprint"] = fingerprint
                frontmatter.write(path, fm, body)
            results.append(
                PushResult(slug=slug, hub_trace_id=amended_id, quarantined=result.get("quarantined", False))
            )
            continue

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
            )
        except (HubClientUnavailable, HubConnectionError) as exc:
            results.append(PushResult(slug=slug, hub_trace_id=None, error=str(exc)))
            continue
        if result.get("error"):
            results.append(PushResult(slug=slug, hub_trace_id=None, error=result["error"]))
            continue

        new_id = result.get("id")
        with frontmatter.locked(path):
            fm, body = frontmatter.read(path)
            fm["hub_trace_id"] = new_id
            fm["hub_pushed_fingerprint"] = fingerprint
            frontmatter.write(path, fm, body)
        results.append(PushResult(slug=slug, hub_trace_id=new_id, quarantined=result.get("quarantined", False)))
    return results


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
