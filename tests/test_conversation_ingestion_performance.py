"""Chronological insertion preserves independently reconstructed belief chains."""
import random

import pytest

from commontrace.conversation.store import Store, write_txn


@pytest.mark.parametrize("seed", [0, 17, 91])
def test_incremental_links_match_sorted_owner_slot_history(tmp_path, seed):
    rng = random.Random(seed)
    with Store(str(tmp_path), "history") as store:
        store.add("sources", [{"text": "Source evidence."}], extract_profile=False)
        turn = store.session_turns("sources")[0].id
        expected = {}
        with write_txn(store.db):
            for number in range(180):
                owner = rng.choice(["ana", "ben", "cam"])
                slot = rng.choice(["home", "work", None])
                at = rng.choice([None, "2023-01-01", "2024-01-01", "2025-01-01"])
                fid = store._insert_fact(turn, "fact", "source", f"Assertion {number}",
                                         at, slot, "test", owner)
                if slot:
                    expected.setdefault((owner, slot), []).append((at or "", fid))
                successors = {}
                for history in expected.values():
                    ordered = sorted(history)
                    successors.update({fid: ordered[index + 1][1] if index + 1 < len(ordered) else None
                                       for index, (_, fid) in enumerate(ordered)})
                rows = store.db.execute("SELECT id, slot, superseded_by FROM facts").fetchall()
                assert all(row["superseded_by"] == successors.get(row["id"]) for row in rows)
                assert store.db.execute("SELECT COUNT(*) FROM fact_sources").fetchone()[0] == number + 1


def test_append_reuses_successor_without_second_neighbor_lookup(tmp_path):
    with Store(str(tmp_path), "history") as store:
        store.add("sources", [{"text": "Source evidence."}], extract_profile=False)
        turn = store.session_turns("sources")[0].id
        with write_txn(store.db):
            store._insert_fact(turn, "fact", "source", "First", "2023-01-01", "home", "test", "ana")
            queries = []
            store.db.set_trace_callback(queries.append)
            try:
                store._insert_fact(turn, "fact", "source", "Second", "2024-01-01", "home", "test", "ana")
            finally:
                store.db.set_trace_callback(None)
            neighbors = [query for query in queries if query.startswith("SELECT") and "FROM facts" in query]
            assert len(neighbors) == 1


def test_failed_insert_leaves_existing_chain_unchanged(tmp_path):
    with Store(str(tmp_path), "history") as store:
        store.add("sources", [{"text": "Source evidence."}], extract_profile=False)
        turn = store.session_turns("sources")[0].id
        with write_txn(store.db):
            store._insert_fact(turn, "fact", "source", "First", "2023-01-01", "home", "test", "ana")
        before = [tuple(row) for row in store.db.execute("SELECT * FROM facts")]
        with pytest.raises(RuntimeError), write_txn(store.db):
            store._insert_fact(turn, "fact", "source", "Second", "2024-01-01", "home", "test", "ana")
            raise RuntimeError("transaction aborted")
        assert [tuple(row) for row in store.db.execute("SELECT * FROM facts")] == before
