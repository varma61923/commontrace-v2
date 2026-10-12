import builtins
import importlib
import sys
from types import ModuleType, SimpleNamespace

from commontrace.client import MemoryClient
from commontrace.memory_journal import Journal
from commontrace.memory_record import MemoryRecord


def _install_fake_genai_types(monkeypatch):
    class Part:
        def __init__(self, *, text):
            self.text = text

    class Content:
        def __init__(self, *, role, parts):
            self.role, self.parts = role, parts

    google = ModuleType("google")
    google.__path__ = []
    genai = ModuleType("google.genai")
    genai.types = SimpleNamespace(Content=Content, Part=Part)
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    return Content, Part


def test_google_adk_adapter_import_is_lazy_and_has_no_store_side_effects(monkeypatch, tmp_path):
    module_name = "commontrace.integrations.google_adk"
    prior_module = sys.modules.pop(module_name, None)
    original_import = builtins.__import__
    before = {name for name in sys.modules if name == "google" or name.startswith("google.")}

    def reject_google(name, *args, **kwargs):
        if name == "google" or name.startswith("google."):
            raise AssertionError("optional Google packages must not be imported at module load")
        return original_import(name, *args, **kwargs)

    try:
        monkeypatch.setattr(builtins, "__import__", reject_google)
        module = importlib.import_module(module_name)
        assert callable(module.before_model_callback)
        assert {name for name in sys.modules if name == "google" or name.startswith("google.")} == before
        assert not (tmp_path / "memory").exists()
    finally:
        sys.modules.pop(module_name, None)
        if prior_module is not None:
            sys.modules[module_name] = prior_module


def test_adk_callback_saves_and_retrieves_once_in_adk_user_scope(monkeypatch, tmp_path):
    Content, Part = _install_fake_genai_types(monkeypatch)
    from commontrace.integrations.google_adk import before_model_callback

    memory = MemoryClient(str(tmp_path), agent_id="adk-agent", context={"tenant": "acme"})
    alice_scope = {
        "tenant": "acme",
        "agent": "adk-agent",
        "user": "alice",
        "session": "session-a",
        "labels": ["app:adk-app"],
    }
    Journal(str(tmp_path)).put(
        MemoryRecord(
            "alice-display-preference",
            "profile",
            1,
            {"text": "Use dark mode for the dashboard.", "critical": True},
            scope=alice_scope,
        )
    )

    original_turn = MemoryClient.turn
    calls = []

    def tracked_turn(client, session_id, query, **kwargs):
        result = original_turn(client, session_id, query, **kwargs)
        calls.append((client.context, session_id, query, kwargs, result))
        return result

    monkeypatch.setattr(MemoryClient, "turn", tracked_turn)
    callback = before_model_callback(memory)
    request = SimpleNamespace(
        contents=[Content(role="user", parts=[Part(text="What display setting do I prefer?")])]
    )
    context = SimpleNamespace(
        user_id="alice",
        app_name="adk-app",
        session=SimpleNamespace(id="session-a", user_id="alice", app_name="adk-app"),
    )

    assert callback(context, request) is None
    assert len(calls) == 1
    routed_context, session_id, query, kwargs, result = calls[0]
    assert {"tenant:acme", "agent:adk-agent", "user:alice", "app:adk-app", "session:session-a"} <= set(
        routed_context
    )
    assert session_id == "session-a"
    assert query == "What display setting do I prefer?"
    assert kwargs["text"] == query
    assert any(
        entry["event"] == "UserMessage" and entry["payload"].get("text") == query
        for entry in result["buffer"]
    )
    assert "Use dark mode for the dashboard." in result["context"]
    assert len(request.contents) == 2
    assert request.contents[-1].role == "user"
    assert request.contents[-1].parts[0].text == result["context"]

    bob_request = SimpleNamespace(
        contents=[Content(role="user", parts=[Part(text="What display setting do I prefer?")])]
    )
    bob_context = SimpleNamespace(
        user_id="bob",
        app_name="adk-app",
        session=SimpleNamespace(id="session-b", user_id="bob", app_name="adk-app"),
    )
    assert callback(bob_context, bob_request) is None
    assert len(calls) == 2
    assert "user:bob" in calls[-1][0]
    assert "session:session-b" in calls[-1][0]
    assert "Use dark mode for the dashboard." not in calls[-1][-1]["context"]
