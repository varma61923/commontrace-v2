"""Independent processes exercise only public vector snapshot contracts.

SQLite always runs; PostgreSQL exact/HNSW run when the service DSN is provided.
Child interpreters create their own connections, so parent-only locks or caches
cannot make these publication, read-lease and crash-recovery tests pass.
"""
from __future__ import annotations

import asyncio
import json
import os
import select
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from commontrace.vector_snapshots import BuildLease, SnapshotConflict
from commontrace.vector_store import PostgresVectorIndex, SQLiteVectorIndex, VectorRecord


async def _opening(settings: dict[str, Any]) -> SQLiteVectorIndex | PostgresVectorIndex:
    scope = {"tenant": settings["tenant"], "namespace": "memory",
             "model": settings.get("model", "contract-embedding"), "dimension": 3}
    if settings["engine"] == "sqlite":
        return await SQLiteVectorIndex.open(settings["path"], **scope)
    return await PostgresVectorIndex.open(settings["dsn"], approximate=settings["engine"] == "pghnsw", **scope)


def _emit(value: object) -> None:
    print(json.dumps(value), flush=True)


async def _worker() -> None:
    request = json.loads(sys.stdin.readline())
    if request["role"] == "canonical-writer":
        from commontrace.conversation import Store

        _emit({"ready": True})
        command = json.loads(await asyncio.to_thread(sys.stdin.readline))
        assert command["action"] == "mutate"
        with Store(request["root"], "memory") as store:
            store.delete_session("cats")
            store.add("replacement", [{"text": "A rocket launches toward space."}], extract_profile=False)
        _emit({"mutated": True})
        return
    index = await _opening(request["settings"])
    try:
        snapshots = index.snapshots
        if request["role"] == "builder":
            generation = request["generation"]
            lease = await snapshots.begin(request["revision"], generation,
                                          lease_seconds=request.get("lease_seconds", 60))
            assert lease is not None
            await snapshots.stage(lease, [VectorRecord(generation, [0, 1, 0])])
            _emit({"ready": True, "lease": asdict(lease)})
            command = json.loads(await asyncio.to_thread(sys.stdin.readline))
            if command["action"] == "crash":
                # Abrupt exit deliberately bypasses pool/lease cleanup. Only
                # the durable database lease may authorize later publication.
                os._exit(0)
            _emit({"published": await snapshots.publish(lease)})
        else:
            read = await snapshots.pin(request["generation"])
            _emit({"ready": True})
            while True:
                command = json.loads(await asyncio.to_thread(sys.stdin.readline))
                if command["action"] == "release":
                    await snapshots.release(read)
                    _emit({"released": True})
                    break
                hits = await snapshots.search(read, [1, 0, 0])
                _emit({"hits": [[hit.key, hit.score] for hit in hits]})
    finally:
        await index.close()


def _send(process: subprocess.Popen[str], value: object) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(value) + "\n")
    process.stdin.flush()


def _receive(process: subprocess.Popen[str]) -> dict[str, Any]:
    assert process.stdout is not None
    assert select.select([process.stdout], [], [], 30)[0], "child did not answer within 30 seconds"
    message = process.stdout.readline()
    assert message, "child exited before its contract response"
    result = json.loads(message)
    assert isinstance(result, dict) and "error" not in result, result
    return result


@contextmanager
def _child(settings: dict[str, Any], role: str, **kwargs: object) -> Iterator[subprocess.Popen[str]]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve())],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env=environment)
    try:
        _send(process, {"settings": settings, "role": role, **kwargs})
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


@pytest.fixture(params=["sqlite", "pgexact", "pghnsw"])
def settings(request, tmp_path):
    dsn = os.environ.get("COMMONTRACE_TEST_PGVECTOR_DSN")
    if request.param != "sqlite" and not dsn:
        pytest.skip("set COMMONTRACE_TEST_PGVECTOR_DSN to exercise real PostgreSQL processes")
    return {"engine": request.param, "path": str(tmp_path / "vectors.db"), "dsn": dsn,
            "tenant": "process-contract-" + uuid.uuid4().hex}


async def _seed(index: SQLiteVectorIndex | PostgresVectorIndex) -> None:
    build = await index.snapshots.begin(0, "base", full=True)
    assert build is not None
    await index.snapshots.stage(build, [VectorRecord("base", [1, 0, 0])])
    assert await index.snapshots.publish(build)


def test_independent_builders_publish_one_complete_snapshot(settings):
    async def exercise() -> None:
        index = await _opening(settings)
        try:
            await _seed(index)
            with _child(settings, "builder", revision=1, generation="left") as left, \
                    _child(settings, "builder", revision=2, generation="right") as right:
                first, second = _receive(left), _receive(right)
                assert first["lease"]["base_revision"] == second["lease"]["base_revision"] == 0
                assert first["lease"]["fence"] != second["lease"]["fence"]
                old = await index.snapshots.pin("base")
                assert [hit.key for hit in await index.snapshots.search(old, [1, 0, 0])] == ["base"]
                _send(left, {"action": "publish"})
                _send(right, {"action": "publish"})
                won = [_receive(left)["published"], _receive(right)["published"]]
                assert sorted(won) == [False, True]
                head = await index.snapshots.head()
                assert head.generation in ("left", "right")
                read = await index.snapshots.pin(head.generation)
                assert {hit.key for hit in await index.snapshots.search(read, [1, 0, 0])} == {"base", head.generation}
                assert [hit.key for hit in await index.snapshots.search(old, [1, 0, 0])] == ["base"]
                await index.snapshots.release(old)
                await index.snapshots.release(read)
        finally:
            await index.close()
    asyncio.run(exercise())


def test_process_reader_pin_survives_publication_and_collection(settings):
    async def exercise() -> None:
        index = await _opening(settings)
        try:
            await _seed(index)
            with _child(settings, "reader", generation="base") as reader:
                assert _receive(reader)["ready"]
                replacement = await index.snapshots.begin(1, "replacement", full=True)
                assert replacement is not None
                await index.snapshots.stage(replacement, [VectorRecord("replacement", [0, 1, 0])])
                assert await index.snapshots.publish(replacement)
                assert await index.snapshots.collect_garbage() == 0
                _send(reader, {"action": "search"})
                assert _receive(reader)["hits"] == [["base", 1.0]]
                current = await index.snapshots.pin("replacement")
                assert [hit.key for hit in await index.snapshots.search(current, [1, 0, 0])] == ["replacement"]
                _send(reader, {"action": "release"})
                assert _receive(reader)["released"]
                assert await index.snapshots.collect_garbage() == 1
                with pytest.raises(SnapshotConflict):
                    await index.snapshots.pin("base")
                await index.snapshots.release(current)
        finally:
            await index.close()
    asyncio.run(exercise())


def test_crashed_builder_cannot_publish_after_lease_expiry(settings):
    async def exercise() -> None:
        index = await _opening(settings)
        try:
            await _seed(index)
            with _child(settings, "builder", revision=1, generation="abandoned", lease_seconds=1) as child:
                ready = _receive(child)
                stale = BuildLease(**ready["lease"])
                _send(child, {"action": "crash"})
                assert child.wait(timeout=10) == 0
            assert (await index.snapshots.head()).generation == "base"
            with pytest.raises(SnapshotConflict):
                await index.snapshots.pin("abandoned")
            await asyncio.sleep(1.1)
            assert not await index.snapshots.publish(stale)
            with pytest.raises(SnapshotConflict):
                await index.snapshots.stage(stale, [VectorRecord("replay", [1, 0, 0])])
            assert await index.snapshots.collect_garbage() == 1
            recovered = await index.snapshots.begin(1, "recovered")
            assert recovered is not None
            await index.snapshots.stage(recovered, [VectorRecord("recovered", [0, 0, 1])])
            assert await index.snapshots.publish(recovered)
            read = await index.snapshots.pin("recovered")
            assert {hit.key for hit in await index.snapshots.search(read, [1, 0, 0])} == {"base", "recovered"}
            await index.snapshots.release(read)
        finally:
            await index.close()
    asyncio.run(exercise())


def test_external_process_mutation_aborts_staged_canonical_build(settings, tmp_path, monkeypatch):
    from commontrace.conversation import AsyncStore, ConversationError, Options, embed

    np = pytest.importorskip("numpy")

    class Encoder:
        def encode(self, texts, **kwargs):
            vectors = []
            for text in texts:
                words = set(text.lower().strip(".?").split())
                vector = np.array([len(words & {"lynx", "feline"}), len(words & {"rocket", "space"}),
                                   len(words & {"sea", "waves"})], dtype=np.float32) + 0.01
                vectors.append(vector / np.linalg.norm(vector))
            return np.asarray(vectors, dtype=np.float32)

    encoder = Encoder()
    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "_model", lambda _tag: encoder)
    configured = {**settings, "model": embed.MODELS["minilm"][0]}
    options = Options(embedder="minilm", rerank=None, neighbours_before=0, neighbours_after=0,
                      profile_facts=0, instructions=0, summaries=False, entity_boost=0, recency_boost=0)

    async def exercise() -> None:
        index = await _opening(configured)
        try:
            async with await AsyncStore.open(str(tmp_path), "memory", vector_index=index,
                                            tenant=settings["tenant"]) as store:
                await store.add("cats", [{"text": "A lynx rests."}], extract_profile=False)
                assert "lynx" in (await store.recall("feline", options=options)).context
                previous = await index.snapshots.head()
                await store.add("sea", [{"text": "Sea waves roll."}], extract_profile=False)
                original = index.snapshots.stage
                changed = False
                with _child(configured, "canonical-writer", root=str(tmp_path)) as writer:
                    assert _receive(writer)["ready"]

                    async def changing(lease, records, deleted_keys=()):
                        nonlocal changed
                        count = await original(lease, records, deleted_keys)
                        if not changed:
                            changed = True
                            _send(writer, {"action": "mutate"})
                            assert (await asyncio.to_thread(_receive, writer))["mutated"]
                        return count

                    monkeypatch.setattr(index.snapshots, "stage", changing)
                    with pytest.raises(ConversationError, match="stale|changed"):
                        await store.recall("feline", options=options)
                    assert changed
                    assert await index.snapshots.head() == previous
                    recovered = await store.recall("space", options=options)
                    assert "rocket" in recovered.context and "lynx" not in recovered.context
                    assert await index.snapshots.head() != previous
        finally:
            await index.close()
    asyncio.run(exercise())


if __name__ == "__main__":
    try:
        asyncio.run(_worker())
    except Exception as exc:
        _emit({"error": type(exc).__name__})
        raise SystemExit(1) from None
