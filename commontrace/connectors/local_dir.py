"""Local-directory connector: mtime file-watcher scan -> markdown chunker.

Scans ``*.md`` files under a source directory, processes only files whose
mtime/size changed since the last state token, chunks them with the shared
ingest markdown chunker, writes atomic facts + provenance (unless dry-run),
and checkpoints an opaque state token in git-tracked JSON.
"""
from __future__ import annotations

import os
from typing import Any

from commontrace.connectors.base import (
    Connector,
    SyncResult,
    categorize_chunk,
    decode_state_token,
    encode_state_token,
    load_connector_state,
    new_run_id,
    register_connector,
    save_connector_state,
)
from commontrace.ingest import IngestionResult


def _scan_markdown_files(source_dir: str) -> dict[str, dict[str, float]]:
    """Map relpath -> {mtime, size} for ``*.md`` files under source_dir."""
    out: dict[str, dict[str, float]] = {}
    for dirpath, _dirs, filenames in os.walk(source_dir):
        for fname in filenames:
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                st = os.stat(fpath)
            except OSError:
                continue
            rel = os.path.relpath(fpath, source_dir)
            out[rel] = {"mtime": float(st.st_mtime), "size": float(st.st_size)}
    return out


def _changed_files(
    previous: dict[str, Any], current: dict[str, dict[str, float]]
) -> list[str]:
    changed: list[str] = []
    prev = previous if isinstance(previous, dict) else {}
    for rel in sorted(current):
        cur = current[rel]
        old = prev.get(rel)
        if not isinstance(old, dict):
            changed.append(rel)
        elif old.get("mtime") != cur.get("mtime") or old.get("size") != cur.get("size"):
            changed.append(rel)
    return changed


@register_connector
class LocalDirConnector(Connector):
    """Watch a local directory of Markdown files."""

    name = "local_dir"

    def __init__(self, source_dir: str = "", scope: str = "", max_files: int = 200):
        self.source_dir = source_dir
        self.scope = scope
        self.max_files = max_files

    def state_key(self) -> str:
        base = os.path.abspath(self.source_dir) if self.source_dir else "default"
        return "local_dir:%s" % base

    def _key_for(self, source_dir: str) -> str:
        base = os.path.abspath(source_dir) if source_dir else "default"
        return "local_dir:%s" % base

    def authorize(self, credentials: dict[str, Any] | None = None) -> dict[str, Any]:
        target = (credentials or {}).get("source_dir", self.source_dir) if credentials else self.source_dir
        if not target:
            return {"ok": False, "connector": self.name, "reason": "no source_dir configured"}
        if not os.path.isdir(target):
            return {"ok": False, "connector": self.name, "reason": "not a directory: %r" % target}
        if not os.access(target, os.R_OK):
            return {"ok": False, "connector": self.name, "reason": "not readable: %r" % target}
        return {"ok": True, "connector": self.name, "source_dir": os.path.abspath(target)}

    def _chunk_file(self, path: str) -> list:
        from commontrace import ingest as _ingest

        try:
            chunks = _ingest._chunk_markdown(path)
        except Exception:
            chunks = []
        if chunks:
            return chunks
        # Fallback: chunk raw text so binary/unusual files still yield something.
        from commontrace.connectors.base import chunk_markdown_text

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return []
        if len(text.strip()) < 50:
            return []
        return chunk_markdown_text(text, path)

    def sync(
        self,
        root: str,
        state_token: str | None = None,
        *,
        scope: str = "",
        run_id: str = "",
        dry_run: bool = False,
        source_dir: str | None = None,
        max_files: int | None = None,
        **kwargs: Any,
    ) -> SyncResult:
        from commontrace import hierarchical
        from commontrace import provenance as _prov

        src = source_dir if source_dir is not None else self.source_dir
        active_scope = scope if scope != "" else self.scope
        limit = max_files if max_files is not None else self.max_files
        rid = run_id or new_run_id()
        result = IngestionResult(source_path=src or "", source_type="local_dir")

        if not src or not os.path.isdir(src):
            result.errors.append("local_dir: source directory not found: %r" % (src,))
            return SyncResult(
                connector=self.name, chunks=[], result=result,
                new_state_token=state_token or "", run_id=rid, dry_run=dry_run,
            )

        key = self._key_for(src)
        cursor: dict[str, Any] = {}
        if state_token:
            cursor = decode_state_token(state_token)
        if not cursor:
            entry = load_connector_state(root, key)
            if entry.get("state_token") and not state_token:
                cursor = decode_state_token(entry.get("state_token", ""))
            if not cursor and isinstance(entry.get("cursor"), dict):
                cursor = dict(entry["cursor"])
        previous_files = cursor.get("files", {})
        if not isinstance(previous_files, dict):
            previous_files = {}

        current = _scan_markdown_files(src)
        changed = _changed_files(previous_files, current)[: int(limit)]

        chunks: list = []
        for rel in changed:
            fpath = os.path.join(src, rel)
            try:
                chunks.extend(self._chunk_file(fpath))
            except Exception as exc:
                result.errors.append("chunk error %s: %s" % (rel, exc))
        result.chunks_extracted = len(chunks)

        if chunks and not dry_run:
            for chunk in chunks:
                statement = "%s: %s" % (chunk.breadcrumb, chunk.content[:200])
                statement = statement.strip()
                if len(statement) > 30:
                    try:
                        hierarchical.add_fact(
                            root,
                            statement=statement[:500],
                            category=categorize_chunk(chunk.breadcrumb),
                            scopes=[active_scope] if active_scope else None,
                            confidence=0.7,
                        )
                        result.facts_written += 1
                    except Exception as exc:
                        result.errors.append("fact error: %s" % exc)
                try:
                    _prov.append_provenance(
                        root,
                        target_kind="chunk",
                        target_id=chunk.chunk_id,
                        source_path=chunk.source_path,
                        run_id=rid,
                        detail={
                            "connector": self.name,
                            "breadcrumb": chunk.breadcrumb,
                            "chunk_type": chunk.chunk_type,
                        },
                    )
                except Exception as exc:
                    result.errors.append("provenance error: %s" % exc)

        new_cursor = {"files": current}
        new_token = encode_state_token(new_cursor)
        if not dry_run:
            try:
                save_connector_state(root, key, new_token, new_cursor)
            except Exception as exc:
                result.errors.append("state persist error: %s" % exc)
        return SyncResult(
            connector=self.name, chunks=chunks, result=result,
            new_state_token=new_token, run_id=rid, dry_run=dry_run,
        )

    def webhook_handler(self, root: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"ok": False, "connector": self.name, "reason": "payload must be a dict"}
        rel = str(payload.get("path", payload.get("relpath", ""))).strip()
        if not rel:
            return {"ok": False, "connector": self.name, "reason": "missing 'path'"}
        base = os.path.abspath(self.source_dir) if self.source_dir else ""
        candidate = os.path.abspath(os.path.join(base or os.getcwd(), rel))
        if base and not (candidate == base or candidate.startswith(base + os.sep)):
            return {"ok": False, "connector": self.name, "reason": "path escapes source_dir"}
        return {"ok": True, "connector": self.name, "path": candidate, "run_id": payload.get("run_id", "")}
