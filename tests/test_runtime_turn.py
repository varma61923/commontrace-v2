from commontrace.client import MemoryClient
from commontrace.framework_middleware import Middleware
from commontrace.memory_journal import Journal


def test_save_retrieve_and_read_after_write_scope(tmp_path):
    client = MemoryClient(str(tmp_path), agent_id="a", context={"user": "u"})
    response = client.turn("run", "What did I prefer?", text="I prefer pytest for validation.")
    assert response["buffer"]
    assert response["profile"]["dynamic"]
    other = MemoryClient(str(tmp_path), agent_id="b", context={"user": "u"})
    assert "validation" not in str(other.turn("run", "pytest")["buffer"])
    assert not other.turn("run", "pytest")["profile"]["dynamic"]


def test_middleware_native_entry_points_and_outcome(tmp_path):
    middleware = Middleware(MemoryClient(str(tmp_path), agent_id="a"), "r")
    state = middleware.langgraph_pre_model({"messages": [{"role": "user", "content": "hello"}]})
    assert "llm_input_messages" in state
    assert middleware.pydantic_history_processor(None, [{"role": "user", "content": "hello"}])
    middleware.after_run({"succeeded": False, "error": "AssertionError"})
    assert Journal(str(tmp_path)).buffer(middleware.run_key, scope=middleware.scope)


def test_record_sdk_rejects_foreign_scope_and_erasure(tmp_path):
    import pytest

    from commontrace.memory_record import MemoryRecord

    owner = MemoryClient(str(tmp_path), agent_id="a")
    owner.put_record(MemoryRecord("private", "fact", 1, {"text": "parser secret"}, scope={"agent": "a"}))
    other = MemoryClient(str(tmp_path), agent_id="b")
    assert other.search_records("parser") == []
    with pytest.raises(PermissionError):
        other.forget_record("private", reason="foreign request")
    with pytest.raises(PermissionError):
        other.put_record(MemoryRecord("forged", "fact", 1, {}, scope={"agent": "a"}))
    assert owner.forget_record("private", reason="owner request")["deleted"] == ["private"]


def test_actual_openai_and_microsoft_native_callbacks(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import pytest

    pytest.importorskip("agents")
    af = pytest.importorskip("agent_framework")
    from agents.run_config import ModelInputData

    middleware = Middleware(MemoryClient(str(tmp_path), agent_id="a"), "native")
    monkeypatch.setattr(
        middleware, "before_run", lambda *a, **kw: {"context": "<memory_update>read only</memory_update>"}
    )
    input_data = ModelInputData(input=[{"role": "user", "content": "task"}], instructions="stable")
    output = middleware.openai_call_model_input_filter(SimpleNamespace(model_data=input_data))
    assert output.instructions == "stable"
    assert len(output.input) == 2
    output2 = middleware.openai_call_model_input_filter(SimpleNamespace(model_data=output))
    assert len(output2.input) == 2
    context = af.AgentContext(agent=SimpleNamespace(), messages=[af.Message("user", text="task")])
    calls = []

    async def next(ctx):
        calls.append(ctx)

    asyncio.run(middleware.microsoft_agent_middleware(context, next))
    assert len(context.messages) == 2 and len(calls) == 1
    asyncio.run(middleware.microsoft_agent_middleware(context, next))
    assert len(context.messages) == 2 and len(calls) == 2


def test_agent_cannot_erase_owner_shared_record(tmp_path):
    import pytest

    from commontrace.memory_record import MemoryRecord

    Journal(str(tmp_path)).put(MemoryRecord("shared", "fact", 1, {"text": "public team reference"}))
    agent = MemoryClient(str(tmp_path), agent_id="a")
    assert agent.search_records("public")
    with pytest.raises(PermissionError):
        agent.forget_record("shared", reason="not the owner")


def test_undated_custom_profile_is_readable(tmp_path):
    from commontrace.memory_record import MemoryRecord

    memory = MemoryClient(str(tmp_path), agent_id="a")
    memory.put_record(MemoryRecord("custom", "profile", 1, {"text": "Use terse answers", "bucket": "custom"}))
    assert memory.turn("s", "preferences")["profile"]["custom"][0]["text"] == "Use terse answers"
