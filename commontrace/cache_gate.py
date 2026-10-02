"""Turns that cannot be worth a retrieval."""

from __future__ import annotations

import re

_TRIVIAL_WORDS = (
    r"y|n|yes|no|yep|nope|yeah|nah|ok|okay|k|kk|sure|fine|"
    r"thanks|thank you|ty|cheers|"
    r"hi|hey|hello|yo|morning|good morning|"
    r"continue|carry on|go ahead|go on|proceed|do it|"
    r"got it|understood|makes sense|sounds good|agreed|"
    r"cool|nice|great|perfect|awesome|good|done|next|lgtm|ship it|"
    r"please|please do|now|again"
)

_TRAILING = r"""[\s!?.:;,'"~…()\[\]{}<>*&^%$#@+=`\-_/\\|]*"""

TRIVIAL_PROMPT_RE = re.compile(
    rf"^(?:{_TRIVIAL_WORDS}){_TRAILING}$",
    re.IGNORECASE,
)


def is_trivial_prompt(text: str | None) -> bool:
    """Is this turn incapable of matching a lesson?"""
    if text is None:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    return bool(TRIVIAL_PROMPT_RE.match(stripped))
