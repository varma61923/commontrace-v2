from __future__ import annotations

import argparse
import csv
import difflib
import importlib
import sys

from commontrace import PROTOCOL_VERSION, __version__

# Every subcommand module imports commontrace.frontmatter, which does a hard
# `import yaml` -- PyYAML is a required dependency, so this fails only on a
# broken install (a checkout run without `pip install -e .`, an image that
# dropped a dependency). Caught here and in main() rather than left to raise,
# so that `doctor` -- whose job is diagnosing exactly this -- and every other
# command report "commontrace isn't installed correctly" instead of a
# traceback through an unrelated module's imports.
try:
    from commontrace.frontmatter import FrontmatterError
except ModuleNotFoundError as _import_exc:
    _MISSING_DEPENDENCY: ModuleNotFoundError | None = _import_exc
    FrontmatterError = None  # type: ignore[assignment,misc]
else:
    _MISSING_DEPENDENCY = None

# Each subcommand lives in commontrace/commands/<name>_cmd.py, in the order
# `commontrace --help` lists them. Only the module for the command being run
# is imported: importing all of them cost ~140ms on every invocation (numpy
# for `commons`, asyncio and ssl for the Hub commands), paid by every
# `capture` an agent hook runs after every task. Help, a bad command name,
# or none at all still loads every module, so those read exactly as before.
_COMMANDS = (
    "init", "install", "capture", "import", "trace", "distill", "lesson", "release",
    "overlap", "commons", "account", "query", "serve", "index", "bench", "reliability",
    "consolidate", "retrieval", "experiment", "export", "prove", "taxonomy", "impact",
    "pilot", "sync", "redact", "doctor",
)


def _command_modules(only: str | None = None) -> list:
    names = (only,) if only in _COMMANDS else _COMMANDS
    return [importlib.import_module(f"commontrace.commands.{name}_cmd") for name in names]


def build_parser(only: str | None = None) -> argparse.ArgumentParser:
    """The CLI's parser: every subcommand, or just `only` when it names one."""
    parser = argparse.ArgumentParser(
        prog="commontrace",
        description="CommonTrace Protocol client - capture experience, curate lessons, "
        "inject them into any agent platform. Spec: protocol/PROTOCOL.md",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"commontrace {__version__} (protocol {PROTOCOL_VERSION})",
    )
    # required=False when subcommands failed to import: with no subparsers to
    # register, argparse's own "required" enforcement has nothing valid to
    # accept and would just reject every invocation with its own confusing
    # "the following arguments are required: command" -- main() below reports
    # the real cause instead before parse_args() is ever reached in that case.
    subparsers = parser.add_subparsers(dest="command", required=_MISSING_DEPENDENCY is None)
    for module in _command_modules(only):
        module.add_parser(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    if _MISSING_DEPENDENCY is not None:
        print(
            f"[commontrace] error: missing required dependency {_MISSING_DEPENDENCY.name!r} -- "
            "commontrace was not installed correctly. Try: pip install -e . "
            "(or pip install commontrace)",
            file=sys.stderr,
        )
        return 1
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        # Nothing asked: the full command list is more use than argparse's
        # one-line "the following arguments are required".
        build_parser().print_help(sys.stderr)
        return 2
    try:
        parser = build_parser(argv[0] if argv else None)
    except ModuleNotFoundError as exc:
        print(
            f"[commontrace] error: missing required dependency {exc.name!r} -- "
            "commontrace was not installed correctly. Try: pip install -e . "
            "(or pip install commontrace)",
            file=sys.stderr,
        )
        return 1
    commands = next(
        (list(a.choices) for a in parser._actions if isinstance(a, argparse._SubParsersAction)), [])
    if not argv[0].startswith("-") and argv[0] not in commands:
        close = difflib.get_close_matches(argv[0], commands, n=3, cutoff=0.6)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        print(
            f"commontrace: unknown command {argv[0]!r}.{hint} "
            "Run `commontrace --help` for the list.",
            file=sys.stderr,
        )
        return 2
    args = parser.parse_args(argv)
    try:
        res = args.func(args)
        return 0 if res is None else int(res)
    except FrontmatterError as exc:
        print(f"[commontrace] error: {exc}", file=sys.stderr)
        return 1
    except (argparse.ArgumentError, argparse.ArgumentTypeError) as exc:
        print(f"[commontrace] error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        # 130 is the shell convention for SIGINT; a traceback here is noise.
        print("\n[commontrace] interrupted.", file=sys.stderr)
        return 130
    except (
        OSError,
        ValueError,
        KeyError,
        csv.Error,
        TypeError,
        IndexError,
        AttributeError,
    ) as exc:
        # Operational errors a user can cause with a bad argument or a
        # corrupt file: a malformed JSON signature file, a directory passed
        # where a file was meant, a missing key in someone else's export.
        # These reached sys.excepthook and printed a raw traceback with
        # absolute paths and interpreter internals -- unreadable as an error
        # message, and noise in any script wrapping this CLI. Narrow on
        # purpose: a genuine bug still raises, because silently returning 1
        # for an unexpected exception would hide defects rather than report
        # them. json.JSONDecodeError is a ValueError subclass and is covered.
        # csv.Error is not a ValueError subclass (it inherits Exception
        # directly) and is added explicitly: `commontrace import` streams a
        # CSV row-by-row through a generator (import_data.py:iter_csv), so a
        # malformed row's csv.Error surfaces here rather than through
        # failure_import.py's own narrower, already-covered except tuple.
        print(f"[commontrace] error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
