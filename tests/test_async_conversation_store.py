from __future__ import annotations

import asyncio
import threading

import pytest

from commontrace.conversation import AsyncStore, ConversationError, Options, Store


def test_concurrent_ingestion_batches_and_recalls(tmp_path, monkeypatch):
    statements = []
    original = Store.__init__

    def observe(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.db.set_trace_callback(statements.append)

    monkeypatch.setattr(Store, "__init__", observe)

    async def run():
        async with await AsyncStore.open(str(tmp_path), "load", max_pending=16, batch_size=8) as store:
            outcomes = await asyncio.gather(*[
                store.add("session", [{"id": str(i), "text": f"Deployment retry number {i}", "role": "user"}],
                          extract_profile=False) for i in range(100)
            ])
            assert sum(row["added"] for row in outcomes) == 100
            assert (await store.stats())["turns"] == 100
            found = await store.recall("Deployment retry", options=Options(embedder=None, rerank=None))
            assert found.turns
            assert "Deployment retry" in found.context
        assert statements.count("COMMIT") < 100

    asyncio.run(run())
    with Store(str(tmp_path), "load") as reopened:
        assert [turn.ref for turn in reopened.session_turns("session")] == [str(i) for i in range(100)]


def test_invalid_append_rolls_back_only_its_savepoint(tmp_path):
    async def run():
        async with await AsyncStore.open(str(tmp_path), "failures") as store:
            outcomes = await asyncio.gather(
                store.add("good", [{"text": "First valid evidence"}]),
                store.add("bad", [{"text": "Partial evidence"}, {"text": "Invalid dated evidence", "at": "bad date"}]),
                store.add("good", [{"text": "Second valid evidence"}]), return_exceptions=True,
            )
            assert isinstance(outcomes[1], ConversationError)
            assert (await store.stats())["turns"] == 2
        with Store(str(tmp_path), "failures") as stored:
            assert not stored.session_turns("bad")
            assert len(stored.session_turns("good")) == 2

    asyncio.run(run())


def test_backpressure_does_not_consume_waiting_input_and_cancelled_write_drains(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    original = Store.add
    consumed = []

    def slow(self, *args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Store, "add", slow)

    def messages():
        consumed.append(True)
        yield {"text": "Waiting evidence"}

    async def run():
        store = await AsyncStore.open(str(tmp_path), "pressure", max_pending=1)
        first = asyncio.create_task(store.add("first", [{"text": "Accepted evidence"}]))
        while not started.is_set():
            await asyncio.sleep(0.001)
        second = asyncio.create_task(store.add("second", messages()))
        await asyncio.sleep(0)
        assert consumed == []
        # The event loop keeps running while a SQLite worker is occupied.
        beats = 0
        for _ in range(10):
            await asyncio.sleep(0)
            beats += 1
        assert beats == 10
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        closing = asyncio.create_task(store.close())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.gather(closing, return_exceptions=True)
        release.set()
        await store.close()
        with pytest.raises(RuntimeError, match="closing"):
            await store.stats()

    try:
        asyncio.run(run())
    finally:
        release.set()
    assert consumed == []
    with Store(str(tmp_path), "pressure") as stored:
        assert stored.stats()["turns"] == 1


def test_input_limits_release_admission_and_read_only_rejects_writes(tmp_path):
    async def run():
        async with await AsyncStore.open(str(tmp_path), "limits", max_pending=1,
                                        max_messages=1, max_chars=20) as store:
            with pytest.raises(ConversationError, match="messages"):
                await store.add("s", [{"text": "one"}, {"text": "two"}])
            with pytest.raises(ConversationError, match="characters"):
                await store.add("s", [{"text": "x" * 21}])
            await asyncio.wait_for(store.add("s", [{"text": "Valid evidence"}]), 5)
        async with await AsyncStore.open(str(tmp_path), "limits", read_only=True) as frozen:
            import sqlite3

            with pytest.raises(sqlite3.OperationalError):
                await frozen.add("s", [{"text": "Forbidden evidence"}])
            assert (await frozen.stats())["turns"] == 1

    asyncio.run(run())


def test_hot_recall_observes_an_external_writer(tmp_path):
    async def run():
        async with await AsyncStore.open(str(tmp_path), "shared") as store:
            await store.add("first", [{"text": "Deployment retry uses jitter"}], extract_profile=False)
            options = Options(embedder=None, rerank=None, neighbours_before=0, neighbours_after=0,
                              summaries=False)
            initial = await store.recall("Deployment retry", options=options)
            assert "jitter" in initial.context

            def external_write():
                with Store(str(tmp_path), "shared") as writer:
                    writer.add("second", [{"text": "Deployment retry uses exponential backoff"}],
                               extract_profile=False)

            await asyncio.to_thread(external_write)
            refreshed = await store.recall("Deployment retry", options=options)
            assert "exponential backoff" in refreshed.context
            assert len(refreshed.turns) == 2

    asyncio.run(run())


@pytest.mark.parametrize("option", ["max_pending", "batch_size", "max_messages", "max_chars"])
def test_invalid_configuration(tmp_path, option):
    with pytest.raises(ValueError, match=option):
        asyncio.run(AsyncStore.open(str(tmp_path), "s", **{option: 0}))
