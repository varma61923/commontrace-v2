"""Batch wire boundaries and replay policy, without model or network access."""

import asyncio

import pytest

from unimem.hub_client import HubSession, HubToolError


class RecordingSession(HubSession):
    def __init__(self, response=None):
        super().__init__("https://example.test/mcp", "unused", 10, 2)
        self.calls = []
        self.response = response

    async def call(self, name, arguments, *, retry_transport=True):
        self.calls.append((name, arguments, retry_transport))
        if self.response is not None:
            return self.response
        return {"results": [{"item": item} for item in next(iter(arguments.values()))]}


def test_batch_chunks_generator_and_preserves_order():
    session = RecordingSession()
    result = asyncio.run(session.get_traces_batch(str(i) for i in range(61)))
    assert [row["item"] for row in result] == [str(i) for i in range(61)]
    assert [len(args["ids"]) for _, args, _ in session.calls] == [25, 25, 11]
    assert all(retry for _, _, retry in session.calls)


@pytest.mark.parametrize("keyed", [True, False])
def test_contribution_replay_requires_every_idempotency_key(keyed):
    session = RecordingSession()
    traces = [{"idempotency_key": "first"}, {"idempotency_key": "second"} if keyed else {}]
    asyncio.run(session.contribute_traces_batch(traces))
    assert session.calls[0][2] is keyed


def test_delete_never_replays_ambiguous_transport_and_empty_batch_skips_wire():
    session = RecordingSession()
    assert asyncio.run(session.delete_traces_batch([])) == []
    assert session.calls == []
    asyncio.run(session.delete_traces_batch(["first"]))
    assert session.calls[0][2] is False


@pytest.mark.parametrize("response", [{}, {"results": []}, {"results": ["invalid"]}])
def test_invalid_envelopes_are_not_silently_accepted(response):
    with pytest.raises(HubToolError, match="invalid batch result"):
        asyncio.run(RecordingSession(response).get_traces_batch(["first"]))


def test_native_contribution_fields_and_legacy_convenience_map_to_real_hub_tool():
    session = RecordingSession(response={"id": "created"})
    asyncio.run(session.contribute_trace("legacy evidence", rationale="legacy solution", tags=["retry"]))
    name, arguments, retry = session.calls[0]
    assert name == "contribute_trace" and retry
    assert arguments == {
        "title": "legacy evidence",
        "context_text": "legacy evidence",
        "solution_text": "legacy solution",
        "agent_type": "agent",
        "tags": ["retry"],
        "idempotency_key": arguments["idempotency_key"],
    }
    assert arguments["idempotency_key"].startswith("trace:")
    asyncio.run(
        session.contribute_trace(
            title="native title",
            context_text="native context",
            solution_text="native solution",
            idempotency_key="stable",
        )
    )
    assert session.calls[1][1] == {
        "title": "native title",
        "context_text": "native context",
        "solution_text": "native solution",
        "agent_type": "agent",
        "idempotency_key": "stable",
    }


def test_subject_purge_uses_the_registered_tool_and_non_idempotent_comments_do_not_replay():
    session = RecordingSession(response={})
    asyncio.run(session.purge_subject_traces("subject-one"))
    asyncio.run(session.add_comment("trace-one", "review"))
    assert session.calls == [
        ("purge_traces_by_subject", {"subject_id": "subject-one"}, False),
        ("add_comment", {"trace_id": "trace-one", "body": "review"}, False),
    ]


def test_typed_helper_arguments_match_the_registered_mcp_schemas():
    schema = pytest.importorskip("jsonschema")
    server_module = pytest.importorskip("hub.server", exc_type=ImportError)
    from hub.config import HubConfig

    server = server_module.build_mcp_server(HubConfig(database_url="postgresql+asyncpg://localhost/audit"), None, None)
    tools = {tool.name: tool.input_schema for tool in asyncio.run(server.list_tools())}
    session = RecordingSession(response={})
    asyncio.run(session.contribute_trace("evidence", rationale="solution"))
    asyncio.run(session.contribute_trace(title="native", context_text="context", solution_text="solution"))
    asyncio.run(session.purge_subject_traces("subject-one"))
    asyncio.run(session.add_comment("trace-one", "review"))
    for name, arguments, _retry in session.calls:
        assert name in tools
        schema.validate(arguments, tools[name])
        assert set(arguments) <= set(tools[name]["properties"])
