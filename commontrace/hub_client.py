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
    re.DOTALL | re.MULTILINE,
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


@dataclass
class PullResult:
    written_paths: list[str] = field(default_factory=list)
    n_found: int = 0


def _lesson_sections(body: str) -> dict[str, str]:
    return {m.group(1).lower(): m.group(2).strip() for m in _SECTION_RE.finditer(body)}


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
    """
    from urllib.parse import urlparse

    scheme = urlparse(hub_url).scheme.lower()
    if scheme not in ("http", "https"):
        raise HubConnectionError(
            f"refusing to use Hub URL {hub_url!r}: scheme must be http or https, got {scheme or '(none)'!r}"
        )


async def _open_session(hub_url: str, api_key: str, timeout_seconds: float):
    _validate_hub_url(hub_url)
    try:
        import httpx2
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
    http_client = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout_seconds,
    )
    return streamable_http_client(hub_url, http_client=http_client), ClientSession


def _is_retryable(exc: Exception) -> bool:
    """Retry transport-level failures (connection refused, timeout, 5xx),
    never application-level ones.

    A rejected API key or a schema-invalid trace fails identically on every
    attempt, so retrying it just multiplies the delay before the user sees
    the real error -- and retrying a rejected credential against a server
    that may be rate-limiting auth failures actively makes things worse.
    """
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
        transport_ctx, ClientSession = await _open_session(hub_url, api_key, timeout_seconds)
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

    raise HubConnectionError(
        f"could not reach the Hub at {hub_url} after {max_attempts} attempt(s): {last_exc}"
    ) from last_exc


async def push_active_lessons(hub_url: str, api_key: str, root: str) -> list[PushResult]:
    """Push every `status: active` lesson to the Hub via contribute_trace,
    recording the returned id back into the lesson's `hub_trace_id`
    frontmatter field. Mapping matches the one previously documented as
    instructional text in this module's predecessor
    (commontrace/commands/sync_cmd.py):
        contribute_trace(title=lesson.description, context_text=applies_when,
                          solution_text="Rule + How to apply", tags=lesson.tags)
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

        try:
            result = await _call_tool(
                hub_url,
                api_key,
                "contribute_trace",
                {
                    "title": fm.get("description") or slug,
                    "context_text": fm.get("applies_when") or "",
                    "solution_text": solution_text,
                    "tags": list(fm.get("tags") or []) if isinstance(fm.get("tags"), list) else [],
                    "agent_type": fm.get("agent_type") or "",
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


async def pull_search_results(
    hub_url: str, api_key: str, root: str, query: str = "", tags: list[str] | None = None
) -> PullResult:
    """Pull search_traces results into memory/traces/ as candidate Traces
    awaiting `commontrace lesson new` promotion (protocol/PROTOCOL.md §5)."""
    response = await _call_tool(hub_url, api_key, "search_traces", {"query": query, "tags": tags or []})
    if response.get("error"):
        raise HubConnectionError(f"search_traces failed: {response['error']}")

    traces = response.get("traces", [])
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
