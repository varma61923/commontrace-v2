"""Regressions at the governance persistence and statistical boundaries."""
from __future__ import annotations

import copy
import json
import math
import multiprocessing
import os
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from unimem import _jsonl, causal_policy, conformal_gate, holdout_io, promotion, safety
from unimem import frontier_protocol as fp


def test_rotation_rejects_excess_rows_without_replacing_the_original(tmp_path, monkeypatch):
    root = str(tmp_path)
    name = "rotation.jsonl"
    fp.append(root, name, {"kind": "original"})
    original = (tmp_path / "memory" / name).read_bytes()
    monkeypatch.setattr(fp, "MAX_ROWS", 3)
    with pytest.raises(ValueError, match="rotation carry rows"):
        fp.rotate(root, name, [{"kind": "carry"}] * 3)
    assert (tmp_path / "memory" / name).read_bytes() == original
    fp.rotate(root, name, [{"kind": "carry"}])
    assert [row["kind"] for row in fp.journal(root, name)] == ["rotation", "carry"]


def test_rotation_checks_actual_serialized_row_size(tmp_path, monkeypatch):
    root = str(tmp_path)
    name = "rotation.jsonl"
    fp.append(root, name, {"kind": "original"})
    original = open(fp.filename(root, name), "rb").read()
    payload = {str(i): 0 for i in range(200)}
    # Choose a limit between compact canonical bytes and the actual on-disk
    # encoding, including the signature and chain metadata.
    sample = fp.sign(root, {**payload, "sequence": 1, "previous": "0" * 64, "recorded_at": fp.now()})
    maximum = len(fp.wire(sample)) + 32
    assert len(json.dumps(sample, ensure_ascii=False).encode()) > maximum
    monkeypatch.setattr(fp, "MAX_BYTES", maximum)
    with pytest.raises(ValueError, match="serialized journal record"):
        fp.rotate(root, name, [payload])
    assert open(fp.filename(root, name), "rb").read() == original


@pytest.mark.parametrize("payload", [b'{"x":\xff}\n', b'{"x":NaN}\n', b'{"x":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}\n'])
def test_journal_parser_normalizes_malformed_input_to_value_error(tmp_path, payload):
    path = tmp_path / "rows.jsonl"
    path.write_bytes(payload)
    with pytest.raises(ValueError):
        fp.rows(str(path))


def test_harm_gate_cache_cannot_be_mutated_by_a_consumer(tmp_path):
    root = str(tmp_path)
    configured = conformal_gate.configure(root, alpha=.02)
    configured["strata"][""]["threshold"] = 0
    configured["scorers"].append("forged")
    fresh = conformal_gate.current(root)
    assert fresh["strata"][""]["threshold"] > 1
    assert fresh["scorers"] == []
    assert not conformal_gate.admit(fresh, .99)


def test_hazard_cache_cannot_be_mutated_by_a_consumer(tmp_path):
    root = str(tmp_path)
    review = fp.sign(root, {"kind": "hazard-review", "id": "hazard"})
    fp.append(root, safety.REGISTRY, {"kind": "mark", "id": "hazard", "site": "private",
                                    "robot": "robot-a", "review": review})
    original = copy.deepcopy(safety.protected(root))
    safety.protected(root)["hazard"]["site"] = ""
    assert safety.protected(root) == original


def test_snipw_stays_finite_for_subnormal_propensity_products():
    tiny = math.nextafter(0, 1)
    rows = [dict(delivered=True, outcome=1, selection_probability=tiny, treatment_probability=tiny),
            dict(delivered=True, outcome=0, selection_probability=tiny, treatment_probability=tiny),
            dict(delivered=False, outcome=0, selection_probability=tiny, treatment_probability=.5)]
    result = causal_policy.snipw(rows)
    assert result["effect"] == .5
    assert result["effective_sample_size"] == {"True": 2, "False": 1}
    assert math.isfinite(result["treated"])


def _fork_lock_probe(path, connection):
    connection.send("ready")
    with _jsonl.locked(path):
        connection.send("acquired")
    connection.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork contract")
def test_child_cannot_inherit_parent_jsonl_lock_ownership(tmp_path):
    path = str(tmp_path / "journal.jsonl")
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=_fork_lock_probe, args=(path, child))
    try:
        with _jsonl.locked(path):
            process.start()
            assert parent.poll(10) and parent.recv() == "ready"
            assert not parent.poll(.1), "child bypassed the parent's file lock"
        assert parent.poll(10) and parent.recv() == "acquired"
        process.join(10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)
        parent.close()
        child.close()


def test_change_plan_registration_is_atomic(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="audit")

    def register(_):
        try:
            promotion.register(root, "one-plan", candidate={"default": 1}, incumbent={"default": 0},
                               allowed_changes=["ranker"], monitoring="harm", rollback="restore")
            return True
        except ValueError as exc:
            assert "immutable" in str(exc)
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(register, range(8))) == 1
    assert len(fp.journal(root, "change-plans.jsonl")) == 1


def test_promotion_evidence_is_bound_to_the_registered_design(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="audit")
    design = promotion.register(root, "upgrade", candidate={"default": 1}, incumbent={"default": 0},
                                allowed_changes=["ranker"], monitoring="harm", rollback="restore")["record"]
    promotion.assign(root, "upgrade", "valid")
    holdout_io.record_outcome(root, "valid", True)
    holdout_io.assign_and_log(root, [design["trial_lesson"]], occasion_id="foreign", rate=.5, salt="audit",
                              revisions={design["trial_lesson"]: "foreign-design"})
    holdout_io.record_outcome(root, "foreign", True)
    assert [row["occasion_id"] for row in promotion.logged_rows(root, "upgrade")] == ["valid"]


@pytest.mark.parametrize("extra", [{"unexpected": True}, {"probe": {"queries": [], "forbidden": ["secret"]}},
                                   {"probe": {"queries": ["query"], "forbidden": ["secret"], "threshold": -1}}])
def test_invalid_erasure_cli_request_has_no_side_effects(tmp_path, monkeypatch, extra):
    from unimem import certified_erasure
    from unimem.commands import frontier_cmd

    def forbidden(*args, **kwargs):
        pytest.fail("invalid request reached the destructive boundary")

    monkeypatch.setattr(certified_erasure, "SubjectVault", forbidden)
    monkeypatch.setattr(certified_erasure, "erase", forbidden)
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"subject": "subject", "identities": ["fact"], "reason": "request",
                                   "vault_directory": str(tmp_path / "vault"), **extra}))
    with pytest.raises(ValueError):
        frontier_cmd.run(Namespace(input=str(request), operation="certified-erase", dest=str(tmp_path / "store")))


@pytest.mark.parametrize("reason,foreign_vault,scalar_ids", [("", False, False), ("reason", True, False),
                                                            ("reason", False, True)])
def test_erasure_preflight_preserves_content_and_subject_keys(tmp_path, reason, foreign_vault, scalar_ids):
    pytest.importorskip("cryptography")
    from unimem import certified_erasure, hierarchical

    root = str(tmp_path / "store")
    vault = certified_erasure.SubjectVault(str(tmp_path / "other") if foreign_vault else root,
                                           str(tmp_path / "vault"))
    fact, _ = hierarchical.add_fact(root, "Private subject preference")
    envelope = vault.seal("subject", fact.id, fact.statement)
    with pytest.raises(ValueError):
        certified_erasure.erase(root, "subject", fact.id if scalar_ids else [fact.id], vault, reason=reason)
    assert fact.id in hierarchical.load_facts(root)
    assert vault.open("subject", envelope) == fact.statement


def test_delegation_cannot_inherit_enclosing_operator_administration(tmp_path):
    from unimem import (
        assurance,
        capabilities,
        certified_erasure,
        compression,
        federation,
        incident,
        memory_authority,
        nextwave_store,
        policy,
        retention,
    )

    root = str(tmp_path / "store")
    assert memory_authority.is_operator()
    # activate() leaves the enclosing writer intact. Even an operator service
    # must not lend its administrative rights to an active delegated request.
    token = capabilities.CURRENT.set({"principal": "delegate", "authority": "agent"})
    operations = [
        lambda: memory_authority.configure_signing(root),
        lambda: nextwave_store.operator(),
        lambda: assurance._operator(),
        lambda: incident.freeze(root, "incident", fp.now()),
        lambda: safety.review(root, "hazard", reviewer="delegate", reason="unsafe"),
        lambda: safety.mark(root, "hazard", "zone", {}),
        lambda: safety.release(root, "hazard", {}),
        lambda: certified_erasure.erase(root, "subject", ["fact"], None, reason="request"),
        lambda: retention.review(root, "rewrite", reviewer="delegate"),
        lambda: retention.evict(root, None, [], token_budget=0),
        lambda: compression.register(root, "proposal"),
        lambda: compression.review(root, "proposal", "experiment", actor="delegate", expected_revision="revision"),
        lambda: policy.install(root, {}),
        lambda: policy.preregister(root, {}),
        lambda: federation.randomized_response(root, [], public_cohort_id="cohort",
                                               public_cohort_size=1, public_steps=[]),
    ]
    try:
        assert not memory_authority.is_operator()
        for operation in operations:
            with pytest.raises(PermissionError):
                operation()
        assert not os.path.exists(root)
    finally:
        capabilities.CURRENT.reset(token)


@pytest.mark.parametrize("invalid", ["", "x", "A" * 64, " " * 64, None, ["0" * 64]])
def test_merkle_degenerate_proofs_still_require_canonical_hashes(invalid):
    from unimem import transparency_log as log

    assert not log.verify_inclusion(invalid, 0, 1, [], invalid)
    assert not log.verify_consistency(1, 1, invalid, invalid, [])
    assert not log.verify_consistency(0, 1, log.EMPTY, invalid, [])


def test_empty_merkle_tree_and_proof_container_are_validated():
    from unimem import transparency_log as log

    valid = log.leaf_hash({})
    assert not log.verify_consistency(0, 0, log.EMPTY, valid, [])
    assert not log.verify_inclusion(valid, 0, 1, "", valid)
    assert not log.verify_consistency(1, 1, valid, valid, "")
    assert log.verify_consistency(0, 0, log.EMPTY, log.EMPTY, [])


def test_hub_merkle_proofs_work_beyond_local_journal_row_bound():
    from unimem import transparency_log as log

    hashes = [log.leaf_hash({})] * (fp.MAX_ROWS + 1)
    root = log.tree_hash(hashes)
    index = len(hashes) - 1
    proof = log.inclusion(hashes, index)
    assert log.verify_inclusion(hashes[index], index, len(hashes), proof, root)
    old_size = fp.MAX_ROWS
    assert log.verify_consistency(old_size, len(hashes), log.tree_hash(hashes[:old_size]), root,
                                  log.consistency(hashes, old_size))


def test_framework_turn_allows_bounded_cold_capture_without_editor_hook_deadline(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from unimem import framework_middleware, hook_kit
    from unimem.memory_journal import Journal
    from unimem.memory_record import MemoryRecord

    root = str(tmp_path)
    scope = {"agent": "crew", "user": "alice"}
    Journal(root).put(MemoryRecord("directive", "directive", 1, {"text": "Verify the blue release gate."},
                                    scope=scope))
    elapsed = [0.0]
    capture = Journal.capture

    def cold_capture(*args, **kwargs):
        result = capture(*args, **kwargs)
        elapsed[0] += .1
        return result

    monkeypatch.setattr(Journal, "capture", cold_capture)
    monkeypatch.setattr(hook_kit, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    result = framework_middleware.turn(root, "framework", "release", context=["agent:crew", "user:alice"],
                                        embedder="none")
    assert "blue release gate" in result["context"]
    # The native editor hook keeps its original latency deadline.
    assert hook_kit.HookKit(root, "crew", scope=scope, embedder="none").handle(
        "UserPromptSubmit", {"session_id": "native", "prompt": "release"})["timed_out"]


def test_spatial_beliefs_admit_one_snapshot_and_match_per_object_queries(tmp_path, monkeypatch):
    from unimem import spatial

    root = str(tmp_path)
    context = {"tenant": "tenant", "site": "site", "robot": "robot"}
    for i in range(3):
        spatial.update(root, {"object_id": f"box-{i}", "class": "crate", "pose": [i, 0, 0], "frame": "map",
                               "robot_id": "robot", "timestamp": "2026-01-01T00:00:00Z", "confidence": .9},
                       context=context)
    at = fp.now()
    expected = [spatial.where_is(root, f"box-{i}", context=context, when=at, known_at=at) for i in range(3)]
    calls = []
    history = spatial.history

    def counted(*args, **kwargs):
        calls.append(kwargs)
        return history(*args, **kwargs)

    monkeypatch.setattr(spatial, "history", counted)
    assert spatial.believed_at(root, "robot", at, context=context) == expected
    assert len(calls) == 1
    assert calls[0]["as_of"] == calls[0]["known_at"] == fp.moment(at)


@pytest.mark.parametrize("tiny", [1e-300, math.nextafter(0, 1)])
def test_working_memory_normalizes_tiny_vectors_without_underflow(tiny):
    from unimem.robotics.working_memory import WorkingMemory

    memory = WorkingMemory(capacity_tokens=10)
    memory.add("tiny", [tiny, tiny], tokens=1, episode_id="episode")
    result = memory.retrieve([tiny, tiny])
    assert result["result"]["id"] == "tiny"
    assert result["result"]["score"] == pytest.approx(1)


def test_robot_signature_ingestion_bounds_ignored_records(tmp_path):
    from unimem.robotics import signatures

    with pytest.raises(ValueError, match="sample count"):
        signatures.ingest(str(tmp_path), ({} for _ in range(100_001)), skill="skill", embodiment="robot", site="site")
    assert not os.path.exists(fp.filename(str(tmp_path), "robot-proprioceptive-samples.jsonl"))


def test_concurrent_room_configuration_cannot_relax_no_memory(tmp_path):
    from unimem import household

    root = str(tmp_path)

    def configure(index):
        try:
            household.configure_room(root, "room", no_memory=index % 3 == 0, actor="owner")
        except PermissionError:
            pass  # A concurrent no-memory decision has already won.

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(configure, range(12)))
    assert household.no_memory(root, "room")
