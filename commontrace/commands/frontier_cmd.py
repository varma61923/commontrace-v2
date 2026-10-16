"""Bounded operator JSON workflows for outcome, fleet and physical-AI evidence."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from commontrace import frontier_protocol as fp
from commontrace import paths

OPERATIONS = ("outcome-correct", "surrogate-fit", "sim-discrepancy", "simulation-weight", "recovery-register",
              "recovery-recall", "cluster-publish", "cluster-assign", "cluster-effect", "valuation",
              "valuation-certify", "valuation-verify", "warranty", "underwriting", "data-value", "spatial-update",
              "spatial-history", "uplift-fit", "attribution-fit", "royalties", "private-effect", "compile-build",
              "compile-trial", "compile-status", "vendor-report", "robot-ingest")


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("frontier", help="Outcome, fleet, valuation and physical-AI JSON workflows.")
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("input", help="Bounded JSON request; use {} for reports without parameters.")
    parser.add_argument("--dest")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    with open(args.input, "rb") as stream:
        raw = stream.read(fp.MAX_BYTES + 1)
    if len(raw) > fp.MAX_BYTES:
        raise ValueError("frontier request exceeds 1 MiB")
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise ValueError("frontier request must be an object")
    fp.wire(body)
    root = paths.resolve_root(args.dest)
    from commontrace import (
        attribution,
        cleanroom,
        compile_memory,
        interference,
        memory_adapters,
        outcomes,
        recovery,
        spatial,
        uplift,
        valuation,
    )

    local = {
        "sim-discrepancy": outcomes.record_discrepancy, "simulation-weight": outcomes.simulation_weight,
        "recovery-register": recovery.register, "recovery-recall": recovery.recall,
        "cluster-publish": interference.publish, "cluster-assign": interference.assign_cluster,
        "valuation-certify": valuation.certify_logged, "valuation-verify": valuation.verify,
        "warranty": valuation.warranty_credit, "underwriting": valuation.underwriting_export,
        "data-value": valuation.export_data,
        "spatial-update": spatial.update, "spatial-history": spatial.history,
        "private-effect": cleanroom.private_effect, "compile-build": compile_memory.build,
        "compile-trial": compile_memory.register_trial, "compile-status": compile_memory.stale,
        "vendor-report": memory_adapters.comparative_report,
    }
    pure = {"outcome-correct": outcomes.corrected_effect, "valuation": valuation.factorial,
            "attribution-fit": attribution.sparse_fit,
            "royalties": cleanroom.royalties}
    if args.operation in local:
        result = local[args.operation](root, **body)
    elif args.operation in pure:
        result = pure[args.operation](**body)
    elif args.operation == "surrogate-fit":
        result = asdict(outcomes.fit_surrogate(**body))
    elif args.operation == "uplift-fit":
        result = asdict(uplift.fit(**body))
        result["positive_classes"] = sorted(result["positive_classes"])
    elif args.operation == "cluster-effect":
        result = interference.cluster_effect([interference.Cluster(**row) for row in body.pop("rows")], **body)
    else:
        from commontrace.robotics import ingest
        format = body.pop("format")
        if format not in ("mcap", "lerobot"):
            raise ValueError("robot format must be mcap or lerobot")
        result = (ingest.mcap_file if format == "mcap" else ingest.lerobot_jsonl)(root, **body)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0
