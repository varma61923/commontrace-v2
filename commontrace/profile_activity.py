"""Scoped recent queries/action checks. Activity is descriptive, never action authority."""
from __future__ import annotations

import json
import os
import stat
import uuid
from datetime import datetime, timezone

from commontrace import _jsonl, memory_authority, memory_guard, paths


def record(root: str, kind: str, text: str, context: list[str]) -> None:
    if kind not in ("query", "action_check") or not isinstance(text, str):
        raise ValueError("unsupported activity")
    path = os.path.join(paths.memory_dir(root), "activity.jsonl")
    paths.enforce_boundary(root, path)
    paths.safe_prepare_output_path(path)
    clean = memory_guard.sanitize_metadata({"text": text[:2000]}, pii=memory_guard.privacy_redaction_enabled())[0]
    row = {"id": uuid.uuid4().hex, "kind": "activity", "activity_type": kind, "text": clean["text"],
           "scopes": sorted(set(context)), "recorded_at": datetime.now(timezone.utc).isoformat()}
    row["origin"] = memory_authority.bind(root, row)
    _jsonl.append_row(path, row)


def recent(root: str, context: list[str], *, limit: int = 10) -> list[dict]:
    from commontrace.memory_control import matches

    if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 100:
        raise ValueError("activity limit must be in 0..100")
    if not limit:
        return []
    path = os.path.join(paths.memory_dir(root), "activity.jsonl")
    paths.enforce_boundary(root, path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return []
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("activity log must be a regular file")
        offset = max(0, info.st_size-256*1024)
        stream.seek(offset)
        if offset:
            stream.readline()
        lines = stream.read(256*1024).splitlines()
    result = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (not isinstance(row, dict) or row.get("kind") != "activity" or not matches(row.get("scopes", []), context)
                or memory_authority.lineage_blocked(root, row.get("id", ""))
                or not memory_authority.verify(root, row.get("origin", {}),
                                               {k: v for k, v in row.items() if k != "origin"})):
            continue
        result.append({k: row[k] for k in ("id", "activity_type", "text", "recorded_at")})
        if len(result) >= limit:
            break
    return result
