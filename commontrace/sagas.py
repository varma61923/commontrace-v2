"""Sagas: Ordered incident and migration narratives with watermarked running briefs.

Adapted from Graphiti & Zep Saga architectures (#5 M):
- Group long-running multi-session events (outages, incident responses, migrations,
  refactors) into a coherent, ordered chronological narrative.
- Maintains a watermarked running brief so agents can catch up on multi-day
  narratives without reprocessing thousands of raw turns.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from commontrace import _jsonl, paths

SAGA_STATUSES = ("active", "resolved", "archived")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sagas_dir(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "sagas")


def _saga_file(root: str, saga_id: str) -> str:
    clean_id = _sanitize_saga_id(saga_id)
    return os.path.join(_sagas_dir(root), f"{clean_id}.json")


def _sagas_index_file(root: str) -> str:
    return os.path.join(_sagas_dir(root), "index.jsonl")


def _lock_file(root: str) -> str:
    return os.path.join(_sagas_dir(root), "sagas")


def _sanitize_saga_id(saga_id: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(saga_id or "").strip().lower())
    if not clean.strip("_-"):
        raise ValueError("Saga ID must contain at least one letter or digit")
    if len(clean) > 80:
        raise ValueError("Saga ID cannot exceed 80 characters")
    return clean


class SagaError(RuntimeError):
    """Error raised on invalid saga operations."""


@dataclass
class SagaEvent:
    id: str
    timestamp: str
    title: str
    description: str = ""
    actor: str = "agent"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Saga:
    id: str
    title: str
    status: str = "active"
    tags: list[str] = field(default_factory=list)
    watermarked_running_brief: str = ""
    watermark: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    events: list[SagaEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["events"] = [e.to_dict() if isinstance(e, SagaEvent) else e for e in self.events]
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Saga:
        raw_events = data.get("events", [])
        events = [
            SagaEvent(**e) if isinstance(e, dict) else e
            for e in raw_events
        ]
        return cls(
            id=data["id"],
            title=data.get("title", data["id"]),
            status=data.get("status", "active"),
            tags=list(data.get("tags", [])),
            watermarked_running_brief=data.get("watermarked_running_brief", ""),
            watermark=data.get("watermark", ""),
            created_at=data.get("created_at", _now()),
            updated_at=data.get("updated_at", _now()),
            events=events,
        )


def create_saga(
    root: str,
    saga_id: str,
    title: str,
    tags: list[str] | None = None,
    brief: str = "",
    status: str = "active",
) -> Saga:
    """Create a new saga narrative with initial brief and tags."""
    clean_id = _sanitize_saga_id(saga_id)
    if status not in SAGA_STATUSES:
        raise SagaError(f"Invalid status {status!r}; choose from {SAGA_STATUSES}")

    os.makedirs(_sagas_dir(root), exist_ok=True)
    saga_path = _saga_file(root, clean_id)

    with _jsonl.locked(_lock_file(root)):
        if os.path.exists(saga_path):
            raise SagaError(f"Saga '{clean_id}' already exists")

        now_iso = _now()
        saga = Saga(
            id=clean_id,
            title=title.strip() or clean_id,
            status=status,
            tags=[t.strip().lower() for t in (tags or []) if t.strip()],
            watermarked_running_brief=brief.strip(),
            watermark=now_iso if brief.strip() else "",
            created_at=now_iso,
            updated_at=now_iso,
            events=[],
        )

        with open(saga_path, "w", encoding="utf-8") as f:
            json.dump(saga.to_dict(), f, indent=2)

        _update_index_locked(root, saga)
        return saga


def get_saga(root: str, saga_id: str) -> Saga | None:
    """Load a saga by its unique ID."""
    try:
        clean_id = _sanitize_saga_id(saga_id)
    except ValueError:
        return None
    saga_path = _saga_file(root, clean_id)
    if not os.path.isfile(saga_path):
        return None
    try:
        with open(saga_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Saga.from_dict(data)
    except (OSError, ValueError, TypeError):
        return None


def list_sagas(
    root: str,
    status: str = "",
    tag: str = "",
    limit: int = 50,
) -> list[Saga]:
    """List sagas optionally filtered by status ('active'/'resolved') or tag."""
    s_dir = _sagas_dir(root)
    if not os.path.isdir(s_dir):
        return []

    status_filter = status.strip().lower()
    tag_filter = tag.strip().lower()

    sagas: list[Saga] = []
    for fname in sorted(os.listdir(s_dir)):
        if fname.endswith(".json") and not fname.startswith("index"):
            saga_id = fname[:-5]
            saga = get_saga(root, saga_id)
            if not saga:
                continue
            if status_filter and saga.status != status_filter:
                continue
            if tag_filter and tag_filter not in [t.lower() for t in saga.tags]:
                continue
            sagas.append(saga)
            if len(sagas) >= limit:
                break
    sagas.sort(key=lambda s: s.updated_at, reverse=True)
    return sagas


def append_saga_event(
    root: str,
    saga_id: str,
    title: str,
    description: str = "",
    actor: str = "agent",
    timestamp: str | None = None,
    metadata: dict[str, Any] | None = None,
    new_brief: str | None = None,
    watermark: str | None = None,
) -> Saga:
    """Append a timestamped milestone or incident event to the ordered saga narrative."""
    clean_id = _sanitize_saga_id(saga_id)
    saga_path = _saga_file(root, clean_id)

    with _jsonl.locked(_lock_file(root)):
        saga = get_saga(root, clean_id)
        if not saga:
            raise SagaError(f"Saga '{clean_id}' does not exist")

        event_ts = timestamp or _now()
        event_id = f"ev_{uuid.uuid4().hex[:10]}"
        event = SagaEvent(
            id=event_id,
            timestamp=event_ts,
            title=title.strip(),
            description=description.strip(),
            actor=actor.strip() or "agent",
            metadata=dict(metadata or {}),
        )

        saga.events.append(event)
        # Keep events sorted chronologically
        saga.events.sort(key=lambda e: e.timestamp)
        saga.updated_at = _now()

        if new_brief is not None:
            saga.watermarked_running_brief = new_brief.strip()
            saga.watermark = watermark or event_ts
        elif watermark:
            saga.watermark = watermark

        with open(saga_path, "w", encoding="utf-8") as f:
            json.dump(saga.to_dict(), f, indent=2)

        _update_index_locked(root, saga)
        return saga


def update_running_brief(
    root: str,
    saga_id: str,
    brief: str,
    watermark: str,
) -> Saga:
    """Update the running synthesis brief with a progressive watermark."""
    clean_id = _sanitize_saga_id(saga_id)
    saga_path = _saga_file(root, clean_id)

    with _jsonl.locked(_lock_file(root)):
        saga = get_saga(root, clean_id)
        if not saga:
            raise SagaError(f"Saga '{clean_id}' does not exist")

        saga.watermarked_running_brief = brief.strip()
        saga.watermark = str(watermark).strip()
        saga.updated_at = _now()

        with open(saga_path, "w", encoding="utf-8") as f:
            json.dump(saga.to_dict(), f, indent=2)

        _update_index_locked(root, saga)
        return saga


def resolve_saga(root: str, saga_id: str, final_brief: str = "") -> Saga:
    """Mark a saga as resolved."""
    clean_id = _sanitize_saga_id(saga_id)
    saga_path = _saga_file(root, clean_id)

    with _jsonl.locked(_lock_file(root)):
        saga = get_saga(root, clean_id)
        if not saga:
            raise SagaError(f"Saga '{clean_id}' does not exist")

        saga.status = "resolved"
        saga.updated_at = _now()
        if final_brief:
            saga.watermarked_running_brief = final_brief.strip()
            saga.watermark = saga.updated_at

        with open(saga_path, "w", encoding="utf-8") as f:
            json.dump(saga.to_dict(), f, indent=2)

        _update_index_locked(root, saga)
        return saga


def delete_saga(root: str, saga_id: str) -> bool:
    """Delete a saga and remove it from the index."""
    clean_id = _sanitize_saga_id(saga_id)
    saga_path = _saga_file(root, clean_id)

    with _jsonl.locked(_lock_file(root)):
        if not os.path.exists(saga_path):
            return False
        try:
            os.remove(saga_path)
        except OSError:
            pass

        idx_path = _sagas_index_file(root)
        if os.path.exists(idx_path):
            rows = [r for r in _jsonl.read_rows(idx_path) if isinstance(r, dict) and r.get("id") != clean_id]
            _jsonl.write_rows(idx_path, rows)
        return True


def search_sagas(root: str, query: str, limit: int = 10) -> list[Saga]:
    """Search sagas by title, tags, running brief, or event contents."""
    q_words = [w.lower() for w in re.findall(r"\w+", query) if len(w) >= 2]
    if not q_words:
        return list_sagas(root, limit=limit)

    scored: list[tuple[int, Saga]] = []
    for saga in list_sagas(root, limit=500):
        text = f"{saga.id} {saga.title} {' '.join(saga.tags)} {saga.watermarked_running_brief}".lower()
        for ev in saga.events:
            text += f" {ev.title} {ev.description}".lower()
        hits = sum(1 for w in q_words if w in text)
        if hits > 0:
            scored.append((hits, saga))

    scored.sort(key=lambda x: (x[0], x[1].updated_at), reverse=True)
    return [s for _score, s in scored[:limit]]


def _update_index_locked(root: str, saga: Saga) -> None:
    idx_path = _sagas_index_file(root)
    rows = [r for r in _jsonl.read_rows(idx_path) if isinstance(r, dict) and r.get("id") != saga.id]
    rows.append({
        "id": saga.id,
        "title": saga.title,
        "status": saga.status,
        "tags": saga.tags,
        "event_count": len(saga.events),
        "watermark": saga.watermark,
        "updated_at": saga.updated_at,
    })
    _jsonl.write_rows(idx_path, rows)
