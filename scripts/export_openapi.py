"""Export the gateway's OpenAPI contracts without a live store.

`sdk/openapi.json` is the memory SDK subset the generated clients are built
from; `openapi/gateway.json` is every gateway route.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from commontrace.gateway import Gateway

ROOT = Path(__file__).resolve().parents[1]


def full_document() -> dict:
    with tempfile.TemporaryDirectory() as root:
        result = Gateway(root)._openapi({}, {})
    result["info"]["license"] = {"name": "MIT", "url": "https://opensource.org/license/mit/"}
    result["servers"] = [{"url": "http://127.0.0.1:8765", "description": "Local gateway; configure HTTPS for hosting"}]
    return result


def document() -> dict:
    with tempfile.TemporaryDirectory() as root:
        result = Gateway(root)._openapi({}, {})
    result.pop("tags", None)
    result["paths"] = {path: value for path, value in result["paths"].items() if path.startswith("/v1/memory/")}
    # The SDK clients take the memory operations only: no shared header parameters and
    # no schemas the memory paths never reference.
    for operation in (op for item in result["paths"].values() for op in item.values()):
        del operation["parameters"]
        for response in operation["responses"].values():
            response.pop("headers", None)
    components = result["components"]
    components.pop("parameters", None)
    components.pop("headers", None)
    wanted, queue = set(), [result["paths"]]
    while queue:
        node = queue.pop()
        if isinstance(node, dict):
            target = node.get("$ref", "")
            if target.startswith("#/components/schemas/") and target.rsplit("/", 1)[1] not in wanted:
                wanted.add(target.rsplit("/", 1)[1])
                queue.append(components["schemas"][target.rsplit("/", 1)[1]])
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    components["schemas"] = {name: value for name, value in components["schemas"].items() if name in wanted}
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
    parser.add_argument("--full-out", type=Path, default=ROOT/"openapi"/"gateway.json")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for out, doc in ((args.out, document()), (args.full_out, full_document())):
        content = json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True)+"\n"
        if args.check:
            if not out.is_file() or out.read_text() != content:
                parser.exit(1, f"OpenAPI contract {out.name} is stale; run python scripts/export_openapi.py\n")
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
