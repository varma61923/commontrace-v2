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
  - it records WHO approved it in the lesson body, so an agent-approved
    lesson is distinguishable from a human-approved one after the fact;
  - and `serve(allow_approval=False)` removes the tool entirely, for a
    deployment that requires a person. Absent, not merely refused.
"""

from __future__ import annotations

import contextlib
import glob
import io
import os
from typing import Any

from commontrace import (
    evidence_io,
    experiment,
    frontmatter,
    holdout_io,
    lesson_io,
    mcp_tools,
    paths,
    retrieval,
    revision,
    taxonomy,
    templates,
    trace_io,
    validate,
)
from commontrace.commands import query_cmd
from commontrace.commands._traces import load_trace_candidates

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
        task: str, top_k: int = 5, occasion_id: str = "", agent_type: str = ""
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

        Only `status: active` lessons are retrievable. A lesson still being
        drafted is invisible here by design.
        """
        try:
            # The SAME loader and ranker `commontrace query` uses, on the same
            # (path, frontmatter) shape -- not a parallel implementation. If
            # the two surfaces ranked differently, a fleet's shell-capable and
            # shell-less agents would be reading different memory.
            with _quiet():
                active = query_cmd._iter_active_lessons(root, agent_type or None)
            ranked = retrieval.rank_lessons(task, active, top_k=max(1, min(int(top_k), 50)))
        except Exception as exc:  # noqa: BLE001 - a malformed store is an answer, not a crash
            return _err(f"could not read the lesson store: {type(exc).__name__}: {exc}")

        slugs = [r.slug for r in ranked]
        withheld: set[str] = set()
        # The STORE's settings, not this module's constants. Hardcoding them
        # here meant an agent-driven fleet could not change its holdout rate
        # at all -- the product could compute exactly what rate a pilot needed
        # and then offer its AI-first half no way to set it -- and it let this
        # surface silently disagree with `commontrace query`, which pools two
        # randomizations into one comparison.
        config = holdout_io.load_config(root)
        if occasion_id and slugs and config.running:
            try:
                withheld = holdout_io.assign_and_log(
                    root, slugs,
                    occasion_id=occasion_id,
                    rate=config.rate,
                    salt=config.salt,
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
        for r in ranked:
            # Re-read for the BODY. `_iter_active_lessons` returns frontmatter
            # only, and the body is where the rule actually is -- returning a
            # lesson without it would hand the agent a title and no
            # instruction. Only the top-k are re-read, not the whole store.
            try:
                fm, body = frontmatter.read(r.path)
            except Exception:  # noqa: BLE001
                continue
            item = _lesson_wire(fm, body, include_body=True)
            item["score"] = round(r.score, 3)
            item["matched"] = list(r.matched_terms or [])
            (held if r.slug in withheld else injected).append(item)

        result = {
            "lessons": injected,
            "n_active": len(active),
            "occasion_id": occasion_id or None,
        }
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
        if not injected and not held:
            result["note"] = (
                "No active lesson matched. That is a real answer -- proceed on your own "
                "judgement, then `capture` what happened so the gap can become a lesson."
                if active else
                "This store has no active lessons yet. `capture` your work, then "
                "`propose_lessons` once a pattern repeats."
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
        if tags:
            argv += ["--tags", ",".join(str(t) for t in tags)]
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
                    fm["tags"] = [str(t) for t in tags]
                if importance is not None:
                    fm["importance"] = int(importance)
                for name, text in sections.items():
                    if text:
                        body = _replace_section(body, name, text)
                lesson_io.write_lesson(path, fm, body, root=root, actor=_agent_actor(),
                                       reason="drafted over MCP")
        except Exception as exc:  # noqa: BLE001
            return _err(f"could not write {slug!r}: {type(exc).__name__}: {exc}")

        fm, body = frontmatter.read(path)
        errors = validate.validate(fm, validate.load_schema("lesson.schema.json"))
        return _ok(lesson=_lesson_wire(fm, body, include_body=True),
                   schema_errors=errors,
                   next_step=("Still scaffolding: "
                              + ", ".join(templates.unfilled_placeholders(fm, body))
                              if templates.unfilled_placeholders(fm, body)
                              else "Ready. Call approve_lesson to activate it."))

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
                    fm["status"] = "active"
                    note = f"Approved by {approved_by}" + (f": {rationale}" if rationale else "")
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
                    body = body.rstrip() + f"\n\n<!-- Rejected: {reason} -->\n"
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

        from commontrace import integrity
        from commontrace.commands import experiment_cmd

        try:
            with _quiet():
                all_rows, rate, corrupt = experiment_cmd._load(root)
        except Exception as exc:  # noqa: BLE001
            return _err(f"could not read the experiment: {type(exc).__name__}: {exc}")

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
        rows, wanted_salt, n_other_salt = experiment_cmd.scope_to_current_salt(root, all_rows)
        if not rows:
            return _ok(
                running=False, integrity=None, effects=[], projections=[],
                note=f"{len(all_rows)} assignment(s) recorded, but none under the current "
                     f"randomization (salt {wanted_salt!r}). They belong to an earlier "
                     "experiment and are not pooled in -- pooling two randomizations would "
                     "let one occasion sit in opposite arms. Start a fresh run with "
                     "`experiment --configure`.",
            )

        report = integrity.audit(rows)
        observations = experiment_cmd._observations(rows)
        effects = experiment.analyze(observations)
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
