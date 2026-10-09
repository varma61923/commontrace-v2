"""Append-only Markdown store protocol with rebuildable, interchangeable indexes.

Indexes never decide authority or validity: reads resolve and verify the current
canonical revision before returning it. Existing fact and trace formats remain
compatible; adapters can index those through rebuild(records).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Protocol

from commontrace import _jsonl, frontmatter, memory_authority, memory_control, paths, ttl


class Store(Protocol):
    def append(self, record: dict) -> dict: ...
    def get(self, record_id: str) -> dict | None: ...
    def all(self) -> list[dict]: ...
    def rebuild(self) -> int: ...


class FilesStore:
    def __init__(self, root: str, *, context: list[str] | None = None):
        self.root = root
        self.context = list(context or [])
        self.directory = os.path.join(paths.memory_dir(root), "records")
        paths.enforce_boundary(root, self.directory)

    def append(self, record: dict) -> dict:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
            raise ValueError("record requires an id")
        if not isinstance(record.get("text"), str) or not record["text"].strip():
            raise ValueError("record requires text")
        from commontrace import memory_guard

        record = {k: v for k, v in record.items() if k != "origin"}
        clean, _ = memory_guard.sanitize_metadata(record, pii=memory_guard.privacy_redaction_enabled())
        row = {**clean, "text": clean["text"].strip(), "revision": uuid.uuid4().hex,
               "recorded_at": datetime.now(timezone.utc).isoformat(), "record_kind": "markdown-store"}
        receipt = memory_authority.bind(self.root, row, sources=row.get("source_traces", []))
        filename = os.path.join(self.directory, row["revision"]+".md")
        paths.enforce_boundary(self.root, filename)
        with _jsonl.locked(self.directory):
            fm = {k: v for k, v in row.items() if k != "text"}
            frontmatter.write(filename, {**fm, "origin": receipt}, row["text"]+"\n")
        return {**row, "origin": receipt}

    def all(self) -> list[dict]:
        latest = {}
        if not os.path.isdir(self.directory):
            return []
        for filename in sorted(os.listdir(self.directory)):
            if not filename.endswith(".md"):
                continue
            path = os.path.join(self.directory, filename)
            paths.enforce_boundary(self.root, path)
            fm, text = frontmatter.read(path)
            row = {**fm, "text": text.rstrip("\n")}
            # The signed revision timestamp determines order, not filesystem mtime.
            stamp = row["recorded_at"]
            if row["id"] not in latest or (stamp, filename) > latest[row["id"]][0]:
                latest[row["id"]] = ((stamp, filename), row)
        live = []
        for _stamp, row in latest.values():
            record = {k: v for k, v in row.items() if k != "origin"}
            if (ttl.trace_is_live(row) and memory_control.matches(row.get("scopes", []), self.context)
                    and memory_authority.permits_record(self.root, row.get("origin", {}), record)):
                live.append(row)
        return live

    def get(self, record_id: str) -> dict | None:
        return next((r for r in self.all() if r["id"] == record_id), None)

    def rebuild(self) -> int:
        return len(self.all())


class SQLiteStore(FilesStore):
    def __init__(self, root: str, *, context: list[str] | None = None):
        super().__init__(root, context=context)
        scope = hashlib.sha256(json.dumps(self.context).encode()).hexdigest()[:16]
        self.index = os.path.join(paths.memory_dir(root), "records-"+scope+".sqlite3")
        paths.enforce_boundary(root, self.index)

    def rebuild(self) -> int:
        records = self.all()
        paths.safe_prepare_output_path(self.index)
        with _jsonl.locked(self.index), sqlite3.connect(self.index) as db:
            db.execute("CREATE TABLE IF NOT EXISTS records "
                       "(id TEXT PRIMARY KEY, digest TEXT NOT NULL, text TEXT NOT NULL)")
            db.execute("DELETE FROM records")
            db.executemany("INSERT INTO records VALUES (?,?,?)", [
                (r["id"], hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest(), r["text"])
                for r in records])
        return len(records)

    def search(self, query: str, *, limit: int = 10) -> list[dict]:
        if not os.path.isfile(self.index):
            self.rebuild()
        with sqlite3.connect(self.index) as db:
            ids = [row[0] for row in db.execute("SELECT id FROM records WHERE text LIKE ? LIMIT ?",
                                               ("%"+query+"%", limit))]
        live = {r["id"]: r for r in self.all()}
        return [live[i] for i in ids if i in live and query.casefold() in live[i]["text"].casefold()]
