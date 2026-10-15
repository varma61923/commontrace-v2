"""Local-directory connector: re-ingest the Markdown files that changed since the last sync."""
from __future__ import annotations

import os
from typing import Any

from commontrace.connectors.base import (
    Connector,
    SyncResult,
    chunk_markdown_text,
    encode_state_token,
    load_connector_state,
    new_run_id,
    record_chunks,
    resolve_cursor,
    save_connector_state,
    scoped_key,
)
from commontrace.ingest import IngestionResult, _read_text, _walk_files


def _file_stamp(st: os.stat_result) -> dict[str, int]:
    return {"mtime_ns": st.st_mtime_ns, "size": st.st_size, "ctime_ns": st.st_ctime_ns,
            "dev": st.st_dev, "inode": st.st_ino}


def _scan_markdown_files(source_dir: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for fpath in _walk_files(source_dir, (".md", ".markdown"), None, strict=True):
        st = os.stat(fpath)
        out[os.path.relpath(fpath, source_dir)] = _file_stamp(st)
    return out


def _changed_files(previous: dict[str, Any], current: dict[str, dict[str, int]]) -> list[str]:
    return [rel for rel in sorted(current) if previous.get(rel) != current[rel]]


class LocalDirConnector(Connector):
    """Watch a local directory of Markdown files."""

    name = "local_dir"

    def __init__(self, source_dir: str = "", scope: str = "", max_files: int = 200):
        self.source_dir = source_dir
        self.scope = scope
        self.max_files = max_files

    @staticmethod
    def _key_for(source_dir: str) -> str:
        return "local_dir:%s" % (os.path.abspath(source_dir) if source_dir else "default")

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
        """Ingest changed files (up to *max_files*); the rest stay pending for the next sync."""
        src = source_dir if source_dir is not None else self.source_dir
        active_scope = scope or self.scope
        limit = max(0, int(max_files if max_files is not None else self.max_files))
        rid = run_id or new_run_id()
        result = IngestionResult(source_path=src or "", source_type="local_dir")
        if not src or not os.path.isdir(src):
            result.errors.append("local_dir: source directory not found: %r" % (src,))
            return SyncResult(connector=self.name, result=result, new_state_token=state_token or "",
                              run_id=rid, dry_run=dry_run)

        key = scoped_key(self._key_for(src), active_scope)
        previous = resolve_cursor(root, key, state_token, scope=active_scope).get("files", {})
        previous = previous if isinstance(previous, dict) else {}
        # Old checkpoints were shared across scopes. Carry every former source
        # into migration with a nonmatching stamp, even across failed scans.
        # Current files reingest; removed files retire only within this scope.
        if active_scope and not previous and not state_token and not load_connector_state(root, key):
            legacy = load_connector_state(root, self._key_for(src)).get("cursor", {}).get("files", {})
            if isinstance(legacy, dict):
                previous = {rel: None for rel in legacy}
        try:
            current = _scan_markdown_files(src)
        except OSError as exc:
            result.errors.append("local_dir: incomplete source scan: %s" % exc)
            cursor = {"files": previous, "scope": active_scope, "connector_key": key}
            return SyncResult(connector=self.name, result=result, new_state_token=encode_state_token(cursor),
                              run_id=rid, dry_run=dry_run, pending=max(1, len(previous)))
        changed = _changed_files(previous, current)
        processed = changed[:limit]
        deleted = [rel for rel in previous if rel not in current]

        chunks: list = []
        per_file: list[tuple[str, list]] = []
        for rel in processed:
            path = os.path.join(src, rel)
            try:
                file_chunks = chunk_markdown_text(_read_text(path), path)
                if _file_stamp(os.stat(path)) != current[rel]:
                    raise ValueError("source changed during read; retry required")
            except (OSError, ValueError) as exc:
                result.errors.append("local_dir: read failed for %r: %s" % (rel, exc))
                continue
            chunks.extend(file_chunks)
            per_file.append((rel, file_chunks))
        result.chunks_extracted = len(chunks)

        # Keep failed deletions as well as failed updates pending. A checkpoint
        # records acknowledged writes, never merely discovered source changes.
        new_files = dict(previous)
        if not dry_run:
            base = os.path.abspath(src)
            for rel, file_chunks in per_file:
                if record_chunks(root, file_chunks,
                                 source_id=f"local_dir:{os.path.join(base, rel)}",
                                 scope=active_scope, run_id=rid, connector=self.name, result=result):
                    new_files[rel] = current[rel]
            for rel in deleted:
                if record_chunks(root, [],
                                 source_id=f"local_dir:{os.path.join(base, rel)}",
                                 scope=active_scope, run_id=rid, connector=self.name, result=result):
                    new_files.pop(rel, None)
        else:
            new_files = {rel: stamp for rel, stamp in previous.items() if rel in current}
            new_files.update({rel: current[rel] for rel, _chunks in per_file})
        new_cursor = {"files": new_files, "scope": active_scope, "connector_key": key}
        new_token = encode_state_token(new_cursor)
        if not dry_run:
            try:
                save_connector_state(root, key, new_token, new_cursor)
            except OSError as exc:
                result.errors.append("state persist error: %s" % exc)
        return SyncResult(connector=self.name, chunks=chunks, result=result,
                          new_state_token=new_token, run_id=rid, dry_run=dry_run,
                          pending=len(_changed_files(new_files, current))
                          + sum(rel not in current for rel in new_files))
