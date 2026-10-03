"""Local-directory connector: re-ingest the Markdown files that changed since the last sync."""
from __future__ import annotations

import os
from typing import Any

from commontrace.connectors.base import (
    Connector,
    SyncResult,
    encode_state_token,
    new_run_id,
    record_chunks,
    resolve_cursor,
    save_connector_state,
)
from commontrace.ingest import IngestionResult, _chunk_markdown, _walk_files


def _scan_markdown_files(source_dir: str) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for fpath in _walk_files(source_dir, (".md", ".markdown"), None):
        try:
            st = os.stat(fpath)
        except OSError:
            continue
        out[os.path.relpath(fpath, source_dir)] = {"mtime": float(st.st_mtime), "size": float(st.st_size)}
    return out


def _changed_files(previous: dict[str, Any], current: dict[str, dict[str, float]]) -> list[str]:
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

        key = self._key_for(src)
        previous = resolve_cursor(root, key, state_token).get("files", {})
        previous = previous if isinstance(previous, dict) else {}
        current = _scan_markdown_files(src)
        changed = _changed_files(previous, current)
        processed = changed[:limit]
        deleted = [rel for rel in previous if rel not in current]

        chunks: list = []
        per_file: list[tuple[str, list]] = []
        for rel in processed:
            file_chunks = _chunk_markdown(os.path.join(src, rel))
            chunks.extend(file_chunks)
            per_file.append((rel, file_chunks))
        result.chunks_extracted = len(chunks)

        new_files = {rel: stamp for rel, stamp in previous.items() if rel in current}
        for rel in processed:
            new_files[rel] = current[rel]
        new_cursor = {"files": new_files}
        new_token = encode_state_token(new_cursor)
        if not dry_run:
            base = os.path.abspath(src)
            for rel, file_chunks in per_file:
                record_chunks(root, file_chunks, source_id=f"local_dir:{os.path.join(base, rel)}",
                              scope=active_scope, run_id=rid, connector=self.name, result=result)
            for rel in deleted:
                record_chunks(root, [], source_id=f"local_dir:{os.path.join(base, rel)}",
                              scope=active_scope, run_id=rid, connector=self.name, result=result)
            try:
                save_connector_state(root, key, new_token, new_cursor)
            except OSError as exc:
                result.errors.append("state persist error: %s" % exc)
        return SyncResult(connector=self.name, chunks=chunks, result=result,
                          new_state_token=new_token, run_id=rid, dry_run=dry_run,
                          pending=len(changed) - len(processed))
