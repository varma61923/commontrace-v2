"""Build a local, unjudged retrieval report without mixing experiment contracts."""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
from pathlib import Path

from benchmarks.measurement import dataset_digest
from benchmarks.phase0 import INPUTS


def read_bound(path: Path):
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def reviewed_experiment(directory: Path, *, development=None) -> dict:
    path = directory / "scorecard.json"
    report, source_sha = read_bound(path)
    manifest, manifest_sha = read_bound(directory / "manifest.json")
    strict = development is not None
    scope = ("unjudged excluded-question strict-budget confirmation" if strict
             else "unjudged development retrieval evidence")
    budget_field = "budgets_text_tokens" if strict else "budgets_estimated"
    if manifest.get("scope") != scope or manifest.get(budget_field) != [1000, 2000]:
        raise ValueError("report directory has a different experiment contract")
    if strict:
        contract = manifest["context_budget"]
        if (manifest.get("development_manifest_sha256") != development["source_manifest_sha256"]
                or contract["unit"] != "selected-tokenizer-context-text"
                or contract["counter"]["name"] != "tiktoken:cl100k_base"
                or contract["max_attempts"] != 16 or contract["full_context"] != "uncapped"):
            raise ValueError("confirmation uses a different development or enforcement contract")
    elif manifest.get("exact_text_counter") != "tiktoken:cl100k_base":
        raise ValueError("development text counter is not cl100k")
    for dataset, selection in manifest["selection"].items():
        if selection["input_sha256"] != INPUTS[dataset][1]:
            raise ValueError("report selection uses different official input bytes")
        ids = selection["question_ids"]
        if len(ids) != len(set(ids)):
            raise ValueError("report question identities are not unique")
        if strict:
            old = set(development["selection"][dataset]["question_ids"])
            if old.intersection(ids) or set(selection["excluded_ids"]) != old:
                raise ValueError("confirmation overlaps or misstates development exclusions")
    if report["answer_accuracy"] is not None:
        raise ValueError("this report renderer supports unjudged retrieval experiments only")
    reviewed = copy.deepcopy(report)
    reviewed["selection"] = manifest["selection"]
    attempts = {a["label"]: a for a in manifest["attempts"]}
    if len(attempts) != len(manifest["attempts"]):
        raise ValueError("manifest contains duplicate attempts")
    excluded = {}
    raw_bindings = {}
    for label, budgets in list(reviewed["conditions"].items()):
        if any(s["overall"]["accuracy"] is not None for s in budgets.values()):
            raise ValueError("unjudged report contains reader accuracy")
        artifact = directory / (label + ".json")
        if artifact.resolve().parent != directory.resolve():
            raise ValueError("condition artifact lies outside the report directory")
        raw, raw_sha = read_bound(artifact)
        attempt = attempts.get(label, {})
        if attempt.get("status") != "completed" or attempt.get("result_sha256") != raw_sha:
            raise ValueError("raw artifact does not match a completed manifest attempt")
        if set(budgets) != {"1000", "2000"}:
            raise ValueError("report lacks the requested budgets")
        if set(raw) != set(budgets) or any(raw[b]["provenance"]["evaluation"]["answer_enabled"] is not False
                                         for b in budgets):
            raise ValueError("raw artifacts do not establish disabled reader inference")
        for budget, summary in budgets.items():
            source = raw[budget]
            for key in ("overall", "by_type", "bootstrap_95ci"):
                if summary[key] != source[key]:
                    raise ValueError("scorecard metric summary disagrees with raw artifact")
            if summary["profile"] != source["retrieval_profile"]:
                raise ValueError("scorecard profile disagrees with raw artifact")
            dataset = source["dataset"]
            evaluation = source["provenance"]["evaluation"]
            if evaluation["context_text_tokenizer"] != "tiktoken:cl100k_base":
                raise ValueError("raw text counter is not cl100k")
            if strict:
                if (evaluation.get("context_budget") != contract
                        or source["provenance"]["sampling"].get("question_exclusions_sha256")
                        != manifest["selection"][dataset]["question_exclusions_sha256"]):
                    raise ValueError("raw strict budget/exclusion contract differs")
                if any(isinstance(r["context_text_tokens"], bool)
                       or not isinstance(r["context_text_tokens"], int)
                       or not 0 <= r["context_text_tokens"] <= int(budget) for r in source["rows"]):
                    raise ValueError("raw context text exceeds strict cap")
            elif "context_budget" in evaluation:
                raise ValueError("development artifact uses an unexpected strict contract")
            ids = [r["id"] for r in source["rows"]]
            if (len(ids) != len(set(ids))
                    or set(ids) != set(manifest["selection"][dataset]["question_ids"])):
                raise ValueError("raw question selection disagrees with manifest")
        raw_bindings[label] = raw_sha
        if any(s["profile"].get("profile") == "graphiti-episodic"
               and s["profile"].get("group_namespace") != "sha256-canonical-space-utf8"
               for s in budgets.values()):
            excluded[label] = {"reason": "case namespace lacks the binding validated for punctuation-bearing IDs",
                               "raw_result_sha256": raw_sha}
            del reviewed["conditions"][label]
    if excluded:
        reviewed["comparisons"] = {label: value for label, value in reviewed["comparisons"].items()
                                   if "graphiti-episodic" not in label}
    reviewed["excluded_artifacts"] = excluded
    reviewed["raw_source_sha256"] = raw_bindings
    reviewed["source_scorecard_sha256"] = source_sha
    reviewed["source_manifest_sha256"] = manifest_sha
    return reviewed


def write_report(development: Path, confirmation: Path, output: Path) -> None:
    dev = reviewed_experiment(development)
    experiments = {"Estimated-budget development": dev,
                   "Strict-cap excluded-question confirmation": reviewed_experiment(confirmation, development=dev)}
    report = {"scope": "offline retrieval diagnostics", "answer_accuracy": None,
              "renderer_sha256": dataset_digest(__file__), "experiments": experiments}
    sections = []
    for title, experiment in experiments.items():
        rows = []
        for label, budgets in sorted(experiment["conditions"].items()):
            for budget, summary in budgets.items():
                overall = summary["overall"]
                evidence = overall["evidence"]
                values = [label, budget, str(overall["n"]),
                          "unknown" if evidence is None else f"{evidence:.1%}",
                          str(overall["context_text_tokens"]), str(overall["exact_budget_exceedances"]),
                          "Not run"]
                rows.append("<tr>" + "".join("<td>" + html.escape(v) + "</td>" for v in values) + "</tr>")
        exclusions = "".join("<li>" + html.escape(label + ": " + reason["reason"]) + "</li>"
                             for label, reason in experiment["excluded_artifacts"].items())
        sections.append("<section><h2>" + html.escape(title) + "</h2><table><thead><tr>"
                        "<th>Condition</th><th>Budget</th><th>Questions</th><th>Source coverage</th>"
                        "<th>Mean cl100k text tokens</th><th>Cap exceedances</th><th>Reader accuracy</th>"
                        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
                        + ("<h3>Excluded configuration artifacts</h3><ul>" + exclusions + "</ul>" if exclusions else "")
                        + "</section>")
    markup = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CommonTrace local retrieval diagnostics</title><style>
body{font:16px system-ui;margin:4vw;color:#17232a;background:#fafbf7}p{max-width:90ch;line-height:1.6}
section{margin-top:3rem}table{border-collapse:collapse;display:block;overflow:auto}
th,td{padding:12px;border-bottom:1px solid #ccd6cf;text-align:left}a{color:#12683d}
</style></head><body><h1>CommonTrace retrieval diagnostics</h1>
<p>Offline subsets, with reader and judge inference disabled. Source-turn coverage can credit an excerpt
without its answer-bearing span. It is not answer accuracy, task success or demonstrated causal value.
Local Mem0 raw and Graphiti episodic profiles omit LLM extraction and managed/cloud behavior.</p>
<p>The two experiments have different questions and budget contracts; compare conditions within each.
Full-context and no-memory controls, per-question source reports, paired intervals, model/source pins and
failed attempts remain in the original artifacts. Inference cost is unmeasured; no reader/judge calls ran.
Indexing follows native lifecycles, so ingestion and recall timing must be read together.</p>
<p>Inspect <a href="reviewed-scorecard.json">the source-bound reviewed scorecard</a>.
Reproduce with <code>python -m benchmarks.phase0</code> then <code>python -m benchmarks.confirmation</code>,
using their documented input/output arguments. Public performance and scale targets remain unestablished.</p>
""" + "".join(sections) + "</body></html>"
    output.mkdir(parents=True, exist_ok=False)
    (output / "reviewed-scorecard.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output / "index.html").write_text(markup, encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development", required=True)
    parser.add_argument("--confirmation", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    write_report(Path(args.development), Path(args.confirmation), Path(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
