"""Batch tools retain the single-tool scope, privacy and idempotency policies."""
import json
import uuid

import pytest

from hub import auth, scopes
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization
from hub.server import build_mcp_server


def payload(result):
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_batch_scopes_and_per_item_write_privacy(config, session_factory):
    server = build_mcp_server(config, session_factory, make_rate_limiter(config))
    assert server.commontrace_tool_scopes['get_traces_batch'] == scopes.SCOPE_READ
    assert server.commontrace_tool_scopes['contribute_traces_batch'] == scopes.SCOPE_WRITE
    assert server.commontrace_tool_scopes['delete_traces_batch'] == scopes.SCOPE_ADMIN
    async with session_scope(session_factory) as session:
        organization = Organization(name='batch-org')
        session.add(organization)
        await session.flush()
        org_id = str(organization.id)
    org = auth.current_org_id.set(org_id)
    grant = auth.current_scopes.set(('read', 'write', 'admin'))
    try:
        item = {'title': 'Batch item', 'context_text': 'Batch context', 'solution_text': 'Batch solution',
                'idempotency_key': 'unique-batch-item'}
        result = payload(await server.call_tool('contribute_traces_batch', {'traces': [item, item]}))
        assert result['results'][0]['id'] == result['results'][1]['id']
        trace_id = result['results'][0]['id']
        read = payload(await server.call_tool('get_traces_batch', {'ids': [trace_id, 'missing']}))
        assert read['results'][0]['id'] == trace_id
        assert read['results'][1]['error'] == 'not_found'
        other = auth.current_org_id.set(str(uuid.uuid4()))
        try:
            read = payload(await server.call_tool('get_traces_batch', {'ids': [trace_id]}))
            assert read['results'][0]['error'] == 'not_found'
        finally:
            auth.current_org_id.reset(other)
        narrow = auth.current_scopes.set(('read',))
        try:
            result = payload(await server.call_tool('contribute_traces_batch', {'traces': [item]}))
            assert result['error'] == 'forbidden'
            assert payload(await server.call_tool('delete_traces_batch', {'ids': [trace_id]}))['error'] == 'forbidden'
        finally:
            auth.current_scopes.reset(narrow)
        deleted = payload(await server.call_tool('delete_traces_batch', {'ids': [trace_id]}))
        assert deleted['results'][0]['deleted'] is True
    finally:
        auth.current_scopes.reset(grant)
        auth.current_org_id.reset(org)
