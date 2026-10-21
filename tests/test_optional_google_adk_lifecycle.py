"""Optional end-to-end lifecycle check against the real Google ADK runtime."""

from __future__ import annotations

import hashlib
import json

import pytest

from unimem.client import MemoryClient
from unimem.framework_middleware import scope_from_labels
from unimem.integrations.google_adk import after_agent_callback, before_model_callback
from unimem.memory_journal import Journal
from unimem.memory_record import MemoryRecord


def _content_text(content) -> str:
    if content is None:
        return ""
    return "\n".join(part.text for part in content.parts or [] if isinstance(part.text, str))


def test_google_adk_lifecycle_injects_memory_and_captures_completion(tmp_path):
    pytest.importorskip("google.adk")
    try:
        from google.adk.agents.llm_agent import LlmAgent
        from google.adk.cli.agent_test_runner import InMemoryRunner, MockModel
        from google.genai import types
    except ImportError as exc:
        pytest.skip(f"Google ADK test runtime is unavailable: {exc}")

    root = str(tmp_path / "memory")
    memory = MemoryClient(root, agent_id="adk-agent", context={"tenant": "acme"})
    model = MockModel.create([types.Content(role="model", parts=[types.Part.from_text(text="Fixture response")])])
    model.requests = []
    agent = LlmAgent(
        name="adk_smoke_agent",
        model=model,
        instruction="Reply briefly.",
        before_model_callback=before_model_callback(memory),
        after_agent_callback=after_agent_callback(memory),
    )
    runner = InMemoryRunner(root_agent=agent)
    session = runner.session
    record_scope = {
        "tenant": "acme",
        "agent": "adk-agent",
        "user": session.user_id,
        "session": session.id,
        "labels": ["app:" + runner.app_name],
    }
    Journal(root).put(
        MemoryRecord(
            "alice-display-preference",
            "profile",
            1,
            {"text": "Use dark mode for the dashboard.", "critical": True},
            scope=record_scope,
        )
    )

    events = runner.run("What display setting do I prefer?")

    assert len(model.requests) == 1
    request_text = "\n".join(_content_text(item) for item in model.requests[0].contents)
    assert "Use dark mode for the dashboard." in request_text
    assert any(_content_text(event.content) == "Fixture response" for event in events)

    session_scope = scope_from_labels(
        memory.context
        + [
            "user:" + session.user_id,
            "session:" + session.id,
            "app:" + runner.app_name,
        ]
    )
    session_scope.setdefault("agent", memory.agent_id or "local")
    run_id = hashlib.sha256(json.dumps([session_scope, session.id], sort_keys=True).encode()).hexdigest()
    captured = Journal(root).buffer(run_id, scope=session_scope)
    assert any(
        entry["event"] == "UserMessage" and entry["payload"].get("text") == "What display setting do I prefer?"
        for entry in captured
    )
    assert any(
        entry["event"] == "SessionEnd"
        and entry["payload"].get("succeeded") is True
        and entry["payload"].get("result") == "Fixture response"
        for entry in captured
    )
