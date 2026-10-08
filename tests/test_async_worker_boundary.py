"""Admission budgets survive cancellation and preserve caller context."""
from __future__ import annotations

import asyncio
import contextvars
import json
import multiprocessing
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from commontrace.async_workers import BoundedWorkers, WorkerCapacityError


@pytest.mark.parametrize("kwargs", [{"workers": 0}, {"workers": True}, {"max_pending": 0}, {"max_pending": 1.5}])
def test_invalid_worker_budgets_are_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        BoundedWorkers(**kwargs)


def test_cancelled_waiter_cannot_evade_admission_and_shutdown_drains() -> None:
    entered, release = threading.Event(), threading.Event()
    finished: list[str] = []
    workers = BoundedWorkers(workers=1, max_pending=1)

    def write() -> str:
        entered.set()
        assert release.wait(5)
        finished.append("durable")
        return "durable"

    async def drive() -> None:
        request = asyncio.create_task(workers.run(write))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            with pytest.raises(WorkerCapacityError):
                await workers.run(lambda: "cannot bypass")
            closing = asyncio.create_task(workers.close())
            await asyncio.sleep(0)
            assert not closing.done()
            with pytest.raises(RuntimeError, match="closed"):
                await workers.run(lambda: "cannot reopen")
        finally:
            release.set()
        await asyncio.wait_for(closing, 3)
        assert finished == ["durable"]

    asyncio.run(drive())


def test_worker_context_does_not_leak_between_requests() -> None:
    tenant: contextvars.ContextVar[str] = contextvars.ContextVar("worker_test_tenant", default="none")
    workers = BoundedWorkers(workers=1, max_pending=4)

    async def invoke(value: str) -> str:
        token = tenant.set(value)
        try:
            return await workers.run(tenant.get)
        finally:
            tenant.reset(token)

    async def drive() -> None:
        try:
            assert await asyncio.gather(invoke("org-a"), invoke("org-b")) == ["org-a", "org-b"]
            assert await workers.run(tenant.get) == "none"
        finally:
            await workers.close()

    asyncio.run(drive())


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable on this platform")
def test_preloaded_worker_pool_recovers_after_fork() -> None:
    workers = BoundedWorkers(workers=1, max_pending=1)
    assert asyncio.run(workers.run(lambda: "parent")) == "parent"
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe(duplex=False)

    def run_child() -> None:
        async def drive() -> None:
            try:
                child.send(await workers.run(lambda: "child"))
            finally:
                await workers.close()

        asyncio.run(drive())
        child.close()

    process = context.Process(target=run_child)
    process.start()
    child.close()
    try:
        assert parent.poll(5), "child reused parent thread state and stalled"
        assert parent.recv() == "child"
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
        asyncio.run(workers.close())


def test_mcp_slow_retrieval_leaves_other_tools_responsive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mcp")
    from commontrace import lesson_cache, mcp_server

    entered, release = threading.Event(), threading.Event()
    original = lesson_cache.load_active_with_terms

    def held(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(lesson_cache, "load_active_with_terms", held)
    server = mcp_server.build_server(str(tmp_path))

    async def drive() -> None:
        pending = asyncio.create_task(server.call_tool("retrieve", {"task": "recover a failed payment transaction"}))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            result = await asyncio.wait_for(server.call_tool("store_status", {}), 1)
            assert json.loads(result.content[0].text)["ok"] is True
        finally:
            release.set()
        assert json.loads((await pending).content[0].text)["ok"] is True

    asyncio.run(drive())


def test_mcp_compare_and_set_rejects_one_of_two_stale_writers(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    from commontrace import mcp_server, memory_blocks

    revision = memory_blocks.set_block(str(tmp_path), "project", "initial project facts").revision
    server = mcp_server.build_server(str(tmp_path))

    async def drive() -> None:
        responses = await asyncio.gather(*[
            server.call_tool("memory_block_update", {"name": "project", "content": value,
                                                    "expected_revision": revision})
            for value in ("changed project facts", "alternative project facts")
        ])
        decoded = [json.loads(response.content[0].text) for response in responses]
        assert sorted(result["ok"] for result in decoded) == [False, True]
        conflict = next(result for result in decoded if not result["ok"])
        current = memory_blocks.get_block(str(tmp_path), "project")
        assert conflict["code"] == "revision_conflict"
        assert conflict["actual_revision"] == current.revision

    asyncio.run(drive())
