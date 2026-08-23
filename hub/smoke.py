"""Post-deploy smoke check: prove a running Hub actually works.

Run this against a deployment you just brought up, before you hand a key to
a customer. It is deliberately end-to-end and deliberately paranoid: it
exercises the full MCP tool surface against the live server over real
HTTP, and it verifies the properties that matter more than uptime does --
that an unauthenticated caller is refused, that one tenant cannot see
another's data, and that a trace not opted into the commons stays private
to the org that owns it.

    python -m hub.smoke --url https://hub.example.com --api-key ct_live_...

    # Also verify isolation, which needs a second org's key:
    python -m hub.smoke --url ... --api-key ct_live_A --other-api-key ct_live_B

Exits 0 if every check passes, 1 otherwise, and prints one line per check so
a failure says which property broke. Safe to run against production: it
writes traces tagged `commontrace-smoke` under the calling org and nothing
else, and `--cleanup-hint` prints how to purge them afterwards.

Why a script and not just the test suite: the tests run against a database
fixture in CI. This runs against the thing you actually deployed, through
the network path, the TLS terminator, and the proxy your platform put in
front of it -- which is where deployments really fail.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

# The only hub-side import in this file: the plan table is the definition of
# what an entitlement check should see, and duplicating the names here would
# let the smoke check quietly pass against a Hub whose plans have changed.
from hub import plans

SMOKE_TAG = "commontrace-smoke"


class CheckFailed(Exception):
    """A smoke check did not hold. The message says which property broke."""


def _content(result):
    """MCP tool results arrive as structured content or as a JSON text block."""
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured
    blocks = [getattr(block, "text", str(block)) for block in result.content]
    if len(blocks) == 1:
        try:
            return json.loads(blocks[0])
        except (json.JSONDecodeError, TypeError):
            return blocks[0]
    return blocks


class Reporter:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def ok(self, label: str, detail: str = "") -> None:
        print(f"  [PASS] {label}" + (f" - {detail}" if detail else ""))

    def fail(self, label: str, detail: str) -> None:
        print(f"  [FAIL] {label} - {detail}", file=sys.stderr)
        self.failures.append(label)

    def check(self, label: str, condition: bool, detail: str = "") -> bool:
        (self.ok if condition else self.fail)(label, detail)
        return condition


def _session(url: str, api_key: str):
    """An MCP client session against the live server, carrying the API key."""
    import httpx
    from mcp.client.streamable_http import streamable_http_client

    return streamable_http_client(
        url, http_client=httpx.AsyncClient(headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0)
    )


def _preflight(url: str, api_key: str) -> str | None:
    """Probe the endpoint over plain HTTP before opening an MCP session.

    The MCP client collapses an HTTP 401 into a generic JSON-RPC internal
    error, so by the time a session fails there is no way to tell "your key
    was rejected" from "the server blew up". A raw request first keeps those
    distinguishable, which is the difference between an operator fixing a
    credential and an operator paging whoever owns the service.

    Returns a message describing the problem, or None if the endpoint is
    reachable and the key is accepted.
    """
    import httpx

    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "commontrace-smoke", "version": "1"}},
    }
    try:
        response = httpx.post(
            url, json=payload, timeout=15.0,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
    except Exception as exc:
        return (f"could not reach {url} ({type(exc).__name__}). Check the --url, that the "
                "deployment is up, and that /readyz answers.")

    if response.status_code in (401, 403):
        return (f"the server rejected the API key (HTTP {response.status_code}). "
                "It may be wrong, revoked, or expired.")
    if response.status_code == 404:
        return (f"HTTP 404 at {url}. The MCP endpoint path is probably wrong -- "
                "it defaults to /mcp (HUB_STREAMABLE_HTTP_PATH).")
    if response.status_code >= 500:
        return (f"the server returned HTTP {response.status_code}. It is reachable but "
                "failing; check its logs and /readyz.")
    return None


# The tool surface, asserted exactly rather than as a subset: a tool
# appearing that this file does not know about is exactly as interesting
# as one going missing, since the surface is what a customer's key can
# reach. Split in two because HUB_COMMONS_ENABLED (hub/config.py) changes
# what a live deployment actually exposes -- smoke has no access to the
# operator's env, only what it observes over MCP, so it checks internal
# consistency (all of COMMONS_TOOLS present together or all absent
# together) rather than one fixed list. Update these deliberately when
# the surface changes.
CORE_TOOLS = [
    # the six org-scoped tools
    "amend_trace", "contribute_trace", "get_trace", "list_tags",
    "search_traces", "vote_trace",
    # entitlements (hub/plans.py) -- unaffected by HUB_COMMONS_ENABLED,
    # since it reports an org's own plan and usage, never another org's data
    "account_usage",
]
COMMONS_TOOLS = ["commons_overlap", "share_trace", "unshare_trace"]
EXPECTED_TOOLS = CORE_TOOLS + COMMONS_TOOLS  # kept for external callers/tests


async def _tool_surface(session, report: Reporter) -> bool:
    """Returns whether the commons tools are present, so later checks know
    whether to expect commons_overlap etc. to exist at all."""
    listed = await session.list_tools()
    names = sorted(tool.name for tool in listed.tools)
    core, commons = sorted(CORE_TOOLS), sorted(COMMONS_TOOLS)
    commons_enabled = names == sorted(core + commons)
    core_only = names == core
    ok = commons_enabled or core_only
    mode = (
        "commons enabled" if commons_enabled
        else "commons disabled (HUB_COMMONS_ENABLED=false)" if core_only
        else "UNEXPECTED"
    )
    report.check(
        f"MCP tool surface is exactly core+commons or core-only -- {mode}", ok,
        f"got {names}" if not ok else ", ".join(names),
    )
    return commons_enabled


async def _round_trip(session, report: Reporter, marker: str) -> str | None:
    """contribute -> search -> get -> vote -> amend, the full write path."""
    created = _content(await session.call_tool("contribute_trace", {
        "title": f"smoke check {marker}",
        "context_text": f"Automated post-deploy smoke check {marker}. Safe to delete.",
        "solution_text": "No action required; this trace exists to prove the write path works.",
        "tags": [SMOKE_TAG],
        "agent_type": "custom",
    }))
    trace_id = created.get("id") if isinstance(created, dict) else None
    if not report.check("contribute_trace writes", bool(trace_id), f"returned {created!r}"):
        return None

    found = _content(await session.call_tool("search_traces", {"query": marker}))
    ids = [t["id"] for t in found.get("traces", [])] if isinstance(found, dict) else []
    report.check("search_traces finds it", trace_id in ids,
                 f"searched for {marker!r}, got {len(ids)} result(s)")

    fetched = _content(await session.call_tool("get_trace", {"id": trace_id}))
    report.check("get_trace returns it", isinstance(fetched, dict) and fetched.get("id") == trace_id,
                 f"got {fetched!r}" if not isinstance(fetched, dict) else "")

    voted = _content(await session.call_tool("vote_trace", {"id": trace_id, "vote": "up"}))
    trust_moved = isinstance(voted, dict) and voted.get("trust", 0) > 0.5
    report.check("vote_trace updates trust", trust_moved,
                 f"trust={voted.get('trust') if isinstance(voted, dict) else voted!r}")

    amended = _content(await session.call_tool("amend_trace", {
        "id": trace_id, "solution_text": "Amended by the smoke check."}))
    is_new_immutable_record = (
        isinstance(amended, dict)
        and amended.get("id") != trace_id
        and amended.get("supersedes_trace_id") == trace_id
    )
    report.check("amend_trace supersedes rather than mutating", is_new_immutable_record,
                 "an amendment must create a new trace that supersedes the original")

    tags = _content(await session.call_tool("list_tags", {}))
    report.check("list_tags includes the smoke tag",
                 isinstance(tags, dict) and SMOKE_TAG in tags.get("tags", []))
    return trace_id


async def _entitlements(session, report: Reporter) -> None:
    """The plan is only real if the server can state it.

    Checked post-deploy because an entitlement layer that fails open is
    invisible until the bill is wrong: every request still succeeds, so
    nothing looks broken. A misconfigured deployment that reports every org
    as unlimited passes every other check in this file.
    """
    usage = _content(await session.call_tool("account_usage", {}))
    if usage.get("error"):
        report.fail("account_usage responds", str(usage))
        return

    q = usage.get("commons_queries") or {}
    report.check(
        "account_usage reports a resolved plan",
        usage.get("plan") in plans.PLANS,
        f"plan={usage.get('plan')!r}, period={usage.get('period')!r}",
    )
    report.check(
        "commons queries are metered, not unlimited",
        q.get("allowance") != plans.UNLIMITED or usage.get("plan") == "operator",
        f"allowance={q.get('allowance')} (only the operator plan may be unlimited here)",
    )
    report.check(
        "earned allowance is accounted separately from granted",
        q.get("granted") is not None and q.get("earned") is not None,
        f"granted={q.get('granted')}, earned={q.get('earned')}, "
        f"delivered_hits={usage.get('delivered_hits')}",
    )


async def _rejects_bad_credentials(url: str, report: Reporter) -> None:
    """Must observe an actual HTTP 401/403 from the server, not merely "some
    exception happened" while opening the MCP session. The bare `except
    Exception: report.ok(...)` this replaced treated a TLS failure, a
    timeout, or a proxy connection reset identically to a genuine
    credential rejection -- all three raise from inside the MCP session
    the same way, and all three reported [PASS] "an invalid API key is
    refused" without the server having rejected anything, or even having
    been reached. _preflight already exists for exactly this reason (see
    its own docstring): a raw HTTP request whose real status code can be
    told apart from a connection failure, used here with the bogus key
    instead of the real one.
    """
    problem = _preflight(url, "ct_live_definitely-not-a-real-key")
    if problem is None:
        report.fail("an invalid API key is refused", "the server ACCEPTED a bogus key")
    elif "rejected the API key" in problem:
        report.ok("an invalid API key is refused")
    else:
        # Reachable-but-not-a-401 (404, 5xx) or entirely unreachable: this
        # check did not observe a rejection, so it must not report success.
        report.fail("an invalid API key is refused", f"could not verify: {problem}")


async def _tenant_isolation(
    url: str, other_key: str, foreign_id: str, marker: str, report: Reporter,
    commons_enabled: bool = True,
) -> None:
    """The other org must not be able to read, vote on, or amend our trace."""
    from mcp import ClientSession

    async with _session(url, other_key) as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for tool, args in (
                ("get_trace", {"id": foreign_id}),
                ("vote_trace", {"id": foreign_id, "vote": "up"}),
                ("amend_trace", {"id": foreign_id, "solution_text": "should never land"}),
            ):
                result = _content(await session.call_tool(tool, args))
                denied = isinstance(result, dict) and result.get("error") == "not_found"
                report.check(
                    f"{tool} across a tenant boundary is refused", denied,
                    # not_found rather than forbidden: a wrong answer here leaks
                    # that the id exists, which is itself a disclosure.
                    f"expected error=not_found, got {result!r}",
                )

            if not commons_enabled:
                # Nothing to probe: _tool_surface already proved these three
                # tools are entirely absent from the server, which is a
                # stronger guarantee than "refused when called" -- there is
                # no path left to check.
                return

            # The commons is the ONLY path by which a row may cross an org
            # boundary, so a deployment check has to prove it stays shut for
            # a trace nobody opted in. The round-trip trace above was never
            # shared, so probing with a signature built from its EXACT text
            # -- the strongest possible probe -- must still find nothing.
            from hub import commons

            probe = commons.signature_for(
                f"smoke check {marker}",
                f"Automated post-deploy smoke check {marker}. Safe to delete.",
                [SMOKE_TAG],
            )
            overlap = _content(await session.call_tool(
                "commons_overlap", {"failures": [{"label": "probe", "signature": probe}]},
            ))
            leaked = isinstance(overlap, dict) and foreign_id in json.dumps(overlap)
            report.check(
                "an unshared trace stays out of the commons",
                isinstance(overlap, dict) and not leaked,
                f"the other org's commons_overlap returned our unshared trace: {overlap!r}",
            )

            # And an org cannot place someone else's trace into the commons.
            shared = _content(await session.call_tool("share_trace", {"id": foreign_id}))
            report.check(
                "share_trace across a tenant boundary is refused",
                isinstance(shared, dict) and shared.get("error") == "not_found",
                f"expected error=not_found, got {shared!r}",
            )


async def run(args: argparse.Namespace) -> int:
    from mcp import ClientSession

    report = Reporter()
    marker = uuid.uuid4().hex[:12]
    print(f"[smoke] {args.url}  (marker {marker})")

    problem = _preflight(args.url, args.api_key)
    if problem:
        print(f"\n[smoke] FAILED: {problem}", file=sys.stderr)
        return 1

    print("\nTool surface and write path")
    trace_id = None
    commons_enabled = True
    async with _session(args.url, args.api_key) as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()
            commons_enabled = await _tool_surface(session, report)
            trace_id = await _round_trip(session, report, marker)
            print("\nEntitlements")
            await _entitlements(session, report)

    print("\nAuthentication")
    await _rejects_bad_credentials(args.url, report)

    print("\nTenant isolation")
    if not args.other_api_key:
        print("  [SKIP] needs --other-api-key from a SECOND organization.\n"
              "         Isolation is the property most worth proving before a\n"
              "         customer's data lands here -- issue a throwaway org's key\n"
              "         and re-run rather than skipping this on a real deployment.")
    elif not trace_id:
        report.fail("tenant isolation", "no trace was created, so nothing could be tested")
    else:
        await _tenant_isolation(
            args.url, args.other_api_key, trace_id, marker, report,
            commons_enabled=commons_enabled,
        )

    print()
    if report.failures:
        print(f"[smoke] FAILED: {len(report.failures)} check(s) - "
              + ", ".join(report.failures), file=sys.stderr)
        return 1

    print("[smoke] all checks passed.")
    if trace_id:
        print(f"[smoke] this run wrote traces tagged '{SMOKE_TAG}'. Remove them with:\n"
              f"          python -m hub.manage purge-trace {trace_id}")
    return 0


def _diagnose(error: BaseException) -> str:
    """Turn a transport or protocol failure into one actionable sentence."""
    seen: list[str] = []

    def walk(exc: BaseException) -> None:
        seen.append(f"{type(exc).__name__}: {exc}")
        for sub in getattr(exc, "exceptions", ()) or ():
            walk(sub)
        if exc.__cause__ is not None:
            walk(exc.__cause__)

    walk(error)
    joined = " | ".join(seen).lower()

    if "connect" in joined or "refused" in joined or "resolve" in joined:
        return "could not reach the server. Check the --url, that the deployment is up, and that /readyz answers."
    if any(m in joined for m in ("401", "unauthorized", "invalid", "revoked", "expired")):
        return "the server rejected the API key. It may be wrong, revoked, or expired."
    if "timeout" in joined or "timed out" in joined:
        return "the server accepted the connection but did not answer in time."
    return seen[0] if seen else "unknown error"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hub.smoke",
        description="Post-deploy smoke check against a running CommonTrace Hub.",
    )
    parser.add_argument("--url", required=True, help="MCP endpoint, e.g. https://hub.example.com/mcp")
    parser.add_argument("--api-key", required=True, help="An API key for the org to write under.")
    parser.add_argument(
        "--other-api-key", default=None,
        help="A key from a DIFFERENT org. Enables the tenant-isolation checks, "
             "which are the ones worth caring about most.",
    )
    args = parser.parse_args(argv)
    if not args.url.rstrip("/").endswith("/mcp"):
        print(f"[smoke] note: {args.url} does not end in /mcp, which is the default "
              "endpoint path (HUB_STREAMABLE_HTTP_PATH).", file=sys.stderr)
    try:
        return asyncio.run(run(args))
    except ImportError:
        print("[smoke] needs the MCP client library: pip install 'commontrace[hub-sync]'",
              file=sys.stderr)
        return 2
    except Exception as exc:
        # An operator running this against a deployment that is down, or with
        # a key that was revoked, should be told which of those it is -- not
        # handed a traceback through an anyio task group. Same reasoning as
        # hub/manage.py: a stack trace reads as "the tool is broken" when the
        # actual news is "the thing you deployed is not reachable".
        # Plain `except`, not `except*`: this must run on Python 3.10, and
        # _diagnose walks ExceptionGroup.exceptions itself.
        print(f"[smoke] FAILED: {_diagnose(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
