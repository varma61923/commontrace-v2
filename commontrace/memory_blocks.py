"""Stateful agent working memory blocks.

Blocks are markdown files stored in ``memory/blocks/``.  Each block has a
character-quota, a SHA-256 revision hash, an immutable audit log, and an
optional *read_only* flag that prevents mutation by agents or MCP callers.

Letta-style safeguards adopted here:
- ``read_only`` protection: mutations on a read-only block raise
  ``ReadOnlyBlockError``.
- Ambiguous ``replace_block`` returns the line numbers of every occurrence so
  the caller can narrow its target string.
- Line-number prefix hallucination guard: ``_strip_line_prefix`` removes the
  ``Line 12: `` / ``12→ `` decorations that LLMs copy from rendered views.
- Tab normalisation: content and replacement strings are tab-expanded before
  comparison and storage.
- ``insert_block``: positional line insertion (0 = top, -1 = bottom, N = line N).
- ``render_memory_blocks``: Letta-style XML rendering for prompt injection.
"""
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

# Strips "Line 12: " or "12→ " or "12> " prefixes that LLMs hallucinate when
# copying text from a line-numbered rendered view.
_LINE_PREFIX_RE = re.compile(r"^(?:Line\s+\d+:\s*|\d+[→>]\s*)", re.MULTILINE)


class MemoryBlockError(Exception):
    """Base error for memory block operations."""


class QuotaExceededError(MemoryBlockError):
    """Raised when block content exceeds its character quota."""


class BlockNotFoundError(MemoryBlockError):
    """Raised when a requested memory block does not exist."""


class SubstringNotFoundError(MemoryBlockError):
    """Raised when replace target string is not found in the block."""


class ReadOnlyBlockError(MemoryBlockError):
    """Raised when a mutation is attempted on a read-only block."""


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
    read_only: bool = False

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
        read_only=bool(meta.get("read_only", False)),
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
    metadata: dict[str, Any] | None, *, read_only: bool = False,
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
    # Preserve an existing read_only=True unless the caller explicitly passes False.
    effective_read_only = existing.get("read_only", False) or read_only
    revision = _compute_revision(clean, content, prev_revision)
    updated_at = _now()
    merged = {**(existing.get("metadata") or {}), **(metadata or {})}
    meta = {
        "name": clean, "max_chars": int(max_chars), "revision": revision,
        "created_at": created_at, "updated_at": updated_at, "metadata": merged,
        "read_only": effective_read_only,
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
        read_only=effective_read_only,
    )


def _check_mutable(block: MemoryBlock) -> None:
    """Raise ReadOnlyBlockError if the block cannot be mutated."""
    if block.read_only:
        raise ReadOnlyBlockError(
            f"Memory block '{block.name}' is read-only and cannot be modified."
        )


def strip_line_prefix(text: str) -> str:
    """Strip LLM-hallucinated line-number prefixes (``Line 12: `` / ``12→ ``)."""
    return _LINE_PREFIX_RE.sub("", text)


def set_block(
    root: str,
    name: str,
    content: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    actor: str = "agent",
    reason: str = "",
    metadata: dict[str, Any] | None = None,
    *,
    read_only: bool = False,
) -> MemoryBlock:
    """Create or overwrite a block; the new content must fit its quota.

    Pass ``read_only=True`` to mark the block immutable after creation.
    """
    clean = _sanitize_name(name)
    with _jsonl.locked(_lock_path(root)):
        # Allow overwrite only if the existing block is not read-only.
        try:
            existing = get_block(root, clean)
            _check_mutable(existing)
        except BlockNotFoundError:
            pass
        return _write_locked(root, clean, content.expandtabs().strip(), max_chars, actor, reason,
                             metadata, read_only=read_only)


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
            _check_mutable(block)
            base, max_chars = block.content, block.max_chars
        except BlockNotFoundError:
            base, max_chars = "", DEFAULT_MAX_CHARS
        appended = text.expandtabs().strip()
        new_content = f"{base}\n{appended}".strip() if base else appended
        return _write_locked(root, clean, new_content, max_chars, actor, reason or "append", None)


def replace_block(
    root: str,
    name: str,
    old_str: str,
    new_str: str,
    actor: str = "agent",
    reason: str = "",
) -> MemoryBlock:
    """Replace the one occurrence of *old_str* in a block.

    Improvements over the original:
    - ``old_str`` and block content are tab-expanded before comparison.
    - LLM-hallucinated line-number prefixes are stripped from *old_str*.
    - When *old_str* appears on multiple lines, the error names all of them.
    """
    if not old_str:
        raise MemoryBlockError("Ambiguous replacement: the target text is empty")
    clean = _sanitize_name(name)
    # Strip hallucinated line prefixes and expand tabs before comparing.
    old_str = strip_line_prefix(old_str.expandtabs())
    new_str = strip_line_prefix(new_str.expandtabs())
    with _jsonl.locked(_lock_path(root)):
        block = get_block(root, clean)
        _check_mutable(block)
        normalised = block.content.expandtabs()
        occurrences = normalised.count(old_str)
        if occurrences == 0:
            raise SubstringNotFoundError(f"Target text not found in memory block '{block.name}'")
        if occurrences > 1:
            # Return the exact line numbers so the LLM can refine its argument.
            lines = [
                i + 1
                for i, line in enumerate(normalised.split("\n"))
                if old_str in line
            ]
            raise MemoryBlockError(
                f"Ambiguous replacement: target text occurs {occurrences} times in block "
                f"'{block.name}' on lines {lines}. "
                "Provide more surrounding context to make the target unique."
            )
        new_content = normalised.replace(old_str, new_str).strip()
        return _write_locked(root, clean, new_content, block.max_chars, actor,
                             reason or f"replaced '{old_str[:20]}' with '{new_str[:20]}'", None)


def insert_block(
    root: str,
    name: str,
    text: str,
    line_number: int = -1,
    actor: str = "agent",
    reason: str = "",
) -> MemoryBlock:
    """Insert *text* at a specific line of a block.

    ``line_number`` interpretation (Letta-style):
    - ``0``: insert before the first line (top of block).
    - ``-1``: append after the last line.
    - ``N`` (1 ≤ N ≤ n_lines): insert after line N.
    """
    clean = _sanitize_name(name)
    with _jsonl.locked(_lock_path(root)):
        try:
            block = get_block(root, clean)
            _check_mutable(block)
            lines = block.content.expandtabs().split("\n")
            max_chars = block.max_chars
        except BlockNotFoundError:
            lines, max_chars = [], DEFAULT_MAX_CHARS
        n = len(lines)
        insertion = text.expandtabs().rstrip("\n")
        if line_number == -1 or line_number >= n:
            lines.append(insertion)
        elif line_number == 0:
            lines.insert(0, insertion)
        elif 1 <= line_number <= n:
            lines.insert(line_number, insertion)
        else:
            raise MemoryBlockError(
                f"line_number {line_number} out of range [0, {n}] for block '{clean}'"
            )
        new_content = "\n".join(lines).strip()
        return _write_locked(root, clean, new_content, max_chars, actor,
                             reason or f"inserted at line {line_number}", None)


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
        existing = _read_meta(meta_path)
        if existing.get("read_only"):
            raise ReadOnlyBlockError(
                f"Memory block '{clean}' is read-only and cannot be deleted."
            )
        prev_revision = str(existing.get("revision", ""))
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


def render_memory_blocks(blocks: list[MemoryBlock]) -> str:
    """Letta-style XML rendering of memory blocks for prompt injection.

    Produces a ``<memory_blocks>`` section showing each block's current content,
    character usage, quota, and read-only flag so an LLM can understand block
    state before making editing calls.

    Example output::

        <memory_blocks>
          <persona>
            <metadata chars_current="342" chars_limit="2000" read_only="false"/>
            <value>You are a concise assistant...</value>
          </persona>
        </memory_blocks>
    """
    if not blocks:
        return ""
    lines = ["<memory_blocks>"]
    for block in blocks:
        tag = block.name
        ro = "true" if block.read_only else "false"
        lines.append(f"  <{tag}>")
        lines.append(
            f'    <metadata chars_current="{block.char_count}" '
            f'chars_limit="{block.max_chars}" read_only="{ro}"/>'
        )
        lines.append(f"    <value>{block.content}</value>")
        lines.append(f"  </{tag}>")
    lines.append("</memory_blocks>")
    return "\n".join(lines)

