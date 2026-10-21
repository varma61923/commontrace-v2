"""UniMem bounded raw-store residue scans and conditional byte-verification receipts.

Fingerprint bytes stay in RAM. Receipts commit only hashes and store outcomes.
A scan covers the enumerated local files at its recorded observation, not remote
replicas, snapshots, storage-device remanence or future writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from unimem import change_stream, fact_store, memory_authority, paths, transparency_log
from unimem import frontier_protocol as fp
from unimem import nextwave_store as store

MAX_FILE = 64 * fp.MAX_BYTES
MAX_TOTAL = 256 * fp.MAX_BYTES
SUFFIXES = {
    ".json",
    ".jsonl",
    ".md",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".wal",
    ".shm",
    ".index",
    ".bin",
    ".hnsw",
    ".arrow",
    ".parquet",
    ".lance",
    ".npy",
    ".npz",
    ".yaml",
    ".yml",
    ".toml",
    ".txt",
    ".ini",
}


def _patterns(strings: Sequence[str], embeddings: Sequence[str], ngram: int) -> tuple[list[bytes], dict[bytes, bytes]]:
    fp.count(len(strings) + len(embeddings), 1, 256)
    fp.count(ngram, 8, 128)
    exact = []
    hashed = {}
    for text in strings:
        fp.text(text, "erasure fingerprint", 8192)
        if len(text.encode()) < 8:
            raise ValueError("UniMem erasure strings must contain at least eight UTF-8 bytes")
        encodings = {
            text.encode(),
            text.encode("utf-16-le"),
            text.encode("utf-16-be"),
            json.dumps(text, ensure_ascii=False)[1:-1].encode(),
            json.dumps(text, ensure_ascii=True)[1:-1].encode(),
        }
        for raw in encodings:
            exact.append(raw)
            for i in range(max(0, len(raw) - ngram + 1)):
                gram = raw[i : i + ngram]
                hashed[hashlib.sha256(gram).digest()] = gram
                fp.count(len(hashed), 0, 4096, "UniMem n-gram fingerprint count")
    for pattern in embeddings:
        if not isinstance(pattern, str) or len(pattern) > 65536:
            raise ValueError("UniMem embedding fingerprint must be bounded hexadecimal bytes")
        raw = bytes.fromhex(pattern)
        if len(raw) < 8:
            raise ValueError("UniMem embedding fingerprint is too short")
        exact.append(raw)
    return exact, hashed


def _scan_file(filename: Path, exact: Sequence[bytes], hashed: dict[bytes, bytes], ngram: int) -> dict[str, Any]:
    longest = max(max(map(len, exact)), ngram)
    offset, overlap = 0, b""
    opaque = False
    hits: set[str] = set()
    digest = hashlib.sha256()
    with filename.open("rb") as stream:
        while True:
            block = stream.read(65536)
            if not block:
                break
            offset += len(block)
            if offset > MAX_FILE:
                raise ValueError("store grew beyond the per-file scan bound")
            digest.update(block)
            window = overlap + block
            opaque |= b"subject-envelope-v1" in window
            hits.update(hashlib.sha256(p).hexdigest() for p in exact if p in window)
            # Plaintext fragments exist only in this RAM workspace. Built-in
            # byte search avoids hashing every byte of a large vector file;
            # only the authenticated fingerprint hashes enter the report.
            hits.update(commitment.hex() for commitment, gram in hashed.items() if gram in window)
            overlap = window[-(longest - 1) :]
    return {
        "status": "residue" if hits else "scanned",
        "bytes": offset,
        "store_digest": digest.hexdigest(),
        "fingerprint_hits": sorted(hits),
        "opaque_encoding": opaque,
    }


def _inventory(root: str) -> list[Path]:
    """Bound directory traversal before materializing an inventory; never follow links."""
    found: list[Path] = []
    directories = 0
    for base in (Path(paths.memory_dir(root)), Path(paths.traces_dir(root)), Path(root) / "skills"):
        if base.is_symlink():
            found.append(base)
            continue
        for directory, subdirs, filenames in os.walk(base, followlinks=False):
            directories += 1
            fp.count(directories, 0, 2048, "UniMem local scan directory count")
            linked = [name for name in subdirs if (Path(directory) / name).is_symlink()]
            subdirs[:] = [name for name in subdirs if name not in linked]
            found.extend(Path(directory) / name for name in [*filenames, *linked])
            fp.count(len(found), 0, 512, "UniMem local scan file count")
    return sorted(set(found))


def scan(
    root: str,
    *,
    strings: Sequence[str],
    embeddings: Sequence[str] = (),
    ngram: int = 32,
    vacuum: bool = False,
    files: Sequence[str] = (),
) -> dict[str, Any]:
    store.operator()
    if type(vacuum) is not bool:
        raise ValueError("UniMem vacuum flag must be boolean")
    root = os.path.realpath(root)
    exact, hashed = _patterns(strings, embeddings, ngram)
    if files:
        fp.count(len(files), 1, 2048)
        candidates = [Path(root) / fp.text(f, "store filename", 1024) for f in files]
    else:
        candidates = _inventory(root)
    fp.count(len(candidates), 1, 512, "UniMem local scan file count")
    results, total = [], 0
    if vacuum:
        fact_store.close_connections(root)
    for filename in sorted(set(candidates)):
        if os.path.commonpath([os.path.abspath(root), os.path.abspath(filename)]) != os.path.abspath(root):
            raise ValueError("UniMem scan file is outside the local store")
        relative = os.path.relpath(filename, root)
        if filename.name.endswith((".lock", ".key")) or filename.name.startswith(".origin-"):
            continue
        row: dict[str, Any] = {"file": relative}
        current = Path(root)
        has_symlink = False
        for component in filename.relative_to(root).parts:
            current = current / component
            if current.is_symlink():
                has_symlink = True
                break
        if has_symlink:
            row.update(status="unverified", reason="symlink")
        elif not filename.is_file():
            row.update(status="unverified", reason="unavailable")
        elif filename.suffix not in SUFFIXES and not filename.name.endswith(("-wal", "-shm")):
            row.update(status="unverified", reason="unsupported store format")
        else:
            paths.enforce_boundary(root, str(filename))
            size = filename.stat().st_size
            if size > MAX_FILE or total + size > MAX_TOTAL:
                row.update(status="unverified", reason="bounded scan size exceeded")
            else:
                try:
                    if vacuum and filename.suffix in {".db", ".sqlite", ".sqlite3"}:
                        db = sqlite3.connect(str(filename), timeout=1)
                        try:
                            db.execute("PRAGMA secure_delete=ON")
                            db.execute("VACUUM")
                            if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                                raise ValueError("active readers prevented SQLite WAL truncation")
                        finally:
                            db.close()
                    before = filename.stat()
                    row.update(_scan_file(filename, exact, hashed, ngram))
                    after = filename.stat()
                    if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns, before.st_size) != (
                        after.st_dev,
                        after.st_ino,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                        after.st_size,
                    ):
                        row.update(status="unverified", reason="store changed during its byte scan")
                    if row.get("opaque_encoding"):
                        row.update(status="unverified", reason="encrypted envelopes require key-destruction evidence")
                    total += row["bytes"]
                    if row["status"] == "scanned" and filename.suffix in {".parquet", ".lance", ".arrow", ".npz"}:
                        row.update(
                            status="unverified", reason="compressed/container codec requires a decoded-store adapter"
                        )
                except (OSError, ValueError, sqlite3.Error) as exc:
                    row.update(status="unverified", reason=type(exc).__name__)
        results.append(row)
    if not files:
        if set(candidates) != set(_inventory(root)):
            results.append(
                {"file": "inventory", "status": "unverified", "reason": "store inventory changed during scan"}
            )
        for identity, metadata in store.latest(root, "tier-namespaces").items():
            if metadata.get("status") != "active":
                continue
            config = store.get(root, "tier-namespaces", identity, context=metadata["scopes"], owner=True)
            if config.get("backend") != "local":
                results.append(
                    {
                        "file": "remote:" + config["backend"] + ":" + identity,
                        "status": "unverified",
                        "reason": "remote object versions require a verification adapter",
                    }
                )
    report = {
        "type": "unimem.byte-erasure-scan",
        "version": 1,
        "at": fp.now(),
        "stores": results,
        "fingerprint_commitments": sorted(hashlib.sha256(p).hexdigest() for p in exact),
        "ngram": ngram,
        "fingerprint_source": "operator-supplied original content patterns",
        "pattern_scope": "UTF-8, UTF-16 and JSON-escaped text; supplied raw embedding bytes",
        "bytes_scanned": total,
        "unverified": [r["file"] for r in results if r["status"] == "unverified"],
        "residue": [r["file"] for r in results if r["status"] == "residue"],
        "scope": "enumerated local file bytes only; remote stores and backups not covered",
    }
    report["passed"] = bool(results) and not report["unverified"] and not report["residue"]
    return report


def verify(
    root: str,
    identities: Sequence[str],
    *,
    strings: Sequence[str],
    embeddings: Sequence[str] = (),
    ngram: int = 32,
    vacuum: bool = True,
    files: Sequence[str] = (),
) -> dict[str, Any]:
    from unimem import _jsonl

    change_stream.recover_pending(root)
    with _jsonl.locked(fp.filename(root, change_stream.JOURNAL) + ".delivery"):
        return _verify(
            root, identities, strings=strings, embeddings=embeddings, ngram=ngram, vacuum=vacuum, files=files
        )


def _verify(
    root: str,
    identities: Sequence[str],
    *,
    strings: Sequence[str],
    embeddings: Sequence[str] = (),
    ngram: int = 32,
    vacuum: bool = True,
    files: Sequence[str] = (),
) -> dict[str, Any]:
    store.operator()
    fp.count(len(identities), 1, 256)
    ids = sorted(set(fp.text(i) for i in identities))
    if any(not memory_authority.lineage_blocked(root, i, canonical_only=True) for i in ids):
        raise PermissionError("UniMem verified erasure requires prior canonical revocation")
    if files:
        raise ValueError("UniMem receipts require automatic inventory; use scan for a file subset")
    obligations = change_stream.obligations(root, targets=ids)
    report = scan(root, strings=strings, embeddings=embeddings, ngram=ngram, vacuum=vacuum, files=files)
    if not report["passed"] or not obligations["all_acknowledged"]:
        return {"status": "unverified", "scan": report, "acknowledgements": obligations, "receipt": None}
    statement = {
        "type": "unimem.byte-verified-erasure",
        "version": 1,
        "ids": ids,
        "scan": report,
        "downstream": "authenticated acknowledgements; remote bytes not inspected",
        "status": "purged and verified within enumerated local stores",
        "classification": "conditional absence of operator-supplied byte fingerprints",
    }
    signed = fp.sign(root, statement)
    anchor = transparency_log.append(root, "byte-verified-erasure", {"receipt_digest": fp.digest(signed)})
    fp.append(root, "byte-erasure-receipts.jsonl", {"receipt": signed, "anchor": anchor})
    return {"status": statement["status"], "receipt": signed, "anchor": anchor}
