"""Stateful agent working memory blocks."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from commontrace import _jsonl, paths

DEFAULT_MAX_CHARS = 2000
MAX_QUOTA_CHARS = 100_000
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
    return os.path.join(paths.memory_dir(root), "blocks")


def _history_file(root: str) -> str:
    return os.path.join(_blocks_dir(root), "history.jsonl")


def _meta_file(root: str, name: str) -> str:
    safe_name = _sanitize_name(name)
    return os.path.join(_blocks_dir(root), f"{safe_name}.meta.json")


def _content_file(root: str, name: str) -> str:
    safe_name = _sanitize_name(name)
    return os.path.join(_blocks_dir(root), f"{safe_name}.md")


def _sanitize_name(name: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(name or "").strip().lower())
    if not clean.strip("_-"):
        raise MemoryBlockError("Memory block name must contain a letter or digit")
    if len(clean) > 64:
        raise MemoryBlockError("Memory block name is longer than 64 characters")
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

    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        with open(content_path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        raise BlockNotFoundError(f"Memory block '{clean}' does not exist") from None
    except ValueError as exc:
        raise MemoryBlockError(f"Memory block '{clean}' metadata is unreadable: {exc}") from None

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
    if not os.path.isdir(b_dir):
        return blocks

    for fname in sorted(os.listdir(b_dir)):
        if fname.endswith(".meta.json"):
            name = fname[:-10]
            try:
                blocks.append(get_block(root, name))
            except MemoryBlockError:
                continue
    return blocks


def _lock_path(root: str) -> str:
    return os.path.join(_blocks_dir(root), "blocks")


def _read_meta(meta_path: str) -> dict[str, Any]:
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _log(root: str, entry: dict[str, Any]) -> None:
    path = _history_file(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _discard(*paths_: str) -> None:
    for path in paths_:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _write_locked(
    root: str, clean: str, content: str, max_chars: int, actor: str, reason: str,
    metadata: dict[str, Any] | None,
) -> MemoryBlock:
    if int(max_chars) > MAX_QUOTA_CHARS:
        raise MemoryBlockError(f"block quota cannot exceed {MAX_QUOTA_CHARS} characters")
    if len(content) > max_chars:
        raise QuotaExceededError(
            f"Content length {len(content)} exceeds quota of {max_chars} chars for block '{clean}'"
        )
    os.makedirs(_blocks_dir(root), exist_ok=True)
    meta_path, content_path = _meta_file(root, clean), _content_file(root, clean)
    existing = _read_meta(meta_path)
    prev_revision = str(existing.get("revision", ""))
    created_at = str(existing.get("created_at") or _now())
    revision = _compute_revision(clean, content, prev_revision)
    updated_at = _now()
    merged = {**(existing.get("metadata") or {}), **(metadata or {})}
    meta = {
        "name": clean, "max_chars": int(max_chars), "revision": revision,
        "created_at": created_at, "updated_at": updated_at, "metadata": merged,
    }
    content_tmp, meta_tmp, backup = content_path + ".tmp", meta_path + ".tmp", content_path + ".bak"
    try:
        with open(content_tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        with open(meta_tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(meta, fh, indent=2)
        _log(root, {
            "timestamp": updated_at, "block": clean, "action": "set", "actor": actor, "reason": reason,
            "revision": revision, "prev_revision": prev_revision, "char_count": len(content),
        })
        had_content = os.path.exists(content_path)
        if had_content:
            os.replace(content_path, backup)
        os.replace(content_tmp, content_path)
        try:
            os.replace(meta_tmp, meta_path)
        except BaseException:
            if had_content:
                os.replace(backup, content_path)
            else:
                _discard(content_path)
            raise
    finally:
        _discard(content_tmp, meta_tmp, backup)
    return MemoryBlock(
        name=clean, content=content, char_count=len(content), max_chars=int(max_chars),
        revision=revision, created_at=created_at, updated_at=updated_at, metadata=merged,
    )


def set_block(
    root: str,
    name: str,
    content: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    actor: str = "agent",
    reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> MemoryBlock:
    """Create or overwrite a block; the new content must fit its quota."""
    clean = _sanitize_name(name)
    with _jsonl.locked(_lock_path(root)):
        return _write_locked(root, clean, content.strip(), max_chars, actor, reason, metadata)


def append_block(
    root: str,
    name: str,
    text: str,
    actor: str = "agent",
    reason: str = "",
) -> MemoryBlock:
    """Append a line to a block (creating it), keeping it within its quota."""
    clean = _sanitize_name(name)
    with _jsonl.locked(_lock_path(root)):
        try:
            block = get_block(root, clean)
            base, max_chars = block.content, block.max_chars
        except BlockNotFoundError:
            base, max_chars = "", DEFAULT_MAX_CHARS
        new_content = f"{base}\n{text.strip()}".strip() if base else text.strip()
        return _write_locked(root, clean, new_content, max_chars, actor, reason or "append", None)


def replace_block(
    root: str,
    name: str,
    old_str: str,
    new_str: str,
    actor: str = "agent",
    reason: str = "",
) -> MemoryBlock:
    """Replace the one occurrence of *old_str* in a block."""
    if not old_str:
        raise MemoryBlockError("Ambiguous replacement: the target text is empty")
    clean = _sanitize_name(name)
    with _jsonl.locked(_lock_path(root)):
        block = get_block(root, clean)
        occurrences = block.content.count(old_str)
        if occurrences == 0:
            raise SubstringNotFoundError(f"Target text not found in memory block '{block.name}'")
        if occurrences > 1:
            raise MemoryBlockError(
                f"Ambiguous replacement: target text occurs {occurrences} times in block '{block.name}'"
            )
        new_content = block.content.replace(old_str, new_str).strip()
        return _write_locked(root, clean, new_content, block.max_chars, actor,
                             reason or f"replaced '{old_str[:20]}' with '{new_str[:20]}'", None)


def delete_block(
    root: str,
    name: str,
    actor: str = "agent",
    reason: str = "",
) -> bool:
    """Delete a block, recording the deletion in its history."""
    clean = _sanitize_name(name)
    with _jsonl.locked(_lock_path(root)):
        meta_path, content_path = _meta_file(root, clean), _content_file(root, clean)
        if not os.path.exists(meta_path) and not os.path.exists(content_path):
            return False
        prev_revision = str(_read_meta(meta_path).get("revision", ""))
        for path in (meta_path, content_path):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        _log(root, {
            "timestamp": _now(), "block": clean, "action": "delete", "actor": actor, "reason": reason,
            "revision": _compute_revision(clean, "", prev_revision), "prev_revision": prev_revision,
            "char_count": 0,
        })
        return True


def block_history(root: str, name: str = "") -> list[dict[str, Any]]:
    """The audit log for every block, or for *name* only."""
    clean = _sanitize_name(name) if name else ""
    return [e for e in _jsonl.read_rows(_history_file(root)) if not clean or e.get("block") == clean]
