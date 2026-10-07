"""Real concurrent sessions retain evidence, scope and idempotency under load."""
from __future__ import annotations

import asyncio
from pathlib import Path

from commontrace.conversation import AsyncStore, Options, Store


def test_concurrent_agent_load_preserves_sessions_and_invalidates_recall(tmp_path: Path) -> None:
    async def drive() -> None:
        async with await AsyncStore.open(str(tmp_path), "fleet", max_pending=16, batch_size=8) as writer:
            async def agent(number: int) -> int:
                added = 0
                for turn in range(50):
                    message = {"id": f"turn-{turn}", "role": "user",
                               "text": f"Agent {number} deployment evidence number {turn}: bounded retry with jitter"}
                    result = await writer.add(f"agent-{number}", [message], extract_profile=False)
                    added += result["added"]
                    if turn % 10 == 0:
                        replay = await writer.add(f"agent-{number}", [message], extract_profile=False)
                        assert replay["added"] == 0
                return added

            assert sum(await asyncio.gather(*(agent(number) for number in range(12)))) == 600
            assert (await writer.stats())["turns"] == 600
            options = Options(embedder=None, rerank=None, sessions=("agent-7",),
                              neighbours_before=0, neighbours_after=0, summaries=False)
            result = await writer.recall("deployment bounded retry", options=options)
            assert result.turns
            with Store(str(tmp_path), "fleet") as external:
                assert set(result.turns) <= {turn.id for turn in external.session_turns("agent-7")}
                external.delete_session("agent-7")
            # A hot result must not resurrect evidence removed by another connection.
            assert not (await writer.recall("deployment bounded retry", options=options)).turns

    asyncio.run(drive())
    with Store(str(tmp_path), "fleet") as reader, Store(str(tmp_path), "other-tenant") as isolated:
        assert reader.stats()["turns"] == 550
        assert isolated.stats()["turns"] == 0
        for number in range(12):
            if number != 7:
                assert [turn.ref for turn in reader.session_turns(f"agent-{number}")] == [
                    f"turn-{turn}" for turn in range(50)
                ]
