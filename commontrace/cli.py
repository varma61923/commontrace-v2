from __future__ import annotations

import argparse
import csv
import difflib
import importlib
import sys

from commontrace import PROTOCOL_VERSION, __version__

try:
    from commontrace.frontmatter import FrontmatterError
except ModuleNotFoundError as _import_exc:
    _MISSING_DEPENDENCY: ModuleNotFoundError | None = _import_exc
    FrontmatterError = None  # type: ignore[assignment,misc]
else:
    _MISSING_DEPENDENCY = None

_COMMANDS = (
    "init", "install", "capture", "import", "trace", "distill", "lesson", "release",
    "overlap", "commons", "kb", "account", "query", "serve", "index", "bench", "reliability",
    "consolidate", "retrieval", "experiment", "source", "function", "proof", "gateway", "fleet", "signals", "export",
    "dream", "bill", "conformance", "gate", "prove", "taxonomy", "impact", "pilot", "sync", "redact", "doctor",
    "block", "fact", "graph", "ingest", "agent", "watch", "daemon",
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
    subparsers = parser.add_subparsers(dest="command", required=_MISSING_DEPENDENCY is None)
    for module in _command_modules(only):
        module.add_parser(subparsers)
    return parser


def _from_worker(argv: list[str]) -> int | None:
    from commontrace import warm

    if not argv or argv[0] not in warm.CLI_COMMANDS or not warm.available():
        return None
    from commontrace.commands._shellout import has_attention_deps

    if not has_attention_deps():
        return None
    answered = warm.run_cli(argv)
    if answered is None:
        return None
    rc, out, err = answered
    if err:
        sys.stderr.write(err)
        sys.stderr.flush()
    sys.stdout.write(out)
    sys.stdout.flush()
    return rc


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
    answered = _from_worker(argv)
    if answered is not None:
        return answered
    if not argv:
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
        print(f"[commontrace] error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
