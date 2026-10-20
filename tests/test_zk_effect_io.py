"""Protocol order, source admission and actual canonical outcomes bind proofs."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from commontrace import hierarchical, holdout_io, memory_authority, privacy_erasure, zk_effect
from commontrace import zk_effect_io as io


def setup(root):
    fact = hierarchical.append_facts(root, [{"statement": "A reviewed optional procedure.", "scopes": ["project:a"]}])[0][0]
    entries = [{"episode": "episode-"+str(index), "propensity": 500_000} for index in range(40)]
    enrolled = io.register(root, "study", entries, [fact.id], context=["project:a"])
    return fact, entries, enrolled


def resolve(root, entries):
    for entry in entries:
        decision = io.assign(root, "study", entry["episode"], context=["project:a"])
        io.observe(root, "study", entry["episode"], decision["assigned"], context=["project:a"])


def test_cohort_precedes_verifier_nonce_and_every_canonical_outcome(tmp_path):
    root = str(tmp_path)
    _, entries, _ = setup(root)
    with pytest.raises(PermissionError, match="challenge"):
        io.assign(root, "study", entries[0]["episode"], context=["project:a"])
    question = io.challenge(root, "study", "ab"*32, context=["project:a"])
    resolve(root, entries)
    result = io.prove(root, "study", context=["project:a"])
    witness = io.export_witness(root, "study", context=["project:a"])
    assert io.verify(root, result["proof"], question["expected_hash"], witness=witness)["verified"]
    assert result["proof"]["statement"]["count"] == 40
    assert "zero-knowledge" not in json.dumps(result).lower()
    assert io.prove(root, "study", context=["project:a"])["proof"] == result["proof"]
    with pytest.raises(PermissionError, match="nonce"):
        io.challenge(root, "study", "cd"*32, context=["project:a"])


def test_nonce_cannot_be_enrolled_after_a_cohort_outcome_and_cohort_cannot_drop_a_row(tmp_path):
    root = str(tmp_path)
    _, entries, _ = setup(root)
    holdout_io.record_outcome(root, entries[0]["episode"], True)
    with pytest.raises(PermissionError, match="before"):
        io.challenge(root, "study", "ab"*32, context=["project:a"])
    fact = hierarchical.load_facts(root).popitem()[1]
    with pytest.raises(PermissionError, match="before"):
        io.register(root, "another", entries, [fact.id], context=["project:a"])


def test_incomplete_or_out_of_cohort_data_never_produces_a_statement(tmp_path):
    root = str(tmp_path)
    _, entries, _ = setup(root)
    io.challenge(root, "study", "ab"*32, context=["project:a"])
    with pytest.raises(PermissionError, match="cohort"):
        io.observe(root, "study", "extra-episode", True, context=["project:a"])
    resolve(root, entries[:-1])
    with pytest.raises(PermissionError, match="every"):
        io.prove(root, "study", context=["project:a"])
    with memory_authority.writer("agent", "agent"):
        with pytest.raises(PermissionError):
            io.export_witness(root, "study", context=["project:a"])


def test_erasure_purges_managed_cohorts_keys_and_proofs_and_clears_cached_ids(tmp_path):
    root = str(tmp_path)
    fact, entries, _ = setup(root)
    io.challenge(root, "study", "ab"*32, context=["project:a"])
    resolve(root, entries)
    io.prove(root, "study", context=["project:a"])
    assert io._CACHE.stats()["entries"]
    privacy_erasure.purge_fact(root, fact.id, reason="Owner erases the proof source.")
    assert io._CACHE.stats()["entries"] == 0
    with pytest.raises(PermissionError):
        io.export_witness(root, "study", context=["project:a"])
    assert not list((Path(root)/"memory"/"unimem"/"effect-keys").glob("*.json"))


def test_source_revision_and_scope_changes_never_bypass_current_admission(tmp_path):
    root = str(tmp_path)
    fact, entries, _ = setup(root)
    io.challenge(root, "study", "ab"*32, context=["project:a"])
    with pytest.raises(PermissionError):
        io.assign(root, "study", entries[0]["episode"], context=["project:b"])
    hierarchical.update_fact(root, fact.id, statement="A changed optional procedure.")
    with pytest.raises(PermissionError, match="revision"):
        io.assign(root, "study", entries[0]["episode"], context=["project:a"])


def test_sdk_backend_cannot_fall_back_to_a_transparent_or_development_receipt(tmp_path):
    root = str(tmp_path)
    _, entries, _ = setup(root)
    io.challenge(root, "study", "ab"*32, context=["project:a"])
    resolve(root, entries)
    with pytest.raises(zk_effect.BackendUnavailable, match="RISC Zero"):
        io.prove(root, "study", backend="risc0", context=["project:a"])
    with pytest.raises(zk_effect.BackendUnavailable, match="chunked"):
        io.prove(root, "study", backend="chunked", context=["project:a"])


def test_external_pin_and_tampered_public_statement_are_checked(tmp_path):
    root = str(tmp_path)
    _, entries, _ = setup(root)
    question = io.challenge(root, "study", "ab"*32, context=["project:a"])
    resolve(root, entries)
    proof = io.prove(root, "study", context=["project:a"])["proof"]
    witness = io.export_witness(root, "study", context=["project:a"])
    altered = deepcopy(proof)
    altered["statement"]["lower_ppm"] += 1
    assert not io.verify(root, altered, question["expected_hash"], witness=witness)["verified"]
    assert not io.verify(root, proof, "f"*64, witness=witness)["verified"]


def test_partial_registration_and_missing_commitment_receipt_recover_without_rearming(tmp_path, monkeypatch):
    from commontrace import nextwave_store

    root = str(tmp_path)
    fact = hierarchical.append_facts(root, [{"statement": "A reviewed procedure."}])[0][0]
    entries = [{"episode": "case-"+str(index), "propensity": 500_000} for index in range(8)]
    original = nextwave_store.event
    failed = False
    def interrupted(root, kind, identity, value):
        nonlocal failed
        if kind == "effect-state" and not failed:
            failed = True
            raise RuntimeError("simulated durable-state write interruption")
        return original(root, kind, identity, value)
    monkeypatch.setattr(nextwave_store, "event", interrupted)
    with pytest.raises(RuntimeError, match="interruption"):
        io.register(root, "interrupted", entries, [fact.id])
    with pytest.raises(PermissionError, match="commitment"):
        io.challenge(root, "interrupted", "ab"*32)
    recovered = io.register(root, "interrupted", entries, [fact.id])
    assert recovered["plan"]["count"] == 8
    question = io.challenge(root, "interrupted", "ab"*32)
    assert question["anchor"]["leaf"] > recovered["anchor"]["leaf"]
    assert io.register(root, "interrupted", entries, [fact.id])["plan"] == recovered["plan"]


def test_cli_uses_real_managed_cohort_and_explicit_witness_disclosure(tmp_path, capsys):
    from commontrace.cli import main

    root = str(tmp_path)
    _, entries, _ = setup(root)
    question = io.challenge(root, "study", "ab"*32, context=["project:a"])
    resolve(root, entries)
    request = {"study": "study", "context": ["project:a"]}
    assert main(["prove", "effect", json.dumps(request), "--dest", root]) == 0
    proof = json.loads(capsys.readouterr().out)["proof"]
    filename = tmp_path/"private-witness.json"
    filename.write_text(json.dumps(io.export_witness(root, "study", context=["project:a"])))
    request = {"proof": proof, "expected_challenge": question["expected_hash"]}
    assert main(["prove", "verify", json.dumps(request), "--witness-file", str(filename), "--dest", root]) == 0
    assert json.loads(capsys.readouterr().out)["verified"]
    request["expected_challenge"] = "ff"*32
    assert main(["prove", "verify", json.dumps(request), "--witness-file", str(filename), "--dest", root]) == 1
    assert not json.loads(capsys.readouterr().out)["verified"]
