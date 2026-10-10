"""Authenticated, store-scoped command access for the browser console.

The CLI remains the canonical implementation. This adapter deliberately invokes
that same entry point instead of maintaining a second UI-only command graph, so
new command behavior and validation stay in one place. Process-lifecycle commands
are catalogued for discoverability but cannot be launched inside a running gateway.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

MAX_ARGS = 256
MAX_OUTPUT_CHARS = 200_000

# These commands own the process or can recursively start another server. The
# console can show their help and copyable command shape, but must not block the
# gateway thread or replace the process serving the UI. Store setup/install are
# safe because the runner pins their destination to the authenticated store.
_TERMINAL_ONLY = frozenset({"daemon", "gateway", "serve", "watch", "evolve"})

_GROUPS: dict[str, tuple[str, ...]] = {
    "Capture": ("capture", "import", "trace", "ingest", "source"),
    "Curate": (
        "block", "community", "consolidate", "distill", "fact", "graph", "lesson",
        "observation", "ontology", "page", "saga", "taxonomy",
    ),
    "Measure": (
        "bench", "bill", "conformance", "experiment", "gate", "impact", "overlap",
        "pilot", "proof", "prove", "reliability", "retrieval", "signals",
    ),
    "Explore": (
        "agent", "conversation", "defense", "doctor", "dream", "export", "fleet",
        "function", "index", "jobs", "kb", "memory", "procedural", "query", "recall",
        "redact", "session_ledger", "sql_query", "viz",
    ),
    "Operate": ("account", "commons", "release", "sync"),
    "Setup": ("init", "install"),
    "Runtime": tuple(sorted(_TERMINAL_ONLY)),
}

_DESCRIPTIONS = {
    "evolve": "Use scoped memory recipes, standing questions, hard rules and coding-agent onboarding.",
    "query": "Search approved lessons for a task or question.",
    "recall": "Fuse lessons, facts, graph relations, and conversations into one budgeted context.",
    "fact": "Manage temporal, scoped atomic facts and their lifecycle.",
    "graph": "Inspect, render, and traverse the temporal knowledge graph.",
    "ingest": "Ingest code, documents, logs, transcripts, and multimodal sources.",
    "lesson": "Create, validate, edit, approve, reject, and inspect lessons.",
    "conversation": "Add, recall, summarize, profile, and forget conversation memory.",
    "experiment": "Design and analyze randomized memory holdout experiments.",
    "proof": "Track proof progress and the evidence needed for a causal verdict.",
    "bench": "Run memory health and benchmark evaluations.",
    "defense": "Screen, redact, and block secrets, PII, and injection payloads.",
    "sql_query": "Run a guarded, read-only SQL query against a store-local SQLite database.",
    "page": "Manage versioned knowledge pages with dry-run diffs.",
    "saga": "Manage ordered incident and migration narratives.",
    "session_ledger": "Inspect per-session token, model, and cost attribution.",
    "procedural": "Record and replay structured agent procedures within token budgets.",
    "ontology": "Inspect or install the store knowledge-graph ontology.",
    "community": "Build and inspect deterministic topic communities.",
    "observation": "Consolidate evidence-grounded observations and trends.",
    "doctor": "Diagnose store integrity, retrieval, and configuration health.",
}

_EXAMPLES = {
    "query": ["describe the task you are about to do", "--top-k", "5"],
    "recall": ["What should I know before this task?", "--budget", "1500"],
    "fact": ["search", "deployment rule"],
    "graph": ["query", "service:api", "--hops", "2"],
    "ingest": ["./docs", "--type", "docs", "--preview"],
    "lesson": ["list", "--status", "review"],
    "conversation": ["recall", "support", "What happened last time?"],
    "experiment": ["--status"],
    "proof": ["status"],
    "bench": ["--help"],
    "defense": ["screen", "paste content here", "--json"],
    "sql_query": ["./memory/conversations/default.db", "SELECT name FROM sqlite_master"],
    "page": ["list"],
    "saga": ["list"],
    "session_ledger": ["summary"],
    "procedural": ["list"],
    "ontology": ["show"],
    "community": ["list"],
    "observation": ["list"],
    "doctor": [],
}


@dataclass(frozen=True)
class CommandSpec:
    name: str
    group: str
    description: str
    runnable: bool
    reason: str = ""
    example: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"example": list(self.example)}


class UICommandError(ValueError):
    """A command request is invalid or not safe to run from the console."""

    def __init__(self, message: str, *, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.status = status
        self.code = code


# Default-deny unreviewed handlers while the operator has disabled approval.
# Match the parsed function, so argparse option ordering/abbreviations cannot
# disguise a write behind a read-only subcommand name. Help exits before a
# function is selected and remains available for every non-lifecycle command.
_READ_ONLY_HANDLERS = {
    "doctor": frozenset({"run"}),
    "query": frozenset({"run"}),
    "recall": frozenset({"run"}),
    "lesson": frozenset({"run_list", "run_validate", "run_history"}),
    "trace": frozenset({"run_list", "run_validate"}),
    "fact": frozenset({"run_list", "run_search", "run_explain"}),
    "conversation": frozenset({"run_spaces", "run_sessions", "run_profile", "run_recall"}),
    "release": frozenset({"run_list", "run_show", "run_diff", "run_env_current", "run_env_pending"}),
    "proof": frozenset({"run_status"}),
}
_LESSON_REVIEW_HANDLERS = frozenset({"run_approve", "run_reject", "run_revoke", "run_auto_approve"})


def _authorize(command: str, parsed: argparse.Namespace, *, allow_approval: bool, scope: str) -> None:
    fn = getattr(parsed, "func", None)
    handler = getattr(fn, "__name__", "")
    module = getattr(fn, "__module__", "")
    if command not in {"init", "install"} and (
            getattr(parsed, "dest", None) is not None or getattr(parsed, "root", None) is not None):
        raise UICommandError("command arguments cannot override the gateway store root")
    if getattr(parsed, "force", False):
        raise UICommandError("force overrides are terminal-only", status=409, code="command_unavailable")
    if command == "lesson" and handler in _LESSON_REVIEW_HANDLERS:
        raise UICommandError("lesson review actions require the revision-checked review endpoints or a terminal",
                             status=409, code="command_unavailable")
    # CLI scopes/spaces are caller-controlled routing parameters. They cannot
    # inherit the gateway's container namespace safely through an arbitrary CLI.
    if scope:
        raise UICommandError("scoped requests must use the scoped API endpoints", status=403, code="forbidden")
    if not allow_approval and (module != f"commontrace.commands.{command.replace('-', '_')}_cmd"
                               or handler not in _READ_ONLY_HANDLERS.get(command, ())):
        raise UICommandError("this command requires a gateway started with --allow-approval",
                             status=403, code="approval_disabled")


_COMMAND_LOCK = threading.RLock()


def _group_for(name: str) -> str:
    for group, names in _GROUPS.items():
        if name in names:
            return group
    return "Other"


def catalog() -> list[dict[str, Any]]:
    """Return every CLI command with a safe browser execution classification."""
    from commontrace.cli import _COMMANDS

    return [
        CommandSpec(
            name=name,
            group=_group_for(name),
            description=_DESCRIPTIONS.get(name, f"Run the CommonTrace {name} command."),
            runnable=name not in _TERMINAL_ONLY,
            reason=(
                "This command owns the gateway/process lifecycle and is available from a terminal."
                if name in _TERMINAL_ONLY else ""
            ),
            example=tuple(_EXAMPLES.get(name, ())),
        ).to_dict()
        for name in _COMMANDS
    ]


def _validate_args(args: object) -> list[str]:
    if args is None:
        return []
    if not isinstance(args, list) or len(args) > MAX_ARGS or not all(isinstance(a, str) for a in args):
        raise UICommandError(f"args must be a list of at most {MAX_ARGS} strings")
    # The browser cannot select a different store or a different process root.
    # All CLI code resolves the authenticated gateway's root through this env.
    for index, value in enumerate(args):
        option = value.split("=", 1)[0]
        if option.startswith("--") and len(option) > 2 and any(
                reserved.startswith(option) for reserved in ("--dest", "--root")):
            raise UICommandError(f"args[{index}] cannot override the gateway store root")
    return list(args)


def run(root: str, command: object, args: object = None, *, allow_approval: bool = False,
        scope: str = "") -> dict[str, Any]:
    """Run one allowlisted CLI command against *root* and capture its output."""
    from commontrace.cli import _COMMANDS, build_parser, main

    if not isinstance(command, str) or command not in _COMMANDS:
        raise UICommandError("unknown CommonTrace command")
    if command in _TERMINAL_ONLY:
        raise UICommandError(
            f"{command} is terminal-only because it owns the gateway or process lifecycle"
        )
    argv = _validate_args(args)
    command_args = [*argv, "--dest", os.path.abspath(root)] if command in {"init", "install"} else argv
    stdout, stderr = io.StringIO(), io.StringIO()
    started = time.perf_counter()
    with _COMMAND_LOCK:
        previous = os.environ.get("COMMONTRACE_ROOT")
        os.environ["COMMONTRACE_ROOT"] = os.path.abspath(root)
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                # Parse exactly what will execute, then clear parser output so
                # the ordinary CLI help/errors are emitted only once by main.
                try:
                    parsed = build_parser(only=command).parse_args([command, *argv])
                except SystemExit:
                    parsed = None  # Help or a parse error cannot invoke a handler.
                stdout.seek(0)
                stdout.truncate(0)
                stderr.seek(0)
                stderr.truncate(0)
                if parsed is not None:
                    _authorize(command, parsed, allow_approval=allow_approval, scope=scope)
                try:
                    exit_code = int(main([command, *command_args]))
                except SystemExit as exc:
                    exit_code = int(exc.code) if isinstance(exc.code, int) else 1
        finally:
            if previous is None:
                os.environ.pop("COMMONTRACE_ROOT", None)
            else:
                os.environ["COMMONTRACE_ROOT"] = previous
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    return {
        "command": command,
        "args": argv,
        "exit_code": exit_code,
        "ok": exit_code == 0,
        "stdout": stdout.getvalue()[:MAX_OUTPUT_CHARS],
        "stderr": stderr.getvalue()[:MAX_OUTPUT_CHARS],
        "truncated": len(stdout.getvalue()) > MAX_OUTPUT_CHARS or len(stderr.getvalue()) > MAX_OUTPUT_CHARS,
        "elapsed_ms": elapsed_ms,
    }
