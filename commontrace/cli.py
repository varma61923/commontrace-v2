from __future__ import annotations

import argparse
import csv
import difflib
import importlib
import os
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
    "block", "fact", "graph", "ingest", "agent", "watch", "daemon", "viz", "conversation", "memory", "recall", "jobs",
    "ontology", "community", "observation", "saga", "page", "session_ledger",
    "procedural", "sql_query", "defense", "evolve",
)




def _command_modules(only: str | None = None) -> list:
    names = (only,) if only in _COMMANDS else _COMMANDS
    seen = set()
    modules = []
    for name in names:
        mod_name = name.replace("-", "_")
        if mod_name not in seen:
            seen.add(mod_name)
            modules.append(importlib.import_module(f"commontrace.commands.{mod_name}_cmd"))
    return modules


class _LazyCommandMap(dict):
    def __init__(self, subparsers_action=None):
        super().__init__()
        self._subparsers_action = subparsers_action
        self._loading: set[str] = set()

    def __contains__(self, key):
        if key in self._loading:
            return False
        clean = key.replace("-", "_") if isinstance(key, str) else key
        return key in _COMMANDS or clean in _COMMANDS or dict.__contains__(self, key)

    def __iter__(self):
        return iter(_COMMANDS)

    def __len__(self):
        return len(_COMMANDS)

    def __getitem__(self, key):
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        clean = key.replace("-", "_") if isinstance(key, str) else key
        if key not in _COMMANDS and clean not in _COMMANDS:
            raise KeyError(key)
        target = clean if clean in _COMMANDS else key
        module = importlib.import_module(f"commontrace.commands.{target}_cmd")
        action = self._subparsers_action
        if action is None:
            raise KeyError(key)
        action._choices_actions = [
            a for a in action._choices_actions if a.dest not in (key, clean)
        ]
        self._loading.add(key)
        try:
            module.add_parser(action)
        finally:
            self._loading.discard(key)
        return dict.__getitem__(self, key)


    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self):  # pragma: no cover - convenience, avoids accidental full import
        return list(_COMMANDS)


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
    clean_only = only.replace("-", "_") if isinstance(only, str) else None
    if clean_only in _COMMANDS:
        importlib.import_module(f"commontrace.commands.{clean_only}_cmd").add_parser(subparsers)
        return parser
    lazy: dict = _LazyCommandMap(subparsers)
    subparsers._name_parser_map = lazy  # type: ignore[assignment]
    subparsers.choices = lazy  # type: ignore[assignment]
    for name in _COMMANDS:
        subparsers._choices_actions.append(
            subparsers._ChoicesPseudoAction(name, (), f"{name} command")
        )
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
    if not argv[0].startswith("-"):
        clean_cmd = argv[0].replace("-", "_")
        if argv[0] not in commands and clean_cmd in commands:
            argv = [clean_cmd, *argv[1:]]
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
        if os.environ.get("COMMONTRACE_LOG_FORMAT") or os.environ.get("COMMONTRACE_LOG_LEVEL") or \
                os.environ.get("COMMONTRACE_OTEL") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from commontrace import telemetry

            telemetry.configure_logging()
            sub = getattr(args, "subcommand", None) or getattr(args, "action", None)
            with telemetry.bind(request_id=telemetry.new_request_id(), command=argv[0]), \
                    telemetry.span(f"cli.{argv[0]}" + (f".{sub}" if isinstance(sub, str) else "")):
                res = args.func(args)
        else:
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
    except BrokenPipeError:
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 0
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
