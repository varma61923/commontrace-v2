"""Connector auto-sync framework for CommonTrace.

A Connector bridges an external source (local directory, website, webhook
emitter) into governed memory. Every connector:

- exposes ``authorize`` / ``sync`` / ``webhook_handler`` (the ABC contract),
- checkpoints progress with opaque state tokens persisted as git-tracked
  JSON under ``memory/connectors/state.json``,
- emits chunks via :mod:`commontrace.ingest` (``Chunk`` / ``IngestionResult``
  / ``_redact_secrets``) and records origin via
  :func:`commontrace.provenance.append_provenance` with
  ``source_path`` + ``run_id``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import textwrap
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from commontrace.ingest import Chunk, IngestionResult, _redact_secrets

CONNECTORS_DIRNAME = "connectors"
STATE_FILENAME = "state.json"

_MAX_MD_CHUNK = 2000


def new_run_id() -> str:
    """Return a short unique run identifier."""
    return uuid.uuid4().hex[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _connectors_dir(root: str) -> str:
    from commontrace import paths

    d = os.path.join(paths.memory_dir(root), CONNECTORS_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def _state_file(root: str) -> str:
    return os.path.join(_connectors_dir(root), STATE_FILENAME)


def load_connector_states(root: str) -> dict[str, Any]:
    """Return the whole persisted state map (git-tracked JSON)."""
    fpath = _state_file(root)
    if not os.path.exists(fpath):
        return {}
    try:
        with open(fpath, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_connector_state(root: str, key: str) -> dict[str, Any]:
    """Return the persisted entry for ``key`` or ``{}``."""
    states = load_connector_states(root)
    entry = states.get(key, {})
    return dict(entry) if isinstance(entry, dict) else {}


def save_connector_state(
    root: str, key: str, state_token: str, cursor: dict[str, Any]
) -> dict[str, Any]:
    """Persist ``state_token`` + ``cursor`` for ``key``; return the entry."""
    fpath = _state_file(root)
    states = load_connector_states(root)
    entry = {
        "state_token": state_token,
        "cursor": cursor,
        "updated_at": _now_iso(),
    }
    states[key] = entry
    tmp = fpath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(states, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, fpath)
    return entry


def encode_state_token(cursor: dict[str, Any]) -> str:
    """Encode a cursor dict as an opaque state token."""
    raw = json.dumps(cursor or {}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_state_token(token: str | None) -> dict[str, Any]:
    """Decode a state token back to a cursor dict (``{}`` on any error)."""
    if not token:
        return {}
    try:
        padded = str(token).strip()
        padded += "=" * (-len(padded) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return dict(data) if isinstance(data, dict) else {}


def chunk_markdown_text(text: str, source_path: str) -> list[Chunk]:
    """Split markdown-ish text by headings into bounded, redacted chunks.

    Mirrors :func:`commontrace.ingest._chunk_markdown` but operates on a
    string (for fetched web content) instead of a file path.
    """
    chunks: list[Chunk] = []
    text = text if isinstance(text, str) else str(text)
    sections = re.split(r"(?m)^(#{1,3}\s.+)$", text)
    heading = os.path.basename(source_path) or source_path
    body = ""

    def flush(h: str, b: str) -> None:
        b = b.strip()
        if len(b) < 50:
            return
        for i, block in enumerate(textwrap.wrap(b, _MAX_MD_CHUNK)):
            chunks.append(Chunk(
                content=_redact_secrets(block),
                source_path=source_path,
                chunk_id="%s_%s_%d" % (_fingerprint(source_path + h), _fingerprint(h)[:8], i),
                breadcrumb=h.strip("# ").strip()[:200],
                chunk_type="markdown_section",
            ))

    for part in sections:
        if re.match(r"^#{1,3}\s", part):
            flush(heading, body)
            heading = part
            body = ""
        else:
            body += part
    flush(heading, body)
    return chunks


def categorize_chunk(breadcrumb: str) -> str:
    """Mirror ingest_markdown_documentation heading -> category routing."""
    bc = (breadcrumb or "").lower()
    if any(kw in bc for kw in ("requirement", "constraint", "rule", "must", "should")):
        return "constraint"
    if any(kw in bc for kw in ("prefer", "recommend", "best practice")):
        return "preference"
    if any(kw in bc for kw in ("architecture", "design", "pattern", "structure")):
        return "architecture"
    return "general"


@dataclass
class SyncResult:
    """Outcome of one connector sync pass."""

    connector: str
    chunks: list[Chunk] = field(default_factory=list)
    result: IngestionResult | None = None
    new_state_token: str = ""
    run_id: str = ""
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        base = self.result.to_dict() if self.result is not None else {}
        base.update({
            "connector": self.connector,
            "chunks": len(self.chunks),
            "new_state_token": self.new_state_token,
            "run_id": self.run_id,
            "dry_run": self.dry_run,
        })
        return base


class Connector(ABC):
    """Abstract connector contract.

    Subclasses must implement :meth:`authorize`, :meth:`sync` and
    :meth:`webhook_handler`. State tokens are opaque base64 cursors;
    persistence lives in git-tracked ``memory/connectors/state.json``.
    """

    name = "base"

    @abstractmethod
    def authorize(self, credentials: dict[str, Any] | None = None) -> dict[str, Any]:
        """Validate access to the source. Returns at least ``{"ok": bool}``."""
        raise NotImplementedError

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
        """Sync new content since ``state_token`` into ``root``."""
        raise NotImplementedError

    @abstractmethod
    def webhook_handler(self, root: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Handle an inbound webhook/event payload. Never raise on bad input."""
        raise NotImplementedError

    # -- shared state-token helpers --------------------------------------
    def state_key(self) -> str:
        return self.name

    def get_persisted_state(self, root: str) -> dict[str, Any]:
        return load_connector_state(root, self.state_key())

    def resolve_cursor(self, root: str, state_token: str | None) -> dict[str, Any]:
        """Explicit token wins; otherwise fall back to persisted state."""
        if state_token:
            cursor = decode_state_token(state_token)
            if cursor:
                return cursor
        entry = self.get_persisted_state(root)
        token = entry.get("state_token", "")
        if token:
            cursor = decode_state_token(token)
            if cursor:
                return cursor
        cursor = entry.get("cursor", {})
        return dict(cursor) if isinstance(cursor, dict) else {}

    def persist_cursor(self, root: str, cursor: dict[str, Any]) -> str:
        token = encode_state_token(cursor)
        save_connector_state(root, self.state_key(), token, cursor)
        return token


_REGISTRY: dict[str, type[Connector]] = {}


def register_connector(cls: type[Connector]) -> type[Connector]:
    _REGISTRY[cls.name] = cls
    return cls


def get_connector(name: str) -> type[Connector] | None:
    return _REGISTRY.get(name)


def list_connectors() -> list[str]:
    return sorted(_REGISTRY)
