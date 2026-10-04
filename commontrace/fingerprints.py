"""Single home for content fingerprinting and Hub request hashes (stdlib only).

Every content hash and request hash in commontrace and the Hub is computed here so
identical inputs always produce identical outputs. Import from this module instead
of reimplementing the formulas elsewhere.
"""
from __future__ import annotations

import hashlib
import json
import os

LAZY_HASH_BYTES = 256 * 1024 * 1024
SAMPLE_BYTES = 1024 * 1024


def normalize_text(text: str) -> str:
    """Lower-cased, whitespace-collapsed text used as the dedupe/hash key."""
    return " ".join(str(text).lower().split())


def content_hash(text: str) -> str:
    """sha256 of the normalised text; identical inputs hash identically across runs."""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def short_fingerprint(text: str) -> str:
    """16-hex-char id for chunk/file labels (a label, not a security boundary)."""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:16]


def md5_hex(text: str) -> str:
    """Non-security name/key helper (cache keys, dedupe ids); never for auth or integrity."""
    return hashlib.md5(str(text).encode("utf-8"), usedforsecurity=False).hexdigest()


def file_fingerprint(
    path: str,
    *,
    known_sizes: frozenset[int] = frozenset(),
    lazy_hash_bytes: int = LAZY_HASH_BYTES,
    sample_bytes: int = SAMPLE_BYTES,
) -> str:
    """sha256 of a file's bytes; a file over *lazy_hash_bytes* is sampled (size, head,
    tail) unless another file of the same size is known, when it is hashed in full."""
    size = os.path.getsize(path)
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        if size <= lazy_hash_bytes or size in known_sizes:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
            return "sha256:" + digest.hexdigest()
        digest.update(str(size).encode())
        digest.update(fh.read(sample_bytes))
        fh.seek(max(0, size - sample_bytes))
        digest.update(fh.read(sample_bytes))
    return "sample:" + digest.hexdigest()


def contribute_request_hash(
    title: str,
    context_text: str,
    solution_text: str,
    tags: list[str],
    agent_type: str = "",
    outcome: dict | None = None,
    profile: str = "",
) -> str:
    """Idempotency hash for a contribute-trace request (Hub server and client agree)."""
    parts = [title, context_text, solution_text, agent_type, "\x1f".join(sorted(tags))]
    if outcome:
        parts.append(json.dumps(outcome, sort_keys=True, ensure_ascii=False))
    if profile:
        parts.append(profile)
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()


def amend_request_hash(
    trace_id: str,
    title: str | None,
    context_text: str | None,
    solution_text: str | None,
    tags: list[str] | None,
    outcome: dict | None = None,
) -> str:
    """Idempotency hash for an amend-trace request (Hub server and client agree)."""
    payload = [
        trace_id, title, context_text, solution_text,
        sorted(tags) if tags is not None else None,
        outcome,
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def push_fingerprint(title: str, context_text: str, solution_text: str, tags: list[str]) -> str:
    """Fingerprint of a lesson push; equal fingerprints mean nothing changed since the last push."""
    parts = [title, context_text, solution_text, "\x1f".join(sorted(tags))]
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()


def trace_push_fingerprint(
    title: str, context_text: str, solution_text: str, tags: list[str], outcome: dict
) -> str:
    """Fingerprint of a trace push; equal fingerprints mean nothing changed since the last push."""
    parts = [
        title, context_text, solution_text, "\x1f".join(sorted(tags)),
        json.dumps(outcome or {}, sort_keys=True, ensure_ascii=False),
    ]
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()
