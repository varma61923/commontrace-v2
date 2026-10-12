"""Google ADK before-model callback for scoped CommonTrace memory.

The Google packages remain optional. Import this module without ADK installed;
the ADK callback imports CommonTrace runtime pieces only when an agent invokes
it, while the existing model-request adapter imports ``google.genai`` only
when it has context to inject.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def _attribute(source: Any, name: str) -> Any:
    value = getattr(source, name, None)
    return value() if callable(value) else value


def _base_scope(memory: Any) -> dict[str, str]:
    scope = {}
    for label in getattr(memory, "context", ()):
        key, separator, value = label.partition(":")
        if separator:
            scope[key] = value
    return scope


def before_model_callback(memory: Any, *, budget: int = 600) -> Callable[[Any, Any], None]:
    """Create a sync callback for ``LlmAgent(before_model_callback=...)``.

    Each request uses ADK's app, user, and session identifiers in the memory
    scope. One ``MemoryClient.turn`` call captures the latest user text and
    retrieves context; its returned read-only block is appended to the model
    request. The callback returns ``None`` so ADK continues the model call.
    """

    def callback(callback_context: Any, llm_request: Any) -> None:
        from commontrace.client import MemoryClient
        from commontrace.framework_middleware import Middleware

        session = _attribute(callback_context, "session")
        session_id = _attribute(callback_context, "session_id") or _attribute(session, "id")
        user_id = _attribute(callback_context, "user_id") or _attribute(session, "user_id")
        app_name = _attribute(callback_context, "app_name") or _attribute(session, "app_name")
        scope = _base_scope(memory)
        if not session_id:
            raise ValueError("Google ADK callback requires a session_id")
        if not user_id:
            user_id = scope.get("user")
        if not user_id:
            raise ValueError("Google ADK callback requires a user_id or a user-scoped MemoryClient")

        scope["user"] = str(user_id)
        scope["session"] = str(session_id)
        if app_name:
            scope["app"] = str(app_name)

        scoped_memory = MemoryClient(
            memory.root,
            url=memory.url,
            token=memory.token,
            agent_id=memory.agent_id,
            context=scope,
        )
        return Middleware(scoped_memory, str(session_id), budget=budget).process_llm_request(
            callback_context, llm_request
        )

    return callback
