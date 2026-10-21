"""Finite policy proofs include purpose, labels, attributes and domain changes."""
from __future__ import annotations

from copy import deepcopy

import pytest

from unimem import policy_proof as policy


def example():
    return {"version": 1, "domain": {
        "principals": [{"id": "operator:local", "class": "administrator", "attributes": {"region": "us"}},
                       {"id": "agent:support", "class": "support", "attributes": {"region": "us"}}],
        "actions": list(policy.ACTIONS), "memory_classes": ["general", "health"],
        "label_sets": [[], ["project:a"], ["health", "project:a"]], "purposes": ["support", "medical"]},
        "rules": [{"id": "admin", "effect": "allow", "principal_classes": ["administrator"]},
                  {"id": "support", "effect": "allow", "principals": ["agent:support"],
                   "actions": ["read", "recall"], "memory_classes": ["general"], "purposes": ["support"],
                   "required_labels": ["project:a"], "attributes": {"region": ["us"]}},
                  {"id": "private-health", "effect": "deny", "required_labels": ["health"]}]}


def request(**changes):
    return {"principal": "agent:support", "action": "read", "memory_class": "general",
            "labels": ["project:a"], "purpose": "support", **changes}


def test_deny_overrides_and_every_undeclared_axis_is_closed():
    current = example()
    assert policy.allows(current, **request())
    for changes in ({"labels": ["health", "project:a"]}, {"purpose": "medical"}, {"action": "export"},
                    {"memory_class": "health"}, {"principal": "operator:forged"}, {"action": "delete"},
                    {"labels": ["project:a", "unknown"]}, {"purpose": "future"}):
        assert not policy.allows(current, **request(**changes))
    assert not policy.allows(current, **request(principal="operator:local", labels=["health", "project:a"]))
    with pytest.raises(ValueError):
        policy.allows(current, **request(labels="project:a"))


def test_equivalent_rewrite_has_no_widening_over_the_full_domain():
    old = example()
    new = deepcopy(old)
    new["rules"] = [rule for rule in new["rules"] if rule["id"] != "admin"]
    new["rules"] += [{"id": "admin-"+action, "effect": "allow", "principal_classes": ["administrator"],
                      "actions": [action]} for action in policy.ACTIONS]
    result = policy.difference(old, new)
    assert result["tuples_checked"] == 96
    assert result["no_widening"] and result["widening_count"] == result["narrowing_count"] == 0
    assert result["old_hash"] != result["new_hash"]


def test_planted_widening_is_detected_for_each_action_class_label_and_purpose():
    old = example()
    baseline = policy.deny(old["domain"])
    # Every individual request can be selected exactly by the finite grammar.
    for action in policy.ACTIONS:
        for memory_class in old["domain"]["memory_classes"]:
            for labels in old["domain"]["label_sets"]:
                for purpose in old["domain"]["purposes"]:
                    new = deepcopy(baseline)
                    new["rules"] = [{"id": "planted", "effect": "allow", "principals": ["agent:support"],
                        "actions": [action], "memory_classes": [memory_class], "purposes": [purpose],
                        "required_labels": labels, "forbidden_labels": sorted({"health", "project:a"}-set(labels))}]
                    result = policy.difference(baseline, new)
                    assert not result["no_widening"] and result["widening_count"] == 1
                    assert result["added"][0] == {"principal": "agent:support", "action": action,
                         "memory_class": memory_class, "labels": sorted(labels), "purpose": purpose}


def test_purpose_domain_expansion_and_attribute_change_are_actual_widening():
    old = example()
    new = deepcopy(old)
    new["domain"]["purposes"].append("new-purpose")
    result = policy.difference(old, new)
    assert result["widening_count"] == 16
    assert {row["purpose"] for row in result["added"]} == {"new-purpose"}
    old["rules"][1]["attributes"] = {"region": ["eu"]}
    new = deepcopy(old)
    new["domain"]["principals"][1]["attributes"]["region"] = "eu"
    result = policy.difference(old, new)
    assert result["widening_count"] == 2


def test_domain_bound_applies_to_policy_and_union_before_evaluation():
    domain = {"principals": [{"id": "p"+str(i), "class": "agent", "attributes": {}} for i in range(32)],
              "actions": list(policy.ACTIONS), "memory_classes": ["m"+str(i) for i in range(16)],
              "label_sets": [[]], "purposes": ["u"+str(i) for i in range(32)]}
    old = policy.deny(domain)
    new = deepcopy(old)
    new["domain"]["label_sets"] = [["other"]]
    with pytest.raises(ValueError, match="tuple count"):
        policy.difference(old, new)
    new["domain"]["label_sets"].append([])
    with pytest.raises(ValueError, match="tuple count"):
        policy.normalize(new)


def test_no_implicit_wildcards_and_unknown_rules_are_not_ignored():
    current = example()
    for update in ({"expression": "principal.role == admin"}, {"actions": ["*"]},
                   {"attributes": {"undeclared": ["value"]}}):
        changed = deepcopy(current)
        changed["rules"][0].update(update)
        with pytest.raises(ValueError):
            policy.normalize(changed)
    changed = deepcopy(current)
    changed["domain"]["principals"][0]["attributes"]["region"] = float("nan")
    with pytest.raises(ValueError):
        policy.normalize(changed)
