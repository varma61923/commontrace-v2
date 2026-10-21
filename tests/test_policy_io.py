"""Operator-reviewed policy activation is content-bound and serialized."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from tests import test_skill_foundry
from tests.test_policy_proof import example
from unimem import frontier_protocol as fp
from unimem import memory_authority
from unimem import policy_io as io

clock = test_skill_foundry.clock


def configured_policy():
    policy = example()
    policy["domain"]["purposes"].append("default")
    return policy


def install(root):
    certificate = io.certify(root, configured_policy())
    review = io.review(root, certificate, reason="Reviewed the exact finite owner and action expansion.")
    return io.activate(root, certificate, review_receipt=review)


def test_unreviewed_widening_never_activates_and_actual_permissions_change(tmp_path, clock):
    root = str(tmp_path)
    assert not io.enabled(root)
    assert io.permits(root, {"memory_type": "general", "scopes": ["legacy"]})
    result = io.diff(root, configured_policy())
    assert not result["difference"]["no_widening"]
    assert not io.enabled(root)
    certificate = io.certify(root, configured_policy())
    with pytest.raises(PermissionError, match="review"):
        io.activate(root, certificate)
    assert not io.enabled(root)
    review = io.review(root, certificate, reason="Reviewed the exact owner/domain matrix.")
    io.activate(root, certificate, review_receipt=review)
    assert io.current(root)["generation"] == 1
    assert io.permits(root, {"memory_type": "general", "scopes": ["project:a"]}, action="read")
    assert not io.permits(root, {"memory_type": "general", "scopes": ["undeclared"]}, action="read")
    with memory_authority.writer("unregistered", "agent"):
        assert not io.permits(root, {"memory_type": "general", "scopes": ["project:a"]})


def test_narrowing_needs_no_extra_review_and_old_certificate_cannot_replay(tmp_path, clock):
    root = str(tmp_path)
    install(root)
    narrowed = configured_policy()
    narrowed["rules"][0]["actions"] = ["read", "recall"]
    certificate = io.certify(root, narrowed)
    assert certificate["certificate"]["record"]["difference"]["no_widening"]
    io.activate(root, certificate)
    assert io.current(root)["generation"] == 2
    assert not io.permits(root, {"memory_type": "general", "scopes": []}, action="export")
    with pytest.raises(PermissionError, match="stale"):
        io.activate(root, certificate)


def test_review_is_bound_to_exact_content_generation_and_root(tmp_path, clock):
    root = str(tmp_path/"owner")
    first = io.certify(root, configured_policy())
    receipt = io.review(root, first, reason="Reviewed first policy only.")
    changed = configured_policy()
    changed["rules"] = [rule for rule in changed["rules"] if rule["id"] != "private-health"]
    second = io.certify(root, changed)
    with pytest.raises(PermissionError, match="review"):
        io.activate(root, second, review_receipt=receipt)
    forged = deepcopy(first)
    forged["certificate"]["record"]["difference"]["no_widening"] = True
    with pytest.raises(PermissionError, match="forged"):
        io.activate(root, forged)
    with pytest.raises(PermissionError, match="forged"):
        io.activate(str(tmp_path/"other"), first, review_receipt=receipt)
    io.activate(root, first, review_receipt=receipt)
    with pytest.raises(PermissionError, match="stale"):
        io.activate(root, second, review_receipt=receipt)


def test_expired_review_and_agent_made_certificate_do_not_activate(tmp_path, clock):
    root = str(tmp_path)
    certificate = io.certify(root, configured_policy())
    receipt = io.review(root, certificate, reason="Short review window.", expires_seconds=60)
    clock.tick(61)
    with pytest.raises(PermissionError, match="expired"):
        io.activate(root, certificate, review_receipt=receipt)
    with memory_authority.writer("agent", "agent"):
        with pytest.raises(PermissionError, match="operator"):
            io.certify(root, configured_policy())
        with pytest.raises(PermissionError, match="operator"):
            io.review(root, certificate, reason="self promotion")
    clock.tick(3600)
    with pytest.raises(PermissionError, match="expired"):
        io.review(root, certificate, reason="late review")


def test_concurrent_same_generation_activation_has_exactly_one_winner(tmp_path, clock):
    root = str(tmp_path)
    certificate = io.certify(root, configured_policy())
    receipt = io.review(root, certificate, reason="Reviewed finite matrix once.")

    def activate(_):
        try:
            io.activate(root, certificate, review_receipt=receipt)
            return True
        except PermissionError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(activate, range(2))) == 1
    assert io.current(root)["generation"] == 1
    assert len(fp.journal(root, io.ACTIVATIONS)) == 1


def test_cedar_proposal_runs_actual_sdk_analysis_before_certification(tmp_path, clock):
    pytest.importorskip("cedarpy", reason="optional native Cedar validation")
    from unimem import policy_cedar

    policy = configured_policy()
    exported = policy_cedar.export(policy)
    certificate = io.certify(str(tmp_path), exported["cedar"], format="cedar", domain=exported["domain"])
    assert certificate["certificate"]["record"]["cedar_analysis"]["requests_compared"] == 144
    assert certificate["certificate"]["record"]["cedar_analysis"]["schema_valid"]


def test_cli_policy_review_and_activation_round_trip(tmp_path, clock, capsys):
    import json

    from unimem import cli

    def invoke(operation, body):
        assert cli.main(["prove", "policy", operation, json.dumps(body), "--dest", str(tmp_path)]) == 0
        return json.loads(capsys.readouterr().out)

    certificate = invoke("certify", {"policy": configured_policy()})
    receipt = invoke("review", {"certificate": certificate, "reason": "Reviewed exact CLI proposal."})
    invoke("activate", {"certificate": certificate, "review_receipt": receipt})
    assert invoke("current", {})["generation"] == 1
