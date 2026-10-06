"""Incremental memory maintenance preserves history and only reads new evidence."""
import json

import pytest

from commontrace.conversation.extract import BATCH, extract
from commontrace.conversation.store import Store, write_txn


def test_deletion_repairs_only_affected_owner_slots(tmp_path):
    with Store(str(tmp_path), "memory") as store:
        for session, date, home in [("old", "2023-01-01", "Oslo"),
                                    ("new", "2025-01-01", "Paris"),
                                    ("middle", "2024-01-01", "Rome")]:
            store.add(session, [{"speaker": "Ana", "text": f"I live in {home}."}], session_at=date)
        store.add("unrelated", [{"speaker": "Ben", "text": "I live in London."}], session_at="2023-01-01")
        unrelated = next(f for f in store.facts() if f["owner"] == "ben")
        store.db.execute("CREATE TEMP TRIGGER preserve_unrelated BEFORE UPDATE ON facts "
                         "WHEN old.owner='ben' BEGIN SELECT RAISE(ABORT, 'unrelated belief written'); END")
        assert store.delete_session("middle") == 1
        old = next(f for f in store.facts(history=True) if f["owner"] == "ana" and "Oslo" in f["statement"])
        assert old["valid_until"] == "2025-01-01T00:00"
        assert store.facts(candidates=[unrelated["id"]]) == [unrelated]
        assert store.facts(as_of="2024-06-01", candidates=[old["id"]])[0]["statement"] == "I live in Oslo."
        assert store.delete_session("new") == 1
        assert store.facts(candidates=[old["id"]])[0]["valid_until"] is None
        assert store.delete_session("does-not-exist") == 0


def test_purge_non_anchor_premise_repairs_chain_and_preserves_other_sources(tmp_path):
    with Store(str(tmp_path), "memory") as store:
        store.add("previous", [{"speaker": "Ana", "text": "I live in Oslo."}], session_at="2022-01-01")
        store.add("derived", [{"text": "Move confirmed", "at": "2023-01-01", "expires": "2024-01-01"},
                              {"text": "Residence is Paris", "at": "2023-02-01"}], extract_profile=False)
        ids = [t.id for t in store.session_turns("derived")]
        store.add_memories("derived", [{"text": "Ana lives in Paris.", "kind": "identity", "owner": "ana",
                                         "slot": "home", "source_turn_ids": ids},
                                        {"text": "The house is blue.", "source_turn_ids": [ids[1]]}], source="model")
        latest = next(f for f in store.facts() if f["slot"] == "home")
        assert latest["turn"] == ids[1]
        assert store.purge(expired_at="2024-06-01") == 1
        assert {f["statement"] for f in store.facts()} == {"I live in Oslo.", "The house is blue."}
        assert store.facts(allowed={ids[1]})[0]["statement"] == "The house is blue."
        assert store.get_meta("extracted:derived") is None


@pytest.mark.parametrize("operation", ["session", "purge"])
def test_maintenance_rolls_back_with_outer_transaction(tmp_path, operation):
    with Store(str(tmp_path), "memory") as store:
        store.add("old", [{"text": "I live in Oslo."}], session_at="2023-01-01")
        store.add("new", [{"text": "I live in Paris.", "expires": "2025-01-01"}], session_at="2024-01-01")
        store.set_meta("extracted:new", "0")
        before = store.facts(history=True)
        with pytest.raises(RuntimeError):
            with write_txn(store.db):
                store.delete_session("new") if operation == "session" else store.purge(expired_at="2026-01-01")
                assert store.facts()[0]["statement"] == "I live in Oslo."
                raise RuntimeError("abort outer transaction")
        assert store.facts(history=True) == before
        assert store.get_meta("extracted:new") == "0"


@pytest.mark.parametrize("operation", ["session", "purge"])
def test_reused_session_has_no_stale_extraction_checkpoint(tmp_path, operation):
    with Store(str(tmp_path), "memory") as store:
        store.add("s", [{"text": "Initial message", "expires": "2025-01-01"}])
        store.set_meta("extracted:s", "9")
        store.delete_session("s") if operation == "session" else store.purge(expired_at="2026-01-01")
        store.add("s", [{"text": "New message"}])
        prompts = []
        result = extract(store, sessions=["s"], complete=lambda p: (prompts.append(p) or '{"memories": []}', {}))
        assert result["calls"] == 1 and "New message" in prompts[0]
        assert store.get_meta("extracted:s") == "0"


def test_extraction_reads_bounded_new_batches_and_defers_concurrent_append(tmp_path, monkeypatch):
    with Store(str(tmp_path), "memory") as store:
        store.add("s", ({"text": f"Old message {i}"} for i in range(100)), extract_profile=False)
        store.set_meta("extracted:s", "99")
        store.add("s", ({"text": f"New message {i}"} for i in range(BATCH + 3)), extract_profile=False)
        original, batches, prompts = store.session_turns, [], []

        def read(session, **kw):
            assert kw["limit"] == BATCH and kw["after_idx"] >= 99
            turns = original(session, **kw)
            batches.append([t.idx for t in turns])
            return turns

        def complete(prompt):
            prompts.append(prompt)
            if len(prompts) == 1:
                with Store(str(tmp_path), "memory") as other:
                    other.add("s", [{"text": "Concurrent append"}], extract_profile=False)
            return json.dumps({"memories": []}), {}

        monkeypatch.setattr(store, "session_turns", read)
        result = extract(store, sessions=["s"], complete=complete)
        assert result["calls"] == 2
        assert [len(b) for b in batches] == [BATCH, 3]
        assert all("Old message" not in p and "Concurrent append" not in p for p in prompts)
        assert store.get_meta("extracted:s") == str(100 + BATCH + 2)
        batches.clear()
        assert extract(store, sessions=["s"], complete=complete)["calls"] == 1
        assert len(batches[0]) == 1 and "Concurrent append" in prompts[-1]
        monkeypatch.setattr(store, "session_turns", lambda *a, **kw: pytest.fail("completed history read"))
        assert extract(store, sessions=["s"], complete=complete)["calls"] == 0


def test_purged_session_tail_rewinds_checkpoint_before_index_reuse(tmp_path):
    with Store(str(tmp_path), "memory") as store:
        store.add("s", [{"text": "Keep observation"}, {"text": "Purge tail", "expires": "2025-01-01"}])
        store.set_meta("extracted:s", "1")
        assert store.purge(expired_at="2026-01-01") == 1
        assert store.get_meta("extracted:s") == "0"
        store.add("s", [{"text": "Replacement tail"}])
        prompts = []
        assert extract(store, sessions=["s"], complete=lambda p: (prompts.append(p) or '{"memories": []}', {}))["calls"] == 1
        assert "Replacement tail" in prompts[0] and "Keep observation" not in prompts[0]


@pytest.mark.parametrize("operation", ["session", "purge"])
@pytest.mark.parametrize("emit_memory", [False, True])
def test_extraction_rejects_recycled_source_id_even_with_same_external_ref(tmp_path, operation, emit_memory):
    from commontrace.conversation.store import ConversationError

    with Store(str(tmp_path), "memory") as store:
        store.add("s", [{"id": "source-reference", "text": "Ana lives in London.", "expires": "2025-01-01"}],
                  session_at="2023-01-01", extract_profile=False)
        old = store.session_turns("s")[0]

        def complete(prompt):
            assert "Ana lives in London." in prompt
            with Store(str(tmp_path), "memory") as writer:
                writer.delete_session("s") if operation == "session" else writer.purge(expired_at="2026-01-01")
                writer.add("s", [{"id": "source-reference", "text": "Ana lives in Paris."}],
                           session_at="2023-01-01", extract_profile=False)
                replacement = writer.session_turns("s")[0]
                assert replacement.id == old.id and replacement.ref == old.ref
            memories = [{"text": "Ana lives in London.", "owner": "ana", "slot": "home",
                         "source_turn_ids": [old.id]}] if emit_memory else []
            return json.dumps({"memories": memories}), {}

        with pytest.raises(ConversationError, match="source evidence changed during extraction; retry"):
            extract(store, sessions=["s"], complete=complete)
        assert store.stats()["facts"] == 0 and store.get_meta("extracted:s") is None
        assert store.session_turns("s")[0].text == "Ana lives in Paris."
        assert extract(store, sessions=["s"], complete=lambda _: ('{"memories": []}', {}))["calls"] == 1
