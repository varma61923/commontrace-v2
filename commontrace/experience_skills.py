"""Contrastive skill proposals, structural retrieval and causal level changes."""
from __future__ import annotations

import hashlib
import json
import os
import re

from commontrace import _jsonl, frontmatter, memory_control, paths

LEVELS = ("trace", "memory", "skill", "rule")


def structural_signature(steps: list[dict]) -> str:
    """Transfer by tool/control-flow shape, not just words in a description."""
    shape = [{"tool": step.get("tool", ""), "inputs": sorted(step.get("inputs", {}).keys()),
              "branch": step.get("branch", ""), "guard": step.get("guard", "")} for step in steps]
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()


def propose(root: str, name: str, *, steps: list[dict], cases: list[dict], applies_when: str,
            do_not_apply_when: str, context: list[str] | None = None) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise ValueError("skill name must be a slug")
    if not steps or not applies_when or not do_not_apply_when:
        raise ValueError("skills require steps and positive/negative applicability bounds")
    if not cases or any(not c.get("trace_id") or not isinstance(c.get("succeeded"), bool) for c in cases):
        raise ValueError("cases require source traces and explicit boolean outcomes")
    from commontrace.commands._traces import load_trace_instances

    traces = {str(t["id"]) for t in load_trace_instances(root)}
    if any(c["trace_id"] not in traces for c in cases):
        raise ValueError("skill cites an unavailable raw trace")
    # Repeated case rows do not become independent reliability evidence.
    outcomes = {}
    for case in cases:
        if case["trace_id"] in outcomes and outcomes[case["trace_id"]] != case["succeeded"]:
            raise ValueError("one trace has conflicting outcomes")
        outcomes[case["trace_id"]] = case["succeeded"]
    successes = sum(outcomes.values())
    failures = len(outcomes) - successes
    data = {"status": "review", "name": name, "steps": steps, "sources": sorted(outcomes),
            "applies_when": applies_when, "do_not_apply_when": do_not_apply_when,
            "signature": structural_signature(steps), "beta_alpha": 1 + successes, "beta_beta": 1 + failures,
            "reliability_mean": (1 + successes) / (2 + successes + failures),
            "contrastive": bool(successes and failures), "level": "skill", "raw_baseline_required": True}
    row = memory_control.put(root, "proposal", "Procedure " + name, labels=context, data=data)
    directory = os.path.join(paths.memory_dir(root), "skill_proposals", name, row["revision"])
    body = "\n".join([f"# {name}", "", f"Applies when: {applies_when}",
                      f"Do not apply when: {do_not_apply_when}", "", "## Steps",
                      *[f"{i + 1}. {step.get('tool', 'action')}: {step.get('description', '')}"
                        for i, step in enumerate(steps)], "", "## Evidence",
                      *["- " + source for source in sorted(outcomes)]])
    frontmatter.write(os.path.join(directory, "SKILL.md"), data, body + "\n")
    return row


def causal_gate(*, raw_effect: float | None, abstract_effect: float | None, ci_low: float | None,
                independent_review: bool, effective_samples: float, min_samples: int = 30) -> dict:
    """Require paired randomised abstraction-vs-raw evidence plus review.

    The bound must be for abstract minus raw. Callers supply an independently
    computed bound; a posterior reliability score alone cannot admit a skill.
    """
    import math

    evidence = (raw_effect, abstract_effect, ci_low, effective_samples)
    if any(v is None or not math.isfinite(v) for v in evidence):
        return {"admit": False, "reason": "causal evidence incomplete"}
    admit = independent_review and effective_samples >= min_samples and ci_low > 0
    return {"admit": admit, "reason": "causal and review gates passed" if admit else "keep raw trace available",
            "raw_effect": raw_effect, "abstract_effect": abstract_effect, "ci_low": ci_low}


def change_level(root: str, memory_id: str, level: str, *, verdict: str, evidence: dict) -> dict:
    if level not in LEVELS or verdict not in ("HELPS", "HURTS", "NO_MEASURABLE_EFFECT"):
        raise ValueError("invalid level or causal verdict")
    if not evidence.get("experiment_id") or not evidence.get("raw_comparison"):
        raise ValueError("level changes require an experiment and comparison to the raw trace")
    path = os.path.join(paths.memory_dir(root), "compression_policy.jsonl")
    row = {"memory_id": memory_id, "level": level, "verdict": verdict, "evidence": evidence}
    with _jsonl.locked(path):
        _jsonl.append_row(path, row)
    return row


def cluster_agent_cases(root: str, *, minimum_cases: int = 2) -> list[dict]:
    """Cluster explicitly instrumented cases by procedure shape; never infer outcomes."""
    from commontrace.commands._traces import load_trace_instances

    clusters = {}
    for trace in load_trace_instances(root):
        procedure = trace.get("extensions", {}).get("profile", {}).get("procedure", {})
        steps = procedure.get("steps")
        succeeded = trace.get("outcome", {}).get("resolved")
        if not isinstance(steps, list) or not steps or not isinstance(succeeded, bool):
            continue
        signature = structural_signature(steps)
        labels = sorted(trace.get("scopes", []))
        scope_signature = hashlib.sha256(json.dumps(labels).encode()).hexdigest()[:12]
        key = (signature, scope_signature)
        cluster = clusters.setdefault(key, {"steps": steps, "cases": [], "scopes": labels})
        cluster["cases"].append({"trace_id": trace["id"], "succeeded": succeeded})
    proposals = []
    existing = {(r["data"].get("signature"), tuple(sorted(r["scopes"])))
                for r in memory_control.records(root, "proposal")}
    for (signature, scope_signature), cluster in clusters.items():
        if len(cluster["cases"]) >= minimum_cases and (signature, tuple(cluster["scopes"])) not in existing:
            proposals.append(propose(root, "procedure-" + signature[:12] + "-" + scope_signature,
                steps=cluster["steps"],
                cases=cluster["cases"], context=cluster["scopes"],
                applies_when="The task matches the evidenced tool and input structure.",
                do_not_apply_when="The tool, input contract, or authority differs from the source cases."))
    return proposals
