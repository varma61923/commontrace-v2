"""The local tier, exposed to an agent over MCP (stdio).

WHY THIS EXISTS
---------------
Everything the Hub does has been callable by an agent since the Hub existed:
its whole tool surface, no shell required. The LOCAL tier never was. `query`,
`capture`, `distill`, `lesson approve`, `index` are argparse commands, so the
only agents that could use their own memory were the ones that happen to have
a terminal.

That is a smaller product than the one this repo describes. The README's
claim is "works with any agent fleet -- code, support, sales, HR, marketing",
and it was true only for the first: a support agent embedded in a product, a
sales agent inside a CRM, an ops agent in a runbook tool -- none of them can
shell out, so none of them could retrieve a lesson, record what happened, or
curate anything. They could talk to a remote Hub and nothing else.

This module closes that. Same eight steps of the protocol, same code paths as
the CLI (nothing here reimplements ranking, arm assignment, or the approval
guard -- it calls the same functions `commontrace query`/`lesson approve` do),
reached over stdio by any MCP-capable agent.

WHY STDIO AND NO AUTHENTICATION
-------------------------------
The client spawns this process and talks to it over its own stdin/stdout.
There is no port, no network listener, and nothing for another program on the
machine to connect to. The trust boundary is the OS: this server reads and
writes files under `memory/` with exactly the permissions of the agent that
launched it, which already had them. Adding a token here would protect
nothing and imply a boundary that does not exist.

That is the opposite of the Hub, which is multi-tenant, network-reachable,
and therefore authenticated on every call.

CAN AN AGENT APPROVE ITS OWN LESSON?
------------------------------------
Yes, and that is deliberate -- but the gate it passes through is real, not
removed. Activating a lesson changes what EVERY later retrieval injects, so
protocol/PROTOCOL.md puts a Validator between proposing and activating. The
important thing about that Validator is that it is a SECOND, INDEPENDENT
judgement, not that it is a human: this project's own reference profile is
Implementer A plus an independent Reviewer B.

So `approve_lesson` exists, and:

  - it enforces the same scaffolding refusal `commontrace lesson approve`
    does, so an agent cannot activate a lesson that is still "TODO:" -- the
    defect that used to let template text become a fleet-wide instruction;
  - it runs the same content-safety scan (commontrace/memory_guard.py), so a
    credential or a prompt-injection payload cannot reach `active`;
  - it records WHO approved it in the lesson body, so an agent-approved
    lesson is distinguishable from a human-approved one after the fact;
  - and `serve(allow_approval=False)` removes the tool entirely, for a
    deployment that requires a person. Absent, not merely refused.

BUT "SECOND JUDGEMENT" WAS OPTIONAL, AND NOW THE STORE DECIDES
--------------------------------------------------------------
Everything above is about WHAT is being activated. None of it stopped the
same actor drafting a lesson and approving it a second later, so the
independence the Validator role exists to supply was available rather than
required -- in exactly the case where it matters most, an agent curating its
own output unattended at machine speed. Recording an agent-approved lesson
as agent-approved makes that auditable; it does not make it reviewed.

commontrace/approval.py makes that a policy the store states
(`memory/approval-policy.yaml`): `mode: two-person` requires that the
approver is not among the lesson's recorded authors, and
`require_human: true` refuses an `mcp:` actor's approval outright. With no
policy file the behaviour above is unchanged, so an existing store sees
nothing new until someone opts in.
"""

from __future__ import annotations

import contextlib
import datetime
import glob
import io
import os
from typing import Any

from commontrace import (
    approval,
    cache_gate,
    dosage,
    evidence_io,
    frontmatter,
    harm,
    holdout_io,
    lesson_cache,
    lesson_io,
    mcp_tools,
    memory_guard,
    paths,
    receipts,
    recency,
    redundancy,
    rerank_arm,
    retrieval,
    retrieval_io,
    revision,
    store_state,
    taxonomy,
    templates,
    trace_io,
    validate,
)
from commontrace import evidence as evidence_mod
from commontrace.commands._format import read_or_warn
from commontrace.commands._traces import load_trace_candidates
from commontrace.commands._validators import REFUSE_CHARS, check_text_size

# The lesson fields an agent may set. Anything outside this set is ignored
# rather than written: `uses`, `last_hit` and `hub_trace_id` are maintained by
# the tooling that owns them, and letting a caller set them would corrupt the
# retrieval telemetry the measurement layer reads.
# Re-exported from commontrace.mcp_tools, which owns the one definition and
# imports nothing. `commontrace install` needs these names and must stay
# importable where PyYAML is absent -- importing this module to read a tuple
# of strings pulled in the entire retrieval stack and broke the Hub's test
# job, which installs no client dependencies at all.
LOCAL_TOOLS = mcp_tools.LOCAL_TOOLS
APPROVAL_TOOLS = mcp_tools.APPROVAL_TOOLS

_AGENT_WRITABLE = (
    "description", "domain", "tags", "importance", "importance_rationale",
    "applies_when", "do_not_apply_when", "source_traces",
)


class LocalStoreError(RuntimeError):
    """A request that the store itself refuses -- a bad slug, a missing
    lesson, scaffolding that must not be activated."""


def _ok(**payload: Any) -> dict:
    return {"ok": True, **payload}


def _err(message: str, **extra: Any) -> dict:
    """Errors are returned, not raised.

    An agent reads a tool result; it does not see a traceback. A structured
    `{"ok": false, "error": ...}` is something it can act on, and an
    exception escaping into the MCP framework is not.
    """
    return {"ok": False, "error": message, **extra}


# --- talking to the CLI without corrupting the transport -----------------
#
# On stdio, THIS PROCESS'S STDOUT IS THE MCP WIRE. Every JSON-RPC frame the
# client reads comes out of it, in order, with nothing else interleaved.
#
# The command modules below are the same ones `commontrace` runs from a
# terminal, and they print: `capture` prints the path it wrote, the loaders
# print a warning for an unreadable file. On a terminal that is helpful; here
# a single stray line lands in the middle of a frame and the client's parser
# fails on the whole session -- not on that one tool call. The failure looks
# like the server crashed, and it would be triggered by something as ordinary
# as one malformed trace file in the store.
#
# So nothing reaches stdout except through the MCP framing. Everything a
# command prints is captured: its stdout because we need the value (capture
# prints the path it wrote), its stderr so the reason for a refusal can be
# returned to the agent instead of vanishing.


@contextlib.contextmanager
def _quiet():
    """Run a block with stdout captured, so a CLI-style print cannot reach the wire."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


def _run_cli(command: str, argv: list[str]) -> tuple[int, str, str]:
    """Run one `commontrace` subcommand in-process and return (rc, stdout, stderr).

    In-process, through the real argparse parser, rather than reconstructing a
    Namespace by hand: the parser is the definition of what the command
    accepts, so an added flag or a renamed dest can never leave this surface
    silently passing something the command does not read. (It already had:
    `--frustration` binds to `frustration`, and a hand-built namespace spelled
    it `frustration_signal` -- accepted in silence, recorded nowhere.)
    """
    import argparse
    import importlib

    module = importlib.import_module(f"commontrace.commands.{command}_cmd")
    parser = argparse.ArgumentParser(prog="commontrace")
    subparsers = parser.add_subparsers(dest="command", required=True)
    module.add_parser(subparsers)

    out, err = io.StringIO(), io.StringIO()
    # parse_args itself, not just args.func(args), needs to be inside the
    # redirect AND inside the SystemExit guard: an argparse `type=` validator
    # that rejects its input (e.g. distill_cmd.py's --similarity-threshold
    # range check) makes argparse print a usage/error message and call
    # parser.exit() -> sys.exit(2), the same as the shell CLI's normal
    # invalid-argument path. SystemExit is a BaseException, not an Exception,
    # so it passed straight through every caller's `except Exception` here --
    # reproduced live: it printed the error to this PROCESS's real stderr
    # (parse_args ran outside the redirect) and then propagated out of the
    # MCP tool call entirely, rather than becoming this function's normal
    # (rc, out, err) contract every other failure already uses.
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            args = parser.parse_args([command, *argv])
            rc = args.func(args)
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 2
    return int(rc or 0), out.getvalue(), err.getvalue()


def _agent_actor(who: str = "") -> str:
    """Who to record in the revision journal for a change made over MCP.

    There is no authentication on this transport and none is claimed (see the
    module docstring): the label exists so a later reader can tell an agent's
    edit from a person's when asking what changed the instruction the fleet
    is following, not to prove anything about who ran it.
    """
    return f"mcp:{who}" if who else "mcp:agent"


def _coerce_tags(tags: object) -> list[str] | None:
    """A loosely-typed MCP client may send a single string instead of a list;
    iterating it char-by-char would corrupt tags to single letters (",".join
    on "mytag" -> "m,y,t,a,g"). Accept a bare string as one tag, mirroring
    distill_cmd._safe_tags' isinstance guard.

    Raises ValueError for anything else (an int, a dict, a bool, ...) rather
    than silently dropping it -- a caller whose tags are quietly discarded
    still gets back a normal-looking success response with no signal that
    its tags never landed.
    """
    if tags is None:
        return None
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, (list, tuple)):
        raise ValueError(f"tags must be a string or a list of strings, got {type(tags).__name__}")
    return [str(t) for t in tags]


def _sanitize_comment(text: str) -> str:
    """Approval/rejection notes are embedded in `<!-- ... -->`; an agent-
    controlled `-->` would break out of the comment and inject markdown/HTML
    into the lesson body that retrieval later injects verbatim."""
    return str(text).replace("-->", "--&gt;").replace("--", "—")


def _lesson_path(root: str, slug: str) -> str:
    from commontrace.commands.lesson_cmd import _SLUG_RE, _resolve_lesson_path

    if not _SLUG_RE.match(slug):
        raise LocalStoreError(
            f"invalid slug {slug!r}: letters, digits, '_' and '-' only (no path separators)"
        )
    path = _resolve_lesson_path(root, slug)
    if path is None:
        raise LocalStoreError(f"no lesson found for slug {slug!r}")
    return path


def _apply_dosage(matched, active, config):
    """Admit core lessons and enforce the budget, returning the wire items.

    Core lessons are loaded from the whole active set rather than from the
    ranked one, because the entire point of `core: true` is that the lesson
    is present whether or not it matched today's vocabulary. A core lesson
    that ALSO matched is not admitted twice -- it is already in `matched`,
    and is marked core there so it keeps its priority.

    Returns (admitted_items, core_items, dose) -- the dose is carried out so
    the caller can report the gauge and what was left out.
    """
    ranked_slugs = {item.get("slug") for item in matched}
    core_items: list[dict] = []
    for path, fm in active:
        if not dosage.is_core(fm):
            continue
        slug = str(fm.get("name", ""))
        if slug in ranked_slugs:
            continue
        try:
            fm_full, body = frontmatter.read(path)
        except Exception:  # noqa: BLE001 - an unreadable lesson is not injected
            continue
        item = _lesson_wire(fm_full, body, include_body=True)
        item["core"] = True
        core_items.append(item)

    # A core lesson that also matched keeps its core priority rather than
    # competing for a ranked slot, which is the whole point of the flag.
    core_slugs = {str(fm.get("name", "")) for _, fm in active if dosage.is_core(fm)}
    for item in matched:
        if item.get("slug") in core_slugs:
            item["core"] = True

    considered = [*core_items, *matched]
    by_slug = {item.get("slug", ""): item for item in considered}
    candidates = [
        dosage.Candidate(
            slug=item.get("slug", ""),
            # The real text, so the character budget is spent against what
            # the agent will actually be handed rather than an estimate.
            text=item.get("body") or "",
            core=bool(item.get("core", False)),
            importance=int(item.get("importance") or 0),
            revision=str(item.get("revision", "")),
            # What redundancy is judged on -- description + applies_when +
            # do_not_apply_when + body, same fields `commontrace consolidate`
            # and the authoring-time check compare, via `_lesson_wire`'s own
            # projection of the frontmatter rather than a second read of the
            # file. See commontrace/redundancy.py's module docstring for why
            # `tags`/`domain` are deliberately excluded.
            compare_text=redundancy.comparable_text(item, item.get("body") or ""),
        )
        for item in considered
    ]
    dose = dosage.select(
        candidates,
        dosage.Budget(
            max_lessons=config.max_lessons,
            max_chars=config.max_chars,
            redundancy_threshold=config.redundancy_threshold,
        ),
    )
    admitted = [by_slug[c.slug] for c in dose.admitted if c.slug in by_slug]
    kept_core = [item for item in admitted if item.get("core")]
    return admitted, kept_core, dose


def _record_receipt(root, occasion_id, task, active, injected, held, dose, config,
                    withdrawn=(), scorer=None):
    """One receipt for this retrieval. See commontrace/receipts.py."""
    from commontrace import release as release_mod

    visible = []
    for path, fm in active:
        slug = str(fm.get("name", ""))
        if not slug:
            continue
        visible.append(
            receipts.Visible(slug=slug, revision=lesson_io.current_revision(path) or "")
        )

    relevance_by_slug = {
        item.get("slug", ""): float(item.get("score", 0.0))
        for item in (*injected, *held)
    }
    admitted = tuple(
        receipts.Admitted(
            slug=item.get("slug", ""),
            revision=str(item.get("revision", "")),
            rank=position,
            relevance=relevance_by_slug.get(item.get("slug", ""), 0.0),
            core=bool(item.get("core", False)),
        )
        for position, item in enumerate(injected, start=1)
    )
    # The withheld-for-the-experiment set and the didn't-fit-the-budget set
    # are both "not injected" and are NOT the same fact: one is a control
    # arm, the other is a capacity limit. Labelled distinctly so a later
    # reader is not left to guess which.
    withheld = tuple(
        [(item.get("slug", ""), "control arm (holdout)") for item in held]
        + [(d.slug, d.reason) for d in dose.dropped]
        + [(slug, "measured harm (withdrawn)") for slug in sorted(withdrawn)]
    )
    receipts.record(root, receipts.Receipt(
        occasion_id=occasion_id,
        at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        visible=tuple(visible),
        admitted=admitted,
        withheld=withheld,
        query=task,
        scorer=scorer or config.scorer,
        floor=config.floor,
        chars_used=dose.chars_used,
        max_chars=dose.budget.max_chars,
        max_lessons=dose.budget.max_lessons,
        release_id=(release_mod.current_id(root) or ""),
    ))


def _lesson_wire(fm: dict, body: str = "", *, include_body: bool = False) -> dict:
    out = {
        "slug": fm.get("name"),
        "description": fm.get("description"),
        "status": fm.get("status"),
        "domain": fm.get("domain"),
        "agent_type": fm.get("agent_type"),
        "tags": list(fm.get("tags") or []),
        "importance": fm.get("importance"),
        "applies_when": fm.get("applies_when"),
        "do_not_apply_when": fm.get("do_not_apply_when"),
        "uses": fm.get("uses"),
        "source_traces": list(fm.get("source_traces") or []),
        # Surfaced on every projection: an agent deciding whether to inject a
        # lesson, or whether to finish writing one, needs to know it is still
        # scaffolding. Leaving it implicit is how template text reached
        # production in the first place.
        "unfilled": templates.unfilled_placeholders(fm, body),
        # Content identity of exactly this text. An experiment measuring this
        # lesson is measuring THIS revision; report it back with the outcome
        # if you keep your own records, and expect the effect estimate to be
        # about it rather than about the slug.
        "revision": revision.revision_of(fm, body),
    }
    if include_body:
        out["body"] = body
    return out


def _no_active_lessons_note(root: str) -> str:
    """Which of the three "no active lessons" states this store is in.

    All three used to get "`capture` your work, then `propose_lessons` once
    a pattern repeats", which is right for an empty store and actively
    misleading for the other two. An operator whose lessons are all sitting
    at status=review has already captured and already proposed; what they
    need is `approve`, and telling them to capture more sends them round a
    loop that cannot terminate.

    Phrased for an agent reading a JSON field rather than a terminal, so it
    names MCP tools and CLI commands as the reader can actually reach them.
    """
    state = store_state.inspect(root)
    if state.review:
        plural = "s" if state.review != 1 else ""
        slugs = ", ".join(state.review_slugs[:3])
        return (
            f"This store has {state.review} lesson{plural} at status=review and none active. "
            "Retrieval only returns ACTIVE lessons, and promotion is a deliberate human gate "
            "-- an unreviewed rule injected into every future run is how memory starts doing "
            f"harm. An operator approves with `commontrace lesson approve <slug>` ({slugs})."
        )
    if state.traces:
        plural = "s" if state.traces != 1 else ""
        return (
            f"This store has {state.traces} trace{plural} but no lessons yet. Traces are raw "
            "experience; retrieval ranks the curated rules distilled from them. Call "
            "`propose_lessons` once a pattern repeats (it needs >= 2 similar traces), or an "
            "operator can write one directly with `commontrace lesson new`."
        )
    return (
        "This store is empty -- nothing has been captured yet. `capture` your work, then "
        "`propose_lessons` once a pattern repeats."
    )


def build_server(root: str, *, allow_approval: bool = True):
    """Build the MCP server for the store at `root`. See module docstring."""
    try:
        from mcp.server.mcpserver import MCPServer
    except ModuleNotFoundError as exc:  # pragma: no cover - environment-dependent
        # The client package installs with PyYAML alone (pyproject.toml), so
        # the SDK is genuinely absent on a default install. Raised as a
        # sentence with the fix in it rather than as a bare ImportError from
        # inside a subprocess an MCP client spawned: there, the traceback is
        # not shown to anyone -- the client reports only that the server
        # exited, which is indistinguishable from a crash.
        raise LocalStoreError(
            "`commontrace serve` needs the MCP SDK, which the base install does "
            "not include. Install it with:  pip install 'commontrace[serve]'"
        ) from exc

    from commontrace import __version__

    mcp = MCPServer(
        name="commontrace-local",
        version=__version__,
        instructions=(
            "Your own fleet's memory, on this machine. Retrieve before you act "
            "(`retrieve`), record what happened afterwards (`capture`), and let "
            "repeated failures become lessons (`propose_lessons` -> `draft_lesson` "
            "-> `approve_lesson`). Nothing here leaves this machine; a Hub, if one "
            "is configured, is a separate server. "
            "Retrieval is the step that pays for the rest: call it with the task "
            "in your own words, not with keywords."
        ),
    )

    @mcp.tool()
    async def retrieve(
        task: str, top_k: int = 5, occasion_id: str = "", agent_type: str = "",
        exclude_shown: str = "",
    ) -> dict:
        """Find the lessons that apply to the task you are about to attempt.

        Describe the task in your own words, in a full sentence -- that is the
        query shape this ranks for. Terms are weighted and OR-ed, so a lesson
        matching part of your description still comes back.

        Returns `lessons` (inject these, highest importance x match first) and,
        when `occasion_id` is given AND a holdout is configured, `withheld` --
        lessons that matched but which you must NOT use on this occasion.
        Honouring that is the whole experiment: using a withheld lesson anyway
        does not fail loudly, it silently biases the measured effect toward
        zero. Report the result afterwards with `capture(occasion_id=...)`.

        Once the store has holdout data, each lesson also carries `evidence`:
        its measured `verdict` (HELPS / HURTS / NO_MEASURABLE_EFFECT /
        UNDERPOWERED / NOT_MEASURED) with `effect` and `ci_95`. Prefer HELPS,
        and treat HURTS as a lesson that made outcomes worse. No numbers are
        shown while the experiment is compromised.

        `exclude_shown`, if given an occasion id, skips any (non-core)
        lesson already logged as injected for that occasion in a prior
        `--experiment`-mode call -- for a long multi-turn task that calls
        this more than once and should not see the same guidance every
        turn. A prior call for that occasion made WITHOUT a holdout
        configured left no record, so nothing is excluded for it.

        Only `status: active` lessons are retrievable. A lesson still being
        drafted is invisible here by design.

        When the store has set `commontrace retrieval --fusion rrf` and its
        semantic index is current, lessons are ranked by keyword AND meaning
        together (the same fused ranking `commontrace query` gives); each
        lesson's `score` is then its fused rank score. Otherwise ranking is
        by keyword, and `fusion_note` says why if fusion was configured.

        A turn whose entire content is an acknowledgement -- "ok", "thanks",
        "go ahead" -- is answered immediately with `skipped: true` and no
        ranking pass, because there is nothing in it for a lesson to match.
        """
        # Before the store is read, and before any arm is assigned. Both halves
        # of that ordering matter: the saving is the corpus parse this skips,
        # and the safety is that a skipped turn never becomes an occasion, so
        # nothing is filtered after its arm is known (commontrace/cache_gate.py).
        if cache_gate.is_trivial_prompt(task):
            return _ok(
                lessons=[], n_active=0, occasion_id=occasion_id or None,
                skipped=True,
                note=(
                    "Nothing was retrieved: this turn carries no task to match a "
                    "lesson against, so no corpus was read and no holdout arm was "
                    "assigned -- it is not an occasion. Describe the work you are "
                    "about to attempt, in a full sentence, to retrieve against it."
                ),
            )
        try:
            # The SAME loader and ranker `commontrace query` uses, on the same
            # (path, frontmatter) shape -- not a parallel implementation. If
            # the two surfaces ranked differently, a fleet's shell-capable and
            # shell-less agents would be reading different memory.
            # Lexical always runs; the semantic arm is fused in below only
            # when the store configured fusion AND its index is fresh -- the
            # same rule `commontrace query` applies, via the same function
            # (commontrace/semantic_arm.py). Lexical reads the lesson files
            # as they are right now and cannot go stale, which is why it is
            # the fallback on both surfaces.
            # `load_active_with_terms`, not `query_cmd._iter_active_lessons`
            # directly, so this long-lived server benefits from the same
            # incremental cache the CLI does -- re-tokenizing only the lesson
            # files that changed since the LAST `retrieve` call, not the
            # whole store on every one. This is where that matters most: a
            # one-shot CLI process pays the parse once regardless; this
            # process answers many `retrieve` calls without exiting.
            with _quiet():
                active, term_cache = lesson_cache.load_active_with_terms(
                    root, agent_type or None,
                    reader=lambda p: read_or_warn(frontmatter.read, p),
                )
            # Never drops a `core: true` lesson (see
            # commontrace/dosage.py's module docstring) -- core is the
            # fleet's unconditional position, present every call by design.
            already_shown: set[str] = set()
            if exclude_shown:
                already_shown = holdout_io.injected_slugs_for_occasion(root, exclude_shown)
                if already_shown:
                    active = [
                        (p, fm) for p, fm in active
                        if dosage.is_core(fm) or str(fm.get("name", "")) not in already_shown
                    ]
            # The store's own retrieval settings, for the same reason the
            # holdout config below is read from the store rather than
            # hardcoded here: scorer and floor decide which lessons are
            # ELIGIBLE, so this surface disagreeing with `commontrace query`
            # would put two different treatments in one experiment.
            retrieval_config = retrieval_io.load_config(root)
            # None (not computed at all) unless this store opted in --
            # `evidence_io.reliability_snapshot` globs episodes/traces and
            # `recency.recency_lookup` parses every lesson's `last_hit`, and
            # a store that has not set either weight should not pay either
            # cost on a surface a long-lived server answers many calls on.
            reliability_lookup = (
                evidence_io.reliability_snapshot(root)
                if retrieval_config.reliability_weight > 0 else None
            )
            recency_lookup = (
                recency.recency_lookup(active)
                if retrieval_config.recency_weight > 0 else None
            )
            # Lessons this store's experiment measured making outcomes worse,
            # if it withdraws them (commontrace/harm.py). Ranked WITH the rest
            # and removed afterwards, over-fetching by their number, so every
            # other lesson's relevance and the slot a withdrawn one vacates
            # are exactly what they would be if it did not exist.
            harmful = evidence_mod.withdrawn(root, retrieval_config.harm_policy)
            want = max(1, min(int(top_k), 50))
            # A reranking store hands the reranker a deeper pool than the
            # page (commontrace/rerank_arm.py), and only if it can actually
            # rerank: otherwise it ranks for the page, as if it had not asked.
            rerank_skipped = ""
            if retrieval_config.rerank != retrieval_io.RERANK_NONE:
                with _quiet():
                    rerank_skipped = rerank_arm.ready(retrieval_config.rerank)
            reranking = retrieval_config.rerank != retrieval_io.RERANK_NONE and not rerank_skipped
            depth = rerank_arm.pool_size(want) if reranking else want
            # Gated fusion ranks below the floor too: such a lesson can still
            # reach the page if the reranker vouches for it (`floor_cleared`
            # below keeps the ones that need no vouching).
            gated = retrieval_config.fusion == retrieval_io.FUSION_GATED and reranking
            ranked = retrieval.rank_lessons(
                task, active,
                top_k=depth + len(harmful),
                floor=0.0 if gated else retrieval_config.floor,
                scorer=retrieval_config.scorer,
                term_cache=term_cache,
                reliability_lookup=reliability_lookup,
                reliability_weight=retrieval_config.reliability_weight,
                recency_lookup=recency_lookup,
                recency_weight=retrieval_config.recency_weight,
            )
        except Exception as exc:  # noqa: BLE001 - a malformed store is an answer, not a crash
            return _err(f"could not read the lesson store: {type(exc).__name__}: {exc}")

        core_slugs = {str(fm.get("name", "")) for _, fm in active if dosage.is_core(fm)}
        ranked, withdrawn_ranked = harm.split(ranked, harmful, core_slugs, depth)
        withdrawn_order = [r.slug for r in withdrawn_ranked]
        floor_cleared = {
            r.slug for r in ranked + withdrawn_ranked if r.relevance >= retrieval_config.floor
        }

        # The semantic arm, fused with the lexical one by rank, when the store
        # configured fusion -- the same function `commontrace query` runs in
        # a subprocess, held in memory here (commontrace/semantic_arm.py), and
        # the same steps as its `_run_hybrid`: the same index-freshness gate
        # (a stale or empty index falls back to lexical, as there), the same
        # over-fetch and harm split, the same exclude_shown filter on this
        # arm's output, the same RRF constant. A surface that fused
        # differently would be a second treatment in the same experiment.
        fused: list[tuple[str, float]] | None = None
        fusion_skipped = ""
        if retrieval_config.fusion == retrieval_io.FUSION_GATED and not gated:
            fusion_skipped = (
                "gated fusion admits semantic candidates only on the reranker's word, "
                f"and the reranker did not run: {rerank_skipped or 'rerank is off'}"
            )
        if retrieval_config.fusion == retrieval_io.FUSION_RRF or gated:
            from commontrace import semantic_arm

            if not semantic_arm.available():
                fusion_skipped = (
                    "the semantic arm needs the attention extra "
                    "(`pip install commontrace[attention]`)"
                )
            else:
                # Refreshed first if stale (commontrace/semantic_arm.py): a
                # stale index used to send the store back to lexical here.
                with _quiet():
                    fusion_skipped = semantic_arm.ensure_fresh(root)
            if not fusion_skipped:
                rc, semantic, _warnings = semantic_arm.ranked_slugs(
                    root, task, depth + len(harmful), agent_type or None,
                )
                if rc != 0:
                    fusion_skipped = "the semantic arm failed: " + "; ".join(_warnings)
                else:
                    semantic, withdrawn_semantic = harm.split(
                        semantic, harmful, core_slugs, depth, slug_of=lambda s: s,
                    )
                    if already_shown:
                        semantic = [
                            s for s in semantic if s in core_slugs or s not in already_shown
                        ]
                    if gated:
                        # The pool, not a ranking: the reranker orders it and
                        # the gate decides what may be on the page.
                        fused = [
                            (slug, 0.0)
                            for slug in dict.fromkeys([r.slug for r in ranked] + semantic)
                        ]
                    else:
                        fused = retrieval.reciprocal_rank_fusion(
                            {"lexical": [r.slug for r in ranked], "semantic": semantic},
                            k=retrieval_config.rrf_k, top_k=depth,
                        )
                    withdrawn_order = list(dict.fromkeys(withdrawn_order + withdrawn_semantic))
        if gated and fused is None:
            # The semantic arm did not run, so this is plain reranked lexical
            # retrieval and is labelled as such: the floor applies, as there.
            ranked = [r for r in ranked if r.slug in floor_cleared]
            withdrawn_order = [s for s in withdrawn_order if s in floor_cleared]

        # Second stage: the reranker reorders the pool and keeps the page
        # (commontrace/rerank_arm.py). Same step, same order, as
        # `commontrace query`'s _rerank_pool.
        path_by_slug = {str(fm.get("name", "")): path for path, fm in active}
        first_stage = fused if fused is not None else [(r.slug, r.relevance) for r in ranked]
        reranked: list[tuple[str, float]] | None = None
        if reranking:
            try:
                reranked, withdrawn_order = rerank_arm.rerank(
                    task, [slug for slug, _ in first_stage],
                    rerank_arm.texts(
                        [slug for slug, _ in first_stage] + withdrawn_order,
                        path_by_slug, frontmatter.read,
                    ),
                    want, withdrawn=withdrawn_order, mode=retrieval_config.rerank,
                    admit=(
                        rerank_arm.admit_gated(floor_cleared, retrieval_config.rerank)
                        if gated and fused is not None else None
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - a failed rerank serves the first stage
                rerank_skipped = f"the reranker failed: {type(exc).__name__}: {exc}"
        if reranking and reranked is None:
            # The model loaded (rerank_arm.ready) and then failed to score --
            # out of memory, in practice. Serve the pool's head rather than
            # nothing. Withdrawn lessons found anywhere in the pool stay
            # named: over-naming a lesson measured to hurt is the safe side.
            if gated:
                # Unvetted semantic and below-floor candidates never reach
                # the page: serve the floor-cleared lexical head, labelled so.
                ranked = [r for r in ranked if r.slug in floor_cleared]
                fused = None
            ranked = ranked[:want]
            if fused is not None:
                fused = fused[:want]

        description_of = {str(fm.get("name", "")): str(fm.get("description", "")) for _, fm in active}
        withdrawn_slugs = set(withdrawn_order)
        withdrawn_items = [
            {"slug": slug, "description": description_of.get(slug, ""), "reason": harm.REASON}
            for slug in withdrawn_order
        ]

        # The label every assignment records, and the relevance beside it:
        # what actually ranked this retrieval.
        eligibility_label = retrieval_io.rerank_label(
            retrieval_config.eligibility_label_for(fused=fused is not None),
            retrieval_config.rerank if reranked is not None else retrieval_io.RERANK_NONE,
        )
        if reranked is not None or fused is not None:
            page = reranked if reranked is not None else fused
            lexical_by_slug = {r.slug: r for r in ranked}
            relevance_by_slug = dict(page)
            to_read = [
                (slug, path_by_slug[slug], score) for slug, score in page if slug in path_by_slug
            ]
        else:
            lexical_by_slug = {r.slug: r for r in ranked}
            relevance_by_slug = {r.slug: r.relevance for r in ranked}
            to_read = [(r.slug, r.path, r.relevance) for r in ranked]

        matched_items = []
        for slug, path, relevance in to_read:
            # Re-read for the BODY. `_iter_active_lessons` returns frontmatter
            # only, and the body is where the rule actually is -- returning a
            # lesson without it would hand the agent a title and no
            # instruction. Only the top-k are re-read, not the whole store.
            try:
                fm, body = frontmatter.read(path)
            except Exception:  # noqa: BLE001
                continue
            item = _lesson_wire(fm, body, include_body=True)
            lexical_hit = lexical_by_slug.get(slug)
            if fused is None and reranked is None:
                item["score"] = round(lexical_hit.score, 3)
            else:
                # The fused or reranked score: what decided this position.
                item["score"] = round(relevance, 4)
            item["matched"] = list(lexical_hit.matched_terms or []) if lexical_hit else []
            item["_relevance"] = relevance
            matched_items.append(item)

        # ALWAYS-ON lessons, and the budget everything is admitted against
        # (commontrace/dosage.py). `top_k` bounds the COUNT and says nothing
        # about the size, so ten terse lessons and ten pages of prose were
        # the same budget -- and a lesson that is the fleet's position
        # rather than a match for today's task had no way to be reliably
        # present except by matching everything, which is the same as making
        # retrieval worse.
        #
        # BEFORE the arms are assigned, and that ordering is the whole point.
        # A lesson the budget crowds out is never administered. Assigning it
        # an arm first would log it as TREATED on an occasion it was never
        # present for, and an occasion counted as treated where no memory was
        # injected pulls the measured effect toward zero -- silently, and
        # worse the tighter the budget is. Only lessons that will actually be
        # handed over are eligible to be randomized.
        admitted_items, core_items, dose = _apply_dosage(
            matched_items, active, retrieval_config
        )

        withheld: set[str] = set()
        # The STORE's settings, not this module's constants. Hardcoding them
        # here meant an agent-driven fleet could not change its holdout rate
        # at all -- the product could compute exactly what rate a pilot needed
        # and then offer its AI-first half no way to set it -- and it let this
        # surface silently disagree with `commontrace query`, which pools two
        # randomizations into one comparison.
        config = holdout_io.load_config(root)
        # Core lessons are excluded from randomization: they are unconditional
        # by definition, so withholding one contradicts the flag. They are
        # also constant across both arms, which is exactly why they cannot
        # confound the comparison -- every occasion gets them.
        eligible = [
            item["slug"] for item in admitted_items
            if item.get("slug") and not item.get("core")
        ]
        if occasion_id and eligible and config.running:
            try:
                withheld = holdout_io.assign_and_log(
                    root, eligible,
                    occasion_id=occasion_id,
                    rate=config.rate,
                    salt=config.salt,
                    # Same evidence `commontrace query` records. Omitting it
                    # here would make an agent-driven fleet's log unauditable
                    # by exactly the checks a shell-driven one gets.
                    relevance=relevance_by_slug,
                    scorer=eligibility_label,
                    floor=retrieval_config.floor,
                )
            except Exception as exc:  # noqa: BLE001
                # An assignment that could not be LOGGED must not be acted on:
                # honouring an unrecorded holdout withholds a lesson from the
                # agent and leaves no record that it was withheld, which is the
                # one failure that corrupts the causal number silently.
                return _err(
                    "could not record the holdout assignment, so no lesson was withheld: "
                    f"{type(exc).__name__}: {exc}"
                )

        injected, held = [], []
        for item in admitted_items:
            item.pop("_relevance", None)
            if item.get("slug") not in withheld:
                injected.append(item)
                continue
            # A withheld lesson is the control arm: the agent is told never
            # to act on it, so its BODY -- the actual instructional text --
            # has no legitimate use once it crosses the wire, only cost
            # (this can run to MAX_TEXT_CHARS-scale content, on EVERY
            # retrieve() call while an experiment is running -- exactly the
            # calls a customer rigorously proving this product's causal
            # claim makes most of) and a small, avoidable priming risk: an
            # agent that has read the rule anyway is not the same
            # experiment as one that has not. Everything except the body
            # still ships, so `withheld` stays informative about WHAT was
            # suppressed, just not usable. Matches `commontrace query`'s own
            # CLI behavior, which has never printed a withheld lesson's body.
            #
            # The slot it vacates is NOT backfilled with the next-ranked
            # lesson. Substituting one would make the control arm "a
            # different lesson" rather than "no lesson", and the contrast
            # this experiment reports would no longer be the one it claims.
            item.pop("body", None)
            held.append(item)

        result = {
            "lessons": injected,
            "n_active": len(active),
            "occasion_id": occasion_id or None,
            "budget": dose.gauge(),
        }
        if retrieval_config.fusion != retrieval_io.FUSION_NONE and fused is None:
            # Configured but not run -- said out loud, because the store asked
            # for a DIFFERENT eligibility rule. The assignment records the
            # lexical label, which is what actually ran, so integrity.
            # check_scorer_drift sees the mix; `commontrace query` falls back
            # the same way, for the same reasons.
            result["fusion_note"] = (
                f"this store configures fusion={retrieval_config.fusion!r}, but this "
                f"retrieval was lexical: {fusion_skipped or 'fusion did not run'}. "
                f"The holdout assignment records {eligibility_label!r} "
                "accordingly."
            )
        if retrieval_config.rerank != retrieval_io.RERANK_NONE and reranked is None:
            # Same posture as fusion_note: asked for, not run, said so.
            result["rerank_note"] = (
                f"this store configures rerank={retrieval_config.rerank!r}, but this "
                f"retrieval kept the first stage's order: {rerank_skipped or 'the reranker did not run'}. "
                f"The holdout assignment records {eligibility_label!r} accordingly."
            )
        if core_items:
            result["core"] = [item["slug"] for item in core_items if item.get("slug")]
        if dose.dropped:
            # Named, never silent: an agent given nine of ten lessons and
            # told it was given ten acts on the missing one's absence as
            # though it were the fleet's position.
            result["not_injected"] = [
                {"slug": d.slug, "reason": d.reason} for d in dose.dropped
            ]
        if dose.noted:
            # A core lesson duplicates another admitted lesson. Never
            # suppressed (see commontrace/dosage.py's module docstring), so
            # both are still in `lessons` -- this is a configuration signal
            # for `commontrace consolidate`, not a thing that happened to
            # this occasion.
            result["core_redundancy"] = [
                {"slug": d.slug, "reason": d.reason} for d in dose.noted
            ]
        if withdrawn_items:
            # Named, never silent -- the same rule as `not_injected`. No body:
            # like a withheld lesson, it is here to say what was not handed
            # over and why, not to be used.
            result["withdrawn"] = withdrawn_items
            result["withdrawn_note"] = harm.note(len(withdrawn_items))
        if occasion_id:
            result["withheld"] = held
            result["holdout_rate"] = config.rate if config.running else 0.0
            result["holdout_note"] = (
                "Lessons under `withheld` matched but are the control arm for this "
                "occasion. Do not use them. Report the outcome with capture(occasion_id=...)."
                if config.running else
                "No holdout is configured for this store, so nothing is withheld and "
                "nothing causal can be measured. An operator starts one with "
                "`commontrace experiment --configure --rate <r>`."
            )
        if not injected and not held and not withdrawn_items:
            result["note"] = (
                "No active lesson matched. That is a real answer -- proceed on your own "
                "judgement, then `capture` what happened so the gap can become a lesson."
                if active else
                _no_active_lessons_note(root)
            )

        # The receipt: what was VISIBLE, what was admitted, and why the rest
        # was not (commontrace/receipts.py). The holdout log records
        # eligibility and arm, which starts one step too late -- a lesson
        # that was never a candidate does not appear in it at all, so "the
        # memory did not help" and "the memory was never offered" are
        # indistinguishable afterwards, and they have opposite remedies.
        #
        # Failure here must not fail the retrieval: unlike a holdout
        # assignment (which CHANGES what the agent is given, so an unlogged
        # one corrupts the experiment silently), a receipt only records what
        # already happened. Losing one costs an audit trail entry; refusing
        # to serve a lesson over it costs the fleet its memory.
        # Each returned lesson's measured causal verdict, as the Hub's search
        # carries it (commontrace/evidence.py). Failure must not fail the
        # retrieval, for the same reason as the receipt below: it describes
        # what was retrieved rather than changing it.
        try:
            with _quiet():
                evidence_mod.attach(root, result, "lessons", "withheld", "withdrawn")
        except Exception as exc:  # noqa: BLE001
            result["evidence_error"] = f"{type(exc).__name__}: {exc}"

        if occasion_id:
            try:
                _record_receipt(
                    root, occasion_id, task, active, injected, held, dose,
                    retrieval_config, withdrawn=withdrawn_slugs, scorer=eligibility_label,
                )
            except Exception as exc:  # noqa: BLE001
                result["receipt_error"] = (
                    f"the retrieval happened but was not recorded: "
                    f"{type(exc).__name__}: {exc}"
                )
        return _ok(**result)

    @mcp.tool()
    async def capture(
        title: str,
        context_text: str,
        solution_text: str,
        tags: list[str] | None = None,
        agent_type: str = "",
        agent_id: str = "",
        occasion_id: str = "",
        resolved: bool | None = None,
        escalated: bool | None = None,
        repeated_error: bool | None = None,
        frustration_signal: bool | None = None,
        tokens_used: int | None = None,
        llm_calls: int | None = None,
        baseline: bool | None = None,
    ) -> dict:
        """Record what you just did, so it can become a lesson later.

        `context_text` is the situation you faced; `solution_text` is what
        actually worked. Write them for a stranger -- the next reader is
        another agent with none of your session.

        Pass the same `occasion_id` you retrieved with, and the outcome fields
        you know (`resolved`, `repeated_error`, `tokens_used`, ...). That join
        is what turns retrieval into a measurable effect rather than an
        anecdote; without it nothing can be said about whether the memory
        helped. `baseline=true` marks work done BEFORE lessons were being
        injected, which is what a before/after comparison needs.
        """
        argv = ["--title", title, "--context", context_text, "--solution", solution_text,
                "--dest", root]
        try:
            coerced = _coerce_tags(tags)
        except ValueError as exc:
            return _err(str(exc))
        if coerced:
            argv += ["--tags", ",".join(str(t) for t in coerced)]
        if agent_type:
            argv += ["--agent-type", agent_type]
        if agent_id:
            argv += ["--agent-id", agent_id]
        if occasion_id:
            argv += ["--occasion-id", occasion_id]
        if tokens_used is not None:
            argv += ["--tokens-used", str(int(tokens_used))]
        if llm_calls is not None:
            argv += ["--llm-calls", str(int(llm_calls))]
        if baseline:
            argv += ["--baseline"]
        for flag, value in (("resolved", resolved), ("escalated", escalated),
                            ("repeated-error", repeated_error),
                            ("frustration", frustration_signal)):
            if value is not None:
                argv += [f"--{flag}" if value else f"--not-{flag}"]

        try:
            rc, out, err = _run_cli("capture", argv)
        except Exception as exc:  # noqa: BLE001
            return _err(f"could not write the trace: {type(exc).__name__}: {exc}")
        if rc != 0:
            return _err(err.strip() or "capture refused this trace.")
        path = out.strip().splitlines()[-1] if out.strip() else ""
        if not path or not os.path.exists(path):
            return _err(f"capture reported success but wrote no readable trace: {out!r}")
        instance, _ = trace_io.read(path)
        # Read back off disk rather than echoed from the arguments. Capturing
        # twice under one occasion id MERGES outcomes with the earlier trace,
        # so what this call passed and what the trace now holds are different
        # things -- and the second is the one the experiment will read.
        recorded = dict(instance.get("outcome") or {})
        return _ok(
            trace_id=instance.get("id"),
            path=os.path.basename(path),
            outcome=recorded,
            note=(
                "No outcome recorded. Nothing about this occasion can be measured until "
                "one is: re-call capture with the same occasion_id and `resolved` once "
                "the task concludes."
                if not recorded else
                "Outcome recorded."
                + ("" if occasion_id else
                   " No occasion_id, so this cannot be joined to a retrieval -- it counts"
                   " toward corpus totals but not toward the measured effect.")
            ),
        )

    @mcp.tool()
    async def propose_lessons(min_cluster: int = 2, similarity: float = 0.3) -> dict:
        """Find repeated failures in what you have captured, and draft a
        candidate lesson for each.

        Pure word-overlap clustering -- no model call. Candidates are written
        at `status: review` with their fields left as scaffolding for you to
        fill in with `draft_lesson`; they are NOT retrievable until approved.
        Traces already covered by an existing lesson are skipped, so running
        this repeatedly does not re-propose what is already curated.
        """
        # `commontrace distill`, not a copy of it. The first draft of this
        # method reimplemented the loop and got the candidate naming wrong
        # (the slug already carries its `lesson_` prefix), producing files the
        # rest of the tooling could not resolve. Every candidate this surface
        # writes is now byte-for-byte the one the CLI writes.
        before = set(glob.glob(os.path.join(paths.lessons_dir(root), "lesson_*.md")))
        argv = ["--dest", root,
                "--min-cluster-size", str(max(2, int(min_cluster))),
                "--similarity-threshold", str(float(similarity))]
        try:
            rc, out, err = _run_cli("distill", argv)
        except Exception as exc:  # noqa: BLE001
            return _err(f"could not scan the trace store: {type(exc).__name__}: {exc}")
        if rc != 0:
            return _err(err.strip() or out.strip() or "distill failed.")

        # Which candidates appeared, read off disk rather than parsed out of
        # the command's prose: the human-readable output is free to change.
        written = []
        for path in sorted(set(glob.glob(os.path.join(paths.lessons_dir(root), "lesson_*.md"))) - before):
            try:
                fm, body = frontmatter.read(path)
            except Exception:  # noqa: BLE001
                continue
            written.append({
                "slug": fm.get("name"),
                "description": fm.get("description"),
                "from_traces": len(fm.get("source_traces") or []),
                "tags": list(fm.get("tags") or []),
                "unfilled": templates.unfilled_placeholders(fm, body),
            })

        n_traces = len(load_trace_candidates(root, None))
        if not written:
            return _ok(candidates=[], n_traces=n_traces,
                       note=(out.strip().splitlines() or ["No repeated pattern yet."])[-1])
        return _ok(candidates=written, n_traces=n_traces,
                   next_step="Fill each one in with `draft_lesson`, then `approve_lesson`.")

    @mcp.tool()
    async def list_lessons(status: str = "") -> dict:
        """Every lesson in this store, newest first. Filter by `status`
        ('active', 'review', 'archived'). Each carries `unfilled`: the parts
        still left as scaffolding."""
        out = []
        for path in sorted(glob.glob(os.path.join(paths.lessons_dir(root), "lesson_*.md"))):
            if os.path.basename(path) == "lesson_template.md":
                continue
            try:
                fm, body = frontmatter.read(path)
            except Exception:  # noqa: BLE001 - one bad file must not hide the rest
                continue
            if status and fm.get("status") != status:
                continue
            out.append(_lesson_wire(fm, body))
        return _ok(lessons=out, count=len(out))

    @mcp.tool()
    async def get_lesson(slug: str) -> dict:
        """One lesson in full -- frontmatter and every body section.

        `list_lessons` and `retrieve` give you enough to choose; this gives you
        enough to EDIT. Read a candidate here before `draft_lesson` so you
        rewrite what is actually there rather than overwriting a section
        someone else already filled in, and read it again before
        `approve_lesson` so the judgement you record is one you actually made.

        `unfilled` names the parts still left as scaffolding.
        """
        try:
            path = _lesson_path(root, slug)
            fm, body = frontmatter.read(path)
        except LocalStoreError as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"could not read {slug!r}: {type(exc).__name__}: {exc}")
        return _ok(lesson=_lesson_wire(fm, body, include_body=True))

    @mcp.tool()
    async def draft_lesson(
        slug: str,
        rule: str = "",
        why: str = "",
        how_to_apply: str = "",
        counter_examples: str = "",
        applies_when: str = "",
        do_not_apply_when: str = "",
        description: str = "",
        importance: int | None = None,
        importance_rationale: str = "",
        tags: list[str] | None = None,
        domain: str = "",
    ) -> dict:
        """Write a candidate lesson's content. This is the curation step.

        Every argument is optional and only what you pass is changed, so you
        can fill a lesson in over several calls. The lesson stays at its
        current status -- drafting never activates anything.

        Write `rule` as one actionable sentence, `applies_when` as the precise
        condition that should trigger it, and `do_not_apply_when` as the
        counter-condition. Those three are what a later retrieval matches on
        and what a later reader acts on; vague ones produce a lesson that
        fires at the wrong time.

        The response reports what is still `unfilled`, which is exactly what
        `approve_lesson` will refuse to activate.
        """
        try:
            path = _lesson_path(root, slug)
        except LocalStoreError as exc:
            return _err(str(exc))

        sections = {
            "Rule": rule, "Why": why,
            "How to apply": how_to_apply, "Counter-examples": counter_examples,
        }
        fields = {
            "applies_when": applies_when, "do_not_apply_when": do_not_apply_when,
            "description": description, "importance_rationale": importance_rationale,
            "domain": domain,
        }
        if not check_text_size({**sections, **fields}, what="lesson"):
            return _err(
                f"refusing to write {slug!r}: the drafted text is too large "
                f"(over {REFUSE_CHARS} chars total). Split the content or "
                "trim the sections and try again."
            )
        try:
            # Locked read-modify-write: a fleet may have several agents
            # drafting at once, and two independent read-then-writes silently
            # lose one of the edits (commontrace/frontmatter.py:locked).
            with frontmatter.locked(path):
                fm, body = frontmatter.read(path)
                for key, value in fields.items():
                    if value and key in _AGENT_WRITABLE:
                        fm[key] = value
                if tags is not None:
                    coerced_tags = _coerce_tags(tags)
                    if coerced_tags is not None:
                        fm["tags"] = coerced_tags
                if importance is not None:
                    fm["importance"] = int(importance)
                for name, text in sections.items():
                    if text:
                        body = _replace_section(body, name, text)
                lesson_io.write_lesson(path, fm, body, root=root, actor=_agent_actor(),
                                       reason="drafted over MCP")
        except Exception as exc:  # noqa: BLE001
            return _err(f"could not write {slug!r}: {type(exc).__name__}: {exc}")

        try:
            fm, body = frontmatter.read(path)
            errors = validate.validate(fm, validate.load_schema("lesson.schema.json"))
            return _ok(lesson=_lesson_wire(fm, body, include_body=True),
                       schema_errors=errors,
                       next_step=("Still scaffolding: "
                                  + ", ".join(templates.unfilled_placeholders(fm, body))
                                  if templates.unfilled_placeholders(fm, body)
                                  else "Ready. Call approve_lesson to activate it."))
        except Exception as exc:  # noqa: BLE001
            return _err(f"could not validate {slug!r}: {type(exc).__name__}: {exc}")

    if allow_approval:
        @mcp.tool()
        async def approve_lesson(slug: str, rationale: str = "", approved_by: str = "agent") -> dict:
            """Activate a reviewed lesson so retrieval starts injecting it.

            This is the Validator step, and it is the one call here with a
            fleet-wide blast radius: an active lesson is fed to every later
            retrieval, verbatim. Two things therefore hold.

            It REFUSES a lesson that still contains scaffolding, naming the
            parts. An agent injects whatever it is given, so activating a
            lesson whose rule is still "TODO:" teaches the fleet nothing and
            displaces a real one -- and it would be counted as coverage by
            every report the customer reads.

            It also REFUSES a lesson whose content trips a high-confidence
            secret or prompt-injection pattern (OWASP ASI06 -- see
            `commontrace/memory_guard.py`), naming what was found. The same
            "injected verbatim" property that makes stale scaffolding costly
            makes a credential or an injection payload dangerous: this call
            is the one gate between drafted text and every later agent
            decision the lesson matches. There is no override on this path
            -- fix the content and call approve_lesson again.

            And it records `approved_by` in the lesson, so an
            agent-approved lesson is distinguishable from a human-approved one
            afterwards. Approve your OWN draft only when you have genuinely
            re-read it against the traces it came from; the value of a second
            judgement is that it is independent.
            """
            try:
                path = _lesson_path(root, slug)
            except LocalStoreError as exc:
                return _err(str(exc))
            try:
                with frontmatter.locked(path):
                    fm, body = frontmatter.read(path)
                    if fm.get("status") != "review":
                        return _err(
                            f"{slug!r} has status {fm.get('status')!r}, not 'review' -- only a "
                            "candidate awaiting review can be approved."
                        )
                    unfilled = templates.unfilled_placeholders(fm, body)
                    if unfilled:
                        return _err(
                            f"refusing to activate {slug!r}: still unedited scaffolding in "
                            + ", ".join(unfilled)
                            + ". An active lesson is injected into agents verbatim -- fill it in "
                              "with `draft_lesson` first.",
                            unfilled=unfilled,
                        )
                    errors = validate.validate(fm, validate.load_schema("lesson.schema.json"))
                    if errors:
                        return _err(f"refusing to activate {slug!r}: it does not satisfy the "
                                    "lesson schema.", schema_errors=errors)
                    # Separation of duties, where the store asks for it
                    # (memory/approval-policy.yaml). Absent, this is a
                    # no-op and an agent may still approve its own draft --
                    # the documented default. Set `mode: two-person` or
                    # `require_human: true` and this is the gate that stops
                    # the agent curating its own output unattended.
                    try:
                        policy = approval.load_policy(root)
                        approval.check(
                            policy, slug=slug, approver=_agent_actor(approved_by),
                            authors=approval.authors_of(root, slug),
                        )
                    except (approval.ApprovalDenied, approval.PolicyError) as exc:
                        return _err(f"refusing to activate {slug!r}: {exc}")

                    # OWASP ASI06 (Memory & Context Poisoning): an active
                    # lesson is injected into every later retrieval verbatim
                    # (this tool's own docstring), so a credential or a
                    # prompt-injection payload reaching `active` here would
                    # be replayed into every later decision the lesson
                    # matches. No --force equivalent on this path, unlike
                    # the CLI's `lesson approve`: an agent approving its own
                    # draft has no interactive human to confirm a deliberate
                    # override, so a HIGH-confidence finding refuses outright
                    # -- edit the lesson and call approve_lesson again.
                    from commontrace.commands.lesson_cmd import _guard_fields
                    guard = memory_guard.scan_fields(_guard_fields(fm, body))
                    if guard.should_block:
                        return _err(
                            f"refusing to activate {slug!r}: the content-safety scan flagged "
                            f"this lesson -- {guard.summary()}. Edit it to remove the flagged "
                            "content and try again.",
                            findings=[
                                {"category": f.category, "label": f.label,
                                 "field": f.field, "excerpt": f.excerpt}
                                for f in guard.blocking_findings
                            ],
                        )

                    # Near-duplicate check (commontrace/redundancy.py), same
                    # gate and same reasoning as the CLI's `lesson approve`
                    # (commontrace/commands/lesson_cmd.py:run_approve): this
                    # is the first point the lesson's real content exists
                    # rather than template scaffolding, and the first point
                    # activating it actually starts competing with the rest
                    # of the corpus for a retrieval slot. An agent curating
                    # unattended re-derives the same rule from a second
                    # trace cluster and has no reason to notice the corpus
                    # already has it -- this is the notice.
                    #
                    # No --force equivalent here, for the same reason the
                    # guard check above has none: an agent approving its own
                    # draft has no interactive human to confirm a deliberate
                    # override. Edit the content to genuinely differentiate
                    # it, or archive the other lesson, and call
                    # approve_lesson again.
                    from commontrace.commands.lesson_cmd import _active_lesson_texts

                    duplicate = redundancy.closest(
                        redundancy.comparable_text(fm, body),
                        _active_lesson_texts(root, exclude=slug),
                        threshold=redundancy.DEFAULT_THRESHOLD,
                    )
                    if duplicate is not None:
                        return _err(
                            f"refusing to activate {slug!r}: it restates the active lesson "
                            f"{duplicate.a!r} (similarity {duplicate.similarity:.2f}). Two "
                            "lessons saying the same thing compete for the same retrieval "
                            "slot forever, and neither wins reliably. Read the other one "
                            f"with get_lesson({duplicate.a!r}) -- edit this draft to "
                            "genuinely differentiate it, or reject it, and try again.",
                            duplicate_of=duplicate.a,
                            similarity=round(duplicate.similarity, 3),
                        )

                    fm["status"] = "active"
                    safe_by = _sanitize_comment(approved_by)
                    safe_rationale = _sanitize_comment(rationale)
                    note = f"Approved by {safe_by}" + (f": {safe_rationale}" if rationale else "")
                    body = body.rstrip() + f"\n\n<!-- {note} -->\n"
                    activated = lesson_io.write_lesson(
                        path, fm, body, root=root, actor=_agent_actor(approved_by),
                        reason=rationale or "approved",
                    )
            except Exception as exc:  # noqa: BLE001
                return _err(f"could not approve {slug!r}: {type(exc).__name__}: {exc}")
            return _ok(
                slug=slug, status="active", approved_by=approved_by, revision=activated,
                note="Retrieval will now inject this lesson. `revision` identifies the "
                     "exact text activated -- an experiment measuring this lesson is "
                     "measuring THIS revision, and editing it mid-run splits the arms "
                     "across two different treatments.",
            )

        @mcp.tool()
        async def reject_lesson(slug: str, reason: str) -> dict:
            """Archive a candidate that should not become a lesson. `reason` is
            required and recorded -- a rejected candidate that says nothing
            about why gets re-proposed by the next `propose_lessons` run."""
            if not reason.strip():
                return _err("a reason is required: without it this candidate gets re-proposed.")
            try:
                path = _lesson_path(root, slug)
                with frontmatter.locked(path):
                    fm, body = frontmatter.read(path)
                    if fm.get("status") != "review":
                        return _err(f"{slug!r} has status {fm.get('status')!r}, not 'review'.")
                    fm["status"] = "archived"
                    body = body.rstrip() + f"\n\n<!-- Rejected: {_sanitize_comment(reason)} -->\n"
                    lesson_io.write_lesson(path, fm, body, root=root,
                                           actor=_agent_actor(), reason=reason)
            except LocalStoreError as exc:
                return _err(str(exc))
            except Exception as exc:  # noqa: BLE001
                return _err(f"could not reject {slug!r}: {type(exc).__name__}: {exc}")
            return _ok(slug=slug, status="archived")

    @mcp.tool()
    async def experiment_status() -> dict:
        """Is the randomized holdout you are feeding actually going to answer?

        Call this when you have been retrieving with an `occasion_id` for a
        while. It reports three things, and the first two are the ones that
        decide whether the run was worth doing.

        `integrity` says whether the comparison can be trusted at all. The
        estimate is computed only on occasions that got an outcome recorded,
        which is unbiased ONLY if both arms record at the same rate -- and the
        withheld arm is by construction the one working without its memory, so
        it is the arm more likely to run long, escalate, or be abandoned
        before anyone reports. When that happens the result does not look
        empty or underpowered. It looks like a confident, significant effect
        with a tight interval, pointing the wrong way. If `verdict` is
        COMPROMISED, stop quoting effects and fix what it names.

        `projections` says how far each lesson is from being answerable and,
        where the log is dated, roughly when. The control arm almost always
        binds: at a 10% holdout it takes ~100 occasions to put 10 in the
        control, so a run reaches an answer about ten times slower than its
        occasion count suggests. Finding that out early is the difference
        between a pilot that lands and one that is spent.

        `effects` is the causal estimate itself, per lesson.

        The one thing NOT checkable here: whether you used a lesson you were
        told to withhold. That leaves no trace, and it biases the effect
        toward zero. Honour `withheld` from `retrieve` or the number is
        yours to have broken.
        """
        import dataclasses

        # commontrace/evidence.py:analyse is the one place the local
        # experiment is computed, so this report and the evidence `retrieve`
        # attaches to each lesson can never disagree.
        try:
            with _quiet():
                analysis = evidence_mod.analyse(root)
        except Exception as exc:  # noqa: BLE001
            return _err(f"could not read the experiment: {type(exc).__name__}: {exc}")
        all_rows, corrupt = analysis.all_rows, analysis.corrupt

        if not all_rows:
            return _ok(
                running=False, integrity=None, effects=[], projections=[],
                note="No holdout assignments yet. Pass `occasion_id` to `retrieve` and "
                     "the same id to `capture` to start measuring cause instead of "
                     "correlation.",
            )

        # SCOPED TO ONE RANDOMIZATION, matching `commontrace experiment` (the CLI
        # report) and what the Hub already does in SQL. Without this, changing
        # the holdout rate (which rotates the salt) makes every assignment ever
        # logged pool into one comparison, which `integrity.audit` correctly
        # flags as COMPROMISED even when the currently-running experiment is
        # perfectly clean -- and the CLI and this tool would then disagree
        # about the same store.
        rows, wanted_salt, n_other_salt = analysis.rows, analysis.wanted_salt, analysis.n_other_salt
        if not rows:
            return _ok(
                running=False, integrity=None, effects=[], projections=[],
                note=f"{len(all_rows)} assignment(s) recorded, but none under the current "
                     f"randomization (salt {wanted_salt!r}). They belong to an earlier "
                     "experiment and are not pooled in -- pooling two randomizations would "
                     "let one occasion sit in opposite arms. Start a fresh run with "
                     "`experiment --configure`.",
            )

        report, effects = analysis.report, analysis.effects
        return _ok(
            running=True,
            # NOT `rate` from `_load()` above -- that is an average over
            # `all_rows`, every randomization ever logged, computed before
            # the scoping just above happened. Everything else returned here
            # (n_assignments/effects/report) is scoped to `rows` (the
            # current salt only); a rate blended across old and new
            # randomizations would silently disagree with them, the exact
            # class of bug this tool's own salt-scoping fix exists to
            # prevent -- one field over. `rows` is non-empty here (guarded
            # by `if not rows` above).
            holdout_rate=sum(r.rate for r in rows) / len(rows),
            n_assignments=report.n_assignments,
            n_resolved=report.n_resolved,
            corrupt_lines=corrupt,
            excluded_other_randomization=n_other_salt,
            integrity={
                "verdict": report.verdict,
                "effects_readable": report.readable,
                "findings": [dataclasses.asdict(f) for f in report.findings],
            },
            projections=[dataclasses.asdict(p) for p in report.projections],
            effects=[dataclasses.asdict(e) for e in effects],
            next_step=(
                "Fix what `integrity` names before reading `effects` -- they are not "
                "estimates of the causal effect right now."
                if not report.readable else
                "Keep retrieving with an occasion_id and capturing the outcome under "
                "the same id."
            ),
        )

    @mcp.tool()
    async def store_status() -> dict:
        """What this store holds, and where its gaps are.

        `gaps` are recurring failure patterns with no lesson covering them --
        the highest-value thing to curate next. A lesson still full of
        scaffolding is NOT counted as coverage, so this number stays honest.
        """
        try:
            traces = load_trace_candidates(root, None)
            # One disk read, not two: all_lessons (status=None) is a strict
            # superset of the active-only list build_taxonomy needs, so the
            # active subset is filtered in memory with the same rule
            # evidence_io.load_active_lessons applies internally, instead of
            # re-globbing and re-parsing every lesson_*.md a second time.
            all_lessons = evidence_io.load_active_lessons(root, status=None)
            lessons = [lesson for lesson in all_lessons if (lesson.get("status") or "active") == "active"]
            tax = taxonomy.build_taxonomy(traces, lessons)
        except Exception as exc:  # noqa: BLE001
            return _err(f"could not read the store: {type(exc).__name__}: {exc}")

        by_status: dict[str, int] = {}
        for lesson in all_lessons:
            key = str(lesson.get("status") or "unknown")
            by_status[key] = by_status.get(key, 0) + 1

        gaps = [
            {"pattern": p.description, "traces": p.n_traces, "tags": list(p.tags)}
            for group in tax.domains for p in group.patterns if not p.covered
        ]
        return _ok(
            root=root,
            agent_type=paths.store_agent_type(root),
            traces=len(traces),
            lessons_by_status=by_status,
            patterns_found=tax.n_patterns,
            patterns_covered=tax.n_covered,
            gaps=gaps[:25],
            next_step=("Curate a gap: `propose_lessons`, then `draft_lesson`."
                       if gaps else "No uncovered recurring pattern right now."),
        )

    return mcp


def _replace_section(body: str, name: str, text: str) -> str:
    """Replace one `## <name>` section, or append it if absent."""
    import re

    pattern = re.compile(
        rf"^(##\s*{re.escape(name)}\s*\n)(.*?)(?=\n##\s|\Z)",
        re.DOTALL | re.MULTILINE | re.IGNORECASE,
    )
    replacement = f"## {name}\n{text.strip()}\n"
    if pattern.search(body):
        return pattern.sub(lambda _m: replacement, body, count=1)
    return body.rstrip() + f"\n\n{replacement}"


def serve(root: str, *, allow_approval: bool = True) -> int:
    """Run the server on stdio until the client disconnects."""
    import anyio

    mcp = build_server(root, allow_approval=allow_approval)
    anyio.run(mcp.run_stdio_async)
    return 0
