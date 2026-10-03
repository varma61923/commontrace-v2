"""Provenance evidence log for graph nodes/edges."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from commontrace import _jsonl, paths


def _provenance_file(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "graph", "provenance.jsonl")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_provenance(
    root: str,
    target_kind: str,
    target_id: str,
    source_path: str = "",
    run_id: str = "",
    detail: Any = "",
) -> dict[str, Any]:
    """Append a provenance record and return it."""
    fpath = _provenance_file(root)
    record = {
        "target_kind": str(target_kind),
        "target_id": str(target_id),
        "source_path": str(source_path or ""),
        "run_id": str(run_id or ""),
        "detail": detail if isinstance(detail, (dict, list)) else str(detail or ""),
        "created_at": _now(),
    }
    _jsonl.append_row(fpath, record)
    return record


def list_provenance(root: str, target_id: str) -> list[dict[str, Any]]:
    """Return all provenance records matching target_id (case-insensitive)."""
    fpath = _provenance_file(root)
    if not os.path.exists(fpath):
        return []
    want = str(target_id).strip().lower()
    out: list[dict[str, Any]] = []
    with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            tid = str(rec.get("target_id", "")).strip().lower()
            if tid == want:
                out.append(rec)
    out.sort(key=lambda r: str(r.get("created_at", "")))
    return out
