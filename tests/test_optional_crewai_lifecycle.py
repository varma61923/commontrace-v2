"""Credential-free runtime contract for CrewAI kickoff callbacks."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from unimem.client import MemoryClient


def test_crewai_kickoff_entry_points_with_callbacks(monkeypatch, tmp_path):
    # Disable CrewAI's process-global telemetry provider before its import so
    # its atexit exporter cannot outlive the optional runtime check.
    monkeypatch.setenv("CREWAI_DISABLE_TELEMETRY", "true")
    pytest.importorskip("crewai")

    from crewai import Agent, Crew, Process, Task
    from crewai.llms.base_llm import BaseLLM
    from pydantic import PrivateAttr

    from unimem.framework_middleware import Middleware

    class ScriptedLLM(BaseLLM):
        _calls: list[tuple[Any, ...]] = PrivateAttr(default_factory=list)

        def call(
            self,
            messages,
            tools=None,
            callbacks=None,
            available_functions=None,
            from_task=None,
            from_agent=None,
            response_model=None,
        ):
            self._calls.append(("sync", messages))
            return "The fixed scripted answer is: release gate is BLUE."

        async def acall(
            self,
            messages,
            tools=None,
            callbacks=None,
            available_functions=None,
            from_task=None,
            from_agent=None,
            response_model=None,
        ):
            self._calls.append(("async", messages))
            await asyncio.sleep(0.005)
            return "The fixed scripted answer is: release gate is BLUE."

    observed: list[tuple[Any, ...]] = []
    heartbeat: list[asyncio.Event | None] = [None]
    loop_ticks: list[list[int] | None] = [None]

    def fake_turn(self, session_id, query, **kwargs):
        observed.append((session_id, query, kwargs.copy()))
        if query:
            # The same blocking work a local or synchronous HTTP-backed turn may do.
            time.sleep(0.03)
            event = heartbeat[0]
            observed.append(("heartbeat_during_before", event.is_set() if event else None))
            return {"context": "Release gate is BLUE; verify the artifact checksum."}
        if kwargs.get("event") == "SessionEnd" and loop_ticks[0] is not None:
            ticks = loop_ticks[0]
            before = ticks[0]
            time.sleep(0.03)
            observed.append(("loop_ticks_during_after", ticks[0] - before))
        return {"context": ""}

    monkeypatch.setattr(MemoryClient, "turn", fake_turn)

    def make_crew(session_id):
        memory = MemoryClient(
            str(tmp_path),
            url="https://gateway.invalid",
            token="test-token",
            agent_id="release",
            context={"tenant": "acme", "user": "alice"},
        )
        middleware = Middleware(memory, session_id)
        llm = ScriptedLLM(model="unimem-test")
        agent = Agent(
            role="release auditor",
            goal="Read the supplied evidence and answer",
            backstory="You are deterministic.",
            llm=llm,
            max_iter=1,
            allow_delegation=False,
            verbose=False,
        )
        task = Task(
            description="Use the UniMem evidence {unimem_memory}. Task: {task}",
            expected_output="One sentence.",
            agent=agent,
        )
        crew = Crew(
            agents=[agent],
            tasks=[task],
            process=Process.sequential,
            verbose=False,
            tracing=False,
            before_kickoff_callbacks=[middleware.crewai_before_kickoff],
            after_kickoff_callbacks=[middleware.crewai_after_kickoff],
        )
        return crew, llm

    expected = "The fixed scripted answer is: release gate is BLUE."
    crew, llm = make_crew("crew-sync")
    result = crew.kickoff(inputs={"task": "prepare release"})

    assert type(result).__name__ == "CrewOutput"
    assert result.raw == expected
    assert len(llm._calls) == 1
    assert "Release gate is BLUE; verify the artifact checksum." in str(llm._calls[0][1])
    sync_before, sync_end = [item for item in observed if len(item) == 3]
    assert sync_before[0] == "crew-sync"
    assert sync_before[2] == {"text": "", "budget": 600}
    assert sync_end[0] == "crew-sync"
    assert sync_end[2]["event"] == "SessionEnd"
    assert sync_end[2]["payload"] == {"succeeded": True, "result": expected}

    observed.clear()

    async def run_threaded_async():
        heartbeat[0] = asyncio.Event()
        ticks = [0]
        loop_ticks[0] = ticks
        crew, llm = make_crew("crew-kickoff-async")
        stop_ticker = [False]

        async def wake_loop():
            heartbeat[0].set()

        async def tick_loop():
            while not stop_ticker[0]:
                ticks[0] += 1
                await asyncio.sleep(0)

        heartbeat_task = asyncio.create_task(wake_loop())
        ticker_task = asyncio.create_task(tick_loop())
        result = await crew.kickoff_async(inputs={"task": "prepare async release"})
        stop_ticker[0] = True
        await heartbeat_task
        await ticker_task
        return result, llm

    threaded_result, threaded_llm = asyncio.run(run_threaded_async())

    assert type(threaded_result).__name__ == "CrewOutput"
    assert threaded_result.raw == expected
    assert len(threaded_llm._calls) == 1
    assert "Release gate is BLUE; verify the artifact checksum." in str(threaded_llm._calls[0][1])
    assert ("heartbeat_during_before", True) in observed
    threaded_after_ticks = [item[1] for item in observed if len(item) == 2 and item[0] == "loop_ticks_during_after"]
    assert threaded_after_ticks and threaded_after_ticks[0] > 0
    threaded_before, threaded_end = [item for item in observed if len(item) == 3]
    assert threaded_before[0] == "crew-kickoff-async"
    assert threaded_end[2]["event"] == "SessionEnd"
    assert threaded_end[2]["payload"] == {"succeeded": True, "result": expected}

    observed.clear()

    async def run_native_async():
        heartbeat[0] = asyncio.Event()
        ticks = [0]
        loop_ticks[0] = ticks
        crew, llm = make_crew("crew-async")
        stop_ticker = [False]

        async def wake_loop():
            heartbeat[0].set()

        async def tick_loop():
            while not stop_ticker[0]:
                ticks[0] += 1
                await asyncio.sleep(0)

        heartbeat_task = asyncio.create_task(wake_loop())
        ticker_task = asyncio.create_task(tick_loop())
        result = await crew.akickoff(inputs={"task": "prepare async release"})
        stop_ticker[0] = True
        await heartbeat_task
        await ticker_task
        return result, llm

    async_result, async_llm = asyncio.run(run_native_async())

    assert type(async_result).__name__ == "CrewOutput"
    assert async_result.raw == expected
    assert len(async_llm._calls) == 1
    assert "Release gate is BLUE; verify the artifact checksum." in str(async_llm._calls[0][1])
    assert ("heartbeat_during_before", False) in observed
    assert ("loop_ticks_during_after", 0) in observed
    async_before, async_end = [item for item in observed if len(item) == 3]
    assert async_before[0] == "crew-async"
    assert async_end[2]["event"] == "SessionEnd"
    assert async_end[2]["payload"] == {"succeeded": True, "result": expected}
