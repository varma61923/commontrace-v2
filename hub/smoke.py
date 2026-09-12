"""Post-deploy smoke check: prove a running Hub actually works.

Run this against a deployment you just brought up, before you hand a key to
a customer. It is deliberately end-to-end and deliberately paranoid: it
exercises the full MCP tool surface against the live server over real
HTTP, and it verifies the properties that matter more than uptime does --
that an unauthenticated caller is refused, that one tenant cannot see
another's data, and that a trace a customer contributed never surfaces to
another org through the Knowledge Base -- the Knowledge Base holds only
operator-curated content, never customer contributions.

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


async def _call(session, report: "Reporter", label: str, tool: str, args: dict):
    """Call an MCP tool and return its content, or record `label` as a
    failed check and return None if the call itself raises.

    A tool call can fail two structurally different ways. The server can
    answer with a shaped error (bad input, not_found, entitlement_exceeded)
    -- an ordinary CallToolResult that _content() reads into a dict with an
    "error" key, which the caller's own report.check() already handles. Or
    the call can never get a result at all: a 429 from the Hub's own rate
    limiter, a dropped connection, a protocol-level error -- the mcp client
    library RAISES for that case instead of returning anything. Without
    this wrapper, that second kind used to abort the whole run with an
    unhandled ExceptionGroup: every later check silently never ran, and the
    only visible output was an opaque traceback that names no property at
    all -- the "one line per check" promise this file's own module
    docstring makes, broken by construction on exactly the failure a
    deployment check exists to catch cleanly.

    Reproduced live, not hypothetically: running this file's own documented
    --other-api-key workflow against a real Hub under its default rate
    limits is, by itself, enough requests from one source address to
    exhaust HUB_AUTH_ATTEMPTS_BURST -- the smoke check's own traffic
    tripped its target's rate limiter and then crashed uninformatively
    reporting that fact.
    """
    try:
        return _content(await session.call_tool(tool, args))
    except Exception as exc:  # noqa: BLE001 - must become one [FAIL] line, never an uncaught crash
        report.fail(label, f"the call itself failed rather than returning a result -- {type(exc).__name__}: {exc}")
        return None


async def _initialize(session, report: "Reporter", label: str) -> bool:
    """session.initialize() is the first request a session makes, and can
    fail exactly the way any tool call can (a 429 from the Hub's own rate
    limiter included) -- but it is not a call_tool(), so _call() cannot
    wrap it. Same treatment: one clean [FAIL] line and a return value the
    caller can act on, rather than an unhandled exception aborting the run
    before a single check has even run."""
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
        """`detail` is what a PASS prints; `fail_detail`, when given, is what
        a FAIL prints instead.

        One shared string used to be printed either way, so a detail written
        to explain a failure was printed verbatim next to `[PASS]`. The worst
        of them was on the tenant-isolation check that matters most, which
        announced "the other org's commons_overlap returned our trace: {...}"
        on a run where nothing leaked -- an operator running this to gain
        confidence in a fresh deployment would reasonably conclude the
        opposite. A check that reports a passing result in the language of
        failure is worse than one that prints nothing.
        """
        if condition:
            self.ok(label, detail)
        else:
            self.fail(label, fail_detail or detail)
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
    if response.status_code == 429:
        # NOT the same as "reachable, key accepted": the auth-attempt
        # limiter in hub/server.py's ApiKeyAuthMiddleware runs BEFORE the
        # key is even parsed, so a 429 here says nothing about the key at
        # all -- it fires identically for a real key, a bogus one, or no
        # Authorization header. Falling through to `return None` (this
        # function's own "the key was accepted" contract) used to make
        # _rejects_bad_credentials read a rate-limited bogus-key probe as
        # "the server ACCEPTED a bogus key" -- a false, alarming security
        # failure for a check that never actually ran. Reproduced live:
        # this smoke check's own request volume (particularly the full
        # --other-api-key workflow) is enough to trip a tightly-configured
        # HUB_AUTH_ATTEMPTS_BURST by itself.
        return ("the server rate-limited this request (HTTP 429) before it could evaluate "
                "the API key -- inconclusive, not a rejection or an acceptance. This can be "
                "the smoke check's own request volume tripping HUB_AUTH_ATTEMPTS_BURST; wait "
                "a few seconds and re-run.")
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
    # self-service deletion (hub/crud.py) -- org-scoped like the six above,
    # unaffected by HUB_COMMONS_ENABLED
    "delete_trace", "request_account_deletion", "cancel_account_deletion",
    "confirm_account_deletion",
    # entitlements (hub/plans.py) -- unaffected by HUB_COMMONS_ENABLED,
    # since it reports an org's own plan and usage, never another org's data
    "account_usage",
    # outcome measurement (hub/outcomes.py) -- reads the caller's own
    # outcome history only, so it is org-scoped and unmetered like the six
    "fleet_outcomes",
    # randomized holdout (hub/crud.py) -- the causal instrument, org-scoped
    "holdout_assign", "record_occasion_outcome",
    # what that instrument was WORTH (commontrace/value.py) -- the causal
    # effect turned into a quantity a price can attach to, which STRATEGY.md
    # 11.5 names as this product's pricing basis. Org-scoped and unmetered.
    "value_delivered",
    # the graduated subset of that instrument (hub/crud.py:working_set) --
    # the memories whose effect is already established, rendered once per
    # session as a pinnable block instead of paid for on every query.
    # Org-scoped and unmetered like the rest of this list.
    "working_set",
    # collaboration on a trace (hub/collab.py) -- comments, assignment,
    # and a notification inbox for a customer's own team. Org-scoped and
    # unmetered like the rest of this list; unaffected by
    # HUB_COMMONS_ENABLED, which only toggles cross-org sharing.
    "add_comment", "list_comments", "assign_trace", "unassign_trace",
    "list_my_notifications", "mark_notification_read",
    # locating traces for a subject-erasure request (hub/crud.py:
    # search_trace_content) -- org-scoped and unmetered like the rest of
    # this list; unaffected by HUB_COMMONS_ENABLED.
    "search_trace_content",
    # structured subject tagging + exact-match find/purge (hub/crud.py:
    # tag_trace_subjects/find_traces_by_subject/purge_traces_by_subject) --
    # the other half of subject-erasure support, for content a curator
    # explicitly tagged. Org-scoped and unmetered like the rest of this
    # list; unaffected by HUB_COMMONS_ENABLED.
    "tag_trace_subjects", "find_traces_by_subject", "purge_traces_by_subject",
]
COMMONS_TOOLS = ["commons_overlap", "commons_search", "submit_kb_entry", "list_my_kb_submissions"]
EXPECTED_TOOLS = CORE_TOOLS + COMMONS_TOOLS  # kept for external callers/tests


async def _tool_surface(session, report: Reporter) -> bool:
    """Returns whether the commons tools are present, so later checks know
    whether to expect commons_overlap etc. to exist at all. False (commons
    treated as absent, the more conservative assumption) if list_tools()
    itself fails -- see _call()'s docstring for why that must be a clean
    [FAIL], not a crash, and _round_trip below still runs regardless: the
    tool surface and the write path are independent things to know about a
    deployment."""
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
    """contribute -> search -> get -> vote -> amend, the full write path."""
    created = await _call(session, report, "contribute_trace writes", "contribute_trace", {
        "title": f"smoke check {marker}",
        "context_text": f"Automated post-deploy smoke check {marker}. Safe to delete.",
        "solution_text": "No action required; this trace exists to prove the write path works.",
        "tags": [SMOKE_TAG],
        "agent_type": "custom",
    })
    if created is None:
        return None  # _call already recorded why
    trace_id = created.get("id") if isinstance(created, dict) else None
    if not report.check("contribute_trace writes", bool(trace_id), f"returned {created!r}"):
        return None

    found = await _call(session, report, "search_traces finds it", "search_traces", {"query": marker})
    if found is not None:
        ids = [t["id"] for t in found.get("traces", [])] if isinstance(found, dict) else []
        report.check("search_traces finds it", trace_id in ids,
                     f"searched for {marker!r}, got {len(ids)} result(s)")

    # The same trace, asked for the way an agent actually asks: a sentence,
    # in words that only PARTLY overlap what was stored. The single-token
    # search above passes under a conjunctive matcher and under a relaxed
    # one alike, so it cannot tell them apart -- and a deployment whose
    # query terms are ANDed returns nothing here while every other check on
    # this page stays green (hub/RETRIEVAL.md: 0.0% recall@1, 100%
    # zero-result, HTTP 200 throughout). This is the check that fails.
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

    # A retry with the same idempotency_key -- over the real deployed MCP
    # protocol, not just the crud.py layer the unit tests exercise --
    # simulating a client that timed out waiting for the first response and
    # tried again. Without a live check here, a regression in the tool
    # wrapper's parameter wiring (server.py, distinct from the crud.py logic
    # it calls) would ship invisibly: nothing else in this file calls
    # amend_trace twice with the same key. Skipped if the first amend_trace
    # call already failed -- there is nothing to retry.
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
    """The plan is only real if the server can state it.

    Checked post-deploy because an entitlement layer that fails open is
    invisible until the bill is wrong: every request still succeeds, so
    nothing looks broken. A misconfigured deployment that reports every org
    as unlimited passes every other check in this file.
    """
    usage = await _call(session, report, "account_usage responds", "account_usage", {})
    if usage is None:
        return  # _call already recorded why
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
                    continue  # _call already recorded why
                denied = isinstance(result, dict) and result.get("error") == "not_found"
                report.check(
                    label, denied,
                    # not_found rather than forbidden: a wrong answer here leaks
                    # that the id exists, which is itself a disclosure.
                    detail="refused with error=not_found, disclosing nothing about the id",
                    fail_detail=f"expected error=not_found, got {result!r}",
                )

            if not commons_enabled:
                # Nothing to probe: _tool_surface already proved these two
                # tools are entirely absent from the server, which is a
                # stronger guarantee than "refused when called" -- there is
                # no path left to check.
                return

            # The Knowledge Base holds only operator-curated content
            # (commons_source == "seed"), never a customer's own traces, so
            # a deployment check has to prove a customer-contributed trace
            # never surfaces there. Probing with a signature built from its
            # EXACT text -- the strongest possible probe -- must still find
            # nothing.
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

    # Clean up before reporting, and on failure as well as success: the
    # traces exist either way, and the run that failed is the one most
    # likely to be repeated.
    #
    # WHY THIS IS NOT OPTIONAL HOUSEKEEPING. Section 12 tells operators this
    # check is safe to run against production, which invites wiring it into
    # a deploy gate -- and every run permanently added two traces (the
    # original plus its amendment) to a real customer org. They are not
    # quarantined, so they come back in `search_traces` results for real
    # agent queries, they count against the org's plan storage, and they
    # inflate its trace counts. Measured on a deployment smoked a handful of
    # times: 12 of 12 traces in the org were this check's own residue. An
    # acceptance check that degrades the thing it certifies is a bad trade,
    # and "remove them with: python -m hub.manage purge-trace <id>" put that
    # work on a human, once per deploy, forever.
    #
    # delete_trace is the right instrument: it is org-scoped, it is reached
    # with the same key the check already holds, and it removes the whole
    # amendment chain -- so one call covers both traces.
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
    """Delete what this run wrote, and say so either way.

    A failure here is reported, never raised: the checks have already run
    and their verdict is what the caller came for. What must not happen is
    silence -- an operator who is not told cleanup failed has no reason to
    look, and the residue accumulates in a customer's corpus.
    """
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
    if "429" in joined or "rate limit" in joined or "too many requests" in joined:
        # Every _call() site already turns a rate-limited tool call into a
        # clean [FAIL] line rather than raising -- this branch is for
        # whatever isn't a tool call (session.initialize(), the transport's
        # own connect/close sequence). Distinguished from "the deployment is
        # broken": this check's OWN traffic can trip the Hub's default
        # per-source-address auth-attempt limit (HUB_AUTH_ATTEMPTS_BURST),
        # especially running the full --other-api-key workflow, which is
        # more requests from one address than a quiet production key
        # normally sends in a burst.
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
