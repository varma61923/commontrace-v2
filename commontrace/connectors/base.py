"""Connector framework: resumable syncs from external sources into governed memory."""
from __future__ import annotations

import base64
import json
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from commontrace import _jsonl, paths
from commontrace.ingest import (
    Chunk,
    IngestionResult,
    _chunk_markdown_text,
    _screened_statement,
    categorize_heading,
)

CONNECTORS_DIRNAME = "connectors"
STATE_FILENAME = "state.json"


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _state_file(root: str) -> str:
    return os.path.join(paths.memory_dir(root), CONNECTORS_DIRNAME, STATE_FILENAME)


def load_connector_states(root: str) -> dict[str, Any]:
    try:
        with open(_state_file(root), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_connector_state(root: str, key: str) -> dict[str, Any]:
    entry = load_connector_states(root).get(key, {})
    return dict(entry) if isinstance(entry, dict) else {}


def save_connector_state(root: str, key: str, state_token: str, cursor: dict[str, Any]) -> dict[str, Any]:
    entry = {
        "state_token": state_token,
        "cursor": cursor,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path = _state_file(root)
    with _jsonl.locked(path):
        states = load_connector_states(root)
        states[key] = entry
        _jsonl.write_json(path, states)
    return entry


def encode_state_token(cursor: dict[str, Any]) -> str:
    raw = json.dumps(cursor or {}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_state_token(token: str | None) -> dict[str, Any]:
    if not token:
        return {}
    try:
        padded = str(token).strip()
        padded += "=" * (-len(padded) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return dict(data) if isinstance(data, dict) else {}


def resolve_cursor(root: str, key: str, state_token: str | None) -> dict[str, Any]:
    """An explicit token wins; otherwise the persisted cursor for *key*."""
    cursor = decode_state_token(state_token)
    if cursor:
        return cursor
    entry = load_connector_state(root, key)
    cursor = decode_state_token(entry.get("state_token"))
    if cursor:
        return cursor
    stored = entry.get("cursor")
    return dict(stored) if isinstance(stored, dict) else {}


def chunk_markdown_text(text: str, source_path: str) -> list[Chunk]:
    return _chunk_markdown_text(text, source_path, os.path.basename(source_path) or source_path)


def record_chunks(
    root: str,
    chunks: list[Chunk],
    *,
    source_id: str,
    scope: str,
    run_id: str,
    connector: str,
    result: IngestionResult,
) -> None:
    """Write *chunks* as facts attributed to *source_id*, retiring that source's stale facts."""
    from commontrace import hierarchical, provenance

    items: list[dict[str, Any]] = []
    for chunk in chunks:
        statement = _screened_statement(f"{chunk.breadcrumb}: {chunk.content[:200]}", result)
        if statement:
            items.append({
                "statement": statement, "category": categorize_heading(chunk.breadcrumb),
                "scopes": [scope] if scope else [], "confidence": 0.7, "source_trace_id": source_id,
            })
    try:
        written = hierarchical.add_facts(root, items)
        result.facts_written += len(written)
        hierarchical.retire_source(root, source_id, keep={f.id for f, _ in written})
    except (OSError, ValueError) as exc:
        result.errors.append(f"fact error: {exc}")
    for chunk in chunks:
        try:
            provenance.append_provenance(
                root, target_kind="chunk", target_id=chunk.chunk_id, source_path=chunk.source_path,
                run_id=run_id,
                detail={"connector": connector, "breadcrumb": chunk.breadcrumb, "chunk_type": chunk.chunk_type},
            )
        except OSError as exc:
            result.errors.append(f"provenance error: {exc}")


@dataclass
class SyncResult:
    """Outcome of one connector sync pass."""
    connector: str
    chunks: list[Chunk] = field(default_factory=list)
    result: IngestionResult | None = None
    new_state_token: str = ""
    run_id: str = ""
    dry_run: bool = False
    pending: int = 0

    def to_dict(self) -> dict[str, Any]:
        base = self.result.to_dict() if self.result is not None else {}
        base.update({
            "connector": self.connector,
            "chunks": len(self.chunks),
            "new_state_token": self.new_state_token,
            "run_id": self.run_id,
            "dry_run": self.dry_run,
            "pending": self.pending,
        })
        return base


class Connector(ABC):
    """A source that syncs new content since its last state token."""

    name = "base"

    @abstractmethod
    def sync(
        self,
        root: str,
        state_token: str | None = None,
        *,
        scope: str = "",
        run_id: str = "",
        dry_run: bool = False,
        **kwargs: Any,
    ) -> SyncResult:
        """Sync content changed since *state_token* into the store at *root*."""
