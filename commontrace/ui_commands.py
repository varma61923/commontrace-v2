"""Authenticated, store-scoped command access for the browser console.

The CLI remains the canonical implementation. This adapter deliberately invokes
that same entry point instead of maintaining a second UI-only command graph, so
new command behavior and validation stay in one place. Process-lifecycle commands
are catalogued for discoverability but cannot be launched inside a running gateway.
"""
from __future__ import annotations

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
_TERMINAL_ONLY = frozenset({"daemon", "gateway", "serve", "watch"})

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
        if value in ("--dest", "--root") or value.startswith("--dest=") or value.startswith("--root="):
            raise UICommandError(f"args[{index}] cannot override the gateway store root")
    return list(args)


def run(root: str, command: object, args: object = None) -> dict[str, Any]:
    """Run one allowlisted CLI command against *root* and capture its output."""
    from commontrace.cli import _COMMANDS, main

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
