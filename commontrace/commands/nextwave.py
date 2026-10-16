"""UniMem JSON workflows, grouped under existing command themes."""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from commontrace import frontier_protocol as fp
from commontrace import paths, pooled_io


def request(value: str) -> dict[str, Any]:
    if value.lstrip().startswith("{"):
        raw = value.encode()
    else:
        with Path(value).open("rb") as stream:
            raw = stream.read(fp.MAX_BYTES + 1)
    if len(raw) > fp.MAX_BYTES:
        raise ValueError("UniMem request exceeds 1 MiB")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("UniMem request must be a JSON object")
    fp.wire(result)
    return result


def add_experiment(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="unimem_experiment")
    pool = commands.add_parser("pool", help="UniMem signed evidence pooling across configured ownership groups.")
    operations = pool.add_subparsers(dest="unimem_pool", required=True)
    for operation in ("identity", "register", "privacy", "contribute", "accept", "report"):
        child = operations.add_parser(operation, help="UniMem pooled evidence: " + operation + ".")
        child.add_argument("request", nargs="?", default="{}", help="UniMem JSON object or request file.")
        child.add_argument("--dest")
        child.set_defaults(func=run_pool)


def run_pool(args: argparse.Namespace) -> int:
    body = request(args.request)
    root = paths.resolve_root(args.dest)
    operations: dict[str, Callable[..., Any]] = {"identity": pooled_io.issue_identity, "register": pooled_io.register,
                  "privacy": pooled_io.configure_privacy, "contribute": pooled_io.contribute,
                  "accept": pooled_io.accept, "report": pooled_io.report}
    try:
        result = operations[args.unimem_pool](root, **body)
    except TypeError as exc:
        raise ValueError("UniMem pooled request fields do not match the operation") from exc
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0
