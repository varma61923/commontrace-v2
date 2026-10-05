"""Execution optimizations preserve the canonical objective and source proof."""
import pytest

from commontrace.conversation import Store, embed, vector_index


class Encoder:
    tag = "scoped-proof"

    def __init__(self, np):
        self.np, self.batches = np, []

    def vectors(self, items):
        self.batches.append(items)
        return self.np.array([[int(text.split()[-1]), 1] for _h, text in items], dtype=self.np.float32)


@pytest.mark.parametrize("damage", ["id", "turn", "hash"])
def test_scoped_cache_checks_each_selected_provenance_field_and_restarts_partial_rankings(tmp_path, monkeypatch, damage):
    np = pytest.importorskip("numpy")
    monkeypatch.setattr(embed, "MAX_INDEX_BYTES", 1)
    monkeypatch.setattr(embed, "SCAN_BATCH", 2)
    with Store(str(tmp_path), "selected") as store:
        store.add("eligible", [{"text": f"passage {i}"} for i in range(6)])
        store.add("excluded", [{"text": f"passage {i}"} for i in range(6, 16)])
        encoder = Encoder(np)
        q = np.array([1, 0], dtype=np.float32)
        embed.search(store, encoder, q, 10)
        allowed = {t.id for t in store.session_turns("eligible")}
        expected = embed.search(store, encoder, q, 6, allowed=allowed)
        embed.forget_store(store)
        records = np.load(vector_index.path_for(store, encoder.tag), mmap_mode="r+", allow_pickle=False)
        records[5][damage] = -99 if damage != "hash" else b"0" * 32
        records.flush()
        del records
        encoder.batches.clear()
        actual = embed.search(store, encoder, q, 6, allowed=allowed)
        assert actual == expected and len({uid for uid, _score in actual}) == len(actual)
        assert sum(len(batch) for batch in encoder.batches) == 6
        assert all(int(body.split()[-1]) < 6 for batch in encoder.batches for _hash, body in batch)


def test_scoped_cache_does_not_publish_partially_validated_rows_to_full_recall(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    monkeypatch.setattr(embed, "MAX_INDEX_BYTES", 1)
    with Store(str(tmp_path), "isolated") as store:
        store.add("eligible", [{"text": f"passage {i}"} for i in range(6)])
        store.add("excluded", [{"text": f"passage {i}"} for i in range(6, 100)])
        encoder = Encoder(np)
        q = np.array([1, 0], dtype=np.float32)
        full = embed.search(store, encoder, q, 10)
        allowed = {t.id for t in store.session_turns("eligible")}
        expected = embed.search(store, encoder, q, 6, allowed=allowed)
        embed.forget_store(store)
        scoped = embed.search(store, encoder, q, 6, allowed=allowed)
        assert scoped == expected
        key = (store.path, store._units_identity, encoder.tag)
        assert key not in embed._INDEX
        assert embed.search(store, encoder, q, 10) == full
        assert key in embed._INDEX
