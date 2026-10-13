"""Production read reuse preserves source boundaries and bounded capacity."""
import os
import sqlite3
import threading

import pytest

from commontrace.conversation import Options, Store, recall, unicode_index
from commontrace.conversation.pool import PoolBusy, ReadPool


def seed(root, space="user", text="Orion launch is ready."):
    with Store(str(root), space) as store:
        store.add("s", [{"role": "user", "text": text}])


def test_reused_connections_are_read_only_and_keep_warm_cache(tmp_path):
    seed(tmp_path)
    pool = ReadPool()
    try:
        with pool.lease(str(tmp_path), "user") as first:
            assert first.read_only and first.db.execute("PRAGMA query_only").fetchone()[0] == 1
            assert first.db.execute("PRAGMA temp_store").fetchone()[0] == 2
            assert first.db.execute("PRAGMA cache_size").fetchone()[0] == -8192
            original = recall(first, "Orion launch", options=Options(embedder="none", rerank=None))
        with pool.lease(str(tmp_path), "user") as second:
            assert second is first
            assert recall(second, "Orion launch", options=Options(embedder="none", rerank=None)) == original
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                second.db.execute("DELETE FROM turns")
    finally:
        pool.close()


def test_pool_applies_deletions_without_reopening_every_request(tmp_path):
    seed(tmp_path)
    pool = ReadPool()
    try:
        with pool.lease(str(tmp_path), "user") as first:
            assert first.turns([1])
        with Store(str(tmp_path), "user") as writer:
            writer.delete_session("s")
        with pool.lease(str(tmp_path), "user") as second:
            assert second is first
            assert second.turns([1]) == {}
    finally:
        pool.close()


def test_atomic_database_replacement_retires_old_read_handle(tmp_path):
    seed(tmp_path, text="Old confidential launch.")
    seed(tmp_path, "replacement", "New public launch.")
    pool = ReadPool()
    try:
        with pool.lease(str(tmp_path), "user") as old:
            path = old.path
        with Store(str(tmp_path), "replacement") as replacement:
            replacement.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            replacement_path = replacement.path
        os.replace(replacement_path, path)
        with pool.lease(str(tmp_path), "user") as fresh:
            assert fresh is not old
            assert "New public" in fresh.turns([1])[1].text
    finally:
        pool.close()


def test_new_canonical_identity_reopens_stale_metadata_and_vector_bindings(tmp_path):
    seed(tmp_path)
    pool = ReadPool()
    try:
        with pool.lease(str(tmp_path), "user") as old:
            old_identity = old._units_identity
        with Store(str(tmp_path), "user") as writer:
            writer.set_meta("units_identity", "replacement-unit-generation")
        with pool.lease(str(tmp_path), "user") as fresh:
            assert fresh is not old and fresh._units_identity != old_identity
            assert fresh._units_identity == "replacement-unit-generation"
    finally:
        pool.close()


def test_unicode_postings_are_repaired_before_pinned_read(tmp_path):
    seed(tmp_path, text="机架七支持超时重试。")
    pool = ReadPool()
    try:
        with pool.lease(str(tmp_path), "user", query="超时重试") as reader:
            assert unicode_index.installed(reader.db)
            assert not unicode_index.dirty(reader.db)
            result = recall(reader, "超时重试", options=Options(embedder="none", rerank=None))
            assert "机架七" in result.context
    finally:
        pool.close()


def test_capacity_and_scope_are_bounded(tmp_path):
    for space in ("alice", "bob", "carol"):
        seed(tmp_path, space, f"Private memory for {space}.")
    pool = ReadPool(maximum=2, per_path=1, wait_seconds=0.02)
    try:
        with pool.lease(str(tmp_path), "alice") as alice, pool.lease(str(tmp_path), "bob") as bob:
            assert "alice" in alice.turns([1])[1].text and "bob" in bob.turns([1])[1].text
            with pytest.raises(PoolBusy):
                with pool.lease(str(tmp_path), "carol"):
                    pytest.fail("capacity exceeded")
            with pytest.raises(PoolBusy):
                with pool.lease(str(tmp_path), "alice"):
                    pytest.fail("same-path capacity exceeded")
            assert len(pool._entries) == 2
        with pool.lease(str(tmp_path), "carol") as carol:
            assert "carol" in carol.turns([1])[1].text
            assert len(pool._entries) <= 2
    finally:
        pool.close()


def test_concurrent_leases_never_share_one_sqlite_connection(tmp_path):
    seed(tmp_path)
    pool = ReadPool(maximum=2, per_path=2)
    barrier = threading.Barrier(2)
    stores, errors = [], []

    def worker():
        try:
            with pool.lease(str(tmp_path), "user") as store:
                stores.append(store)
                barrier.wait(timeout=5)
                assert store.turns([1])
        except Exception as exc:
            errors.append(exc)

    workers = [threading.Thread(target=worker) for _ in range(2)]
    try:
        for worker_thread in workers:
            worker_thread.start()
        for worker_thread in workers:
            worker_thread.join(timeout=10)
        assert not errors and len(stores) == 2 and stores[0] is not stores[1]
    finally:
        pool.close()


def test_close_allows_active_read_to_finish_then_releases_handle(tmp_path):
    seed(tmp_path)
    pool = ReadPool()
    with pool.lease(str(tmp_path), "user") as store:
        pool.close()
        assert store.turns([1])
    assert not pool._entries
    with pytest.raises(PoolBusy):
        with pool.lease(str(tmp_path), "user"):
            pytest.fail("closed pool reopened")


@pytest.mark.parametrize("limits", [{"maximum": 0}, {"per_path": 17}, {"wait_seconds": float("nan")},
                                   {"idle_seconds": "60"}, {"wait_seconds": True}])
def test_invalid_resource_limits_fail_closed(limits):
    with pytest.raises(ValueError):
        ReadPool(**limits)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX process boundary")
def test_service_pool_and_response_cache_do_not_inherit_locked_parent_state(tmp_path):
    from commontrace.conversation import search
    from commontrace.conversation.pool import READ_POOL

    seed(tmp_path)
    with READ_POOL.lease(str(tmp_path), "user") as store:
        recall(store, "Orion launch", options=Options(embedder="none", rerank=None))
    READ_POOL._condition.acquire()
    search._RECALL_LOCK.acquire()
    child = os.fork()
    if child == 0:
        try:
            import signal

            signal.alarm(5)
            with READ_POOL.lease(str(tmp_path), "user") as fresh:
                result = recall(fresh, "Orion launch", options=Options(embedder="none", rerank=None))
                os._exit(0 if "ready" in result.context else 2)
        except BaseException:
            os._exit(3)
    READ_POOL._condition.release()
    search._RECALL_LOCK.release()
    try:
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        READ_POOL.discard_root(str(tmp_path))
