"""Backups preserve historical beliefs and evidence, and failed restores leave no partial state."""
import copy
import json

import pytest

from commontrace.conversation import Store
from commontrace.conversation.extract import extract
from commontrace.conversation.store import ConversationError, write_txn


def seed(store):
    store.add("early", [{"speaker": "Ana", "text": "I live in London."},
                        {"speaker": "Ana", "text": "Project budget approved."}], session_at="2023-01-01")
    store.add("late", [{"speaker": "Ben", "text": "No personal changes."}], session_at="2024-01-01")
    ids = [t.id for t in store.session_turns("early")]
    store.add_memories("early", [{"text": "The budget for Zephyr is 120 units.", "owner": "Project Zephyr",
                                  "source_turn_ids": ids}], source="model", extracted_through=1)
    # All three assertions share a timestamp and anchor; their insertion order
    # still distinguishes a genuine reversion from a duplicate observation.
    store.add_memories("late", [{"text": "Ben prefers tea.", "slot": "drink"},
                                 {"text": "Ben prefers coffee.", "slot": "drink"},
                                 {"text": "Ben prefers tea.", "slot": "drink"}], source="manual")
    # Extraction from the earlier session happens after the later-session facts.
    store.add_memories("early", [{"text": "Ben prefers water.", "owner": "Ben", "slot": "drink",
                                  "at": "2024-01-01", "source_turn_ids": ids}], source="manual")
    store.set_summary("early", "Budget approved; Ana lives in London.", "extractive")


def beliefs(store):
    return [(f["owner"], f["kind"], f["statement"], f["at"], f["slot"], f["source"], f["valid_until"])
            for f in store.facts(history=True)]


def test_archive_preserves_every_belief_provenance_summary_checkpoint_and_reversion(tmp_path):
    root = str(tmp_path)
    with Store(root, "original") as original, Store(root, "copy") as restored:
        seed(original)
        # Destination ids differ from archive ids, including session-local indices.
        restored.add("unrelated", [{"text": "Existing unrelated memory."}])
        rows = json.loads(json.dumps(list(original.export())))
        result = restored.import_sessions(rows)
        assert result["added"] == 3 and result["memories"] == len(original.facts(history=True))
        assert beliefs(restored) == beliefs(original)
        assert restored.get_meta("extracted:early") == "1"
        assert restored.summaries()["early"] == original.summaries()["early"]
        fact = next(f for f in restored.facts() if "120" in f["statement"])
        assert [e["text"] for e in restored.fact_evidence(fact["id"])] == [
            "I live in London.", "Project budget approved."]
        assert {f["statement"] for f in restored.facts() if f["owner"] == "ben"} == {"Ben prefers water."}
        again = restored.import_sessions(rows)
        assert again["added"] == again["memories"] == 0
        assert beliefs(restored) == beliefs(original)
        # The restored checkpoint avoids another extraction of processed messages.
        assert extract(restored, sessions=["early"], complete=lambda _: pytest.fail("unexpected inference"))["calls"] == 0
        restored.purge(before="2023-01-01T00:01")
        assert all("120" not in f["statement"] for f in restored.facts(history=True))


@pytest.mark.parametrize("damage", ["missing_source", "bad_version", "bad_kind", "bad_checkpoint", "duplicate_fact"])
def test_corrupt_archive_rolls_back_all_sessions_and_derived_facts(tmp_path, damage):
    root = str(tmp_path)
    with Store(root, "original") as original, Store(root, "copy") as restored:
        seed(original)
        rows = copy.deepcopy(list(original.export()))
        if damage == "missing_source":
            rows[-1]["memories"][-1]["source_turn_ids"] = [999999]
        elif damage == "bad_version":
            rows[-1]["format_version"] = 999
        elif damage == "bad_kind":
            rows[-1]["memories"][-1]["kind"] = "unknown"
        elif damage == "bad_checkpoint":
            rows[-1]["extracted_source_id"] = 999999
        else:
            rows[-1]["memories"].append(rows[-1]["memories"][-1])
        with pytest.raises(ConversationError):
            restored.import_sessions(rows)
        assert restored.stats()["turns"] == restored.stats()["sessions"] == restored.stats()["facts"] == 0
        assert restored.lexical("London", 10) == []


def test_restore_never_attaches_archive_sources_to_other_local_sessions(tmp_path):
    root = str(tmp_path)
    with Store(root, "original") as original, Store(root, "copy") as restored:
        seed(original)
        rows = list(original.export())
        rows[-1]["memories"][0]["source_turn_ids"] = [rows[0]["messages"][0]["source_id"]]
        rows[-1]["memories"][0]["anchor_source_id"] = rows[0]["messages"][0]["source_id"]
        with pytest.raises(ConversationError, match="their session"):
            restored.import_sessions(rows)
        assert restored.stats()["turns"] == 0


def test_import_into_longer_session_does_not_certify_partial_summary_or_checkpoint(tmp_path):
    root = str(tmp_path)
    with Store(root, "original") as original, Store(root, "copy") as restored:
        seed(original)
        restored.add("early", [{"text": "Additional unprocessed evidence."}])
        restored.import_sessions(list(original.export()))
        assert "early" not in restored.summaries()
        assert restored.get_meta("extracted:early") is None


def test_restore_conflicting_message_ref_rolls_back_without_overwriting_evidence(tmp_path):
    root = str(tmp_path)
    with Store(root, "original") as original, Store(root, "copy") as restored:
        original.add("s", [{"id": "external-id", "text": "Home is Oslo."}])
        restored.add("s", [{"id": "external-id", "text": "Home is Paris."}])
        with pytest.raises(ConversationError, match="conflicts"):
            restored.import_sessions(list(original.export()))
        assert restored.session_turns("s")[0].text == "Home is Paris."


def test_nested_transaction_failure_rolls_back_only_its_savepoint_when_caught(tmp_path):
    with Store(str(tmp_path), "transactions") as store:
        with write_txn(store.db):
            store.add("keep", [{"text": "Committed outer evidence."}])
            with pytest.raises(ConversationError):
                store.add("invalid", [{"text": "Will roll back."}, {"at": "bad date", "text": "Invalid."}])
            store.add("also_keep", [{"text": "Another outer write."}])
        assert {s["id"] for s in store.sessions()} == {"keep", "also_keep"}


def test_streaming_import_rolls_back_on_invalid_later_json_line(tmp_path, monkeypatch, capsys):
    import io

    from commontrace.cli import main

    root = str(tmp_path)
    first = json.dumps({"session": "valid", "messages": [{"text": "Will roll back."}]})
    monkeypatch.setattr("sys.stdin", io.StringIO(first + "\n{invalid json}\n"))
    assert main(["conversation", "import", "copy", "-", "--dest", root]) == 2
    with Store(root, "copy") as store:
        assert store.stats()["turns"] == store.stats()["sessions"] == 0


def test_file_export_is_atomic_on_a_later_session_failure(tmp_path, monkeypatch, capsys):
    from commontrace.cli import main

    root = str(tmp_path)
    target = tmp_path / "archive.jsonl"
    target.write_text("previous complete backup\n", encoding="utf-8")
    with Store(root, "original") as store:
        seed(store)

    def fail(self):
        yield {"session": "first", "messages": []}
        raise ConversationError("export interrupted")

    monkeypatch.setattr(Store, "export", fail)
    assert main(["conversation", "export", "original", "--out", str(target), "--dest", root]) == 2
    assert target.read_text(encoding="utf-8") == "previous complete backup\n"
    assert not list(tmp_path.glob(".commontrace-export-*"))
