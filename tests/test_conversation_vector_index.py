"""Disk-mapped exact recall remains source-generation-safe and expendable."""
import os
import stat

import pytest

from commontrace.conversation import Store, embed, vector_index


class Encoder:
    tag = "mapped-tests"

    def __init__(self, np):
        self.np, self.batches = np, []

    def vectors(self, items):
        self.batches.append([body for _hash, body in items])
        return self.np.array([[int(body.split()[-1]), 1] for _hash, body in items], dtype=self.np.float32)


@pytest.fixture
def setup_index(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    monkeypatch.setattr(embed, "MAX_INDEX_BYTES", 1)
    monkeypatch.setattr(embed, "SCAN_BATCH", 3)
    root = str(tmp_path)
    return root, np, Encoder(np), np.array([1, 0], dtype=np.float32)


def test_mapped_index_survives_connections_without_reloading_corpus_and_preserves_filters(setup_index, monkeypatch):
    root, np, encoder, q = setup_index
    with Store(root, "mapped") as store:
        store.add("a", [{"text": f"passage {i}"} for i in range(5)])
        store.add("b", [{"text": f"passage {i}"} for i in range(5, 10)])
        original = embed.search(store, encoder, q, 4)
        key = (store.path, store._units_identity, encoder.tag)
        assert embed._INDEX[key].mapped and isinstance(embed._INDEX[key].matrix, np.memmap)
        path = vector_index.path_for(store, encoder.tag)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        embed.forget_store(store)
    encoder.batches.clear()
    with Store(root, "mapped") as store:
        monkeypatch.setattr(store, "unit_batches", lambda *_a, **_kw: pytest.fail("must not reread source bodies"))
        assert embed.search(store, encoder, q, 4) == original
        allowed = {t.id for t in store.session_turns("a")}
        page = embed.search(store, encoder, q, 4, allowed=allowed)
        assert set(store.unit_turns(uid for uid, _s in page).values()) <= allowed
        turns = store.unit_turns(uid for uid, _s in page)
        assert [int(store.turns([turns[uid]])[turns[uid]].text.split()[-1]) for uid, _s in page] == [4, 3, 2, 1]
        assert not encoder.batches


def test_frozen_stores_read_existing_snapshot_but_never_publish_one(setup_index):
    root, np, encoder, q = setup_index
    with Store(root, "frozen") as store:
        store.add("s", [{"text": "passage 2"}])
        path = vector_index.path_for(store, encoder.tag)
    with Store(root, "frozen", read_only=True) as store:
        expected = embed.search(store, encoder, q, 1)
        assert not os.path.exists(path)
        embed.forget_store(store)
    with Store(root, "frozen") as store:
        assert embed.search(store, encoder, q, 1) == expected
        embed.forget_store(store)
    before = os.stat(path)
    encoder.batches.clear()
    with Store(root, "frozen", read_only=True) as store:
        assert embed.search(store, encoder, q, 1) == expected
        assert not encoder.batches
    assert os.stat(path).st_mtime_ns == before.st_mtime_ns


def test_deleted_or_replaced_sources_cannot_reuse_same_generation(setup_index):
    root, np, encoder, q = setup_index
    with Store(root, "updated") as reader, Store(root, "updated") as writer:
        writer.add("obsolete", [{"text": "passage 9"}])
        old = embed.search(reader, encoder, q, 1)
        old_revision = reader.unit_stamp()
        writer.delete_session("obsolete")
        writer.add("replacement", [{"text": "passage 1"}])
        encoder.batches.clear()
        new = embed.search(reader, encoder, q, 1)
        assert reader.unit_stamp() != old_revision
        assert new[0][1] == 1 and old[0][1] == 9
        assert len(encoder.batches) == 1 and encoder.batches[0][0].endswith("passage 1")
        records = vector_index.load(reader, encoder.tag, np, reader.unit_stamp()[0], 2)
        assert len(records) == 2 and records[1]["vector"][0] == 1


def test_old_read_snapshot_cannot_regress_published_disk_generation(setup_index):
    root, np, encoder, q = setup_index
    with Store(root, "snapshots") as reader, Store(root, "snapshots") as writer:
        writer.add("old", [{"text": "passage 1"}])
        with reader.read_snapshot():
            revision = reader.unit_stamp()
            writer.add("new", [{"text": "passage 9"}])
            latest = embed.search(writer, encoder, q, 2)
            old = embed.search(reader, encoder, q, 2)
            assert len(old) == 1 and len(latest) == 2
            assert vector_index.load(writer, encoder.tag, np, writer.unit_stamp()[0], 2) is not None
            assert vector_index.load(reader, encoder.tag, np, revision[0], 2) is None
        assert embed.search(reader, encoder, q, 2) == latest


@pytest.mark.parametrize("damage", ["truncated", "dimensions", "ordering", "generation", "incomplete"])
def test_invalid_disk_snapshots_are_rebuilt_from_evidence(setup_index, damage):
    root, np, encoder, q = setup_index
    with Store(root, "damage") as store:
        store.add("s", [{"text": f"passage {i}"} for i in range(6)])
        original = embed.search(store, encoder, q, 3)
        path = vector_index.path_for(store, encoder.tag)
        embed.forget_store(store)
        if damage == "truncated":
            with open(path, "wb") as file:
                file.write(b"incomplete")
        elif damage == "dimensions":
            with open(path, "wb") as file:
                np.save(file, np.zeros(7, dtype=vector_index.dtype(np, 3)), allow_pickle=False)
        elif damage == "incomplete":
            records = np.load(path, allow_pickle=False)[:-1]
            with open(path, "wb") as file:
                np.save(file, records, allow_pickle=False)
        else:
            records = np.load(path, mmap_mode="r+", allow_pickle=False)
            records[0 if damage == "generation" else 3]["id"] = -1
            records.flush()
            del records
        encoder.batches.clear()
        assert embed.search(store, encoder, q, 3) == original
        assert sum(map(len, encoder.batches)) == 6
        assert vector_index.load(store, encoder.tag, np, store.unit_stamp()[0], 2) is not None


def test_failed_inference_cleans_temporary_snapshot_and_retry_is_complete(setup_index, monkeypatch):
    root, np, encoder, q = setup_index
    original_encode = encoder.vectors

    def fail_second_batch(items):
        if encoder.batches:
            raise RuntimeError("interrupted local inference")
        return original_encode(items)

    monkeypatch.setattr(encoder, "vectors", fail_second_batch)
    with Store(root, "interrupted") as store:
        store.add("s", [{"text": f"passage {i}"} for i in range(7)])
        with pytest.raises(RuntimeError, match="interrupted local inference"):
            embed.search(store, encoder, q, 1)
        assert not os.path.exists(vector_index.path_for(store, encoder.tag))
        assert not any(entry.startswith(".dense-") for entry in os.listdir(os.path.dirname(store.path)))
        monkeypatch.setattr(encoder, "vectors", original_encode)
        assert embed.search(store, encoder, q, 1)[0][1] == 6


def test_disk_capacity_and_allocation_failures_leave_bounded_streaming_available(setup_index, monkeypatch):
    root, np, encoder, q = setup_index
    with Store(root, "fallback") as store:
        store.add("s", [{"text": f"passage {i}"} for i in range(7)])
        with monkeypatch.context() as m:
            m.setattr(vector_index, "MAX_BYTES", 1)
            assert embed.search(store, encoder, q, 1)[0][1] == 6
            assert not os.path.exists(vector_index.path_for(store, encoder.tag))
        if hasattr(os, "posix_fallocate"):
            monkeypatch.setattr(os, "posix_fallocate", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
            assert embed.search(store, encoder, q, 1)[0][1] == 6
            assert not os.path.exists(vector_index.path_for(store, encoder.tag))
            assert not any(entry.startswith(".dense-") for entry in os.listdir(os.path.dirname(store.path)))
        assert max(map(len, encoder.batches)) <= embed.SCAN_BATCH


def test_disk_cache_prunes_old_files_without_invalidating_active_readers(setup_index, monkeypatch):
    root, np, encoder, q = setup_index
    monkeypatch.setattr(vector_index, "MAX_FILES", 2)
    with Store(root, "first") as first:
        first.add("s", [{"text": "passage 1"}])
        expected = embed.search(first, encoder, q, 1)
        for name in ("second", "third"):
            with Store(root, name) as store:
                store.add("s", [{"text": "passage 2"}])
                embed.search(store, encoder, q, 1)
        directory = os.path.dirname(first.path)
        assert len([entry for entry in os.listdir(directory) if entry.startswith("dense-") and entry.endswith(".npy")]) == 2
        assert embed.search(first, encoder, q, 1) == expected


def test_publication_removes_dead_process_temporaries_but_keeps_active_builders(setup_index):
    root, np, encoder, q = setup_index
    with Store(root, "cleanup") as store:
        store.add("s", [{"text": "passage 1"}])
        directory = os.path.dirname(store.path)
        orphan = os.path.join(directory, ".dense-99999999-orphan.npy")
        active = os.path.join(directory, f".dense-{os.getpid()}-active.npy")
        for path in (orphan, active):
            with open(path, "wb") as file:
                file.write(b"temporary")
        embed.search(store, encoder, q, 1)
        assert not os.path.exists(orphan) and os.path.exists(active)


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="requires POSIX symlink-safe opens")
def test_symlinked_publication_lock_is_a_cache_miss_and_leaves_sources_available(setup_index):
    root, np, encoder, q = setup_index
    with Store(root, "symlink") as store:
        store.add("s", [{"text": "passage 1"}])
        directory = os.path.dirname(store.path)
        target = os.path.join(directory, "unchanged")
        with open(target, "wb") as file:
            file.write(b"unchanged")
        os.symlink(target, os.path.join(directory, ".dense.lock"))
        assert embed.search(store, encoder, q, 1)[0][1] == 1
        assert not os.path.exists(vector_index.path_for(store, encoder.tag))
        with open(target, "rb") as file:
            assert file.read() == b"unchanged"
