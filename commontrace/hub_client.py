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

from commontrace import frontmatter, paths, templates

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


async def commons_overlap(
    hub_url: str,
    api_key: str,
    failures: list[dict],
    threshold: float | None = None,
    include_matches: bool = True,
) -> dict:
    """Ask the Hub: of these recurring failures, how many has some other
    fleet already solved?

    `failures` carries MinHash signatures only -- generated locally by
    `commontrace commons sign`. No failure text is sent. See hub/commons.py
    for the boundary on what comes back.
    """
    arguments: dict[str, Any] = {"failures": failures, "include_matches": include_matches}
    if threshold is not None:
        arguments["threshold"] = threshold
    response = await _call_tool(hub_url, api_key, "commons_overlap", arguments)
    if response.get("error"):
        raise HubConnectionError(f"commons_overlap failed: {response['error']}: {response.get('detail', '')}")
    return response


async def share_trace(hub_url: str, api_key: str, trace_id: str, rationale: str = "") -> dict:
    """Contribute one of your own Hub traces to the cross-org commons."""
    response = await _call_tool(
        hub_url, api_key, "share_trace", {"id": trace_id, "rationale": rationale}
    )
    if response.get("error"):
        raise HubConnectionError(f"share_trace failed: {response['error']}: {response.get('detail', '')}")
    return response


async def unshare_trace(hub_url: str, api_key: str, trace_id: str) -> dict:
    """Withdraw one of your traces from the cross-org commons."""
    response = await _call_tool(hub_url, api_key, "unshare_trace", {"id": trace_id})
    if response.get("error"):
        raise HubConnectionError(f"unshare_trace failed: {response['error']}: {response.get('detail', '')}")
    return response


async def account_usage(hub_url: str, api_key: str) -> dict:
    """What this org's plan entitles it to, and what it has used.

    Free to call: reading the meter does not consume a commons query.
    """
    response = await _call_tool(hub_url, api_key, "account_usage", {})
    if response.get("error"):
        raise HubConnectionError(f"account_usage failed: {response['error']}: {response.get('detail', '')}")
    return response


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
    offset = 0
    while True:
        response = await _call_tool(
            hub_url, api_key, "search_traces", {"query": query, "tags": tags or [], "offset": offset}
        )
        if response.get("error"):
            raise HubConnectionError(f"search_traces failed: {response['error']}")
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

    return PullResult(written_paths=written, n_found=len(traces))
