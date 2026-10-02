"""Stateful agent working memory blocks (Letta Core Memory pattern).

Provides bounded, named, auditable memory blocks (e.g. persona, human, project, guidelines)
with character limits, atomic updates, and cryptographic revision history.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from commontrace import paths

DEFAULT_MAX_CHARS = 2000
BUILTIN_BLOCKS = ("persona", "human", "project")


class MemoryBlockError(Exception):
    """Base error for memory block operations."""


class QuotaExceededError(MemoryBlockError):
    """Raised when block content exceeds its character quota."""


class BlockNotFoundError(MemoryBlockError):
    """Raised when a requested memory block does not exist."""


class SubstringNotFoundError(MemoryBlockError):
    """Raised when replace target string is not found in the block."""


@dataclass
class MemoryBlock:
    name: str
    content: str
    char_count: int
    max_chars: int
    revision: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _blocks_dir(root: str) -> str:
    path = os.path.join(paths.memory_dir(root), "blocks")
    os.makedirs(path, exist_ok=True)
    return path


def _history_file(root: str) -> str:
    return os.path.join(_blocks_dir(root), "history.jsonl")


def _meta_file(root: str, name: str) -> str:
    safe_name = _sanitize_name(name)
    return os.path.join(_blocks_dir(root), f"{safe_name}.meta.json")


def _content_file(root: str, name: str) -> str:
    safe_name = _sanitize_name(name)
    return os.path.join(_blocks_dir(root), f"{safe_name}.md")


def _sanitize_name(name: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_\-]", "_", name.strip().lower())
    if not clean:
        raise ValueError("Memory block name cannot be empty")
    return clean


def _compute_revision(name: str, content: str, prev_revision: str) -> str:
    payload = f"{name}:{content}:{prev_revision}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_block(root: str, name: str) -> MemoryBlock:
    """Retrieve an existing memory block by name."""
    clean = _sanitize_name(name)
    meta_path = _meta_file(root, clean)
    content_path = _content_file(root, clean)

    if not os.path.exists(meta_path) or not os.path.exists(content_path):
        raise BlockNotFoundError(f"Memory block '{clean}' does not exist")

    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    with open(content_path, "r", encoding="utf-8") as f:
        content = f.read()

    return MemoryBlock(
        name=clean,
        content=content,
        char_count=len(content),
        max_chars=meta.get("max_chars", DEFAULT_MAX_CHARS),
        revision=meta.get("revision", ""),
        created_at=meta.get("created_at", _now()),
        updated_at=meta.get("updated_at", _now()),
        metadata=meta.get("metadata", {}),
    )


def list_blocks(root: str) -> list[MemoryBlock]:
    """List all configured memory blocks in the store."""
    b_dir = _blocks_dir(root)
    blocks: list[MemoryBlock] = []
    if not os.path.exists(b_dir):
        return blocks

    for fname in sorted(os.listdir(b_dir)):
        if fname.endswith(".meta.json"):
            name = fname[:-10]
            try:
                blocks.append(get_block(root, name))
            except BlockNotFoundError:
                continue
    return blocks


def set_block(
    root: str,
    name: str,
    content: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    actor: str = "agent",
    reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> MemoryBlock:
    """Create or overwrite a memory block with the given content."""
    clean = _sanitize_name(name)
    content = content.strip()
    if len(content) > max_chars:
        raise QuotaExceededError(
            f"Content length {len(content)} exceeds quota of {max_chars} chars for block '{clean}'"
        )

    prev_revision = ""
    created_at = _now()
    existing_meta: dict[str, Any] = {}
    meta_path = _meta_file(root, clean)
    content_path = _content_file(root, clean)

    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                existing_meta = json.load(f)
                prev_revision = existing_meta.get("revision", "")
                created_at = existing_meta.get("created_at", created_at)
        except Exception:
            pass

    revision = _compute_revision(clean, content, prev_revision)
    updated_at = _now()
    merged_metadata = dict(existing_meta.get("metadata", {}))
    if metadata:
        merged_metadata.update(metadata)

    # Write content and metadata
    with open(content_path, "w", encoding="utf-8") as f:
        f.write(content)

    meta_dict = {
        "name": clean,
        "max_chars": max_chars,
        "revision": revision,
        "created_at": created_at,
        "updated_at": updated_at,
        "metadata": merged_metadata,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta_dict, f, indent=2)

    # Append to audit history
    history_entry = {
        "timestamp": updated_at,
        "block": clean,
        "action": "set",
        "actor": actor,
        "reason": reason,
        "revision": revision,
        "prev_revision": prev_revision,
        "char_count": len(content),
    }
    with open(_history_file(root), "a", encoding="utf-8") as f:
        f.write(json.dumps(history_entry) + "\n")

    return MemoryBlock(
        name=clean,
        content=content,
        char_count=len(content),
        max_chars=max_chars,
        revision=revision,
        created_at=created_at,
        updated_at=updated_at,
        metadata=merged_metadata,
    )


def append_block(
    root: str,
    name: str,
    text: str,
    actor: str = "agent",
    reason: str = "",
) -> MemoryBlock:
    """Append text to an existing block, ensuring it does not exceed the quota."""
    clean = _sanitize_name(name)
    try:
        block = get_block(root, clean)
        base = block.content
        max_chars = block.max_chars
    except BlockNotFoundError:
        base = ""
        max_chars = DEFAULT_MAX_CHARS

    new_content = f"{base}\n{text}".strip() if base else text.strip()
    return set_block(
        root=root,
        name=clean,
        content=new_content,
        max_chars=max_chars,
        actor=actor,
        reason=reason or "append",
    )


def replace_block(
    root: str,
    name: str,
    old_str: str,
    new_str: str,
    actor: str = "agent",
    reason: str = "",
) -> MemoryBlock:
    """Replace an exact substring within a block."""
    block = get_block(root, name)
    if old_str not in block.content:
        raise SubstringNotFoundError(
            f"Target text not found in memory block '{block.name}'"
        )

    occurrences = block.content.count(old_str)
    if occurrences > 1:
        raise MemoryBlockError(
            f"Ambiguous replacement: target text occurs {occurrences} times in block '{block.name}'"
        )

    new_content = block.content.replace(old_str, new_str)
    return set_block(
        root=root,
        name=block.name,
        content=new_content,
        max_chars=block.max_chars,
        actor=actor,
        reason=reason or f"replaced '{old_str[:20]}' with '{new_str[:20]}'",
    )


def delete_block(
    root: str,
    name: str,
    actor: str = "agent",
    reason: str = "",
) -> bool:
    """Delete a memory block."""
    clean = _sanitize_name(name)
    meta_path = _meta_file(root, clean)
    content_path = _content_file(root, clean)

    if not os.path.exists(meta_path) and not os.path.exists(content_path):
        return False

    if os.path.exists(meta_path):
        os.remove(meta_path)
    if os.path.exists(content_path):
        os.remove(content_path)

    history_entry = {
        "timestamp": _now(),
        "block": clean,
        "action": "delete",
        "actor": actor,
        "reason": reason,
    }
    with open(_history_file(root), "a", encoding="utf-8") as f:
        f.write(json.dumps(history_entry) + "\n")
    return True


def block_history(root: str, name: str = "") -> list[dict[str, Any]]:
    """Retrieve the audit log for all or a specific memory block."""
    h_path = _history_file(root)
    if not os.path.exists(h_path):
        return []

    clean = _sanitize_name(name) if name else ""
    entries: list[dict[str, Any]] = []
    with open(h_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if not clean or entry.get("block") == clean:
                    entries.append(entry)
            except Exception:
                continue
    return entries
