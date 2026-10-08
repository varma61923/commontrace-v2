"""Export reviewed structural experience, omitting raw traces and tenant identity.

The operator supplies public tool categories and a signed export approval.
This format is an interoperability primitive, not differential privacy or a
federated-learning trainer. No external transmission happens here.
"""
from __future__ import annotations

import hashlib
import json

from commontrace import origin


def prepare(skill: dict, *, public_tools: dict[str, str], minimum_cases: int = 5) -> dict:
    data = skill.get("data", {})
    cases = int(data.get("beta_alpha", 1)) + int(data.get("beta_beta", 1)) - 2
    if cases < minimum_cases:
        raise ValueError("insufficient aggregate experience for federation")
    public_steps = []
    for step in data.get("steps", []):
        tool = step.get("tool", "")
        if tool not in public_tools:
            raise ValueError("every step requires an operator-approved public tool category")
        public_steps.append({"tool_category": public_tools[tool], "has_guard": bool(step.get("guard")),
                             "has_branch": bool(step.get("branch"))})
    payload = {"schema_version": 1, "steps": public_steps, "case_count": cases,
               "reliability_mean": data.get("reliability_mean"),
               "abstracted": True, "raw_data_included": False}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**payload, "export_digest": digest}


def export(skill: dict, *, public_tools: dict[str, str], approval: dict,
           principals: dict[str, origin.Principal], minimum_cases: int = 5) -> dict:
    payload = prepare(skill, public_tools=public_tools, minimum_cases=minimum_cases)
    if not origin.verify(approval, principals, authority="federation-export") or (
            approval.get("record", {}).get("export_digest") != payload["export_digest"]):
        raise PermissionError("federation export requires approval bound to this exact public payload")
    return payload
