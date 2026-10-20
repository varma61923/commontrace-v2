"""The optional adapter uses the actual pinned Cedar engine, never a mock."""
from __future__ import annotations

import pytest

pytest.importorskip("cedarpy", reason="install commontrace[policy-cedar] for native Cedar checks")

from commontrace import policy_cedar, policy_proof  # noqa: E402
from tests.test_policy_proof import example  # noqa: E402


def test_actual_native_cedar_export_import_has_identical_finite_permissions():
    original = example()
    exported = policy_cedar.export(original)
    assert exported["analysis"]["schema_valid"] and exported["analysis"]["requests_compared"] == 96
    restored = policy_cedar.import_policy(exported["cedar"], exported["domain"])
    difference = policy_proof.difference(original, restored["policy"])
    assert difference["no_widening"] and difference["narrowing_count"] == 0


def test_real_cedar_broad_permit_is_detected_as_widening_in_the_finite_domain():
    original = example()
    imported = policy_cedar.import_policy("permit(principal, action, resource);", original["domain"])
    difference = policy_proof.difference(original, imported["policy"])
    assert difference["widening_count"] > 0
    assert "not imported" in imported["analysis"]["outside_domain"]


def test_real_cedar_schema_analysis_rejects_undeclared_attribute_or_bad_syntax():
    for text in ('permit(principal,action,resource) when {principal.undeclared == "admin"};',
                 'permit this is not a policy;'):
        with pytest.raises(ValueError):
            policy_cedar.import_policy(text, example()["domain"])
