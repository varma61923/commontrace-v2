"""Batch wire boundaries and replay policy, without model or network access."""
import asyncio

import pytest

from commontrace.hub_client import HubSession, HubToolError


class RecordingSession(HubSession):
    def __init__(self, response=None):
        super().__init__('https://example.test/mcp', 'unused', 10, 2)
        self.calls = []
        self.response = response

    async def call(self, name, arguments, *, retry_transport=True):
        self.calls.append((name, arguments, retry_transport))
        if self.response is not None:
            return self.response
        return {'results': [{'item': item} for item in next(iter(arguments.values()))]}


def test_batch_chunks_generator_and_preserves_order():
    session = RecordingSession()
    result = asyncio.run(session.get_traces_batch(str(i) for i in range(61)))
    assert [row['item'] for row in result] == [str(i) for i in range(61)]
    assert [len(args['ids']) for _, args, _ in session.calls] == [25, 25, 11]
    assert all(retry for _, _, retry in session.calls)


@pytest.mark.parametrize('keyed', [True, False])
def test_contribution_replay_requires_every_idempotency_key(keyed):
    session = RecordingSession()
    traces = [{'idempotency_key': 'first'}, {'idempotency_key': 'second'} if keyed else {}]
    asyncio.run(session.contribute_traces_batch(traces))
    assert session.calls[0][2] is keyed


def test_delete_never_replays_ambiguous_transport_and_empty_batch_skips_wire():
    session = RecordingSession()
    assert asyncio.run(session.delete_traces_batch([])) == []
    assert session.calls == []
    asyncio.run(session.delete_traces_batch(['first']))
    assert session.calls[0][2] is False


@pytest.mark.parametrize('response', [{}, {'results': []}, {'results': ['invalid']}])
def test_invalid_envelopes_are_not_silently_accepted(response):
    with pytest.raises(HubToolError, match='invalid batch result'):
        asyncio.run(RecordingSession(response).get_traces_batch(['first']))
