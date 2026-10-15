"""Train the learned ontology typer shipped in ``commontrace/ontology_typing_model.json``.

Rows come from labeled typing fixtures (``name``, ``context``, ``expected``; a null
``expected`` trains the ``none`` class). Training is deterministic for a given seed
and set of fixtures, so the shipped weights can be reproduced:

    python -m benchmarks.train_ontology_typing --fixture benchmarks/fixtures/ontology_typing_gemma_train.json

Never train on a fixture that is used to report held-out accuracy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

from commontrace import ontology_learned


def load_rows(paths: list[str]) -> tuple[list[tuple[str, str, str]], list[str]]:
    rows, digests = [], []
    for path in paths:
        with open(path, "rb") as fh:
            raw = fh.read()
        digests.append(f"{os.path.basename(path)}:{hashlib.sha256(raw).hexdigest()[:16]}")
        for row in json.loads(raw)["examples"]:
            rows.append((row["name"], row["context"], row["expected"] or ontology_learned.NONE))
    return rows, digests


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", action="append", required=True, help="labeled fixture; repeatable")
    parser.add_argument("--out", default=ontology_learned.MODEL_FILE)
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    rows, digests = load_rows(args.fixture)
    model = ontology_learned.train(rows, epochs=args.epochs, seed=args.seed)
    model.meta.update({"fixtures": digests, "epochs": args.epochs, "seed": args.seed})
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(model.to_dict(), fh, separators=(",", ":"), sort_keys=True)
        fh.write("\n")
    print(f"{len(rows)} rows, {model.meta['features']} features -> {args.out} "
          f"({os.path.getsize(args.out) // 1024} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
