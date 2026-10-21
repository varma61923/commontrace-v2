"""Optional real Cedar validation and finite-domain policy interchange.

The adapter projects Cedar onto the explicitly declared UniMem request domain.
It does not claim equivalence for requests outside that domain or expose a Lean
proof. The pinned Rust-backed SDK performs schema validation and authorization.
"""
from __future__ import annotations

import importlib
import itertools
import json
import math
from importlib.metadata import version
from typing import Any

from unimem import frontier_protocol as fp
from unimem import policy_proof as pure

SDK_VERSION = "4.12.2"
MAX_REQUESTS = 8192


def _sdk() -> Any:
    try:
        sdk = importlib.import_module("cedarpy")
    except ImportError as exc:
        raise RuntimeError("Install unimem[policy-cedar] for actual Cedar validation") from exc
    if version("cedarpy") != SDK_VERSION:
        raise ValueError("UniMem Cedar adapter requires its tested cedarpy=="+SDK_VERSION+" pin")
    return sdk


def _resource(memory_class: str, labels: tuple[str, ...]) -> str:
    return fp.digest({"memory_class": memory_class, "labels": list(labels)})


def _schema(policy: dict[str, Any]) -> dict[str, Any]:
    principals = policy["domain"]["principals"]
    names = {name for principal in principals for name in principal["attributes"]}
    attributes = {name: {"type": "String", "required": all(name in row["attributes"] for row in principals)}
                  for name in names}
    if "class" in attributes:
        raise ValueError("UniMem Cedar reserves the principal class attribute name")
    attributes["class"] = {"type": "String"}
    return {"UniMem": {"entityTypes": {
        "Principal": {"shape": {"type": "Record", "attributes": attributes}},
        "Resource": {"shape": {"type": "Record", "attributes": {
            "memory_class": {"type": "String"}, "labels": {"type": "Set", "element": {"type": "String"}}}}}},
        "actions": {name: {"appliesTo": {"principalTypes": ["Principal"], "resourceTypes": ["Resource"],
            "context": {"type": "Record", "attributes": {"purpose": {"type": "String"}}}}}
                    for name in policy["domain"]["actions"]}}}


def _matrix(policy: dict[str, Any]) -> list[tuple[str, str, str, tuple[str, ...], str]]:
    domain = policy["domain"]
    fp.count(math.prod(len(domain[axis]) for axis in pure.AXES), 1, MAX_REQUESTS,
             "UniMem Cedar adapter finite requests")
    rows = list(itertools.product([p["id"] for p in domain["principals"]], domain["actions"],
        domain["memory_classes"], [tuple(labels) for labels in domain["label_sets"]], domain["purposes"]))
    fp.count(len(rows), 1, MAX_REQUESTS, "UniMem Cedar adapter finite requests")
    return rows


def _analyze(text: str, policy: dict[str, Any]) -> tuple[list[tuple[str, str, str, tuple[str, ...], str]], list[bool]]:
    if not isinstance(text, str) or "\x00" in text or len(text.encode()) > 512*1024:
        raise ValueError("UniMem Cedar policy text exceeds 512 KiB or is malformed")
    sdk = _sdk()
    schema = _schema(policy)
    validation = sdk.validate_policies(text, schema)
    if not validation.validation_passed:
        raise ValueError("UniMem Cedar schema validation failed; no policy was imported or activated")
    domain = policy["domain"]
    entities = [{"uid": {"type": "UniMem::Principal", "id": row["id"]},
                 "attrs": {"class": row["class"], **row["attributes"]}, "parents": []}
                for row in domain["principals"]]
    resources = set()
    for memory_class, labels in itertools.product(domain["memory_classes"], domain["label_sets"]):
        identity = _resource(memory_class, tuple(labels))
        if identity in resources:
            raise ValueError("UniMem Cedar resource content commitment collision")
        resources.add(identity)
        entities.append({"uid": {"type": "UniMem::Resource", "id": identity},
                         "attrs": {"memory_class": memory_class, "labels": labels}, "parents": []})
    fp.wire(entities)
    rows = _matrix(policy)
    requests = [{"principal": "UniMem::Principal::"+json.dumps(p), "action": "UniMem::Action::"+json.dumps(a),
                 "resource": "UniMem::Resource::"+json.dumps(_resource(c, labels)), "context": {"purpose": purpose},
                 "correlation_id": str(index)} for index, (p, a, c, labels, purpose) in enumerate(rows)]
    results = sdk.is_authorized_batch(requests, text, entities, schema=schema)
    if (len(results) != len(rows) or any(result.diagnostics.errors or result.correlation_id != str(index)
                                        for index, result in enumerate(results))):
        raise ValueError("UniMem Cedar authorization analysis returned errors or reordered requests")
    return rows, [bool(result.allowed) for result in results]


def export(policy: dict[str, Any]) -> dict[str, Any]:
    normalized = pure.normalize(policy)
    groups: dict[tuple[str, str, str, tuple[str, ...]], list[str]] = {}
    for principal, action, memory_class, labels, purpose in _matrix(normalized):
        if pure._allows(normalized, principal, action, memory_class, labels, purpose):
            groups.setdefault((principal, action, memory_class, labels), []).append(purpose)
    statements = []
    for (principal, action, memory_class, labels), purposes in sorted(groups.items()):
        condition = " || ".join("context.purpose == "+json.dumps(purpose) for purpose in purposes)
        statements.append("permit (principal == UniMem::Principal::"+json.dumps(principal)
            +", action == UniMem::Action::"+json.dumps(action)+", resource == UniMem::Resource::"
            +json.dumps(_resource(memory_class, labels))+") when { "+condition+" };")
    text = "\n".join(statements)
    rows, decisions = _analyze(text, normalized)
    if any(decision != pure._allows(normalized, *row) for row, decision in zip(rows, decisions)):
        raise ValueError("UniMem Cedar export disagrees with the finite reference semantics")
    return {"cedar": text, "schema": _schema(normalized), "domain": normalized["domain"],
            "policy_hash": fp.digest(normalized), "analysis": {"sdk": "cedarpy=="+SDK_VERSION,
                "engine": "Cedar 4.12.0", "schema_valid": True, "requests_compared": len(rows),
                "classification": "actual SDK finite-domain equivalence check; not a general formal proof"}}


def import_policy(text: str, domain: dict[str, Any]) -> dict[str, Any]:
    baseline = pure.deny(domain)
    rows, decisions = _analyze(text, baseline)
    names = {label for labels in baseline["domain"]["label_sets"] for label in labels}
    rules: list[dict[str, Any]] = []
    for row, decision in zip(rows, decisions):
        if not decision:
            continue
        fp.count(len(rules), 0, 127, "UniMem imported finite rules; narrow the declared domain")
        principal, action, memory_class, labels, purpose = row
        rules.append({"id": "cedar-"+fp.digest(row), "effect": "allow", "principals": [principal],
                      "actions": [action], "memory_classes": [memory_class], "purposes": [purpose],
                      "required_labels": list(labels), "forbidden_labels": sorted(names-set(labels))})
    policy = pure.normalize({**baseline, "rules": rules})
    return {"policy": policy, "source_hash": fp.digest(text), "analysis": {"sdk": "cedarpy=="+SDK_VERSION,
        "engine": "Cedar 4.12.0", "schema_valid": True, "requests_compared": len(rows),
        "classification": "finite-domain projection of an actual Cedar authorization analysis",
        "outside_domain": "not imported; every undeclared UniMem request is denied"}}
