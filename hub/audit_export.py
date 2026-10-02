from __future__ import annotations

import json
from typing import Any

from commontrace import __version__

FORMATS = ("jsonl", "cef")

_HIGH = ("purge", "delete", "revoke", "disable", "unlink", "release")


def _fields(entry: Any) -> dict:
    return {
        "id": entry.id,
        "time": entry.created_at.isoformat(),
        "actor": entry.actor,
        "org_id": entry.org_id,
        "action": entry.action,
        "target_type": entry.target_type,
        "target_id": entry.target_id,
        "summary": entry.summary,
    }


def to_jsonl(entry: Any) -> str:
    return json.dumps(_fields(entry), sort_keys=True)


def _header(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|")


def _ext(value: Any) -> str:
    text = "" if value is None else str(value)
    return (text.replace("\\", "\\\\").replace("=", "\\=")
            .replace("\r", "\\r").replace("\n", "\\n"))


def severity(action: str) -> int:
    return 8 if any(word in action for word in _HIGH) else 3


def to_cef(entry: Any) -> str:
    target = f"{entry.target_type}:{entry.target_id}" if entry.target_type else ""
    extension = " ".join(f"{k}={_ext(v)}" for k, v in (
        ("rt", int(entry.created_at.timestamp() * 1000)),
        ("externalId", entry.id),
        ("suser", entry.actor),
        ("cs1Label", "org_id"), ("cs1", entry.org_id or ""),
        ("cs2Label", "target"), ("cs2", target),
        ("msg", entry.summary),
    ))
    header = "|".join(_header(v) for v in (
        "CommonTrace", "Hub", __version__, entry.action, entry.action, severity(entry.action),
    ))
    return f"CEF:0|{header}|{extension}"


def render(entry: Any, fmt: str) -> str:
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}, got {fmt!r}")
    return to_cef(entry) if fmt == "cef" else to_jsonl(entry)
