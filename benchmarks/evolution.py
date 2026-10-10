"""Open adapter benchmark with cost, context, manifests and causal fixture metrics.

The default suite is a deterministic downstream-action simulator. Its causal
effects describe that fixture only, never vendor quality or live business value.
External adapters must supply measured cost/latency and stable evidence IDs.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import importlib
import importlib.metadata
import json
import platform
import random
import re
import statistics
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from commontrace import hierarchical, memory_control
from commontrace.causal_policy import snipw
from commontrace.search_recipes import recipe_manifest


@dataclass
class Retrieval:
    ids: list[str]
    context: str
    tokens: int
    latency_ms: float
    cost_usd: float | None


class Adapter(Protocol):
    def ingest(self, memories: list[dict]) -> None: ...
    def retrieve(self, question: str, budget: int) -> Retrieval: ...


class LocalAdapter:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory()
        self.ids: dict[str, str] = {}

    def ingest(self, memories: list[dict]) -> None:
        for memory in memories:
            fact, _ = hierarchical.append_facts(self.directory.name, [{"statement": memory["text"]}])[0]
            self.ids[fact.id] = memory["id"]

    def retrieve(self, question: str, budget: int) -> Retrieval:
        start = time.perf_counter()
        result = memory_control.reflect(self.directory.name, question, budget=budget, causal=False)
        return Retrieval([self.ids[r["id"]] for r in result["evidence"] if r["id"] in self.ids],
                         result["context"], result["tokens_estimate"], (time.perf_counter() - start) * 1000, 0.0)

    def close(self):
        self.directory.cleanup()


class NoMemoryAdapter:
    def ingest(self, memories: list[dict]) -> None:
        pass

    def retrieve(self, question: str, budget: int) -> Retrieval:
        return Retrieval([], "", 0, 0.0, 0.0)


def fixture(size: int = 40) -> dict:
    memories = [{"id": f"case-{i}", "text": f"Project meridian{i} release requires gatekeeper{i} approval."}
                for i in range(size)]
    cases = [{"id": f"task-{i}", "question": f"Which approval does meridian{i} release require?",
              "required": [f"case-{i}"], "action_value": f"gatekeeper{i}",
              "default_action": "unknown"} for i in range(size)]
    return {"name": "synthetic-release-approvals", "synthetic": True, "memories": memories, "cases": cases}


def _source_manifest() -> dict:
    def git(*args):
        result = subprocess.run(["git", *args], capture_output=True, text=True, check=False, timeout=30)
        return result.stdout.strip() if result.returncode == 0 else "unavailable"
    return {"git_commit": git("rev-parse", "HEAD"),
            "patch_sha256": hashlib.sha256(git("diff", "HEAD").encode()).hexdigest(),
            "python": platform.python_version(),
            "dependencies": {name: importlib.metadata.version(name) for name in ("PyYAML",)}}


def run(adapters: dict[str, Adapter], dataset: dict, *, budget: int = 120, seed: int = 17) -> dict:
    if not dataset.get("synthetic"):
        raise ValueError("this action simulator requires an explicitly synthetic dataset; use a real agent evaluator")
    rng = random.Random(seed)
    report = {"schema_version": 1, "synthetic": True, "dataset": dataset["name"], "seed": seed,
              "budget": budget, "dataset_sha256": hashlib.sha256(json.dumps(dataset, sort_keys=True).encode()).hexdigest(),
              "manifest": {**_source_manifest(), "recipes": recipe_manifest()}, "adapters": {}}
    # Same randomised assignment for every vendor, paired across identical tasks.
    assignments = [rng.random() < 0.5 for _ in dataset["cases"]]
    for name, adapter in adapters.items():
        adapter.ingest(dataset["memories"])
        rows, causal_rows = [], []
        try:
            for case, delivered in zip(dataset["cases"], assignments):
                result = adapter.retrieve(case["question"], budget)
                if result.tokens > budget or result.tokens < 0:
                    raise ValueError(f"{name} exceeded the declared context budget")
                required = set(case["required"])
                recall = len(required & set(result.ids)) / max(1, len(required))
                # Re-answer after deletion: actual emitted context, not retrieved IDs.
                full = float(bool(re.search(r"(?<!\w)" + re.escape(case["action_value"]) + r"(?!\w)",
                                            result.context)))
                empty = float(case["default_action"] == case["action_value"])
                outcome = full if delivered else empty
                causal_rows.append({"delivered": delivered, "outcome": outcome,
                                    "selection_probability": 1.0, "treatment_probability": 0.5})
                rows.append({"case": case["id"], "recall": recall, "action_success": full,
                             "ablation_credit": full - empty, "tokens": result.tokens,
                             "latency_ms": result.latency_ms, "cost_usd": result.cost_usd})
        finally:
            close = getattr(adapter, "close", None)
            if close:
                close()
        latencies = sorted(r["latency_ms"] for r in rows)
        metrics = {"recall": statistics.mean(r["recall"] for r in rows),
                   "action_success": statistics.mean(r["action_success"] for r in rows),
                   "mean_tokens": statistics.mean(r["tokens"] for r in rows),
                   "p50_latency_ms": statistics.median(latencies),
                   "p95_latency_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))],
                   "cost_usd": sum(r["cost_usd"] for r in rows) if all(r["cost_usd"] is not None for r in rows) else None,
                   "causal": snipw(causal_rows), "rows": rows}
        report["adapters"][name] = metrics
    return report


def stable_metrics(report: dict) -> dict:
    return {name: {"recall": m["recall"], "action_success": m["action_success"],
                   "mean_tokens": m["mean_tokens"], "causal_effect": m["causal"]["effect"]}
            for name, m in report["adapters"].items()}


def write_site(report: dict, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "run.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    cells = []
    for name, row in report["adapters"].items():
        values = [name, f"{row['recall']:.1%}", f"{row['action_success']:.1%}", f"{row['mean_tokens']:.1f}",
                  f"{row['p50_latency_ms']:.2f}", f"{row['p95_latency_ms']:.2f}", str(row["cost_usd"]),
                  str(row["causal"]["effect"])]
        cells.append("<tr>" + "".join("<td>" + html.escape(v) + "</td>" for v in values) + "</tr>")
    markup = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>CommonTrace reproducible memory benchmark</title><style>
body{font:17px system-ui;margin:4vw;max-width:1100px;color:#17232a;background:#fafbf7}
table{border-collapse:collapse;width:100%;display:block;overflow:auto}td,th{padding:14px;border-bottom:1px solid #ccd6cf;text-align:left}
a{color:#12683d}p{max-width:75ch;line-height:1.6}code{background:#e7ede5;padding:6px}
</style><h1>Memory at a small context budget</h1>
<p>Synthetic release-approval tasks. Accuracy, recall, context, latency and measured adapter cost appear together.
The randomised delivery/control estimate applies to this downstream-action simulator. It does not establish
real agent performance, independently verified vendor scores, or billable business value.</p>
<p>Reproduce in independent processes: <code>./reproduce.sh</code>. Inspect the dataset hash, configuration snapshot,
source revision and per-task results in <a href="run.json">the run manifest</a>.</p>
<table><thead><tr><th>Adapter</th><th>Recall</th><th>Action success</th><th>Tokens</th><th>p50 ms</th><th>p95 ms</th>
<th>Cost USD</th><th>Fixture effect</th></tr></thead><tbody>""" + "".join(cells) + "</tbody></table></html>"
    (destination / "index.html").write_text(markup, encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="benchmark-results/evolution")
    parser.add_argument("--budget", type=int, default=120)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--adapter", action="append", default=[], help="name=python.module:factory")
    args = parser.parse_args(argv)
    adapters: dict[str, Adapter] = {"commontrace-local": LocalAdapter(), "no-memory": NoMemoryAdapter()}
    for specification in args.adapter:
        name, factory_path = specification.split("=", 1)
        module, factory = factory_path.split(":", 1)
        adapters[name] = getattr(importlib.import_module(module), factory)()
    report = run(adapters, fixture(), budget=args.budget, seed=args.seed)
    report["manifest"]["adapter_factories"] = args.adapter
    write_site(report, Path(args.output))
    print(json.dumps(stable_metrics(report), indent=2))
    return 0 if report["adapters"]["commontrace-local"]["recall"] == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
