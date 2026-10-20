"""Sentinel restrictions consume authenticated prospective randomized evidence."""
from __future__ import annotations

import json

import pytest

from commontrace import (
    cli,
    contagion,
    holdout_io,
    lineage,
    memory_authority,
    promotion,
    unit_trials,
)
from commontrace import frontier_protocol as fp
from commontrace import nextwave_store as store
from commontrace import (
    sentinel_io as io,
)
from tests import test_skill_foundry
from tests.test_contagion import fact

clock = test_skill_foundry.clock


def prepare(root, clock, *, enabled=True):
    holdout_io.configure(root, rate=.5, salt="sentinel-test")
    if enabled:
        io.configure(root)
    clock.tick()


def transfer_traffic(root, unit, clock, *, n=150, benign=False, prefix="transfer"):
    for segment in ("origin", "other"):
        for index in range(n):
            occasion = f"{prefix}-{segment}-{index}"
            held = holdout_io.assign_and_log(root, [unit], occasion_id=occasion, rate=.5,
                                            salt="sentinel-test", context=segment)
            injected = unit not in held
            holdout_io.record_outcome(root, occasion, injected if segment == "origin" or benign else not injected)
            clock.tick(1)


def test_prospective_poison_blocks_induction_compilation_reflection_and_export(tmp_path, clock):
    from commontrace import compile_memory, memory_control, skill_io
    from commontrace.commands import dream_cmd, export_cmd

    root = str(tmp_path)
    prepare(root, clock)
    source = fact(root, "Inspect the checksum before release.")
    derived = fact(root, "Require checksum verification for every deployment.", sources=[source.id])
    contagion.record_read(root, [source.id], "reviewer", "episode", "report")
    registration = io.register(root, source.id, origin="origin", segments=["origin", "other"])
    assert registration["alpha"] == .025
    transfer_traffic(root, source.id, clock)
    assert io.status(root, source.id)["quarantine"]
    # The source check runs before a trainer, model, or candidate is invoked.
    with pytest.raises(PermissionError, match="sentinel"):
        compile_memory.build(root, "blocked", [source.id], target="lora", model="tiny", open_weight=True)
    assert not fp.journal(root, "compiled-artifacts.jsonl")
    assert memory_authority.lineage_blocked(root, source.id)
    assert memory_authority.lineage_blocked(root, derived.id)
    assert not memory_authority.lineage_blocked(root, source.id, canonical_only=True)
    assert lineage.labels(root, derived.id)["review_required"]
    assert io.status(root, source.id)["quarantine"]  # Operator inspection survives read denial.
    for operation in (
        lambda: memory_control.reflect(root, "release"),
        lambda: skill_io.induce(root, "Release review", "Measured review", [source.id]),
        lambda: list(export_cmd._trace_rows(root, None)),
        lambda: dream_cmd.main_dream(root),
    ):
        with pytest.raises(PermissionError, match="sentinel"):
            operation()
    event = store.events(root, io.QUARANTINES)[0]
    assert "evidence_digest" in event and "radius_digest" in event
    assert source.statement not in json.dumps(event)


def test_benign_transfer_and_source_scope_are_preserved(tmp_path, clock):
    root = str(tmp_path)
    prepare(root, clock)
    source = fact(root, "Inspect private firmware signatures.", scopes=["private"])
    with pytest.raises(PermissionError):
        io.register(root, source.id, origin="origin", segments=["origin", "other"])
    io.register(root, source.id, origin="origin", segments=["origin", "other"], context=["private"])
    transfer_traffic(root, source.id, clock, benign=True)
    with pytest.raises(PermissionError):
        io.status(root, source.id)
    result = io.quarantine(root, source.id, context=["private"])
    assert not result["quarantine"]
    io.assert_generalize(root, [source.id], context=["private"])
    assert not memory_authority.lineage_blocked(root, source.id)
    private_poison = fact(root, "Use measured firmware timing before release.", scopes=["private"])
    io.register(root, private_poison.id, origin="origin", segments=["origin", "other"], context=["private"])
    transfer_traffic(root, private_poison.id, clock, n=100, prefix="private-poison")
    # Global operator jobs must not miss a private family by using empty context.
    with pytest.raises(PermissionError, match="sentinel"):
        io.assert_generalize(root, [private_poison.id])


def test_preregistration_excludes_old_evidence_and_spends_alpha_once_per_source(tmp_path, clock):
    root = str(tmp_path)
    prepare(root, clock)
    source = fact(root, "Review package signatures.")
    transfer_traffic(root, source.id, clock, n=50, prefix="old")
    io.register(root, source.id, origin="origin", segments=["origin", "other"])
    result = io.status(root, source.id)
    assert not result["quarantine"] and result["tests"][0]["n"] == 0
    with pytest.raises(ValueError, match="already preregistered"):
        io.register(root, source.id, origin="other", segments=["origin", "other"])
    second = fact(root, "Review unrelated robot force limits.")
    registered = io.register(root, second.id, origin="origin", segments=["origin", "other"])
    assert registered["alpha"] == pytest.approx(.05/6)


def test_incomplete_unanchored_or_changed_source_cannot_authorize_generalization(tmp_path, clock):
    root = str(tmp_path)
    prepare(root, clock)
    source = fact(root, "Use the measured current limit.")
    io.register(root, source.id, origin="origin", segments=["origin", "other"])
    holdout_io.assign_and_log(root, [source.id], occasion_id="pending", rate=.5,
                             salt="sentinel-test", context="origin")
    with pytest.raises(ValueError, match="every assigned"):
        io.assert_generalize(root, [source.id])
    holdout_io.record_outcome(root, "pending", True)
    io.assert_generalize(root, [source.id])
    from commontrace import hierarchical

    def change(facts):
        from dataclasses import replace
        facts[source.id] = replace(facts[source.id], statement="Changed current limit.")

    with hierarchical.mutate_facts(root) as facts:
        change(facts)
    with pytest.raises(PermissionError):
        io.assert_generalize(root, [source.id])


def proven_canary(root, clock):
    payload = {"id": "delta:"+"a"*64, "delta": "Verify the signature.", "artifact_hash": fp.digest("canary"),
               "scopes": []}
    unit = payload["id"]
    store.put(root, "optimizer-candidates", unit, payload, immutable=True)
    unit_trials.register(root, unit, payload["artifact_hash"], training_size=10, minimum_support=1)
    clock.tick()
    for index in range(120):
        arm = unit_trials.assign(root, unit, "canary-"+str(index))
        holdout_io.record_outcome(root, "canary-"+str(index), arm["injected"])
        clock.tick(1)
    certificate = unit_trials.certify(root, unit)
    assert promotion.verify(root, certificate)
    return store.reference("optimizer-candidates", unit), certificate


def test_actual_factorial_forgetting_blocks_promotion_and_preserves_provenance(tmp_path, clock):
    root = str(tmp_path)
    prepare(root, clock, enabled=False)
    canary, certificate = proven_canary(root, clock)
    io.configure(root)
    registered = io.ledger(root, "register", payload={"adapter": "regressing-v1"}, canary=canary,
                           certificate=certificate, signer="operator:test", tolerance=.1)
    update = registered["update"]
    clock.tick()
    initial = io.ledger(root, update=update)
    assert not initial["allow"] and not initial["supported"]
    assert initial["kl_to_base"]["status"] == "unchecked"
    for index in range(200):
        occasion = "update-"+str(index)
        delivered = io.ledger(root, "expose", update=update, occasion=occasion)
        u = delivered["update_payload"] is not None
        c = delivered["canary_payload"] is not None
        assert delivered == io.ledger(root, "expose", update=update, occasion=occasion)
        holdout_io.record_outcome(root, occasion, c != u)
        clock.tick(1)
    result = io.ledger(root, update=update)
    assert result["regression"] and not result["allow"]
    assert result["signer"] == "operator:test"
    assert result["score_bound"] == 4
    assert len(holdout_io.read_log(root)[0]) == 520
    with pytest.raises(PermissionError, match="non-regression"):
        unit_trials.certify(root, update)
    promotion.rollback(root, certificate, "canary withdrawn")
    with pytest.raises(PermissionError, match="certificate"):
        io.ledger(root, "expose", update=update, occasion="after-rollback")


def test_ledger_refuses_forged_or_unproven_canary_and_operator_only_configuration(tmp_path, clock):
    root = str(tmp_path)
    prepare(root, clock)
    source = fact(root, "Use the verified signature.")
    with pytest.raises((PermissionError, ValueError)):
        io.ledger(root, "register", payload={"adapter": "v1"}, canary=source.id,
                  certificate={}, signer="claimed")
    with memory_authority.writer("agent", "agent"):
        with pytest.raises(PermissionError, match="operator"):
            io.configure(root)


def test_cli_sentinel_configuration_is_real_and_default_remains_disabled(tmp_path, capsys):
    root = str(tmp_path)
    assert not io.enabled(root)
    io.assert_generalize(root, ["legacy-source"])
    assert cli.main(["sentinel", "configure", '{"alpha":0.1}', "--dest", root]) == 0
    assert json.loads(capsys.readouterr().out)["alpha"] == .1
    assert io.enabled(root)


def test_prefix_commitment_is_streamed_and_canonical_forgery_is_refused(tmp_path, clock):
    from commontrace import _jsonl, experiment

    # Each record is bounded independently; a prior cohort larger than 1 MiB
    # must not be squeezed through the single-record digest limit.
    large = [{"value": "a"*2000, "index": i} for i in range(600)]
    assert len(io._prefix_digest(large)) == 64
    assert io._prefix_digest(large) != io._prefix_digest(list(reversed(large)))
    root = str(tmp_path)
    prepare(root, clock)
    source = fact(root, "Use the governed firmware checksum.")
    io.register(root, source.id, origin="origin", segments=["origin", "other"])
    holdout_io.assign_and_log(root, [source.id], occasion_id="real", rate=.5,
                             salt="sentinel-test", context="origin")
    holdout_io.record_outcome(root, "forged", True)
    rows = fp.rows(holdout_io.holdout_log_path(root))
    rows[0]["occasion_id"] = "forged"
    rows[0]["injected"] = not experiment.is_held_out(source.id, "forged", .5, "sentinel-test")
    _jsonl.write_rows(holdout_io.holdout_log_path(root), rows)
    with pytest.raises(PermissionError, match="anchored"):
        io.status(root, source.id)


def test_leaf_namespace_does_not_authorize_sentinel_metadata(tmp_path, clock):
    from commontrace import capability_io
    from tests import test_capability_io

    root, token = test_capability_io.prepare(tmp_path)
    prepare(root, clock)
    source = fact(root, "Inspect the scoped deployment signature.", scopes=["project:a"])
    io.register(root, source.id, origin="origin", segments=["origin", "other"], context=["project:a"])
    authorization = test_capability_io.authenticate(root, token, test_capability_io.proof(root, token))
    with capability_io.activate(authorization):
        with pytest.raises(PermissionError, match="leaf capability"):
            io.status(root, source.id, context=["project:a"])


def test_hazard_is_never_a_randomized_or_quarantined_source(tmp_path, clock):
    from commontrace import safety

    root = str(tmp_path)
    prepare(root, clock)
    source = fact(root, "Stop motion while a person is in the work cell.")
    review = safety.review(root, source.id, reviewer="safety-operator", reason="human proximity")
    safety.mark(root, source.id, "human-proximity", review)
    with pytest.raises(PermissionError):
        io.register(root, source.id, origin="origin", segments=["origin", "other"])
    assert not io.blocked(root, source.id)
