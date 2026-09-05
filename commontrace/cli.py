from __future__ import annotations

import argparse
import csv
import sys

from commontrace import PROTOCOL_VERSION, __version__

# Every subcommand module transitively imports commontrace.frontmatter, which
# does a hard `import yaml` -- PyYAML is a required (not optional) dependency
# per pyproject.toml, so this only fails on a broken/incomplete install (a
# checkout run without `pip install -e .`, a container image that dropped a
# dependency). But because these are top-level imports, that failure happened
# here, at module-import time, before main() below is ever reached -- so a
# try/except inside main() could not catch it, and commontrace doctor (whose
# own job is diagnosing exactly this) crashed with a raw ModuleNotFoundError
# instead of ever running. Caught here instead, so main() can report a clear,
# actionable error and every subcommand's --help / doctor / --version still
# degrade to "commontrace isn't installed correctly" rather than a traceback
# pointing at an unrelated subcommand's transitive import.
try:
    from commontrace.commands import (
        account_cmd,
        bench_cmd,
        capture_cmd,
        commons_cmd,
        distill_cmd,
        doctor_cmd,
        experiment_cmd,
        impact_cmd,
        import_cmd,
        index_cmd,
        init_cmd,
        install_cmd,
        lesson_cmd,
        overlap_cmd,
        pilot_cmd,
        prove_cmd,
        query_cmd,
        reliability_cmd,
        serve_cmd,
        sync_cmd,
        taxonomy_cmd,
        trace_cmd,
    )
    from commontrace.frontmatter import FrontmatterError
except ModuleNotFoundError as _import_exc:
    _MISSING_DEPENDENCY: ModuleNotFoundError | None = _import_exc
    FrontmatterError = None  # type: ignore[assignment,misc]
    _SUBCOMMANDS: list = []
else:
    _MISSING_DEPENDENCY = None
    _SUBCOMMANDS = [
        init_cmd,
        install_cmd,
        capture_cmd,
        import_cmd,
        trace_cmd,
        distill_cmd,
        lesson_cmd,
        overlap_cmd,
        commons_cmd,
        account_cmd,
        query_cmd,
        serve_cmd,
        index_cmd,
        bench_cmd,
        reliability_cmd,
        experiment_cmd,
        prove_cmd,
        taxonomy_cmd,
        impact_cmd,
        pilot_cmd,
        sync_cmd,
        doctor_cmd,
    ]


def build_parser() -> argparse.ArgumentParser:
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
    for module in _SUBCOMMANDS:
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
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FrontmatterError as exc:
        print(f"[commontrace] error: {exc}", file=sys.stderr)
        return 1
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
