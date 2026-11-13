"""Export the Hub's OpenAPI 3.1.0 document, generated from its live route table.

Needs the Hub's dependencies (`pip install -r hub/requirements.txt`) but no
database: the Hub is built with every optional surface on and never started.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys

ROOT = os.environ.get("UNIMEM_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DEFAULT_OUT = os.path.join(ROOT, "hub", "openapi.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=DEFAULT_OUT, help="where to write the document (default: hub/openapi.json)")
    parser.add_argument("--check", action="store_true", help="exit 1 if --out is missing or stale; write nothing")
    args = parser.parse_args(argv)
    try:
        from hub.openapi import document, render

        content = render(document())
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] in {"hub", "unimem"}:
            raise
        install = shlex.join([sys.executable, "-m", "pip", "install", "-r", "hub/requirements.txt"])
        parser.exit(
            2,
            f"Hub OpenAPI export requires the optional Hub dependencies (missing {exc.name!r}).\n"
            f"From the repository root, run: {install}\n"
            "For an existing Hub environment, use make check-hub PYTHON=/path/to/venv/bin/python.\n",
        )
    if args.check:
        try:
            with open(args.out, encoding="utf-8") as handle:
                current = handle.read()
        except FileNotFoundError:
            current = None
        if current != content:
            parser.exit(1, f"{args.out} is stale; run python scripts/export_hub_openapi.py\n")
        return 0
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
