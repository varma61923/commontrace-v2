"""Bounded operator JSON workflows for outcome, fleet and physical-AI evidence."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from unimem import frontier_protocol as fp
from unimem import paths

OPERATIONS = ("outcome-correct", "surrogate-fit", "sim-discrepancy", "simulation-weight", "recovery-register",
              "recovery-recall", "cluster-publish", "cluster-assign", "cluster-effect", "valuation",
              "valuation-certify", "valuation-verify", "warranty", "underwriting", "data-value", "spatial-update",
              "spatial-history", "uplift-fit", "attribution-fit", "royalties", "private-effect", "compile-build",
              "compile-trial", "compile-status", "vendor-report", "robot-ingest", "rewrite-propose",
              "rewrite-review", "rewrite-status", "canary-create", "canary-check", "certified-erase",
              "certified-verify", "symbiosis-bias")


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("frontier", help="Outcome, fleet, valuation and physical-AI JSON workflows.")
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("input", help="Bounded JSON request; use {} for reports without parameters.")
    parser.add_argument("--dest")
    parser.set_defaults(func=run)


def certified_verify(root: str, *, certificate: dict) -> bool:
    from unimem import certified_erasure

    return certified_erasure.verify(root, certificate)


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
    from unimem import (
        attribution,
        cleanroom,
        compile_memory,
        interference,
        memory_adapters,
        memory_ifc,
        outcomes,
        recovery,
        retention,
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
        "rewrite-propose": retention.propose, "rewrite-review": retention.review, "rewrite-status": retention.report,
        "canary-create": memory_ifc.create_canary, "canary-check": memory_ifc.egress_match,
        "certified-verify": lambda root, **request: {"valid": certified_verify(root, **request)},
    }
    pure = {"outcome-correct": outcomes.corrected_effect, "valuation": valuation.factorial,
            "attribution-fit": attribution.sparse_fit,
            "royalties": cleanroom.royalties, "symbiosis-bias": interference.symbiosis_bias}
    if args.operation in local:
        result = local[args.operation](root, **body)
    elif args.operation in pure:
        result = pure[args.operation](**body)
    elif args.operation == "surrogate-fit":
        result = asdict(outcomes.fit_surrogate(**body))
    elif args.operation == "uplift-fit":
        result = asdict(uplift.fit(**body))
        result["positive_classes"] = sorted(result["positive_classes"])
    elif args.operation == "certified-erase":
        from unimem import certified_erasure

        required = {"vault_directory", "subject", "identities", "reason"}
        if set(body) - required - {"probe"} or not required <= set(body):
            raise ValueError("certified-erase requires vault_directory, subject, identities and reason; "
                             "only probe is optional")
        probe = body.get("probe")
        retrieval_probe = None
        if probe is not None:
            from unimem import recall

            if (not isinstance(probe, dict) or set(probe) - {"queries", "forbidden", "threshold"}
                    or not {"queries", "forbidden"} <= set(probe)
                    or not isinstance(probe["queries"], list) or not isinstance(probe["forbidden"], list)):
                raise ValueError("erasure probe requires query and forbidden-token lists and optional threshold")
            # Validate every probe field before constructing a vault or deleting
            # content; the real retrieval diagnostic still runs after erasure.
            certified_erasure.probe(lambda query: [], probe["queries"], forbidden=probe["forbidden"],
                                    threshold=probe.get("threshold", 0))
            def retrieval_probe() -> dict:
                return certified_erasure.probe(
                    lambda query: [{"text": item.text} for item in recall.recall(root, query).items],
                    probe["queries"], forbidden=probe["forbidden"], threshold=probe.get("threshold", 0))
        vault = certified_erasure.SubjectVault(root, fp.text(body["vault_directory"], "vault directory", 4096))
        result = certified_erasure.erase(root, body["subject"], body["identities"], vault,
                                         reason=body["reason"], retrieval_probe=retrieval_probe)
    elif args.operation == "cluster-effect":
        result = interference.cluster_effect([interference.Cluster(**row) for row in body.pop("rows")], **body)
    else:
        from unimem.robotics import ingest
        format = body.pop("format")
        if format not in ("mcap", "lerobot"):
            raise ValueError("robot format must be mcap or lerobot")
        result = (ingest.mcap_file if format == "mcap" else ingest.lerobot_jsonl)(root, **body)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0
