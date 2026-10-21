import json

import pytest

from unimem.memory_journal import Journal
from unimem.memory_record import MemoryRecord


@pytest.mark.parametrize("scope", [{}, {"agent": "new-owner"}])
def test_expiration_cannot_erase_a_reused_identity_at_the_same_version(tmp_path, monkeypatch, scope):
    journal = Journal(str(tmp_path))
    journal.put(MemoryRecord("due", "observation", 1, {
        "text": "Expired content.", "forget_after": "2000-01-01T00:00:00Z", "forget_reason": "expired",
    }))
    original = journal.forget

    def reuse_before_expiration(identity, **kwargs):
        original(identity, reason="Concurrent owner erasure.")
        journal.put(MemoryRecord(identity, "observation", 1, {"text": "Keep new content."}, scope=scope))
        return original(identity, **kwargs)

    monkeypatch.setattr(journal, "forget", reuse_before_expiration)
    assert journal.reap_due() == []
    assert journal.current("due", scope=scope).payload["text"] == "Keep new content."


def test_expiration_binds_the_stored_legacy_envelope_without_adding_defaults(tmp_path):
    journal = Journal(str(tmp_path))
    record = journal.put(MemoryRecord("due", "observation", 1, {
        "text": "Expired legacy content.", "forget_after": "2000-01-01T00:00:00Z", "forget_reason": "expired",
    }))
    body = record.to_dict()
    body.pop("created_at")  # Older envelopes can omit optional fields.
    with journal.connect(write=True) as db:
        db.execute("UPDATE records SET body=? WHERE id=?", (json.dumps(body), record.id))
    assert journal.reap_due()[0]["deleted"] == [record.id]
    assert journal.latest(record.id) is None
