"""Exhaustive authorization-change proofs over bounded, closed finite domains.

Unknown principals, actions, classes, label sets and purposes are denied. Deny
rules override permits. Completeness applies only to the declared finite domain;
this is not a theorem about arbitrary attribute expressions or external services.
"""
from __future__ import annotations

import hashlib
import itertools
import math
from collections.abc import Mapping, Sequence
from typing import Any

from unimem import frontier_protocol as fp

MAX_TUPLES = 65536
ACTIONS = ("read", "write", "recall", "export")
AXES = ("principals", "actions", "memory_classes", "label_sets", "purposes")


def _names(values: Any, label: str, maximum: int = 64, *, empty: bool = False) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("UniMem policy "+label+" must be a finite list")
    fp.count(len(values), 0 if empty else 1, maximum, label)
    result = [fp.text(value, label, 128) for value in values]
    if len(set(result)) != len(result):
        raise ValueError("UniMem policy "+label+" contains repeated values")
    return sorted(result)


def normalize(policy: Mapping[str, Any]) -> dict[str, Any]:
    fp.wire(policy)
    if set(policy) != {"version", "domain", "rules"} or type(policy["version"]) is not int or policy["version"] != 1:
        raise ValueError("UniMem expected finite policy version 1, domain and rules")
    source = policy["domain"]
    if not isinstance(source, dict) or set(source) != set(AXES):
        raise ValueError("UniMem policy domain must declare all five finite axes")
    principals = source["principals"]
    if not isinstance(principals, list):
        raise ValueError("UniMem policy principals must be a finite list")
    fp.count(len(principals), 1, 32)
    normalized: list[dict[str, Any]] = []
    attributes: set[str] = set()
    for principal in principals:
        if not isinstance(principal, dict) or set(principal) != {"id", "class", "attributes"}:
            raise ValueError("UniMem principal needs an id, class and operator-owned attributes")
        if not isinstance(principal["attributes"], dict):
            raise ValueError("UniMem principal attributes must be a finite string mapping")
        fp.count(len(principal["attributes"]), 0, 8)
        principal_values = {fp.text(key, "principal attribute", 64): fp.text(value, "attribute value", 128)
                  for key, value in principal["attributes"].items()}
        attributes.update(principal_values)
        normalized.append({"id": fp.text(principal["id"], "principal", 128),
                           "class": fp.text(principal["class"], "principal class", 64),
                           "attributes": dict(sorted(principal_values.items()))})
    if len({principal["id"] for principal in normalized}) != len(normalized):
        raise ValueError("UniMem policy principal identities must be unique")
    domain: dict[str, Any] = {"principals": sorted(normalized, key=lambda row: row["id"]),
              "actions": _names(source["actions"], "actions", 4),
              "memory_classes": _names(source["memory_classes"], "memory classes", 32),
              "purposes": _names(source["purposes"], "purposes", 32)}
    if not set(domain["actions"]) <= set(ACTIONS):
        raise ValueError("UniMem policy supports read, write, recall and export only")
    label_sets = source["label_sets"]
    if not isinstance(label_sets, list):
        raise ValueError("UniMem policy label sets must be an explicitly enumerated list")
    fp.count(len(label_sets), 1, 64)
    labels = [_names(values, "labels", 32, empty=True) for values in label_sets]
    if len({tuple(values) for values in labels}) != len(labels):
        raise ValueError("UniMem policy label sets must be unique")
    domain["label_sets"] = sorted(labels)
    _bound(domain)
    rules = policy["rules"]
    if not isinstance(rules, list):
        raise ValueError("UniMem policy rules must be a bounded list")
    fp.count(len(rules), 0, 128)
    result: list[dict[str, Any]] = []
    identities: set[str] = set()
    selectors = {"principals", "principal_classes", "actions", "memory_classes", "purposes"}
    label_names = {label for values in labels for label in values}
    for rule in rules:
        if (not isinstance(rule, dict) or not {"id", "effect"} <= set(rule)
                or set(rule)-{"id", "effect", *selectors, "attributes", "required_labels", "forbidden_labels"}):
            raise ValueError("UniMem policy rule contains unknown fields or lacks id/effect")
        identity = fp.text(rule["id"], "rule identity", 128)
        if identity in identities or rule["effect"] not in ("allow", "deny"):
            raise ValueError("UniMem policy requires unique rule ids and allow/deny effects")
        identities.add(identity)
        item: dict[str, Any] = {"id": identity, "effect": rule["effect"]}
        for key in selectors:
            if key not in rule:
                continue
            values = _names(rule[key], "rule "+key, 64, empty=True)
            available = ({row["id"] for row in normalized} if key == "principals" else
                         {row["class"] for row in normalized} if key == "principal_classes" else set(domain[key]))
            if not set(values) <= available:
                raise ValueError("UniMem rule selector references an undeclared domain value")
            item[key] = values
        for key in ("required_labels", "forbidden_labels"):
            if key in rule:
                values = _names(rule[key], key, 32, empty=True)
                if not set(values) <= label_names:
                    raise ValueError("UniMem rule references an undeclared label")
                item[key] = values
        if "attributes" in rule:
            if not isinstance(rule["attributes"], dict) or not set(rule["attributes"]) <= attributes:
                raise ValueError("UniMem rule references an undeclared principal attribute")
            item["attributes"] = {key: _names(values, "attribute alternatives", 32, empty=True)
                                  for key, values in sorted(rule["attributes"].items())}
        result.append(item)
    return {"version": 1, "domain": domain, "rules": sorted(result, key=lambda row: row["id"])}


def _bound(domain: Mapping[str, Any]) -> int:
    count = math.prod(len(domain[axis]) for axis in AXES)
    fp.count(count, 1, MAX_TUPLES, "UniMem finite authorization tuple count")
    return count


def _allows(policy: Mapping[str, Any], principal: str, action: str, memory_class: str,
            labels: tuple[str, ...], purpose: str) -> bool:
    domain = policy["domain"]
    subject = next((row for row in domain["principals"] if row["id"] == principal), None)
    if (subject is None or action not in domain["actions"] or memory_class not in domain["memory_classes"]
            or purpose not in domain["purposes"] or list(labels) not in domain["label_sets"]):
        return False
    allowed = False
    for rule in policy["rules"]:
        if any(key in rule and value not in rule[key] for key, value in (
                ("principals", principal), ("principal_classes", subject["class"]),
                ("actions", action), ("memory_classes", memory_class), ("purposes", purpose))):
            continue
        if (not set(rule.get("required_labels", [])) <= set(labels)
                or set(rule.get("forbidden_labels", [])) & set(labels)):
            continue
        if any(subject["attributes"].get(key) not in values for key, values in rule.get("attributes", {}).items()):
            continue
        if rule["effect"] == "deny":
            return False
        allowed = True
    return allowed


def allows(policy: Mapping[str, Any], *, principal: str, action: str, memory_class: str,
           labels: Sequence[str], purpose: str) -> bool:
    normalized = normalize(policy)
    if isinstance(labels, (str, bytes)):
        raise ValueError("UniMem request labels must be a finite sequence of label strings")
    names = _names(list(labels), "request labels", 32, empty=True)
    return _allows(normalized, principal, action, memory_class, tuple(names), purpose)


def deny(domain: Mapping[str, Any]) -> dict[str, Any]:
    return normalize({"version": 1, "domain": dict(domain), "rules": []})


def difference(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """Complete symmetric change analysis, including changes in declared domains."""
    before, after = normalize(old), normalize(new)
    left, right = before["domain"], after["domain"]
    domain: dict[str, Any] = {key: sorted(set(left[key]) | set(right[key]))
                              for key in ("actions", "memory_classes", "purposes")}
    domain["principals"] = sorted({row["id"] for row in [*left["principals"], *right["principals"]]})
    domain["label_sets"] = sorted({tuple(values) for values in [*left["label_sets"], *right["label_sets"]]})
    count = _bound(domain)
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    widening, narrowing = hashlib.sha256(), hashlib.sha256()
    counts = [0, 0]
    for principal, action, memory_class, labels, purpose in itertools.product(*(domain[axis] for axis in AXES)):
        old_allow = _allows(before, principal, action, memory_class, labels, purpose)
        new_allow = _allows(after, principal, action, memory_class, labels, purpose)
        if old_allow == new_allow:
            continue
        index = 0 if new_allow else 1
        counts[index] += 1
        row = {"principal": principal, "action": action, "memory_class": memory_class,
               "labels": list(labels), "purpose": purpose}
        (widening if new_allow else narrowing).update(fp.wire(row)+b"\n")
        target = added if new_allow else removed
        if len(target) < 64:
            target.append(row)
    return {"old_hash": fp.digest(before), "new_hash": fp.digest(after), "domain_hash": fp.digest(domain),
            "tuples_checked": count, "widening_count": counts[0], "narrowing_count": counts[1],
            "no_widening": counts[0] == 0, "added": added, "removed": removed,
            "widening_digest": widening.hexdigest(), "narrowing_digest": narrowing.hexdigest(),
            "witnesses_truncated": counts[0] > len(added) or counts[1] > len(removed),
            "classification": "sound and complete exhaustive check over the declared closed finite domain",
            "bound": MAX_TUPLES,
            "assumptions": ["undeclared principal/action/class/label set/purpose is denied",
                            "principal attributes are operator-owned and bound to authenticated identities",
                            "deny overrides allow; declarative v1 grammar only",
                            "external relationship services and future undeclared domains are not proved"]}
