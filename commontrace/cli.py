from __future__ import annotations

import argparse
import sys

from commontrace import PROTOCOL_VERSION, __version__
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
    query_cmd,
    reliability_cmd,
    sync_cmd,
    taxonomy_cmd,
    trace_cmd,
)
from commontrace.frontmatter import FrontmatterError

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
    index_cmd,
    bench_cmd,
    reliability_cmd,
    experiment_cmd,
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
    subparsers = parser.add_subparsers(dest="command", required=True)
    for module in _SUBCOMMANDS:
        module.add_parser(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
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
    except (OSError, ValueError, KeyError) as exc:
        # Operational errors a user can cause with a bad argument or a
        # corrupt file: a malformed JSON signature file, a directory passed
        # where a file was meant, a missing key in someone else's export.
        # These reached sys.excepthook and printed a raw traceback with
        # absolute paths and interpreter internals -- unreadable as an error
        # message, and noise in any script wrapping this CLI. Narrow on
        # purpose: a genuine bug still raises, because silently returning 1
        # for an unexpected exception would hide defects rather than report
        # them. json.JSONDecodeError is a ValueError subclass and is covered.
        print(f"[commontrace] error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
