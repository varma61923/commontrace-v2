"""Microsoft Agent Framework context provider for scoped UniMem memory.

The Microsoft SDK is optional. Importing this module remains safe without it;
constructing the provider requires the current ``agent_framework`` package.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from agent_framework import ContextProvider as _ContextProvider
else:
    _ContextProvider = object

_SCOPE_DIMENSIONS = {"tenant", "user", "agent", "app", "project", "session", "repo"}
_TURN_STATE_KEY = "unimem_turn"
_MAX_CAPTURE_CHARS = 20_000
# Agent Framework 1.21 stamps every iteration with one shared object and removes
# it before the turn-scoped after_run. Keep only its integer identity in durable
# provider state, which the SDK requires to remain JSON serializable.
_LOOP_OPTION_KEY = "_agent_loop_iteration"


def _base_scope(memory: Any) -> dict[str, str]:
    """Recover the validated scope labels already configured on MemoryClient."""
    labels = getattr(memory, "context", ())
    if isinstance(labels, Mapping):
        return {str(key): str(value) for key, value in labels.items() if key in _SCOPE_DIMENSIONS and value}

    scope: dict[str, str] = {}
    for label in labels:
        key, separator, value = str(label).partition(":")
        if separator and key in _SCOPE_DIMENSIONS and value:
            if key in scope and scope[key] != value:
                raise ValueError(f"ambiguous UniMem memory scope dimension: {key}")
            scope[key] = value
    return scope


def _text_for_role(message: Any, expected_role: str) -> str:
    if isinstance(message, Mapping):
        role = message.get("role")
        value = message.get("text", message.get("content", message.get("contents", "")))
    else:
        role = getattr(message, "role", None)
        value = getattr(message, "text", None)
        if not isinstance(value, str):
            value = getattr(message, "content", getattr(message, "contents", ""))

    if role != expected_role:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            else:
                item_text = getattr(item, "text", None)
                if isinstance(item_text, str):
                    parts.append(item_text)
        return "\n".join(part.strip() for part in parts if part and part.strip())
    return ""


def _latest_user_text(messages: Any) -> str:
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes, bytearray)):
        return ""
    for message in reversed(messages):
        text = _text_for_role(message, "user")
        if text:
            return text
    return ""


def _assistant_text(context: Any) -> str:
    response = getattr(context, "response", None)
    messages = getattr(response, "messages", None)
    if not isinstance(messages, Sequence):
        return ""
    parts = [text for message in messages if (text := _text_for_role(message, "assistant"))]
    return "\n".join(parts)[:_MAX_CAPTURE_CHARS]


def _session_id(context: Any, session: Any) -> str:
    value = getattr(context, "session_id", None) or getattr(session, "session_id", None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Microsoft Agent Framework memory provider requires a session_id")
    return value.strip()


def _query_key(session_id: str, query: str) -> str:
    return hashlib.sha256(f"{session_id}\0{query}".encode("utf-8")).hexdigest()


# Mypy cannot resolve an optional package that is imported only at construction.
class UniMemContextProvider(_ContextProvider):  # type: ignore[misc]
    """Inject scoped memory before an agent run and capture its final response.

    Supply a user scope either through ``user_id`` or through the provided
    ``MemoryClient``. For a shared provider, ``context.options['user_id']`` is
    used when neither is configured. The Agent Framework session ID selects
    the UniMem conversation buffer; it is not added to the memory scope,
    so user facts remain available across sessions.
    """

    # Agent Framework defers this provider's after hook until an agent loop
    # completes, avoiding one SessionEnd capture for each tool iteration.
    after_run_once_per_turn = True

    def __new__(cls, *args: Any, **kwargs: Any) -> UniMemContextProvider:
        """Load the optional SDK only when a provider instance is requested."""
        if cls is UniMemContextProvider:
            try:
                from agent_framework import ContextProvider
            except ModuleNotFoundError as exc:
                if exc.name != "agent_framework":
                    raise
                raise ImportError(
                    "UniMemContextProvider requires the optional Microsoft Agent Framework package "
                    "'agent-framework-core'"
                ) from exc
            cls = type(
                "_MicrosoftAgentFrameworkUniMemContextProvider",
                (UniMemContextProvider, ContextProvider),
                {"__module__": __name__},
            )
        return cast(UniMemContextProvider, super().__new__(cls))

    def __init__(
        self,
        memory: Any,
        *,
        user_id: str | None = None,
        budget: int = 600,
        source_id: str = "unimem_memory",
    ) -> None:
        super().__init__(source_id=source_id)
        self.memory = memory
        self.user_id = user_id
        self.budget = budget

    def _scoped_memory(self, context: Any) -> Any:
        from unimem.client import MemoryClient

        scope = _base_scope(self.memory)
        configured_user = self.user_id or scope.get("user")
        if configured_user is None:
            options = getattr(context, "options", None)
            if isinstance(options, Mapping):
                configured_user = options.get("user_id")
        if not isinstance(configured_user, str) or not configured_user.strip():
            raise ValueError(
                "Microsoft Agent Framework memory provider requires user_id, a user-scoped MemoryClient, "
                "or context.options['user_id']"
            )
        scope["user"] = configured_user.strip()

        return MemoryClient(
            self.memory.root,
            url=self.memory.url,
            token=self.memory.token,
            agent_id=self.memory.agent_id,
            context=scope,
        )

    @staticmethod
    def _middleware(memory: Any, session_id: str, budget: int) -> Any:
        from unimem.framework_middleware import Middleware

        return Middleware(memory, session_id, budget=budget)

    async def before_run(
        self,
        *,
        agent: Any,
        session: Any,
        context: Any,
        state: dict[str, Any],
    ) -> None:
        """Capture the latest user text and inject retrieved memory, fail-open."""
        user_text = _latest_user_text(getattr(context, "input_messages", None))
        options = getattr(context, "options", None)
        loop_token = options.get(_LOOP_OPTION_KEY) if isinstance(options, Mapping) else None
        if not user_text and loop_token is None:
            state.pop(_TURN_STATE_KEY, None)
            return
        session_id = _session_id(context, session)
        scoped_memory = self._scoped_memory(context)
        loop_key = id(loop_token) if loop_token is not None else None
        previous = state.get(_TURN_STATE_KEY)
        if (
            loop_key is not None
            and isinstance(previous, dict)
            and previous.get("loop_key") == loop_key
            and previous.get("session_id") == session_id
            and previous.get("scope") == scoped_memory.context
        ):
            # The framework's continuation/progress messages are generated by
            # the loop, not the user. Reuse context without another capture or
            # retrieval, and retain the original query for deferred after_run.
            memory_context = previous.get("memory_context")
            if isinstance(memory_context, str) and memory_context:
                context.extend_instructions(self.source_id, memory_context)
            return

        state.pop(_TURN_STATE_KEY, None)
        if not user_text:
            return
        marker = {
            "session_id": session_id,
            "query_key": _query_key(session_id, user_text),
            "scope": scoped_memory.context,
            "captured_user": False,
            "loop_key": loop_key,
        }
        state[_TURN_STATE_KEY] = marker

        try:
            middleware = self._middleware(scoped_memory, session_id, self.budget)
            result = await asyncio.to_thread(middleware.before_run, user_text, text=user_text)
            marker["captured_user"] = True
            memory_context = result.get("context", "")
            if isinstance(memory_context, str) and memory_context:
                marker["memory_context"] = memory_context
                context.extend_instructions(self.source_id, memory_context)
        except Exception:
            logging.getLogger(__name__).debug(
                "Microsoft Agent Framework memory retrieval failed; continuing without injection", exc_info=True
            )

    async def after_run(
        self,
        *,
        agent: Any,
        session: Any,
        context: Any,
        state: dict[str, Any],
    ) -> None:
        """Capture the completed assistant response without changing it."""
        marker = state.pop(_TURN_STATE_KEY, None)
        if not isinstance(marker, dict) or marker.get("captured_user") is not True:
            return

        try:
            session_id = _session_id(context, session)
            user_text = _latest_user_text(getattr(context, "input_messages", None))
            if marker.get("session_id") != session_id or marker.get("query_key") != _query_key(session_id, user_text):
                return
            assistant_text = _assistant_text(context)
            if not assistant_text:
                return

            scoped_memory = self._scoped_memory(context)
            if marker.get("scope") != scoped_memory.context:
                return
            middleware = self._middleware(scoped_memory, session_id, self.budget)
            await asyncio.to_thread(
                middleware.after_run,
                {"succeeded": True, "result": assistant_text},
            )
        except Exception:
            logging.getLogger(__name__).debug(
                "Microsoft Agent Framework response capture failed; preserving the agent result", exc_info=True
            )
