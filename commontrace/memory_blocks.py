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

Scoping: every operation takes an optional ``scope`` -- ``""`` (global, the
default and the only behaviour before scopes existed), ``session:<id>`` or
``agent:<id>``. Scoped blocks live under ``memory/blocks/scopes/<kind>/``
and share the global lock and audit log (their journal rows carry
``scope``). Reads that want the effective view use ``resolve_block`` /
``resolved_blocks``: a session block shadows an agent block of the same
name, which shadows the global one.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from xml.sax.saxutils import escape

from commontrace import _jsonl, paths

DEFAULT_MAX_CHARS = 2000
MAX_QUOTA_CHARS = 100_000
BUILTIN_BLOCKS = ("persona", "human", "project")
SCOPE_KINDS = ("session", "agent")
GLOBAL_SCOPE = ""
_SCOPE_RE = re.compile(r"^(session|agent):(.{1,200})$", re.DOTALL)
_SCOPE_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

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


class RevisionConflictError(MemoryBlockError):
    """A compare-and-set operation observed a newer working-memory revision."""

    def __init__(self, name: str, expected: str, actual: str) -> None:
        self.name, self.expected_revision, self.actual_revision = name, expected, actual
        super().__init__(f"Memory block '{name}' revision conflict: expected {expected!r}, actual {actual!r}")


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
    scope: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if not data["scope"]:
            # Global blocks serialise exactly as they did before scopes existed.
            del data["scope"]
        return data


def normalize_scope(scope: str | None) -> str:
    """Canonical scope label: ``""`` for global, else ``session:<id>`` / ``agent:<id>``."""
    if scope is None:
        return GLOBAL_SCOPE
    text = str(scope).strip()
    if text in ("", "global"):
        return GLOBAL_SCOPE
    match = _SCOPE_RE.match(text)
    if not match or not match.group(2).strip() or _SCOPE_CONTROL.search(match.group(2)):
        raise MemoryBlockError(
            "memory block scope must be 'global', 'session:<id>' or 'agent:<id>' "
            "(id: 1-200 characters, no control characters)")
    return f"{match.group(1)}:{match.group(2).strip()}"


def _scope_dirname(scope: str) -> str:
    kind, ident = scope.split(":", 1)
    # Lossy, traversal-proof label for humans plus a digest of the exact id,
    # so two ids that sanitise alike can never share a directory.
    label = re.sub(r"[^A-Za-z0-9_-]", "_", ident)[:48].strip("_-") or "id"
    digest = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:12]
    return os.path.join(kind, f"{label}-{digest}")


def resolution_order(session: str = "", agent: str = "") -> list[str]:
    """Scopes consulted when reading, most specific first: session > agent > global."""
    order: list[str] = []
    if session:
        order.append(normalize_scope(session if session.startswith("session:") else f"session:{session}"))
    if agent:
        order.append(normalize_scope(agent if agent.startswith("agent:") else f"agent:{agent}"))
    order.append(GLOBAL_SCOPE)
    return order



def _root_blocks_dir(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "blocks")


def _blocks_dir(root: str, scope: str = "") -> str:
    base = _root_blocks_dir(root)
    return os.path.join(base, "scopes", _scope_dirname(scope)) if scope else base


def _history_file(root: str) -> str:
    return os.path.join(_root_blocks_dir(root), "history.jsonl")


def _meta_file(root: str, name: str, scope: str = "") -> str:
    safe_name = _sanitize_name(name)
    return os.path.join(_blocks_dir(root, scope), f"{safe_name}.meta.json")


def _content_file(root: str, name: str, scope: str = "") -> str:
    safe_name = _sanitize_name(name)
    return os.path.join(_blocks_dir(root, scope), f"{safe_name}.md")


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


def get_block(root: str, name: str, *, scope: str = "") -> MemoryBlock:
    """Retrieve one coherent metadata/content snapshot, serialized with writers.

    ``scope`` reads exactly that scope; use ``resolve_block`` for the
    session > agent > global fallback.
    """
    scope = normalize_scope(scope)
    with _jsonl.locked(_lock_path(root)):
        return _get_block_locked(root, name, scope)


def _get_block_locked(root: str, name: str, scope: str = "") -> MemoryBlock:
    clean = _sanitize_name(name)
    meta_path = _meta_file(root, clean, scope)
    content_path = _content_file(root, clean, scope)

    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        with open(content_path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        where = f" in scope '{scope}'" if scope else ""
        raise BlockNotFoundError(f"Memory block '{clean}' does not exist{where}") from None
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
        scope=scope,
    )


def list_blocks(root: str, *, scope: str = "") -> list[MemoryBlock]:
    """List the memory blocks of one scope (global by default)."""
    scope = normalize_scope(scope)
    b_dir = _blocks_dir(root, scope)
    blocks: list[MemoryBlock] = []
    if not os.path.isdir(b_dir):
        return blocks

    for fname in sorted(os.listdir(b_dir)):
        if fname.endswith(".meta.json"):
            name = fname[:-10]
            try:
                blocks.append(get_block(root, name, scope=scope))
            except MemoryBlockError:
                continue
    return blocks


def list_scopes(root: str) -> list[str]:
    """Every non-global scope that holds at least one block, sorted."""
    base = os.path.join(_root_blocks_dir(root), "scopes")
    found: set[str] = set()
    for kind in SCOPE_KINDS:
        kind_dir = os.path.join(base, kind)
        if not os.path.isdir(kind_dir):
            continue
        for entry in sorted(os.listdir(kind_dir)):
            directory = os.path.join(kind_dir, entry)
            if not os.path.isdir(directory):
                continue
            for fname in sorted(os.listdir(directory)):
                if fname.endswith(".meta.json"):
                    label = _read_meta(os.path.join(directory, fname)).get("scope")
                    try:
                        scope = normalize_scope(label)
                    except MemoryBlockError:
                        continue
                    if scope and _blocks_dir(root, scope) == directory:
                        found.add(scope)
                        break
    return sorted(found)


def resolve_block(root: str, name: str, *, session: str = "", agent: str = "") -> MemoryBlock:
    """The effective block *name*: the session's, else the agent's, else the global one."""
    order = resolution_order(session, agent)
    with _jsonl.locked(_lock_path(root)):
        for scope in order:
            try:
                return _get_block_locked(root, name, scope)
            except BlockNotFoundError:
                continue
    raise BlockNotFoundError(f"Memory block '{_sanitize_name(name)}' does not exist in {order[:-1] + ['global']}")


def resolved_blocks(root: str, *, session: str = "", agent: str = "") -> list[MemoryBlock]:
    """The effective block set, one per name, with session > agent > global shadowing.

    Sorted by name, so the rendering is deterministic for a given store state.
    """
    chosen: dict[str, MemoryBlock] = {}
    for scope in resolution_order(session, agent):
        for block in list_blocks(root, scope=scope):
            chosen.setdefault(block.name, block)
    return [chosen[name] for name in sorted(chosen)]


def _lock_path(root: str) -> str:
    return os.path.join(_root_blocks_dir(root), "blocks")


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
    had_history = os.path.exists(path)
    previous_size = os.path.getsize(path) if had_history else 0
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        # All block mutations hold the same lock, so no other append can be
        # lost when rolling back a partial or non-durable journal write.
        if had_history:
            with open(path, "r+b") as journal:
                journal.truncate(previous_size)
        else:
            _discard(path)
        raise


def _discard(*paths_: str) -> None:
    for path in paths_:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _write_locked(
    root: str, clean: str, content: str, max_chars: int, actor: str, reason: str,
    metadata: dict[str, Any] | None, *, read_only: bool = False, scope: str = "",
) -> MemoryBlock:
    if int(max_chars) > MAX_QUOTA_CHARS:
        raise MemoryBlockError(f"block quota cannot exceed {MAX_QUOTA_CHARS} characters")
    if len(content) > max_chars:
        raise QuotaExceededError(
            f"Content length {len(content)} exceeds quota of {max_chars} chars for block '{clean}'"
        )
    os.makedirs(_blocks_dir(root, scope), exist_ok=True)
    meta_path, content_path = _meta_file(root, clean, scope), _content_file(root, clean, scope)
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
    if scope:
        meta["scope"] = scope
    content_tmp, meta_tmp = content_path + ".tmp", meta_path + ".tmp"
    backup, meta_backup = content_path + ".bak", meta_path + ".bak"
    had_content, had_meta = os.path.exists(content_path), os.path.exists(meta_path)
    content_saved = content_written = meta_written = False
    cleanup_backups = False
    try:
        with open(content_tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        with open(meta_tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(meta, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        if had_meta:
            # Preserve the live metadata until its replacement succeeds.
            shutil.copyfile(meta_path, meta_backup)
        if had_content:
            os.replace(content_path, backup)
            content_saved = True
        os.replace(content_tmp, content_path)
        content_written = True
        os.replace(meta_tmp, meta_path)
        meta_written = True
        _log(root, {
            "timestamp": updated_at, "block": clean, "action": "set", "actor": actor, "reason": reason,
            "revision": revision, "prev_revision": prev_revision, "char_count": len(content),
            **({"scope": scope} if scope else {}),
        })
        cleanup_backups = True
    except BaseException:
        if meta_written:
            if had_meta:
                os.replace(meta_backup, meta_path)
            else:
                _discard(meta_path)
        if content_saved:
            os.replace(backup, content_path)
        elif content_written:
            _discard(content_path)
        cleanup_backups = True
        raise
    finally:
        _discard(content_tmp, meta_tmp)
        # If rollback itself failed, retain backups for operator recovery.
        if cleanup_backups:
            _discard(backup, meta_backup)
    return MemoryBlock(
        name=clean, content=content, char_count=len(content), max_chars=int(max_chars),
        revision=revision, created_at=created_at, updated_at=updated_at, metadata=merged,
        read_only=effective_read_only, scope=scope,
    )


def _check_mutable(block: MemoryBlock) -> None:
    """Raise ReadOnlyBlockError if the block cannot be mutated."""
    if block.read_only:
        raise ReadOnlyBlockError(
            f"Memory block '{block.name}' is read-only and cannot be modified."
        )


def _check_revision(name: str, actual: str, expected: str | None) -> None:
    # None preserves unconditional legacy writes. Empty means create-only.
    if expected is not None and actual != expected:
        raise RevisionConflictError(name, expected, actual)


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
    expected_revision: str | None = None,
    scope: str = "",
) -> MemoryBlock:
    """Create or overwrite a block; the new content must fit its quota.

    Pass ``read_only=True`` to mark the block immutable after creation.
    ``expected_revision`` compares under the write lock; ``""`` is create-only.
    ``scope`` (``session:<id>`` / ``agent:<id>``) writes a scoped block.
    """
    clean = _sanitize_name(name)
    scope = normalize_scope(scope)
    with _jsonl.locked(_lock_path(root)):
        # Allow overwrite only if the existing block is not read-only.
        try:
            existing = _get_block_locked(root, clean, scope)
            _check_revision(clean, existing.revision, expected_revision)
            _check_mutable(existing)
        except BlockNotFoundError:
            _check_revision(clean, "", expected_revision)
        return _write_locked(root, clean, content.expandtabs().strip(), max_chars, actor, reason,
                             metadata, read_only=read_only, scope=scope)


def append_block(
    root: str,
    name: str,
    text: str,
    actor: str = "agent",
    reason: str = "",
    *,
    expected_revision: str | None = None,
    scope: str = "",
) -> MemoryBlock:
    """Append a line to a block (creating it), keeping it within its quota."""
    clean = _sanitize_name(name)
    scope = normalize_scope(scope)
    with _jsonl.locked(_lock_path(root)):
        try:
            block = _get_block_locked(root, clean, scope)
            _check_revision(clean, block.revision, expected_revision)
            _check_mutable(block)
            base, max_chars = block.content, block.max_chars
        except BlockNotFoundError:
            _check_revision(clean, "", expected_revision)
            base, max_chars = "", DEFAULT_MAX_CHARS
        appended = text.expandtabs().strip()
        new_content = f"{base}\n{appended}".strip() if base else appended
        return _write_locked(root, clean, new_content, max_chars, actor, reason or "append", None, scope=scope)


def replace_block(
    root: str,
    name: str,
    old_str: str,
    new_str: str,
    actor: str = "agent",
    reason: str = "",
    *,
    expected_revision: str | None = None,
    scope: str = "",
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
    scope = normalize_scope(scope)
    # Strip hallucinated line prefixes and expand tabs before comparing.
    old_str = strip_line_prefix(old_str.expandtabs())
    new_str = strip_line_prefix(new_str.expandtabs())
    with _jsonl.locked(_lock_path(root)):
        block = _get_block_locked(root, clean, scope)
        _check_revision(clean, block.revision, expected_revision)
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
                             reason or f"replaced '{old_str[:20]}' with '{new_str[:20]}'", None, scope=scope)


def insert_block(
    root: str,
    name: str,
    text: str,
    line_number: int = -1,
    actor: str = "agent",
    reason: str = "",
    *,
    expected_revision: str | None = None,
    scope: str = "",
) -> MemoryBlock:
    """Insert *text* at a specific line of a block.

    ``line_number`` interpretation (Letta-style):
    - ``0``: insert before the first line (top of block).
    - ``-1``: append after the last line.
    - ``N`` (1 ≤ N ≤ n_lines): insert after line N.
    """
    clean = _sanitize_name(name)
    scope = normalize_scope(scope)
    with _jsonl.locked(_lock_path(root)):
        try:
            block = _get_block_locked(root, clean, scope)
            _check_revision(clean, block.revision, expected_revision)
            _check_mutable(block)
            lines = block.content.expandtabs().split("\n")
            max_chars = block.max_chars
        except BlockNotFoundError:
            _check_revision(clean, "", expected_revision)
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
                             reason or f"inserted at line {line_number}", None, scope=scope)


def delete_block(
    root: str,
    name: str,
    actor: str = "agent",
    reason: str = "",
    *,
    expected_revision: str | None = None,
    scope: str = "",
) -> bool:
    """Delete a block, recording the deletion in its history."""
    clean = _sanitize_name(name)
    scope = normalize_scope(scope)
    with _jsonl.locked(_lock_path(root)):
        meta_path, content_path = _meta_file(root, clean, scope), _content_file(root, clean, scope)
        if not os.path.exists(meta_path) and not os.path.exists(content_path):
            _check_revision(clean, "", expected_revision)
            return False
        existing = _read_meta(meta_path)
        _check_revision(clean, str(existing.get("revision", "")), expected_revision)
        if existing.get("read_only"):
            raise ReadOnlyBlockError(
                f"Memory block '{clean}' is read-only and cannot be deleted."
            )
        prev_revision = str(existing.get("revision", ""))
        saved: list[tuple[str, str]] = []
        cleanup_backups = False
        try:
            for path in (meta_path, content_path):
                if os.path.exists(path):
                    backup = path + ".bak"
                    os.replace(path, backup)
                    saved.append((path, backup))
            _log(root, {
                "timestamp": _now(), "block": clean, "action": "delete", "actor": actor, "reason": reason,
                "revision": _compute_revision(clean, "", prev_revision), "prev_revision": prev_revision,
                "char_count": 0, **({"scope": scope} if scope else {}),
            })
            cleanup_backups = True
        except BaseException:
            for path, backup in reversed(saved):
                os.replace(backup, path)
            cleanup_backups = True
            raise
        finally:
            if cleanup_backups:
                _discard(*(backup for _path, backup in saved))
        return True


def block_history(root: str, name: str = "", *, scope: str = "") -> list[dict[str, Any]]:
    """The audit log for every block, or for *name* only, in one scope.

    The default is the global scope (exactly the pre-scope behaviour); pass
    ``scope="*"`` for every scope's rows.
    """
    clean = _sanitize_name(name) if name else ""
    every = scope == "*"
    wanted = "" if every else normalize_scope(scope)
    with _jsonl.locked(_lock_path(root)):
        return [e for e in _jsonl.read_rows(_history_file(root))
                if (not clean or e.get("block") == clean) and (every or str(e.get("scope") or "") == wanted)]


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
        tag = _sanitize_name(block.name)
        # Names may start with digits or hyphens; use a valid XML tag in that case.
        if not re.match(r"^[A-Za-z_]", tag):
            tag = "block_" + tag
        ro = "true" if block.read_only else "false"
        scope_attr = f' scope="{escape(block.scope, {chr(34): "&quot;"})}"' if block.scope else ""
        lines.append(f"  <{tag}>")
        lines.append(
            f'    <metadata chars_current="{block.char_count}" '
            f'chars_limit="{block.max_chars}" read_only="{ro}"{scope_attr}/>'
        )
        lines.append(f"    <value>{escape(block.content)}</value>")
        lines.append(f"  </{tag}>")
    lines.append("</memory_blocks>")
    return "\n".join(lines)
