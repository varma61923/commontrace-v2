"""Curated knowledge pages with dry-run diffs and version history.

Adapted from Hindsight mental models & curated pages architecture (#2 L):
- Persistent, versioned markdown synthesis pages (e.g. architecture guides, runbooks, mental models).
- Supports dry-run diffs so agents can inspect proposed line-by-line changes before committing updates.
- Optimistic concurrency control via expected_version to avoid race conditions between agents.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from commontrace import _jsonl, paths


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pages_dir(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "pages")


def _history_file(root: str) -> str:
    return os.path.join(_pages_dir(root), "history.jsonl")


def _lock_file(root: str) -> str:
    return os.path.join(_pages_dir(root), "pages")


def _sanitize_slug(slug: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_\-\/]", "_", str(slug or "").strip().lower()).strip("/_")
    if not clean:
        raise ValueError("Page slug must contain at least one alphanumeric character")
    if len(clean) > 120:
        raise ValueError("Page slug cannot exceed 120 characters")
    return clean


def _meta_file(root: str, clean_slug: str) -> str:
    flat_name = clean_slug.replace("/", "__")
    return os.path.join(_pages_dir(root), f"{flat_name}.meta.json")


def _content_file(root: str, clean_slug: str) -> str:
    flat_name = clean_slug.replace("/", "__")
    return os.path.join(_pages_dir(root), f"{flat_name}.md")


class KnowledgePageError(RuntimeError):
    """Base error for knowledge page operations."""


class PageNotFoundError(KnowledgePageError):
    """Raised when a requested knowledge page does not exist."""


class VersionConflictError(KnowledgePageError):
    """Raised when expected_version does not match the current page version."""


@dataclass
class KnowledgePage:
    slug: str
    title: str
    content: str
    version: int
    revision: str
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    last_actor: str = "agent"
    last_comment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def char_count(self) -> int:
        return len(self.content)


def dry_run_diff(old_content: str, new_content: str, slug: str = "page") -> str:
    """Generate a clean unified diff between old and new markdown content."""
    old_lines = (old_content.splitlines(keepends=True) if old_content else [])
    new_lines = (new_content.splitlines(keepends=True) if new_content else [])
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{slug}.md",
        tofile=f"b/{slug}.md",
        lineterm="",
    )
    return "\n".join(diff).strip()


def get_page(root: str, slug: str, version: int | None = None) -> KnowledgePage | None:
    """Fetch the latest or a historical version of a knowledge page."""
    try:
        clean = _sanitize_slug(slug)
    except ValueError:
        return None

    if version is not None:
        # Fetch specific historical version from history
        for entry in page_history(root, clean):
            if entry.get("version") == version:
                return KnowledgePage(
                    slug=clean,
                    title=entry.get("title", clean),
                    content=entry.get("content", ""),
                    version=entry.get("version", version),
                    revision=entry.get("revision", ""),
                    tags=entry.get("tags", []),
                    created_at=entry.get("created_at", _now()),
                    updated_at=entry.get("timestamp", _now()),
                    last_actor=entry.get("actor", "agent"),
                    last_comment=entry.get("comment", ""),
                )
        return None

    meta_path = _meta_file(root, clean)
    content_path = _content_file(root, clean)
    if not os.path.isfile(meta_path) or not os.path.isfile(content_path):
        return None

    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        with open(content_path, "r", encoding="utf-8") as f:
            content = f.read()
        return KnowledgePage(
            slug=clean,
            title=meta.get("title", clean),
            content=content,
            version=meta.get("version", 1),
            revision=meta.get("revision", ""),
            tags=meta.get("tags", []),
            created_at=meta.get("created_at", _now()),
            updated_at=meta.get("updated_at", _now()),
            last_actor=meta.get("last_actor", "agent"),
            last_comment=meta.get("last_comment", ""),
        )
    except (OSError, ValueError):
        return None


def list_pages(root: str, tag: str = "", limit: int = 100) -> list[KnowledgePage]:
    """List knowledge pages, optionally filtered by tag."""
    p_dir = _pages_dir(root)
    if not os.path.isdir(p_dir):
        return []

    tag_filter = tag.strip().lower()
    pages: list[KnowledgePage] = []

    for fname in sorted(os.listdir(p_dir)):
        if fname.endswith(".meta.json"):
            flat_name = fname[:-10]
            clean_slug = flat_name.replace("__", "/")
            page = get_page(root, clean_slug)
            if not page:
                continue
            if tag_filter and tag_filter not in [t.lower() for t in page.tags]:
                continue
            pages.append(page)
            if len(pages) >= limit:
                break
    pages.sort(key=lambda p: p.updated_at, reverse=True)
    return pages


def update_page(
    root: str,
    slug: str,
    content: str,
    title: str = "",
    tags: list[str] | None = None,
    expected_version: int | None = None,
    dry_run: bool = False,
    actor: str = "agent",
    comment: str = "",
) -> dict[str, Any]:
    """Create or update a curated knowledge page with dry-run diff preview."""
    clean = _sanitize_slug(slug)
    os.makedirs(_pages_dir(root), exist_ok=True)

    with _jsonl.locked(_lock_file(root)):
        current = get_page(root, clean)
        old_content = current.content if current else ""
        current_version = current.version if current else 0
        proposed_version = current_version + 1

        if expected_version is not None and current is not None:
            if current.version != expected_version:
                raise VersionConflictError(
                    f"Page '{clean}' version conflict: expected v{expected_version}, but current is v{current.version}"
                )

        diff = dry_run_diff(old_content, content, slug=clean)
        changed = (old_content.strip() != content.strip())

        if dry_run:
            return {
                "dry_run": True,
                "slug": clean,
                "changed": changed,
                "current_version": current_version,
                "proposed_version": proposed_version,
                "diff": diff,
                "char_count": len(content),
            }

        now_iso = _now()
        created_at = current.created_at if current else now_iso
        page_title = title.strip() or (current.title if current else clean)
        page_tags = (
            [t.strip().lower() for t in tags if t.strip()]
            if tags is not None
            else (current.tags if current else [])
        )
        prev_rev = current.revision if current else ""
        revision = hashlib.sha256(f"{clean}:{proposed_version}:{content}:{prev_rev}".encode()).hexdigest()[:16]

        meta = {
            "slug": clean,
            "title": page_title,
            "version": proposed_version,
            "revision": revision,
            "tags": page_tags,
            "created_at": created_at,
            "updated_at": now_iso,
            "last_actor": actor.strip() or "agent",
            "last_comment": comment.strip(),
        }

        meta_path = _meta_file(root, clean)
        content_path = _content_file(root, clean)

        # Atomic write
        tmp_content = content_path + ".tmp"
        tmp_meta = meta_path + ".tmp"
        with open(tmp_content, "w", encoding="utf-8") as f:
            f.write(content)
        with open(tmp_meta, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        os.replace(tmp_content, content_path)
        os.replace(tmp_meta, meta_path)

        # Append to revision audit log
        _jsonl.append_row(_history_file(root), {
            "slug": clean,
            "version": proposed_version,
            "revision": revision,
            "title": page_title,
            "tags": page_tags,
            "char_count": len(content),
            "actor": actor,
            "comment": comment,
            "timestamp": now_iso,
            "created_at": created_at,
            "content": content,
            "diff": diff,
        })

        updated_page = KnowledgePage(
            slug=clean,
            title=page_title,
            content=content,
            version=proposed_version,
            revision=revision,
            tags=page_tags,
            created_at=created_at,
            updated_at=now_iso,
            last_actor=actor,
            last_comment=comment,
        )

        return {
            "dry_run": False,
            "slug": clean,
            "changed": changed,
            "version": proposed_version,
            "diff": diff,
            "page": updated_page.to_dict(),
        }


def delete_page(root: str, slug: str, actor: str = "agent", comment: str = "") -> bool:
    """Delete a knowledge page, archiving the deletion in history."""
    clean = _sanitize_slug(slug)
    with _jsonl.locked(_lock_file(root)):
        current = get_page(root, clean)
        if not current:
            return False

        meta_path = _meta_file(root, clean)
        content_path = _content_file(root, clean)
        for p in (meta_path, content_path):
            try:
                os.remove(p)
            except OSError:
                pass

        _jsonl.append_row(_history_file(root), {
            "slug": clean,
            "version": current.version + 1,
            "action": "delete",
            "actor": actor,
            "comment": comment or "Page deleted",
            "timestamp": _now(),
        })
        return True


def page_history(root: str, slug: str = "") -> list[dict[str, Any]]:
    """Return immutable version history for one or all knowledge pages."""
    path = _history_file(root)
    rows = _jsonl.read_rows(path)
    if not slug:
        return rows
    try:
        clean = _sanitize_slug(slug)
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("slug") == clean]


def search_pages(root: str, query: str, limit: int = 10) -> list[KnowledgePage]:
    """Search knowledge pages by slug, title, tags, or markdown body."""
    q_words = [w.lower() for w in re.findall(r"\w+", query) if len(w) >= 2]
    if not q_words:
        return list_pages(root, limit=limit)

    scored: list[tuple[int, KnowledgePage]] = []
    for page in list_pages(root, limit=500):
        text = f"{page.slug} {page.title} {' '.join(page.tags)} {page.content}".lower()
        score = sum(1 for w in q_words if w in text)
        if score > 0:
            scored.append((score, page))

    scored.sort(key=lambda x: (x[0], x[1].updated_at), reverse=True)
    return [p for _s, p in scored[:limit]]
