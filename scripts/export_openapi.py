"""Export the public memory SDK's standard OpenAPI contract without a live store."""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from commontrace.gateway import Gateway

ROOT = Path(__file__).resolve().parents[1]


def document() -> dict:
    with tempfile.TemporaryDirectory() as root:
        result = Gateway(root)._openapi({}, {})
    result["paths"] = {path: value for path, value in result["paths"].items() if path.startswith("/v1/memory/")}
    result["info"]["description"] = (
        "Append-only memory, source-linked recall and observed outcomes. Bearer agent keys bind the owner scope; "
        "a request cannot replace its principal's context. Retrieved evidence never authorizes an action. "
        "Use HTTPS for hosted gateways; localhost is intended for a trusted local operator.")
    result["info"]["license"] = {"name": "MIT", "url": "https://opensource.org/license/mit/"}
    result["servers"] = [{"url": "http://127.0.0.1:8765", "description": "Local gateway; configure HTTPS for hosting"}]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT/"sdk"/"openapi.json")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = json.dumps(document(), indent=2, ensure_ascii=False, sort_keys=True)+"\n"
    if args.check:
        if not args.out.is_file() or args.out.read_text() != content:
            parser.exit(1, "OpenAPI contract is stale; run python scripts/export_openapi.py\n")
    else:
        args.out.write_text(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
