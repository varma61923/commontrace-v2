"""What a lesson SAID -- content identity for the thing an experiment measured."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

INJECTED_FIELDS = (
    "description",
    "domain",
    "tags",
    "importance",
    "applies_when",
    "do_not_apply_when",
)

REVISION_LENGTH = 12

_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANK_RUN = re.compile(r"\n{3,}")


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAILING_WS.sub("", text)
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip()


def _canonical(fm: dict, body: str) -> str:
    payload: dict[str, Any] = {}
    for key in INJECTED_FIELDS:
        value = fm.get(key)
        if isinstance(value, list):
            payload[key] = sorted(str(v) for v in value)
        elif isinstance(value, str):
            payload[key] = _normalize(value)
        else:
            payload[key] = value
    payload["_body"] = _normalize(body or "")
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def revision_of(fm: dict, body: str = "") -> str:
    """Content identity for one lesson, as a short hex digest."""
    digest = hashlib.sha256(_canonical(fm, body).encode("utf-8")).hexdigest()
    return digest[:REVISION_LENGTH]


def revision_of_trace(
    title: str, context_text: str, solution_text: str, tags: list[str] | None = None
) -> str:
    """Content identity for a Hub trace, under the same rules as a lesson."""
    payload = {
        "title": _normalize(title or ""),
        "context_text": _normalize(context_text or ""),
        "solution_text": _normalize(solution_text or ""),
        "tags": sorted(str(t) for t in (tags or [])),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:REVISION_LENGTH]
