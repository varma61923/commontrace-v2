"""Bounded asynchronous access to the embedded conversation store.

One connection is owned by one dedicated thread for the entire lifetime. SQLite,
profile extraction and optional embedding inference never execute on the event
loop. Adjacent append requests share a commit, with a savepoint for each caller.
Use ``async with await AsyncStore.open(root, space)`` to drain accepted work and
close the connection. Cancellation after admission does not retract a write.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from commontrace.conversation.search import DenseCandidates, Options, Recall, _embedder, recall, subqueries
from commontrace.conversation.store import ConversationError, Store, write_txn

if TYPE_CHECKING:
    from commontrace.vector_store import VectorIndex

Operation = Literal["add", "recall", "stats", "vector_plan", "vector_batch", "vector_ids"]


@dataclass
class _Request:
    operation: Operation
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    result: asyncio.Future[Any]


class AsyncStore:
    """A loop-bound, bounded queue over a persistent local ``Store``.

    ``max_pending`` bounds both queued and executing requests. Each append is
    additionally bounded by message count and aggregate text size; input is
    copied only after admission. Results are delivered only after commit. A
    failed append rolls back its own savepoint, preserving other valid appends.
    Reads retain FIFO ordering with writes and use the ordinary recall guards.
    """

    def __init__(self, executor: ThreadPoolExecutor, store: Store, *, max_pending: int,
                 batch_size: int, max_messages: int, max_chars: int,
                 vector_index: VectorIndex | None = None) -> None:
        self._loop = asyncio.get_running_loop()
        self._executor, self._store = executor, store
        self._admission = asyncio.Semaphore(max_pending)
        self._queue: asyncio.Queue[_Request | None] = asyncio.Queue(max_pending)
        self._batch_size, self._max_messages, self._max_chars = batch_size, max_messages, max_chars
        self._closing = False
        self._vector_index = vector_index
        self._vector_lock = asyncio.Lock()
        self._vector_calls = asyncio.Semaphore(max_pending)
        self._indexed_generation: str | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._worker = self._loop.create_task(self._run(), name="commontrace-conversation-store")

    @property
    def root(self) -> str:
        """Configured local store root, for adapter scope validation."""
        return self._store.root

    @property
    def space(self) -> str:
        """Pinned conversation namespace; operations cannot override it."""
        return self._store.space

    @property
    def vector_generation(self) -> str | None:
        """Last prepared generation, for explicit owner maintenance.

        External vector retention is separate from canonical trace retention.
        Call ``index.prune(generation)`` only after quiescing every reader and
        builder of that scoped index, including other processes/instances.
        """
        return self._indexed_generation

    @classmethod
    async def open(cls, root: str, space: str, *, create: bool = True, read_only: bool = False,
                   max_pending: int = 64, batch_size: int = 16,
                   max_messages: int = 2048, max_chars: int = 8 * 1024 * 1024,
                   vector_index: VectorIndex | None = None, tenant: str | None = None) -> AsyncStore:
        """Open off-loop; defaults retain at most 64 requests of 8 MiB each.

        Set smaller limits for a fleet with many independent spaces. The limits
        are admission budgets rather than SQLite or process RSS limits.
        """
        for name, value in (("max_pending", max_pending), ("batch_size", batch_size),
                            ("max_messages", max_messages), ("max_chars", max_chars)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        owner = tenant or hashlib.sha256(os.path.realpath(root).encode("utf-8")).hexdigest()
        if vector_index is not None and (vector_index.tenant != owner or vector_index.namespace != space):
            raise ValueError("vector index does not match the pinned tenant and conversation space")
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="commontrace-sqlite")
        loop = asyncio.get_running_loop()
        opening = loop.run_in_executor(executor, lambda: Store(root, space, create=create, read_only=read_only))
        try:
            store = await asyncio.shield(opening)
        except BaseException:
            # Initialization can finish after cancellation. Close that late
            # connection on its owner thread instead of abandoning it.
            def dispose(future: asyncio.Future[Store]) -> None:
                if not future.cancelled() and future.exception() is None:
                    executor.submit(future.result().close)
                executor.shutdown(wait=False)

            opening.add_done_callback(dispose)
            raise
        return cls(executor, store, max_pending=max_pending, batch_size=batch_size,
                   max_messages=max_messages, max_chars=max_chars, vector_index=vector_index)

    def _check_open(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("AsyncStore must be used on the event loop that opened it")
        if self._closing:
            raise RuntimeError("AsyncStore is closing or closed")

    async def _admit(self) -> None:
        self._check_open()
        await self._admission.acquire()
        try:
            self._check_open()
        except BaseException:
            self._admission.release()
            raise

    async def _enqueue(self, operation: Operation,
                       args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        result = self._loop.create_future()
        # A cancelled caller no longer observes an admitted operation's error.
        # Retrieve it to avoid unhandled-Future warnings while preserving normal
        # exception delivery to callers who remain attached.
        result.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
        self._queue.put_nowait(_Request(operation, args, kwargs, result))
        return await asyncio.shield(result)

    async def add(self, session: str, messages: Iterable[Mapping[str, Any]], *, session_at: Any = None,
                  user_speakers: Iterable[str] = (), extract_profile: bool = True) -> dict[str, Any]:
        """Append atomically; backpressure precedes input consumption/copying."""
        await self._admit()
        try:
            copied: list[dict[str, Any]] = []
            chars = 0
            for message in messages:
                if len(copied) >= self._max_messages:
                    raise ConversationError(f"append exceeds {self._max_messages} messages")
                chars += len(str(message.get("text", message.get("content", "")) or ""))
                if chars > self._max_chars:
                    raise ConversationError(f"append exceeds {self._max_chars} text characters")
                copied.append(copy.deepcopy(dict(message)))
            kwargs = {"session_at": session_at, "user_speakers": tuple(user_speakers),
                      "extract_profile": extract_profile}
        except BaseException:
            self._admission.release()
            raise
        return cast(dict[str, Any], await self._enqueue("add", (session, copied), kwargs))

    async def recall(self, question: str, *, options: Options | None = None) -> Recall:
        """Recall off-loop, using an optional scoped engine as the dense RRF arm.

        A configured engine is populated lazily from canonical retrieval units,
        using bounded embedding batches. Evidence revisions and embedding model
        must match exactly; concurrent mutation can require the caller to retry.
        Engine ownership/lifetime remains with its configuring application.
        """
        if self._vector_index is not None and question.strip() and (
            options is None or options.embedder is not None and options.pool > 0
        ):
            self._check_open()
            async with self._vector_calls:
                self._check_open()
                return await self._vector_recall(question, copy.deepcopy(options) if options is not None else Options())
        await self._admit()
        try:
            kwargs = {"options": copy.deepcopy(options)}
        except BaseException:
            self._admission.release()
            raise
        return cast(Recall, await self._enqueue("recall", (question,), kwargs))

    async def _request(self, operation: Operation, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        await self._admit()
        return await self._enqueue(operation, args, kwargs)

    def _vector_plan(self, question: str, options: Options) -> dict[str, Any] | None:
        from commontrace.conversation import embed

        index = self._vector_index
        assert index is not None
        with self._store.read_snapshot():
            encoder = _embedder(self._store, options.embedder)
            if encoder is None:
                return None  # Same optional-model lexical fallback as ordinary recall.
            if index.model != embed.MODELS[encoder.tag][0]:
                raise ConversationError("vector index uses a different embedding model")
            if self._store._units_identity is None:
                raise ConversationError("external vector retrieval requires an upgraded conversation store")
            stamp = self._store.unit_stamp()
            identity = self._store._units_identity or self._store.cache_identity
            generation = hashlib.sha256(json.dumps([str(identity), stamp, index.model, index.dimension,
                                                     embed.MAX_SEQ], sort_keys=True).encode()).hexdigest()
            queries = list(dict.fromkeys(subqueries(question.strip())))
            vectors = encoder.encode(queries, query=True) if queries else []
            allowed = self._store.allowed(sessions=options.sessions, speakers=options.speakers,
                                         since=options.since, until=options.until)
            return {"stamp": stamp, "identity": identity, "generation": generation, "queries": queries,
                    "vectors": vectors, "allowed": None if allowed is None else json.dumps(sorted(allowed)),
                    "choice": encoder.tag}

    def _vector_ids(self, plan: dict[str, Any], after: int) -> list[str]:
        """Keyset pages bound the filter payload even for very large sessions."""
        with self._store.read_snapshot():
            if self._store.unit_stamp() != plan["stamp"]:
                raise ConversationError("conversation changed while querying the vector index; retry recall")
            return [str(row[0]) for row in self._store.db.execute(
                "SELECT id FROM units WHERE id>? AND turn IN (SELECT value FROM json_each(?)) "
                "ORDER BY id LIMIT 10000", (after, plan["allowed"]))]

    def _vector_batch(self, plan: dict[str, Any], after: int) -> list[Any]:
        from commontrace.vector_store import VectorRecord

        with self._store.read_snapshot():
            if self._store.unit_stamp() != plan["stamp"]:
                raise ConversationError("conversation changed while preparing the vector index; retry recall")
            rows = self._store.units(after_id=after, limit=512)
            encoder = _embedder(self._store, plan["choice"])
            assert encoder is not None
            vectors = encoder.vectors([(row[3], row[2]) for row in rows])
            return [VectorRecord(str(row[0]), vector, plan["generation"]) for row, vector in zip(rows, vectors)]

    async def _vector_recall(self, question: str, options: Options) -> Recall:
        index = self._vector_index
        assert index is not None
        async with self._vector_lock:
            plan = await self._request("vector_plan", (question, options), {})
            if plan is None:
                return cast(Recall, await self._request("recall", (question,), {"options": options}))
            # A copied SQLite database preserves its UUID but is a distinct
            # authoritative source. Bind both its canonical locator and UUID.
            source = hashlib.sha256(json.dumps([os.path.realpath(self._store.path), str(plan["identity"])])
                                    .encode()).hexdigest()
            await index.bind_source(source)
            if self._indexed_generation != plan["generation"]:
                after = 0
                while rows := await self._request("vector_batch", (plan, after), {}):
                    await index.upsert(rows)
                    after = int(rows[-1].key)
                self._indexed_generation = plan["generation"]
            rankings: dict[str, tuple[int, ...]] = {}
            pages: dict[str, list[Any]] = {query: [] for query in plan["queries"]}
            after = 0
            while True:
                ids = None if plan["allowed"] is None else await self._request("vector_ids", (plan, after), {})
                if ids == []:
                    break
                for query, vector in zip(plan["queries"], plan["vectors"]):
                    hits = await index.search(vector, top_k=options.pool, generation=plan["generation"],
                                              allowed_ids=ids)
                    pages[query] = sorted(pages[query] + hits, key=lambda hit: (-hit.score, hit.key))[:options.pool]
                if ids is None:
                    break
                after = int(ids[-1])
            for query, hits in pages.items():
                # Never trust provider metadata or text. IDs are rejoined to
                # canonical units and rechecked against eligibility by recall.
                rankings[query] = tuple(int(hit.key) for hit in hits if hit.key.isdecimal())
            dense = DenseCandidates(plan["identity"], plan["stamp"], index.model, rankings)
            return cast(Recall, await self._request("recall", (question,),
                                                   {"options": options, "dense_candidates": dense}))

    async def stats(self) -> dict[str, Any]:
        """Read durable store counts after all previously admitted operations."""
        await self._admit()
        return cast(dict[str, Any], await self._enqueue("stats", (), {}))

    def _execute(self, batch: list[_Request]) -> list[tuple[Any, Exception | None]]:
        outcomes: list[tuple[Any, Exception | None]] = []
        offset = 0
        while offset < len(batch):
            request = batch[offset]
            if request.operation == "add":
                end = offset + 1
                while end < len(batch) and batch[end].operation == "add":
                    end += 1
                group: list[tuple[Any, Exception | None]] = []
                try:
                    with self._store._lock, write_txn(self._store.db):
                        for pending in batch[offset:end]:
                            try:
                                group.append((self._store.add(*pending.args, **pending.kwargs), None))
                            except Exception as exc:
                                group.append((None, exc))
                except Exception as exc:
                    # No caller receives success if the shared COMMIT failed.
                    group = [(None, exc) for _ in batch[offset:end]]
                outcomes.extend(group)
                offset = end
                continue
            try:
                value: Any
                if request.operation == "recall":
                    value = recall(self._store, *request.args, **request.kwargs)
                elif request.operation == "vector_plan":
                    value = self._vector_plan(*request.args)
                elif request.operation == "vector_batch":
                    value = self._vector_batch(*request.args)
                elif request.operation == "vector_ids":
                    value = self._vector_ids(*request.args)
                else:
                    value = self._store.stats()
                outcomes.append((value, None))
            except Exception as exc:
                outcomes.append((None, exc))
            offset += 1
        return outcomes

    async def _run(self) -> None:
        try:
            stop = False
            while not stop:
                request = await self._queue.get()
                if request is None:
                    self._queue.task_done()
                    break
                batch = [request]
                while len(batch) < self._batch_size and not self._queue.empty():
                    pending = self._queue.get_nowait()
                    if pending is None:
                        self._queue.task_done()
                        stop = True
                        break
                    batch.append(pending)
                try:
                    outcomes = await self._loop.run_in_executor(self._executor, self._execute, batch)
                except Exception as exc:
                    outcomes = [(None, exc) for _ in batch]
                for pending, (value, error) in zip(batch, outcomes):
                    if error is None:
                        pending.result.set_result(value)
                    else:
                        pending.result.set_exception(error)
                    self._admission.release()
                    self._queue.task_done()
        finally:
            await self._loop.run_in_executor(self._executor, self._store.close)
            self._executor.shutdown(wait=False)

    async def _drain(self) -> None:
        await self._queue.put(None)
        await self._worker

    async def close(self) -> None:
        """Drain accepted requests; concurrent/cancelled closes share one drain."""
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("AsyncStore must be closed on its owning event loop")
        if self._close_task is None:
            self._closing = True
            self._close_task = self._loop.create_task(self._drain())
        await asyncio.shield(self._close_task)

    async def __aenter__(self) -> AsyncStore:
        self._check_open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
