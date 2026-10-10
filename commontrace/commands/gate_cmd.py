"""`commontrace gate`: fail a build when this store's memory is not safe to ship."""
from __future__ import annotations

import argparse
import sys

from commontrace import gate, paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "gate",
        help="Exit non-zero if this store's memory should not ship: a compromised experiment, "
        "an active lesson measured to hurt, unsafe or unfinished lesson text.",
        description=(
            "Run in CI before a release is promoted. Exit 0 means every blocking check passed; "
            "exit 1 means at least one failed (each is named). Warnings (contradicting lessons, "
            "no verdicts yet) block only with --strict."
        ),
    )
    p.add_argument("--dest", default=None, help="Store root (default: auto-detected).")
    p.add_argument("--strict", action="store_true", help="Treat warnings as failures.")
    p.add_argument(
        "--format", choices=sorted(gate.RENDERERS), default="text",
        help="text (default), json, junit (for CI test reports), or github (Actions annotations).",
    )
    p.add_argument("--output", default=None, help="Write the report to this file instead of stdout.")
    p.add_argument("--policy",
                   help="JSON candidate exploration delivery policy; block unidentified or harmful changes.")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    result = gate.run(root, strict=args.strict)
    if getattr(args, "policy", None):
        import json

        from commontrace import policy

        try:
            with open(args.policy, encoding="utf-8") as fh:
                report = policy.evaluate(root, json.load(fh))
            result.checks.append(gate.Check("policy", gate.PASS if report["safe_to_release"] else gate.FAIL,
                                           report.get("reason", json.dumps(report))))
        except (ValueError, OSError, PermissionError) as exc:
            result.checks.append(gate.Check("policy", gate.FAIL, str(exc)))
    rendered = gate.RENDERERS[args.format](result)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        print(gate.render_text(result) if args.format != "text" else rendered)
    else:
        print(rendered)
    return 0 if result.passed else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run(argparse.Namespace(dest=None, strict=False, format="text", output=None)))
