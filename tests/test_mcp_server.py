"""The local tier over MCP -- the surface an agent with no terminal uses.

Everything here goes through the tool layer the way a client does
(`MCPServer.call_tool`), never by calling the inner functions directly: the
schema the SDK derives from each signature is part of the contract, and a test
that bypasses it would pass while the tool is uncallable.

`test_stdio_transport_end_to_end` goes one level further and spawns the real
`commontrace serve` subprocess, speaking MCP over its stdio. That one exists
for a failure mode nothing else here can catch: the command modules this
server reuses PRINT, and on stdio a single stray line on stdout corrupts the
JSON-RPC framing for the whole session.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest

from commontrace import mcp_server, paths

# The SDK is an optional extra (`pip install commontrace[serve]`) -- the base
# client installs with PyYAML alone -- so these skip rather than fail on an
# install that deliberately does not have it. `commontrace serve` says the same
# thing in a sentence when actually run; test_serve_without_the_sdk_says_so
# below covers that path, and does not need the SDK itself.
pytest.importorskip("mcp", reason="`commontrace serve` needs the MCP SDK: pip install 'commontrace[serve]'")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- helpers -------------------------------------------------------------

def _payload(result) -> dict:
    """The dict a tool returned, out of whichever envelope the SDK used."""
    if getattr(result, "structured_content", None):
        sc = result.structured_content
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


def call(server, name: str, **arguments) -> dict:
    return _payload(asyncio.run(server.call_tool(name, arguments)))


def tool_names(server) -> set[str]:
    return {t.name for t in asyncio.run(server.list_tools())}


def cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "commontrace.cli", *argv],
        capture_output=True, text=True, cwd=REPO_ROOT, check=False,
    )


@pytest.fixture
def store(tmp_path):
    root = str(tmp_path / "fleet")
    assert cli("init", "--dest", root, "--agent-type", "support").returncode == 0
    return root


@pytest.fixture
def server(store):
    return mcp_server.build_server(store)


def _capture_pattern(server, n: int = 3, **extra) -> list[str]:
    """n near-identical failures, which is what makes a distillable cluster."""
    ids = []
    for i in range(n):
        out = call(
            server, "capture",
            title=f"Password reset email never arrived #{i}",
            context_text="Customer requested a password reset; the reset email never "
                         "arrived and the customer was blocked from logging in.",
            solution_text="Checked the email suppression list; the address was suppressed "
                          "after a bounce. Removed the suppression and re-sent the email.",
            tags=["email", "password-reset"],
            occasion_id=f"ticket-{i}",
            **extra,
        )
        assert out["ok"], out
        ids.append(out["trace_id"])
    return ids


def _fill_in(server, slug: str) -> dict:
    return call(
        server, "draft_lesson", slug=slug,
        rule="When a customer reports a missing password-reset email, check the email "
             "suppression list before re-sending.",
        why="A bounced address is auto-suppressed, so every re-send silently no-ops.",
        how_to_apply="Look up the address in the suppression list; if present, remove the "
                     "suppression, then trigger the reset again.",
        counter_examples="Not when the customer received the email but the link expired.",
        applies_when="A customer reports not receiving a password-reset email.",
        do_not_apply_when="The customer received the email but the link failed.",
        description="Check the email suppression list before re-sending a reset.",
        importance=4,
        importance_rationale="Blocks login entirely and recurs weekly.",
    )


def _curate(server) -> str:
    """capture -> propose -> draft -> approve. Returns the now-active slug."""
    _capture_pattern(server)
    proposed = call(server, "propose_lessons")
    slug = proposed["candidates"][0]["slug"]
    _fill_in(server, slug)
    assert call(server, "approve_lesson", slug=slug)["ok"]
    return slug


# --- the surface itself --------------------------------------------------

def test_tool_surface_matches_the_advertised_list(server):
    # `commontrace install` writes LOCAL_TOOLS into the generated MCP config
    # without importing the SDK (the client package ships without it), so the
    # constant is the only thing keeping that file honest.
    assert tool_names(server) == set(mcp_server.LOCAL_TOOLS)


def test_the_names_live_in_a_dependency_free_module():
    """`install_cmd` reads these to write its config, and must stay importable
    where PyYAML is absent -- which is the Hub's own test environment.
    Importing `mcp_server` for a tuple of strings pulled in the whole
    retrieval stack and broke every hub-tests job at collection.

    Same object, not an equal copy, so the split cannot let the two drift.
    """
    from commontrace import mcp_tools

    assert mcp_server.LOCAL_TOOLS is mcp_tools.LOCAL_TOOLS
    assert mcp_server.APPROVAL_TOOLS is mcp_tools.APPROVAL_TOOLS


def test_no_approval_removes_the_tools_rather_than_refusing_them(store):
    names = tool_names(mcp_server.build_server(store, allow_approval=False))
    # Absent, not present-and-refusing: an agent plans against the tools it can
    # see, and one that is listed but always fails wastes a turn every time.
    assert not (names & set(mcp_server.APPROVAL_TOOLS))
    assert "retrieve" in names and "capture" in names


def test_every_tool_has_a_description_for_the_model(server):
    # The description IS the interface here -- it is all the agent gets.
    for tool in asyncio.run(server.list_tools()):
        assert tool.description and len(tool.description) > 80, tool.name


# --- retrieve ------------------------------------------------------------

def test_retrieve_on_an_empty_store_explains_itself(server):
    out = call(server, "retrieve", task="customer cannot reset their password")
    assert out["ok"] and out["lessons"] == [] and out["n_active"] == 0
    assert "no active lessons" in out["note"]


def test_retrieve_returns_the_body_not_just_the_frontmatter(server):
    _curate(server)
    out = call(server, "retrieve", task="customer says the password reset email never arrived")
    assert len(out["lessons"]) == 1
    lesson = out["lessons"][0]
    # Without the body the agent gets a title and no instruction.
    assert "suppression" in lesson["body"]
    assert lesson["score"] > 0 and lesson["matched"]


def test_a_candidate_under_review_is_not_retrievable(server):
    _capture_pattern(server)
    slug = call(server, "propose_lessons")["candidates"][0]["slug"]
    out = call(server, "retrieve", task="password reset email not received")
    assert out["lessons"] == [], "an unapproved candidate must never be injected"
    assert slug in {lesson["slug"] for lesson in call(server, "list_lessons")["lessons"]}


def test_retrieve_matches_the_cli_ranking(server, store):
    _curate(server)
    task = "password reset email never arrived"
    mcp_slugs = [lesson["slug"] for lesson in call(server, "retrieve", task=task)["lessons"]]
    result = cli("query", task, "--dest", store)
    assert result.returncode == 0, result.stderr
    # Same loader, same ranker: a fleet's shell-capable and shell-less agents
    # must not be reading different memory.
    for slug in mcp_slugs:
        assert slug in result.stdout


# --- the holdout ---------------------------------------------------------

def test_a_withheld_lesson_never_ships_its_body(server, store):
    """The control arm exists so the agent does NOT act on it -- shipping the
    instructional text anyway is pure cost (this can run to
    MAX_TEXT_CHARS-scale content, on every retrieve() call while an
    experiment runs) plus a small, avoidable priming risk. Metadata stays,
    so `withheld` is still informative about WHAT was suppressed.

    rate must be < 1.0 (holdout_io.configure's own range check), so this
    loops occasions -- same pattern as
    test_holdout_assigns_both_arms_and_logs_every_one below -- until the
    control arm actually shows up, rather than trying to force it."""
    from commontrace import holdout_io

    _curate(server)
    holdout_io.configure(store, rate=0.9)
    withheld_item = None
    for i in range(40):
        out = call(server, "retrieve", task="password reset email never arrived", occasion_id=f"occ-{i}")
        if out["withheld"]:
            withheld_item = out["withheld"][0]
            break
    assert withheld_item is not None, "40 occasions at rate=0.9 produced no withheld lesson"
    assert "body" not in withheld_item
    # Still informative -- just not usable.
    assert withheld_item["slug"] and withheld_item["description"]
    assert withheld_item["score"] > 0 and withheld_item["matched"]


def test_an_injected_lesson_still_ships_its_body(server, store):
    """The other half of the same property: NOT withholding must not
    accidentally start stripping bodies from lessons the agent is meant to
    use."""
    from commontrace import holdout_io

    _curate(server)
    holdout_io.configure(store, rate=0.0)  # deterministic: nothing withheld
    out = call(server, "retrieve", task="password reset email never arrived", occasion_id="occ-1")
    assert len(out["lessons"]) == 1
    assert "suppression" in out["lessons"][0]["body"]
    assert out["withheld"] == []


def test_holdout_assigns_both_arms_and_logs_every_one(server, store):
    _curate(server)
    arms = set()
    for i in range(40):
        out = call(server, "retrieve", task="password reset email", occasion_id=f"occ-{i}")
        arms.add(bool(out["withheld"]))
        assert "holdout_note" in out
    assert arms == {True, False}, "40 occasions produced only one arm"

    lines = [json.loads(x) for x in open(
        os.path.join(paths.memory_dir(store), "holdout_log.jsonl"), encoding="utf-8")]
    # Eligibility is what makes the later comparison causal, so EVERY matched
    # lesson is logged -- injected and withheld alike, not just the withheld.
    assert len(lines) == 40
    assert {line["injected"] for line in lines} == {True, False}


def test_the_same_occasion_always_gets_the_same_arm(server):
    _curate(server)
    first = call(server, "retrieve", task="password reset", occasion_id="occ-stable")
    again = call(server, "retrieve", task="password reset", occasion_id="occ-stable")
    # A retry that flipped arms would put one occasion in both, which is not a
    # randomized comparison any more.
    assert bool(first["withheld"]) == bool(again["withheld"])


def test_no_occasion_id_means_no_holdout_and_no_log(server, store):
    _curate(server)
    out = call(server, "retrieve", task="password reset")
    assert "withheld" not in out
    assert not os.path.exists(os.path.join(paths.memory_dir(store), "holdout_log.jsonl"))


def test_the_mcp_loop_alone_produces_a_measurable_experiment(server, store):
    """The whole point: an agent with no terminal can run the causal loop.

    Retrieve with an occasion id, act, capture the outcome under the SAME id --
    and `commontrace experiment` finds both halves and reports arms.
    """
    _curate(server)
    for i in range(60):
        occasion = f"case-{i}"
        out = call(server, "retrieve", task="password reset email missing", occasion_id=occasion)
        # A control-arm occasion is one where the agent worked without the
        # lesson, which is exactly the outcome we want it to record.
        resolved = not out["withheld"]
        assert call(
            server, "capture",
            title=f"Password reset case {i}",
            context_text="Customer reports the password reset email never arrived.",
            solution_text="Investigated and responded to the customer.",
            occasion_id=occasion, resolved=resolved,
        )["ok"]

    result = cli("experiment", "--dest", store)
    assert result.returncode == 0, result.stderr
    # The failure this guards against is the silent one: assignments logged,
    # outcomes captured, and nothing joining them -- which reads as a clean
    # "no data" rather than as a bug.
    assert "no recorded outcome" not in result.stdout.lower(), result.stdout
    # The count itself, not a substring of it -- "about 100 occasions" in the
    # power projection contains "0 occasion" and made the loose form pass or
    # fail for reasons unrelated to what it was checking.
    assert "Occasions analyzed: **0**" not in result.stdout, result.stdout
    assert "Occasions analyzed: **60**" in result.stdout, result.stdout
    # And the run has to be sound: an experiment this test drove end to end
    # with an outcome captured for every occasion must not report attrition.
    assert "**Sound.**" in result.stdout, result.stdout


def test_experiment_status_scopes_to_the_current_randomization(server, store):
    """The MCP tool must not pool an earlier randomization into the current
    report -- the same defect `commontrace experiment` (the CLI) was fixed
    for, in the entry scoping analysis to one salt. Changing the holdout rate
    rotates the salt on purpose (a re-randomization, not a bigger sample), so
    assignments made under the old salt are a different experiment and must
    not be counted here either -- pooling them would make a clean rate change
    read as a COMPROMISED experiment."""
    from commontrace import holdout_io

    _curate(server)
    for i in range(20):
        call(server, "retrieve", task="password reset email", occasion_id=f"old-{i}")

    holdout_io.configure(store, rate=0.5)

    out = call(server, "experiment_status")
    assert out["running"] is False
    assert "none under the current randomization" in out["note"]

    for i in range(20):
        occ = f"new-{i}"
        r = call(server, "retrieve", task="password reset email", occasion_id=occ)
        assert call(
            server, "capture",
            title=f"Password reset case {occ}",
            context_text="Customer reports the password reset email never arrived.",
            solution_text="Investigated and responded to the customer.",
            occasion_id=occ, resolved=not r["withheld"],
        )["ok"]

    out = call(server, "experiment_status")
    assert out["running"] is True
    assert out["n_assignments"] == 20, "pooled the old salt's assignments in"
    assert out["excluded_other_randomization"] == 20
    # The current salt's own rate (50%), not a blend with the old salt's
    # default 10% -- (20*0.1 + 20*0.5)/40 = 0.3 is what an unscoped average
    # over every logged assignment would report. `holdout_rate` used to be
    # read from `_load()`'s unscoped average, computed before the scoping
    # above ran, even though n_assignments/effects were already scoped.
    assert out["holdout_rate"] == 0.5, "blended the old salt's rate in"


# --- capture -------------------------------------------------------------

def test_capture_records_the_outcome_it_reports(server):
    _capture_pattern(server, n=1, resolved=True, tokens_used=1200)
    out = call(
        server, "capture", title="One more", context_text="c" * 40,
        solution_text="s" * 40, occasion_id="tick", escalated=False, llm_calls=3,
    )
    assert out["outcome"] == {"escalated": False, "llm_calls": 3}


def test_recapturing_an_occasion_merges_rather_than_replaces(server):
    call(server, "capture", title="Ticket", context_text="c" * 40,
         solution_text="s" * 40, occasion_id="same", tokens_used=900)
    out = call(server, "capture", title="Ticket", context_text="c" * 40,
               solution_text="s" * 40, occasion_id="same", resolved=True)
    # Reported from the trace on disk, not echoed from this call's arguments:
    # attaching an outcome later must not erase the earlier one.
    assert out["outcome"] == {"tokens_used": 900, "resolved": True}


def test_capture_without_an_occasion_id_says_it_cannot_be_measured(server):
    out = call(server, "capture", title="Untracked", context_text="c" * 40,
               solution_text="s" * 40, resolved=True)
    assert out["ok"] and "not toward the measured effect" in out["note"]


def test_capture_with_no_outcome_says_so(server):
    out = call(server, "capture", title="No outcome", context_text="c" * 40,
               solution_text="s" * 40)
    assert out["outcome"] == {} and "Nothing about this occasion can be measured" in out["note"]


def test_capture_refuses_an_invalid_trace_instead_of_writing_it(server, store):
    out = call(server, "capture", title="Bad", context_text="c" * 40,
               solution_text="s" * 40, tokens_used=-5)
    assert not out["ok"] and "invalid" in out["error"].lower()
    written = [n for n in os.listdir(paths.traces_dir(store)) if n.endswith(".md")
               and n != "README.md"]
    assert not written, written


def test_the_cli_can_read_what_the_mcp_server_wrote(server, store):
    _curate(server)
    assert cli("trace", "validate", "--dest", store).returncode == 0
    assert cli("lesson", "validate", "--dest", store).returncode == 0


# --- curation ------------------------------------------------------------

def test_propose_writes_candidates_the_rest_of_the_tooling_can_resolve(server, store):
    _capture_pattern(server)
    out = call(server, "propose_lessons")
    assert out["candidates"], out
    slug = out["candidates"][0]["slug"]
    # The candidate naming belongs to `commontrace distill`; a second copy of
    # it here produced files (`lesson_lesson_candidate_...`) that every other
    # command failed to find.
    assert os.path.isfile(os.path.join(paths.lessons_dir(store), f"{slug}.md"))
    assert call(server, "get_lesson", slug=slug)["ok"]
    assert out["candidates"][0]["unfilled"], "a fresh candidate is scaffolding"


def test_propose_on_an_empty_store_is_an_answer_not_an_error(server):
    out = call(server, "propose_lessons")
    assert out["ok"] and out["candidates"] == [] and out["note"]


def test_propose_rejects_a_degenerate_similarity_cleanly(server, store):
    """`similarity<=0` makes `distill.find_clusters` merge the WHOLE store
    into one cluster (a deliberate, documented library behavior --
    tests/test_distill.py exercises it directly) and then makes
    `representative()`'s O(k^2) medoid search run over that single giant
    cluster instead of the small near-duplicate groups it is sized for. An
    agent passing similarity=0 to this customer-facing tool should get a
    clean rejection, not an expensive scan and not a crash. This also
    covers a second, adjacent bug the fix for the first one exposed:
    argparse's `type=` validator calling sys.exit() on a bad value used to
    propagate a bare SystemExit out of `_run_cli` -- uncaught by every
    caller's `except Exception` -- instead of becoming this tool's normal
    {"ok": false} contract."""
    _capture_pattern(server)
    out = call(server, "propose_lessons", similarity=0)
    assert out["ok"] is False
    assert "similarity" in out["error"].lower()


def test_propose_rejects_a_similarity_above_one(server):
    out = call(server, "propose_lessons", similarity=1.5)
    assert out["ok"] is False
    assert "similarity" in out["error"].lower()


def test_draft_can_fill_a_lesson_in_over_several_calls(server):
    _capture_pattern(server)
    slug = call(server, "propose_lessons")["candidates"][0]["slug"]
    first = call(server, "draft_lesson", slug=slug, rule="Check the suppression list first.")
    assert first["ok"] and "Rule" in first["lesson"]["body"]
    second = call(server, "draft_lesson", slug=slug, importance=5)
    # Only what was passed changes: the rule from the first call survives.
    assert second["lesson"]["importance"] == 5
    assert "Check the suppression list first." in second["lesson"]["body"]


def test_draft_never_activates(server):
    _capture_pattern(server)
    slug = call(server, "propose_lessons")["candidates"][0]["slug"]
    assert _fill_in(server, slug)["lesson"]["status"] == "review"
    assert call(server, "retrieve", task="password reset email")["lessons"] == []


def test_draft_reports_what_is_still_scaffolding(server):
    _capture_pattern(server)
    slug = call(server, "propose_lessons")["candidates"][0]["slug"]
    partial = call(server, "draft_lesson", slug=slug, rule="Check the suppression list.")
    assert partial["lesson"]["unfilled"]
    assert "Still scaffolding" in partial["next_step"]
    assert not _fill_in(server, slug)["lesson"]["unfilled"]


def test_draft_ignores_fields_the_agent_does_not_own(server):
    _capture_pattern(server)
    slug = call(server, "propose_lessons")["candidates"][0]["slug"]
    # `uses` is retrieval telemetry the measurement layer reads; a caller that
    # could set it could manufacture the reuse numbers a customer is shown.
    before = call(server, "get_lesson", slug=slug)["lesson"]["uses"]
    call(server, "draft_lesson", slug=slug, description="legitimate")
    after = call(server, "get_lesson", slug=slug)["lesson"]
    assert after["uses"] == before and after["description"] == "legitimate"


# --- the approval gate ---------------------------------------------------

def test_approve_refuses_scaffolding_and_names_it(server):
    _capture_pattern(server)
    slug = call(server, "propose_lessons")["candidates"][0]["slug"]
    out = call(server, "approve_lesson", slug=slug)
    # An active lesson is fed to every later retrieval verbatim; activating one
    # whose rule is still "TODO:" teaches the fleet nothing AND is counted as
    # coverage by every report the customer reads.
    assert not out["ok"] and out["unfilled"]
    assert call(server, "get_lesson", slug=slug)["lesson"]["status"] == "review"


def test_approve_records_who_approved_it(server):
    _capture_pattern(server)
    slug = call(server, "propose_lessons")["candidates"][0]["slug"]
    _fill_in(server, slug)
    assert call(server, "approve_lesson", slug=slug, approved_by="support-agent-7",
                rationale="Re-read against all three traces.")["ok"]
    body = call(server, "get_lesson", slug=slug)["lesson"]["body"]
    # An agent may approve, but which judgement was applied stays auditable.
    assert "support-agent-7" in body and "Re-read against all three traces." in body


def test_approve_only_applies_to_a_candidate_under_review(server):
    slug = _curate(server)
    out = call(server, "approve_lesson", slug=slug)
    assert not out["ok"] and "not 'review'" in out["error"]


def test_reject_requires_a_reason(server):
    _capture_pattern(server)
    slug = call(server, "propose_lessons")["candidates"][0]["slug"]
    # A rejection with no reason gets the same candidate re-proposed forever.
    assert not call(server, "reject_lesson", slug=slug, reason="   ")["ok"]
    out = call(server, "reject_lesson", slug=slug, reason="Two of the three traces differ.")
    assert out["ok"] and out["status"] == "archived"
    assert "Two of the three traces differ." in call(server, "get_lesson", slug=slug)["lesson"]["body"]


# --- refusing bad input --------------------------------------------------

@pytest.mark.parametrize("slug", [
    "../../etc/passwd", "../secret", "a/b", "..", "", "lesson name",
])
def test_a_slug_cannot_escape_the_lessons_directory(server, slug):
    out = call(server, "get_lesson", slug=slug)
    assert not out["ok"], f"{slug!r} was accepted"
    assert "error" in out


def test_a_missing_lesson_is_reported_not_raised(server):
    out = call(server, "get_lesson", slug="lesson_does_not_exist")
    assert not out["ok"] and "no lesson found" in out["error"]


def test_an_unreadable_lesson_does_not_hide_the_readable_ones(server, store):
    slug = _curate(server)
    broken = os.path.join(paths.lessons_dir(store), "lesson_broken.md")
    with open(broken, "w", encoding="utf-8") as fh:
        fh.write("---\nthis: [is not: valid yaml\n---\nbody\n")
    listed = {lesson["slug"] for lesson in call(server, "list_lessons")["lessons"]}
    assert slug in listed
    assert call(server, "retrieve", task="password reset email")["ok"]


# --- status --------------------------------------------------------------

def test_status_reports_gaps_before_curation_and_none_after(server):
    _capture_pattern(server)
    before = call(server, "store_status")
    assert before["traces"] == 3 and before["gaps"] and before["patterns_covered"] == 0

    slug = call(server, "propose_lessons")["candidates"][0]["slug"]
    still = call(server, "store_status")
    # A candidate full of "TODO:" covers nothing. Counting it would report the
    # customer's remaining work as done.
    assert still["gaps"] == before["gaps"] and still["patterns_covered"] == 0
    assert still["lessons_by_status"] == {"review": 1}

    _fill_in(server, slug)
    call(server, "approve_lesson", slug=slug)
    after = call(server, "store_status")
    assert after["patterns_covered"] == 1 and after["gaps"] == []
    assert after["lessons_by_status"] == {"active": 1}


# --- the transport itself ------------------------------------------------

def test_stdio_transport_end_to_end(store):
    """Spawn `commontrace serve` and speak MCP to it, as a client does.

    The reused command modules print. On stdio one stray line on stdout
    corrupts the framing for the WHOLE session, and the symptom looks like a
    server crash triggered by something as ordinary as a malformed trace file.
    Nothing short of a real subprocess proves that cannot happen.
    """
    pytest.importorskip("mcp.client.stdio")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # A file the loaders warn about, so the noisy path is exercised on the wire.
    with open(os.path.join(paths.lessons_dir(store), "lesson_broken.md"), "w",
              encoding="utf-8") as fh:
        fh.write("---\nbad: [unclosed\n---\n")

    async def drive():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "commontrace.cli", "serve", "--dest", store],
            cwd=REPO_ROOT, env={**os.environ},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.server_info.name == "commontrace-local"
                names = {t.name for t in (await session.list_tools()).tools}
                assert names == set(mcp_server.LOCAL_TOOLS)

                captured = _payload(await session.call_tool("capture", {
                    "title": "Reset email missing",
                    "context_text": "Customer reports the reset email never arrived.",
                    "solution_text": "Removed the address from the suppression list.",
                    "occasion_id": "wire-1", "resolved": True,
                }))
                assert captured["ok"], captured
                status = _payload(await session.call_tool("store_status", {}))
                assert status["ok"] and status["traces"] == 1
                return True

    assert asyncio.run(drive())


def test_serve_resolves_the_store_before_the_client_connects(tmp_path):
    """A bad --dest must fail loudly at startup, not look like an empty store."""
    result = subprocess.run(
        [sys.executable, "-m", "commontrace.cli", "serve", "--dest",
         str(tmp_path / "nope")],
        capture_output=True, text=True, cwd=REPO_ROOT, input="", timeout=60, check=False,
    )
    # Whatever it does, it must not write MCP-looking frames to a caller that
    # never spoke: stdout is the wire.
    assert '"jsonrpc"' not in result.stdout


# --- what `commontrace install` advertises -------------------------------

class TestTheGeneratedLocalConfig:
    """`commontrace install` writes the MCP entry that attaches an agent to
    this store. It has to be correct WITHOUT importing the SDK: the client
    package installs with PyYAML alone, so install_cmd cannot enumerate the
    real server and reads mcp_server.LOCAL_TOOLS instead. That constant is
    checked against the built server by
    test_tool_surface_matches_the_advertised_list above; these check the file.
    """

    @staticmethod
    def _doc(root="/srv/fleet"):
        from commontrace.commands import install_cmd
        return json.loads(install_cmd._local_mcp_config(root))

    def test_it_is_valid_json_and_names_every_tool(self):
        doc = self._doc()
        for tool in mcp_server.LOCAL_TOOLS:
            assert tool in doc["_comment"], tool

    def test_it_launches_the_stdio_server_at_an_absolute_root(self):
        entry = self._doc()["mcpServers"]["commontrace-local"]
        assert entry["command"] == "commontrace"
        assert entry["args"] == ["serve", "--dest", "/srv/fleet"]
        # No url/headers: this tier has no endpoint and no credential, and a
        # config carrying either would imply a boundary that does not exist.
        assert "url" not in entry and "headers" not in entry

    def test_a_relative_root_would_resolve_somewhere_else(self, store):
        """The client launches the server with a working directory of its own
        choosing, so the config must carry the resolved root -- a relative one
        silently lands on a different (usually empty) store."""
        from commontrace.commands import install_cmd
        entry = json.loads(install_cmd._local_mcp_config(store))["mcpServers"]["commontrace-local"]
        assert os.path.isabs(entry["args"][-1])

    def test_install_writes_it_for_every_mcp_capable_target(self, tmp_path, store):
        from commontrace.commands import install_cmd

        for target in ("claude-code", "cursor", "windsurf", "generic-mcp"):
            dest = tmp_path / target
            dest.mkdir()
            os.environ["COMMONTRACE_ROOT"] = store
            try:
                import argparse
                assert install_cmd.run(argparse.Namespace(
                    target=target, dest=str(dest))) == 0
            finally:
                os.environ.pop("COMMONTRACE_ROOT", None)
            written = dest / "commontrace.local.mcp.json"
            # Without this file the agent gets the Hub entry only -- it can
            # search what other orgs published and cannot touch one lesson of
            # its own.
            assert written.is_file(), target
            assert json.loads(written.read_text())["mcpServers"]["commontrace-local"]


def test_serve_without_the_sdk_says_so(store, monkeypatch):
    """The base install has no MCP SDK, and this is the least legible place
    for a bare ImportError: an MCP client spawns the server as a subprocess
    and reports only that it exited -- indistinguishable from a crash."""
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.startswith("mcp"):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    with pytest.raises(mcp_server.LocalStoreError) as excinfo:
        mcp_server.build_server(store)
    assert "commontrace[serve]" in str(excinfo.value)
