"""Export reviewed structural experience, omitting raw traces and tenant identity.

The operator supplies public tool categories and a signed export approval.
This format is an interoperability primitive, not differential privacy or a
federated-learning trainer. No external transmission happens here.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets

from commontrace import _jsonl, memory_control, origin, paths


def prepare(skill: dict, *, public_tools: dict[str, str], minimum_cases: int = 5) -> dict:
    data = skill.get("data", {})
    alpha, beta = data.get("beta_alpha", 1), data.get("beta_beta", 1)
    if (not isinstance(minimum_cases, int) or isinstance(minimum_cases, bool) or minimum_cases < 1
            or any(not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 10000000 for v in (alpha, beta))):
        raise ValueError("federation requires bounded integer aggregate counts")
    cases = alpha + beta - 2
    if cases < minimum_cases:
        raise ValueError("insufficient aggregate experience for federation")
    public_steps = []
    for step in data.get("steps", []):
        tool = step.get("tool", "")
        if tool not in public_tools:
            raise ValueError("every step requires an operator-approved public tool category")
        category = public_tools[tool]
        if not isinstance(category, str) or not category or len(category) > 64:
            raise ValueError("public tool category must be bounded text")
        public_steps.append({"tool_category": category, "has_guard": bool(step.get("guard")),
                             "has_branch": bool(step.get("branch"))})
    payload = {"schema_version": 1, "steps": public_steps, "case_count": cases,
               "reliability_mean": alpha / (alpha + beta),
               "abstracted": True, "raw_data_included": False}
    digest = hashlib.sha256(origin._bytes(payload)).hexdigest()
    return {**payload, "export_digest": digest}


def export(skill: dict, *, public_tools: dict[str, str], approval: dict,
           principals: dict[str, origin.Principal], minimum_cases: int = 5) -> dict:
    payload = prepare(skill, public_tools=public_tools, minimum_cases=minimum_cases)
    if not origin.verify(approval, principals, authority="federation-export") or (
            approval.get("record", {}).get("export_digest") != payload["export_digest"]):
        raise PermissionError("federation export requires approval bound to this exact public payload")
    return payload


def configure_privacy(root: str, *, epsilon: float) -> None:
    """One immutable noise budget for experimental aggregate releases."""
    if isinstance(epsilon, bool) or not math.isfinite(epsilon) or not 0 < epsilon <= 10:
        raise ValueError("privacy budget must be finite in (0, 10]")
    filename = os.path.join(paths.memory_dir(root), "federation_privacy.json")
    with _jsonl.locked(filename):
        if os.path.exists(filename):
            raise ValueError("privacy budget is already configured")
        _jsonl.write_json(filename, {"epsilon": epsilon, "neighbor_unit": "experimental outcome counts"})


def private_prepare(root: str, skill: dict, *, public_tools: dict[str, str], epsilon: float) -> dict:
    """Release Laplace-protected outcome counts, charging sequential composition.

    Public procedure structure is separately operator-approved; only outcome
    counts receive experimental noise. Eligibility and finite-precision sampling
    are not a verified differential-privacy mechanism.
    A release is charged even if an operator later decides not to export it.
    """
    if isinstance(epsilon, bool) or not math.isfinite(epsilon) or epsilon <= 0:
        raise ValueError("release epsilon must be positive and finite")
    prepared = prepare(skill, public_tools=public_tools)
    directory = paths.memory_dir(root)
    config = os.path.join(directory, "federation_privacy.json")
    ledger = os.path.join(directory, "federation_releases.jsonl")
    with _jsonl.locked(config):
        with open(config, encoding="utf-8") as fh:
            budget = json.load(fh)
        spent = sum(row["epsilon"] for row in _jsonl.read_rows(ledger))
        if spent + epsilon > budget["epsilon"] + 1e-12:
            raise PermissionError("federation privacy budget exhausted")
        rng = secrets.SystemRandom()
        def noisy(value):
            u = max(-.5 + 1e-12, min(.5 - 1e-12, rng.random() - .5))
            return value - (2 / epsilon) * math.copysign(math.log1p(-2 * abs(u)), u)
        data = skill["data"]
        payload = {"schema_version": 2, "steps": prepared["steps"],
            "private_counts": {"successes": noisy(data["beta_alpha"] - 1),
                               "failures": noisy(data["beta_beta"] - 1)},
            "privacy": {"epsilon": epsilon, "delta": None, "neighbor_unit": "experimental outcome counts",
                        "structure_protected": False}, "abstracted": True, "raw_data_included": False}
        digest = hashlib.sha256(origin._bytes(payload)).hexdigest()
        _jsonl.append_row(ledger, {"epsilon": epsilon, "export_digest": digest})
    return {**payload, "export_digest": digest}


def authorize_payload(payload: dict, *, approval: dict, principals: dict[str, origin.Principal]) -> dict:
    body = {k: v for k, v in payload.items() if k != "export_digest"}
    digest = hashlib.sha256(origin._bytes(body)).hexdigest()
    if payload.get("export_digest") != digest or not origin.verify(approval, principals, authority="federation-export"):
        raise PermissionError("invalid protected payload or export authority")
    if approval.get("record", {}).get("export_digest") != digest:
        raise PermissionError("approval is bound to another public payload")
    return payload


def receive(root: str, payload: dict, *, approval: dict, principals: dict[str, origin.Principal],
            context: list[str] | None = None) -> dict:
    """Import authenticated protected experience as a review-only proposal."""
    authorize_payload(payload, approval=approval, principals=principals)
    base = {"schema_version", "steps", "abstracted", "raw_data_included", "export_digest"}
    version = payload.get("schema_version")
    expected = base | ({"case_count", "reliability_mean"} if version == 1 else {"private_counts", "privacy"})
    if (version not in (1, 2) or set(payload) != expected or payload["abstracted"] is not True
            or payload["raw_data_included"] is not False):
        raise ValueError("unsupported protected experience schema")
    steps = payload["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 100:
        raise ValueError("protected experience requires bounded structural steps")
    for step in steps:
        if (not isinstance(step, dict) or set(step) != {"tool_category", "has_guard", "has_branch"}
                or not isinstance(step["tool_category"], str) or not 1 <= len(step["tool_category"]) <= 64
                or not all(isinstance(step[k], bool) for k in ("has_guard", "has_branch"))):
            raise ValueError("invalid public structural step")
    if version == 1:
        count, mean = payload["case_count"], payload["reliability_mean"]
        if (not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 20000000
                or isinstance(mean, bool) or not isinstance(mean, (int, float))
                or not math.isfinite(mean) or not 0 <= mean <= 1):
            raise ValueError("invalid aggregate statistics")
    else:
        counts, privacy = payload["private_counts"], payload["privacy"]
        if (not isinstance(counts, dict) or set(counts) != {"successes", "failures"}
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                       for v in counts.values()) or not isinstance(privacy, dict)
                or set(privacy) != {"epsilon", "delta", "neighbor_unit", "structure_protected"}
                or privacy["delta"] is not None or privacy["structure_protected"] is not False
                or privacy["neighbor_unit"] != "experimental outcome counts"
                or isinstance(privacy["epsilon"], bool) or not isinstance(privacy["epsilon"], (int, float))
                or not math.isfinite(privacy["epsilon"]) or not 0 < privacy["epsilon"] <= 10):
            raise ValueError("invalid private aggregate statistics")
    rid = "federated-" + payload["export_digest"]
    existing = next((r for r in memory_control.records(root, "proposal") if r["id"] == rid), None)
    if existing:
        return existing  # Replays never create independent evidence or change a rejection.
    return memory_control.put(root, "proposal", "Review protected cross-organization procedure experience",
           record_id=rid, labels=context, data={"status": "review", "received_statistics": payload,
           "source_organization": principals[approval["principal"]].organization,
           "proof_count": 0, "raw_baseline_required": True})


def pool_effects(estimates: list[dict]) -> dict:
    """Descriptive random-effects meta-analysis; normal intervals, not anytime-valid proof."""
    if len(estimates) < 2:
        raise ValueError("pooling needs at least two independent organizations")
    organizations = [e["organization"] for e in estimates]
    if len(set(organizations)) != len(organizations):
        raise ValueError("one independent estimate per configured organization is required")
    values = [(float(e["effect"]), float(e["standard_error"])) for e in estimates]
    if any(not math.isfinite(y) or not -1 <= y <= 1 or not math.isfinite(se) or not 1e-9 <= se <= 1e6
           for y, se in values):
        raise ValueError("invalid bounded effect or standard error")
    weights = [1 / se ** 2 for _y, se in values]
    fixed = sum(w * y for w, (y, _se) in zip(weights, values)) / sum(weights)
    q = sum(w * (y - fixed) ** 2 for w, (y, _se) in zip(weights, values))
    c = sum(weights) - sum(w * w for w in weights) / sum(weights)
    tau2 = max(0, (q - (len(values) - 1)) / c)
    random_weights = [1 / (se ** 2 + tau2) for _y, se in values]
    mean = sum(w * y for w, (y, _se) in zip(random_weights, values)) / sum(random_weights)
    se = math.sqrt(1 / sum(random_weights))
    return {"effect": mean, "ci_low": mean - 1.959964 * se, "ci_high": mean + 1.959964 * se,
            "tau_squared": tau2, "i_squared": max(0, (q - len(values) + 1) / q) if q else 0,
            "organizations": len(values), "billing_eligible": False, "interval": "normal approximation"}
