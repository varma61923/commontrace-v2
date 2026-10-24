from __future__ import annotations

import json
from typing import Any

from hub.audit import safe_summary
from unimem import __version__

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
        "summary": safe_summary(entry.action, entry.summary),
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
        ("msg", safe_summary(entry.action, entry.summary)),
    ))
    header = "|".join(_header(v) for v in (
        "UniMem", "Hub", __version__, entry.action, entry.action, severity(entry.action),
    ))
    return f"CEF:0|{header}|{extension}"


def render(entry: Any, fmt: str) -> str:
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}, got {fmt!r}")
    return to_cef(entry) if fmt == "cef" else to_jsonl(entry)


def verify_incident_pack(bundle: dict, principals: dict) -> bool:
    """Verify exported bytes offline against independently pinned signer keys."""
    from unimem import frontier_protocol as fp
    from unimem import origin, transparency_log

    try:
        manifest, anchor = bundle["manifest"], bundle["anchor"]
        head = anchor["head"]["record"]
        return (origin.verify(manifest, principals) and origin.verify(anchor["head"], principals)
                and origin.verify(anchor["statement"], principals)
                and anchor["statement"]["record"]["statement"]["manifest_digest"] == fp.digest(manifest)
                and transparency_log.verify_inclusion(transparency_log.leaf_hash(anchor["statement"]),
                    anchor["leaf"], head["tree_size"], anchor["proof"], head["root_hash"])
                and set(manifest["record"]["files"]) == set(bundle["files"])
                and all(fp.digest(bundle["files"][name]) == digest
                        for name, digest in manifest["record"]["files"].items()))
    except (ValueError, KeyError, TypeError):
        return False
