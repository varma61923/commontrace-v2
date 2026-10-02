"""Post-deploy smoke check: prove a running Hub actually works."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from hub import plans

SMOKE_TAG = "commontrace-smoke"


def _content(result):
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


async def _call(session, report: "Reporter", label: str, tool: str, args: dict):
    try:
        return _content(await session.call_tool(tool, args))
    except Exception as exc:  # noqa: BLE001 - must become one [FAIL] line, never an uncaught crash
        report.fail(label, f"the call itself failed rather than returning a result -- {type(exc).__name__}: {exc}")
        return None


async def _initialize(session, report: "Reporter", label: str) -> bool:
    try:
        await session.initialize()
        return True
    except Exception as exc:  # noqa: BLE001 - must become one [FAIL] line, never an uncaught crash
        report.fail(label, f"session initialize failed -- {type(exc).__name__}: {exc}")
        return False


class Reporter:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def ok(self, label: str, detail: str = "") -> None:
        print(f"  [PASS] {label}" + (f" - {detail}" if detail else ""))

    def fail(self, label: str, detail: str) -> None:
        print(f"  [FAIL] {label} - {detail}", file=sys.stderr)
        self.failures.append(label)

    def check(self, label: str, condition: bool, detail: str = "", fail_detail: str = "") -> bool:
        if condition:
            self.ok(label, detail)
        else:
            self.fail(label, fail_detail or detail)
        return condition


def _session(url: str, api_key: str):
    import httpx
    from mcp.client.streamable_http import streamable_http_client

    return streamable_http_client(
        url, http_client=httpx.AsyncClient(headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0)
    )


def _preflight(url: str, api_key: str) -> str | None:
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
    if response.status_code == 429:
        return ("the server rate-limited this request (HTTP 429) before it could evaluate "
                "the API key -- inconclusive, not a rejection or an acceptance. This can be "
                "the smoke check's own request volume tripping HUB_AUTH_ATTEMPTS_BURST; wait "
                "a few seconds and re-run.")
    if response.status_code >= 500:
        return (f"the server returned HTTP {response.status_code}. It is reachable but "
                "failing; check its logs and /readyz.")
    return None


CORE_TOOLS = [
    "amend_trace", "contribute_trace", "get_trace", "list_tags",
    "search_traces", "vote_trace",
    "delete_trace", "request_account_deletion", "cancel_account_deletion",
    "confirm_account_deletion",
    "account_usage",
    "fleet_outcomes",
    "holdout_assign", "record_occasion_outcome",
    "value_delivered",
    "working_set",
    "add_comment", "list_comments", "assign_trace", "unassign_trace",
    "list_my_notifications", "mark_notification_read",
    "search_trace_content",
    "tag_trace_subjects", "find_traces_by_subject", "purge_traces_by_subject",
]
COMMONS_TOOLS = [
    "commons_overlap", "commons_search", "commons_export",
    "submit_kb_entry", "list_my_kb_submissions",
]
EXPECTED_TOOLS = CORE_TOOLS + COMMONS_TOOLS


async def _tool_surface(session, report: Reporter) -> bool:
    label = "MCP tool surface is exactly core+commons or core-only"
    try:
        listed = await session.list_tools()
    except Exception as exc:  # noqa: BLE001 - must become one [FAIL] line, never an uncaught crash
        report.fail(label, f"the call itself failed rather than returning a result -- {type(exc).__name__}: {exc}")
        return False
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
        label, ok,
        f"{mode} -- " + (f"got {names}" if not ok else ", ".join(names)),
    )
    return commons_enabled


async def _round_trip(session, report: Reporter, marker: str) -> str | None:
    created = await _call(session, report, "contribute_trace writes", "contribute_trace", {
        "title": f"smoke check {marker}",
        "context_text": f"Automated post-deploy smoke check {marker}. Safe to delete.",
        "solution_text": "No action required; this trace exists to prove the write path works.",
        "tags": [SMOKE_TAG],
        "agent_type": "custom",
    })
    if created is None:
        return None
    trace_id = created.get("id") if isinstance(created, dict) else None
    if not report.check("contribute_trace writes", bool(trace_id), f"returned {created!r}"):
        return None

    found = await _call(session, report, "search_traces finds it", "search_traces", {"query": marker})
    if found is not None:
        ids = [t["id"] for t in found.get("traces", [])] if isinstance(found, dict) else []
        report.check("search_traces finds it", trace_id in ids,
                     f"searched for {marker!r}, got {len(ids)} result(s)")

    phrased = await _call(
        session, report, "search_traces finds it from a natural-language description",
        "search_traces", {"query": f"automated deploy verification {marker} nothing needs doing here"},
    )
    if phrased is not None:
        phrased_ids = [t["id"] for t in phrased.get("traces", [])] if isinstance(phrased, dict) else []
        report.check(
            "search_traces finds it from a natural-language description", trace_id in phrased_ids,
            detail=f"a sentence-length query returned it among {len(phrased_ids)} result(s)",
            fail_detail=(
                f"a sentence-length query returned {len(phrased_ids)} result(s) and not the "
                "trace just written -- if this is the only failing check, query terms are "
                "being combined with AND rather than ranked"
            ),
        )

    fetched = await _call(session, report, "get_trace returns it", "get_trace", {"id": trace_id})
    if fetched is not None:
        report.check("get_trace returns it", isinstance(fetched, dict) and fetched.get("id") == trace_id,
                     f"got {fetched!r}" if not isinstance(fetched, dict) else "")

    voted = await _call(session, report, "vote_trace updates trust", "vote_trace",
                         {"id": trace_id, "vote": "up"})
    if voted is not None:
        trust_moved = isinstance(voted, dict) and voted.get("trust", 0) > 0.5
        report.check("vote_trace updates trust", trust_moved,
                     f"trust={voted.get('trust') if isinstance(voted, dict) else voted!r}")

    amend_args = {"id": trace_id, "solution_text": "Amended by the smoke check.",
                  "idempotency_key": f"smoke-amend-{marker}"}
    amended = await _call(session, report, "amend_trace supersedes rather than mutating",
                           "amend_trace", amend_args)
    amended_id = None
    if amended is not None:
        is_new_immutable_record = (
            isinstance(amended, dict)
            and amended.get("id") != trace_id
            and amended.get("supersedes_trace_id") == trace_id
        )
        report.check(
            "amend_trace supersedes rather than mutating", is_new_immutable_record,
            detail=(
                f"{amended.get('id')!r} supersedes {trace_id!r}"
                if is_new_immutable_record else ""
            ),
            fail_detail="an amendment must create a new trace that supersedes the original",
        )
        amended_id = amended.get("id") if isinstance(amended, dict) else None

    if amended_id is not None:
        retried = await _call(
            session, report, "amend_trace with a repeated idempotency_key returns the original, not a fork",
            "amend_trace", amend_args,
        )
        if retried is not None:
            report.check(
                "amend_trace with a repeated idempotency_key returns the original, not a fork",
                isinstance(retried, dict) and retried.get("id") == amended_id,
                f"first call returned {amended_id!r}, retry returned "
                f"{retried.get('id') if isinstance(retried, dict) else retried!r}",
            )

    tags = await _call(session, report, "list_tags includes the smoke tag", "list_tags", {})
    if tags is not None:
        report.check("list_tags includes the smoke tag",
                     isinstance(tags, dict) and SMOKE_TAG in tags.get("tags", []))
    return trace_id


async def _entitlements(session, report: Reporter) -> None:
    usage = await _call(session, report, "account_usage responds", "account_usage", {})
    if usage is None:
        return
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
        "commons_queries reports used/allowance/remaining",
        all(q.get(k) is not None for k in ("used", "allowance", "remaining")),
        f"used={q.get('used')}, allowance={q.get('allowance')}, remaining={q.get('remaining')}",
    )


async def _rejects_bad_credentials(url: str, report: Reporter) -> None:
    problem = _preflight(url, "ct_live_definitely-not-a-real-key")
    if problem is None:
        report.fail("an invalid API key is refused", "the server ACCEPTED a bogus key")
    elif "rejected the API key" in problem:
        report.ok("an invalid API key is refused")
    else:
        report.fail("an invalid API key is refused", f"could not verify: {problem}")


async def _tenant_isolation(
    url: str, other_key: str, foreign_id: str, marker: str, report: Reporter,
    commons_enabled: bool = True,
) -> None:
    from mcp import ClientSession

    async with _session(url, other_key) as (read, write, *_):
        async with ClientSession(read, write) as session:
            if not await _initialize(session, report, "session initialize (other org's key)"):
                return
            for tool, args in (
                ("get_trace", {"id": foreign_id}),
                ("vote_trace", {"id": foreign_id, "vote": "up"}),
                ("amend_trace", {"id": foreign_id, "solution_text": "should never land"}),
            ):
                label = f"{tool} across a tenant boundary is refused"
                result = await _call(session, report, label, tool, args)
                if result is None:
                    continue
                denied = isinstance(result, dict) and result.get("error") == "not_found"
                report.check(
                    label, denied,
                    detail="refused with error=not_found, disclosing nothing about the id",
                    fail_detail=f"expected error=not_found, got {result!r}",
                )

            if not commons_enabled:
                return

            from hub import commons

            probe = commons.signature_for(
                f"smoke check {marker}",
                f"Automated post-deploy smoke check {marker}. Safe to delete.",
                [SMOKE_TAG],
            )
            overlap_label = "a customer's trace never surfaces through the Knowledge Base"
            overlap = await _call(
                session, report, overlap_label, "commons_overlap",
                {"failures": [{"label": "probe", "signature": probe}]},
            )
            if overlap is not None:
                leaked = isinstance(overlap, dict) and foreign_id in json.dumps(overlap)
                report.check(
                    overlap_label,
                    isinstance(overlap, dict) and not leaked,
                    detail=(
                        "probed with a signature built from the trace's exact text; "
                        "the other org's commons_overlap did not return it"
                    ),
                    fail_detail=f"the other org's commons_overlap returned our trace: {overlap!r}",
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
            if await _initialize(session, report, "session initialize (primary key)"):
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

    if trace_id and not args.keep:
        await _cleanup(args.url, args.api_key, trace_id)

    print()
    if report.failures:
        print(f"[smoke] FAILED: {len(report.failures)} check(s) - "
              + ", ".join(report.failures), file=sys.stderr)
        return 1

    print("[smoke] all checks passed.")
    return 0


async def _cleanup(url: str, api_key: str, trace_id: str) -> None:
    from mcp import ClientSession

    try:
        async with _session(url, api_key) as (read, write, *_):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("delete_trace", {"id": trace_id})
                payload = _content(result)
                deleted = isinstance(payload, dict) and payload.get("deleted") is True
    except Exception as exc:  # noqa: BLE001 - cleanup must never mask the verdict
        deleted = False
        payload = f"{type(exc).__name__}: {exc}"

    if deleted:
        print(f"[smoke] cleaned up: deleted the trace(s) this run wrote (tagged '{SMOKE_TAG}').")
    else:
        print(
            f"[smoke] WARNING: could not delete the trace this run wrote ({payload!r}).\n"
            f"          It is tagged '{SMOKE_TAG}' and will otherwise stay in this org's\n"
            f"          corpus, where real searches can return it. Remove it with:\n"
            f"          python -m hub.manage purge-trace {trace_id}",
            file=sys.stderr,
        )


def _diagnose(error: BaseException) -> str:
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
    if "429" in joined or "rate limit" in joined or "too many requests" in joined:
        return (
            "the server rate-limited this check's own requests (HTTP 429). This is not "
            "necessarily a deployment problem -- running with --other-api-key issues enough "
            "requests from one source address to reach a tightly-configured "
            "HUB_AUTH_ATTEMPTS_BURST/HUB_READ_RATE_LIMIT_BURST. Wait a few seconds and re-run, "
            "or raise those limits if this check legitimately needs more headroom."
        )
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
    parser.add_argument(
        "--keep", action="store_true",
        help="Leave the traces this check writes in place. By default they are "
             "deleted when the run finishes, so repeatedly smoking a production "
             "deployment does not accumulate them in a real org's corpus.",
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
        print(f"[smoke] FAILED: {_diagnose(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
