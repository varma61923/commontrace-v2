"""Google ADK callbacks for scoped UniMem memory.

The Google packages remain optional. Import this module without ADK installed;
the ADK callback imports UniMem runtime pieces only when an agent invokes
it, while the existing model-request adapter imports ``google.genai`` only
when it has context to inject.
"""

from __future__ import annotations

import logging
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
        from unimem.client import MemoryClient
        from unimem.framework_middleware import Middleware

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
        try:
            return Middleware(scoped_memory, str(session_id), budget=budget).process_llm_request(
                callback_context, llm_request
            )
        except Exception:
            logging.getLogger(__name__).debug(
                "Google ADK memory retrieval failed; continuing without injection", exc_info=True
            )
            return None

    return callback


def after_agent_callback(memory: Any) -> Callable[[Any], None]:
    """Create a best-effort ``LlmAgent(after_agent_callback=...)`` callback.

    ADK invokes this after the agent finishes. It captures text emitted by this
    agent during the current invocation through ``Middleware.after_run``. The
    callback returns ``None`` so the agent's original response is preserved.
    """

    def callback(callback_context: Any) -> None:
        from unimem.client import MemoryClient
        from unimem.framework_middleware import Middleware

        try:
            session = _attribute(callback_context, "session")
            session_id = _attribute(session, "id")
            invocation_id = _attribute(callback_context, "invocation_id")
            user_id = _attribute(callback_context, "user_id") or _attribute(session, "user_id")
            app_name = _attribute(callback_context, "app_name") or _attribute(session, "app_name")
            scope = _base_scope(memory)
            if not session_id or not invocation_id:
                raise ValueError("Google ADK completion callback requires session and invocation IDs")
            if not user_id:
                user_id = scope.get("user")
            if not user_id:
                raise ValueError("Google ADK completion callback requires a user ID or user-scoped memory")

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
            middleware = Middleware(scoped_memory, str(session_id))
            agent_name = _attribute(callback_context, "agent_name")
            output = []
            for event in _attribute(session, "events") or ():
                if _attribute(event, "invocation_id") != invocation_id:
                    continue
                if agent_name and _attribute(event, "author") != agent_name:
                    continue
                content = _attribute(event, "content")
                if _attribute(content, "role") not in (None, "model"):
                    continue
                for part in _attribute(content, "parts") or ():
                    text = _attribute(part, "text")
                    if isinstance(text, str) and text:
                        output.append(text)

            middleware.after_run(
                {
                    "succeeded": not bool(_attribute(callback_context, "is_aborted")),
                    "result": "\n".join(output)[:20_000],
                }
            )
        except Exception:
            logging.getLogger(__name__).debug("Google ADK completion capture failed; continuing", exc_info=True)
        return None

    return callback
