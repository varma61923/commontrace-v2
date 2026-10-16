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
