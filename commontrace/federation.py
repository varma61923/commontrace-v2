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
    expected = base | ({"case_count", "reliability_mean"} if version == 1 else
                       {"public_cohort_id", "public_cohort_size", "randomized_positive", "privacy"}
                       if version == 3 else {"private_counts", "privacy"})
    if (version not in (1, 2, 3) or set(payload) != expected or payload["abstracted"] is not True
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
    elif version == 3:
        size, count, privacy = payload["public_cohort_size"], payload["randomized_positive"], payload["privacy"]
        if (isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 1000000
                or isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= size
                or not isinstance(payload["public_cohort_id"], str) or not 1 <= len(payload["public_cohort_id"]) <= 128
                or privacy != {"mechanism": "binary-randomized-response-3/4", "epsilon": math.log(3), "delta": 0,
                   "adjacency": "one outcome replacement in fixed public cohort",
                   "membership_protected": False, "structure_protected": False}):
            raise ValueError("invalid exact randomized response release")
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
    c = 2*sum(a*b for i, a in enumerate(weights) for b in weights[i+1:])/sum(weights)
    tau2 = max(0, (q - (len(values) - 1)) / c)
    random_weights = [1 / (se ** 2 + tau2) for _y, se in values]
    mean = sum(w * y for w, (y, _se) in zip(random_weights, values)) / sum(random_weights)
    se = math.sqrt(1 / sum(random_weights))
    return {"effect": mean, "ci_low": mean - 1.959964 * se, "ci_high": mean + 1.959964 * se,
            "tau_squared": tau2, "i_squared": max(0, (q - len(values) + 1) / q) if q else 0,
            "organizations": len(values), "billing_eligible": False, "interval": "normal approximation"}


def randomized_response(root: str, outcomes: list[bool], *, public_cohort_id: str,
                        public_cohort_size: int, public_steps: list[dict]) -> dict:
    """Exact binary randomized response, event-level replacement adjacency.

    Each bit is retained with probability 3/4 and flipped with probability 1/4.
    An outcome replacement changes the output likelihood by at most 3, giving
    epsilon=ln(3), delta=0 under parallel composition within a fixed PUBLIC cohort.
    Membership, cohort size and separately approved structure are not protected.
    Secure integer sampling avoids floating-point Laplace truncation claims.
    """
    from commontrace import memory_authority

    if memory_authority.WRITER.get()[1] != "operator":
        raise PermissionError("protected releases require an operator")
    if (not isinstance(public_cohort_id, str) or not 1 <= len(public_cohort_id) <= 128
            or isinstance(public_cohort_size, bool) or not isinstance(public_cohort_size, int)
            or not 1 <= public_cohort_size <= 1000000 or len(outcomes) != public_cohort_size
            or not all(isinstance(v, bool) for v in outcomes)):
        raise ValueError("fixed public cohort size and boolean outcomes required")
    if not isinstance(public_steps, list) or not 1 <= len(public_steps) <= 100:
        raise ValueError("operator-approved public structure required")
    for step in public_steps:
        if (not isinstance(step, dict) or set(step) != {"tool_category", "has_guard", "has_branch"}
                or not isinstance(step["tool_category"], str) or not 1 <= len(step["tool_category"]) <= 64
                or not all(isinstance(step[k], bool) for k in ("has_guard", "has_branch"))):
            raise ValueError("invalid public structural step")
    config = os.path.join(paths.memory_dir(root), "federation_privacy.json")
    ledger = os.path.join(paths.memory_dir(root), "federation_releases.jsonl")
    epsilon = math.log(3)
    with _jsonl.locked(config):
        with open(config, encoding="utf-8") as fh:
            budget = json.load(fh)
        releases = _jsonl.read_rows(ledger)
        spent = sum(r["epsilon"] for r in releases)
        if spent+epsilon > budget["epsilon"]:
            raise PermissionError("federation privacy budget exhausted")
        positive = sum(v if secrets.randbelow(4) else not v for v in outcomes)
        body = {"schema_version": 3, "steps": public_steps, "public_cohort_id": public_cohort_id,
                "public_cohort_size": public_cohort_size, "randomized_positive": positive,
                "privacy": {"mechanism": "binary-randomized-response-3/4", "epsilon": epsilon,
                            "delta": 0, "adjacency": "one outcome replacement in fixed public cohort",
                            "membership_protected": False, "structure_protected": False},
                "abstracted": True, "raw_data_included": False}
        digest = hashlib.sha256(origin._bytes(body)).hexdigest()
        _jsonl.append_row(ledger, {"epsilon": epsilon, "export_digest": digest, "mechanism": "exact-binary-rr"})
    return {**body, "export_digest": digest}


def replicated_lift(receipts: list[dict], *, principals: dict[str, origin.Principal],
                    signer: origin.Principal) -> dict:
    """Authenticate per-fleet estimates before descriptive random-effects pooling."""
    estimates, reference = [], None
    for receipt in receipts:
        if not origin.verify(receipt, principals, authority="fleet-effect"):
            raise PermissionError("untrusted fleet effect attestation")
        record = receipt["record"]
        required = {"artifact_sha256", "comparison", "metric", "effect", "standard_error", "simulated"}
        if set(record) != required or not isinstance(record["simulated"], bool):
            raise ValueError("fleet effect requires a common artifact, comparison, metric and evidence class")
        if (not isinstance(record["artifact_sha256"], str) or len(record["artifact_sha256"]) != 64
                or not isinstance(record["comparison"], str) or not record["comparison"]
                or not isinstance(record["metric"], str) or not record["metric"]):
            raise ValueError("invalid fleet effect identity")
        identity = (record["artifact_sha256"], record["comparison"], record["metric"], record["simulated"])
        if reference is not None and identity != reference:
            raise ValueError("cannot pool different artifacts, comparisons, metrics or simulated/real evidence")
        reference = identity
        estimates.append({"organization": principals[receipt["principal"]].organization,
                          "effect": record["effect"], "standard_error": record["standard_error"]})
    result = pool_effects(estimates)
    record = {"artifact_sha256": reference[0], "comparison": reference[1], "metric": reference[2],
              "simulated": reference[3], "pooled": result, "input_sha256": hashlib.sha256(
                  origin._bytes({"receipts": receipts})).hexdigest(), "fleet_receipts": receipts,
              "billable_proof": False, "kind": "replicated-lift-certificate"}
    return origin.bind(record, signer)
