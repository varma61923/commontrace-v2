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

import glob
import os
import re
from dataclasses import dataclass, field
from typing import Any

from commontrace import frontmatter, paths, templates

_SECTION_RE = re.compile(
    r"^##\s*(Rule|Why|How to apply|Counter-examples)\s*\n(.*?)(?=\n##\s*(?:Rule|Why|How to apply|Counter-examples)\s*\n|\Z)",
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


async def _open_session(hub_url: str, api_key: str):
    try:
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:
        raise HubClientUnavailable(
            "the Hub client needs the optional 'mcp' dependency. Install it with "
            "`pip install commontrace[hub-sync]`."
        ) from exc

    http_client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {api_key}"})
    return streamable_http_client(hub_url, http_client=http_client), ClientSession


async def _call_tool(hub_url: str, api_key: str, name: str, arguments: dict[str, Any]) -> dict:
    transport_ctx, ClientSession = await _open_session(hub_url, api_key)
    try:
        async with transport_ctx as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
            if result.is_error:
                text = "; ".join(getattr(c, "text", str(c)) for c in result.content)
                raise HubConnectionError(f"Hub tool {name!r} returned an error: {text}")
            if result.structured_content is not None:
                return result.structured_content
            # Fallback: some transports only populate .content (text blocks of JSON).
            import json

            text = "".join(getattr(c, "text", "") for c in result.content)
            return json.loads(text) if text else {}
    except HubConnectionError:
        raise
    except Exception as exc:
        raise HubConnectionError(f"could not reach the Hub at {hub_url}: {exc}") from exc


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
                    "title": fm.get("description", slug),
                    "context_text": fm.get("applies_when", ""),
                    "solution_text": solution_text,
                    "tags": fm.get("tags", []),
                    "agent_type": fm.get("agent_type", ""),
                },
            )
        except (HubClientUnavailable, HubConnectionError) as exc:
            results.append(PushResult(slug=slug, hub_trace_id=None, error=str(exc)))
            continue

        if result.get("error"):
            results.append(PushResult(slug=slug, hub_trace_id=None, error=result["error"]))
            continue

        hub_trace_id = result.get("id")
        fm["hub_trace_id"] = hub_trace_id
        frontmatter.write(path, fm, body)
        results.append(PushResult(slug=slug, hub_trace_id=hub_trace_id, quarantined=result.get("quarantined", False)))
    return results


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

    written: list[str] = []
    for trace in traces:
        trace_id = trace.get("id", "")
        slug = re.sub(r"[^a-z0-9]+", "-", trace.get("title", "trace").lower()).strip("-")[:60] or "trace"
        filename = f"hub_{slug}_{trace_id[:8]}.md" if trace_id else f"hub_{slug}.md"
        out_path = os.path.join(tdir, filename)
        if os.path.exists(out_path):
            continue  # already pulled in a previous sync

        fm = templates.trace_frontmatter(
            trace_id or slug,
            trace.get("title", ""),
            trace.get("agent_type", ""),
            list(trace.get("tags", [])),
            trace.get("profile", ""),
            trace.get("outcome") or None,
        )
        fm["hub_trace_id"] = trace_id
        body = templates.trace_body(trace.get("context_text", ""), trace.get("solution_text", ""))
        frontmatter.write(out_path, fm, body)
        written.append(out_path)

    return PullResult(written_paths=written, n_found=len(traces))
