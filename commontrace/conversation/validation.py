"""Strict message shapes at untrusted conversation transport boundaries.

The embedded Store keeps its historical coercions for trusted importers. HTTP
and MCP clients must supply textual fields as strings rather than allowing
containers to become accidental knowledge through their Python repr.
"""
from __future__ import annotations

from commontrace.conversation.store import ConversationError

_TEXT_FIELDS = ("text", "content", "role", "speaker", "name")
MAX_MESSAGES = 1000


def validate_messages(messages: object) -> None:
    """Validate without copying, changing, or consuming the caller's messages.

    Missing and null optional text fields retain legacy empty-message behavior.
    Message IDs remain unconstrained here: numeric IDs are supported by the
    store's idempotent capture contract. The transport bounds encoded body size;
    the Store separately checks message lengths and timestamp validity.
    """
    if not isinstance(messages, list):
        raise ConversationError("messages must be a list of objects with text (or content)")
    if len(messages) > MAX_MESSAGES:
        raise ConversationError(f"at most {MAX_MESSAGES} messages per request")
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ConversationError("messages must be a list of objects with text (or content)")
        for name in _TEXT_FIELDS:
            value = message.get(name)
            if value is not None and not isinstance(value, str):
                raise ConversationError(f"messages[{index}].{name} must be a string or null")
