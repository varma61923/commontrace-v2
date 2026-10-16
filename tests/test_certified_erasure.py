from __future__ import annotations

import copy

import pytest

from commontrace import certified_erasure as erase
from commontrace import frontier_protocol as fp
from commontrace import hierarchical, incident

pytest.importorskip("cryptography")


def test_pre_erasure_encrypted_backups_become_unreadable(tmp_path):
    root = str(tmp_path / "store")
    vault = erase.SubjectVault(root, str(tmp_path / "vault"))
    original = vault.seal("subject-a", "record-a", "private subject content")
    other = vault.seal("subject-b", "record-b", "unaffected content")
    backup = copy.deepcopy(original)
    assert vault.open("subject-a", original) == "private subject content"
    vault.destroy("subject-a")
    with pytest.raises(PermissionError, match="destroyed"):
        vault.open("subject-a", backup)
    assert vault.open("subject-b", other) == "unaffected content"
    with pytest.raises(PermissionError):
        vault.seal("subject-a", "replay", "old identity")


def test_cross_store_envelope_forgery_and_hub_outer_wrapping(tmp_path):
    pytest.importorskip("hub.encryption")
    from hub.encryption import EnvelopeCipher, generate_key

    first = erase.SubjectVault(str(tmp_path / "a"), str(tmp_path / "vault"))
    second = erase.SubjectVault(str(tmp_path / "b"), str(tmp_path / "vault"))
    cipher = EnvelopeCipher.from_config(generate_key(), "")
    sealed = cipher.encrypt_subject(first, "subject", "record", "confidential")
    assert cipher.decrypt_subject(first, "subject", sealed) == "confidential"
    with pytest.raises(PermissionError):
        cipher.decrypt_subject(second, "subject", sealed)
    forged = copy.deepcopy(sealed)
    forged["record_id"] = "different"
    with pytest.raises(ValueError, match="authentication"):
        cipher.decrypt_subject(first, "subject", forged)


def test_live_cascade_probe_certificate_and_legal_hold(tmp_path):
    root = str(tmp_path / "store")
    vault = erase.SubjectVault(root, str(tmp_path / "vault"))
    fact, _ = hierarchical.add_fact(root, "Subject A private preference")
    vault.seal("subject", fact.id, fact.statement)
    incident.freeze(root, "hold", fp.now())
    with pytest.raises(PermissionError, match="legal hold"):
        erase.erase(root, "subject", [fact.id], vault, reason="request")
    incident.release(root, "hold", "resolved")
    certificate = erase.erase(root, "subject", [fact.id], vault, reason="request",
                              retrieval_probe=lambda: erase.probe(lambda q: [], ["Subject A"], forbidden=["private"]))
    assert erase.verify(root, certificate)
    assert certificate["certificate"]["record"]["probe"]["passed"]
    assert fact.id not in hierarchical.load_facts(root)
    assert "plaintext backups excluded" in certificate["certificate"]["record"]["backup_scope"]


def test_vault_location_and_nonfinite_probe_refused(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        erase.SubjectVault(str(tmp_path), str(tmp_path / "keys"))
    with pytest.raises(ValueError):
        erase.probe(lambda q: [], ["query"], forbidden=["token"], threshold=float("nan"))


def test_cli_erasure_reaches_derived_lessons_rewrites_and_live_recall(tmp_path):
    pytest.importorskip("mcp")
    import json
    import os

    from commontrace import holdout_io, lesson_io, recall, retention
    from tests.test_mcp_server import _write_lesson, cli

    root = str(tmp_path / "store")
    vault_dir = str(tmp_path / "vault")
    vault = erase.SubjectVault(root, vault_dir)
    fact, _ = hierarchical.add_fact(root, "Zelda prefers the quiet loading dock entrance")
    vault.seal("zelda", fact.id, fact.statement)
    _write_lesson(root, "zelda-dock", body="Route Zelda deliveries through the quiet loading dock entrance.",
                  source_traces=[fact.id])
    holdout_io.configure(root, rate=.5, salt="erase")
    proposal = retention.propose(root, "zelda-dock", "Always use the quiet dock entrance for Zelda.", author="dreamer")
    blob = retention._blob_path(root, proposal["record"]["body_digest"])
    assert os.path.exists(blob)
    assert any("Zelda" in item.text for item in recall.recall(root, "Zelda loading dock").items)
    request = tmp_path / "erase.json"
    request.write_text(json.dumps({"subject": "zelda", "identities": [fact.id],  # the lesson is found by lineage
                                   "vault_directory": vault_dir, "reason": "data subject request",
                                   "probe": {"queries": ["Zelda loading dock", "quiet dock entrance"],
                                             "forbidden": ["Zelda"]}}))
    out = cli("frontier", "certified-erase", str(request), "--dest", root)
    assert out.returncode == 0, out.stderr
    certificate = json.loads(out.stdout)
    assert certificate["certificate"]["record"]["probe"]["passed"]
    assert not any("Zelda" in item.text for item in recall.recall(root, "Zelda loading dock").items)
    assert lesson_io.lesson_path(root, "zelda-dock") is None and not os.path.exists(blob)
    verify = tmp_path / "verify.json"
    verify.write_text(json.dumps({"certificate": certificate}))
    out = cli("frontier", "certified-verify", str(verify), "--dest", root)
    assert out.returncode == 0 and json.loads(out.stdout)["valid"]
