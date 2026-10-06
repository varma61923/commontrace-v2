"""Batch sync bounds both task allocation and shutdown of live operations."""
import asyncio

import pytest

from commontrace import hub_client


def test_batch_consumes_lazily_and_preserves_order():
    async def run():
        produced = completed = 0
        peak_tasks = 0

        def source():
            nonlocal produced
            for i in range(1000):
                produced += 1
                assert produced - completed <= 7
                yield str(i)

        async def call(value):
            nonlocal completed, peak_tasks
            peak_tasks = max(peak_tasks, len(asyncio.all_tasks()))
            await asyncio.sleep(0)
            completed += 1
            return int(value)

        result = await hub_client._gather_bounded(source(), call, 7)
        assert result == list(range(1000))
        assert peak_tasks <= 8  # seven workers and the caller

    asyncio.run(run())


@pytest.mark.parametrize('concurrency', [0, -4, 1, 8])
def test_empty_batch_does_not_call_worker(concurrency):
    async def call(_value):
        pytest.fail('empty input must not run a callback')

    assert asyncio.run(hub_client._gather_bounded(iter(()), call, concurrency)) == []


@pytest.mark.parametrize('failure', ['callback', 'iterator', 'cancel'])
def test_batch_failure_finishes_other_workers(failure):
    async def run():
        active = set()
        ready = asyncio.Event()

        def source():
            yield 'waiting'
            yield 'other'
            if failure == 'iterator':
                raise ValueError('source failed')
            yield 'third'

        async def call(value):
            active.add(value)
            try:
                if value == 'waiting':
                    ready.set()
                    await asyncio.Event().wait()
                if failure == 'callback':
                    await ready.wait()
                    raise ValueError('callback failed')
                await asyncio.Event().wait()
            finally:
                active.remove(value)

        callback = call
        if failure == 'iterator':
            # Let one callback complete so a worker advances the failing source.
            async def completes(value):
                if value == 'waiting':
                    return value
                return await call(value)
            callback = completes
        task = asyncio.create_task(hub_client._gather_bounded(source(), callback, 2))
        if failure == 'cancel':
            await ready.wait()
            task.cancel()
        with pytest.raises(asyncio.CancelledError if failure == 'cancel' else ValueError):
            await task
        assert active == set()
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(run())


def test_paced_slot_obeys_new_rate_limit_pause(monkeypatch):
    async def run():
        class Clock:
            now = 0.0

            def time(self):
                return self.now

        clock = Clock()
        gate = hub_client._RateLimitGate()
        gate._next_slot = 0.2
        sleeps = []

        async def sleep(delay):
            sleeps.append(delay)
            clock.now += delay
            if len(sleeps) == 1:
                gate.pause(1.0)

        monkeypatch.setattr(hub_client.asyncio, 'get_running_loop', lambda: clock)
        monkeypatch.setattr(hub_client.asyncio, 'sleep', sleep)
        await gate.wait()
        assert clock.now >= 1.2
        assert sleeps == pytest.approx([0.2, 1.0])
        await gate.wait()
        assert clock.now >= 1.25  # resume still spaces the next operation

    asyncio.run(run())
