"""Scope admission remains equivalent to the frozen oracle at every label shape."""
from __future__ import annotations

import itertools
import random

import pytest

from benchmarks.scope_admission import measure, original_matches
from unimem import memory_control


def test_exhaustive_mixed_scope_sets_and_principal_modes():
    vocabulary = ("tenant:owner", "project:p", "legacy", "unknown:kind", "repo", ":empty", "session:")
    combinations = [list(items) for size in range(3) for items in itertools.combinations(vocabulary, size)]
    for principal in ("", "tenant:owner", "tenant:other"):
        token = memory_control.REQUIRED_PRINCIPAL_SCOPE.set(principal)
        try:
            for labels, context, governed in itertools.product(combinations, combinations, (False, True)):
                assert memory_control.matches(labels, context, governed=governed) == original_matches(
                    labels, context, governed=governed), (principal, labels, context, governed)
        finally:
            memory_control.REQUIRED_PRINCIPAL_SCOPE.reset(token)


def test_duplicate_unicode_and_dense_labels_match_oracle():
    rng = random.Random(4518)
    vocabulary = [f"project:{i}" for i in range(1000)] + ["tenant:租户", "legacy", "unknown:kind", "tenant"]
    for _ in range(50):
        labels = rng.choices(vocabulary, k=rng.randint(1, 1500))
        context = labels + ["extra"] if rng.randrange(2) else rng.choices(vocabulary, k=1500)
        assert memory_control.matches(labels, context) == original_matches(labels, context)


def test_principal_and_context_changes_apply_immediately():
    labels, context = ["tenant:owner", "legacy"], ["tenant:owner", "legacy"]
    token = memory_control.REQUIRED_PRINCIPAL_SCOPE.set("tenant:owner")
    try:
        assert memory_control.matches(labels, context)
        memory_control.REQUIRED_PRINCIPAL_SCOPE.set("tenant:other")
        assert not memory_control.matches(labels, context)
        assert memory_control.matches(labels, context, governed=False)
        context.remove("legacy")
        assert not memory_control.matches(labels, context, governed=False)
    finally:
        memory_control.REQUIRED_PRINCIPAL_SCOPE.reset(token)


def test_benchmark_has_equal_decisions_and_restores_principal():
    token = memory_control.REQUIRED_PRINCIPAL_SCOPE.set("tenant:owner")
    try:
        output = measure(labels=10, trials=1, repetitions=2)
        assert len(output["admission_sha256"]) == 64
        assert output["calls_per_trial"] == 10
        assert all(len(values) == 1 for values in output["samples_ms"].values())
        assert memory_control.REQUIRED_PRINCIPAL_SCOPE.get() == "tenant:owner"
    finally:
        memory_control.REQUIRED_PRINCIPAL_SCOPE.reset(token)


@pytest.mark.parametrize("parameter,value", [("labels", True), ("labels", 0), ("labels", 10001),
                                           ("trials", 0), ("repetitions", 0), ("repetitions", 1.5)])
def test_benchmark_rejects_unbounded_or_ambiguous_inputs(parameter, value):
    with pytest.raises(ValueError):
        measure(**{"labels": 1, "trials": 1, "repetitions": 1, parameter: value})
