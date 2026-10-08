"""Write-time origin binding and action-specific filtering for governed facts.

The store operator owns the local key. Text and scope labels cannot nominate
their authority; a gateway supplies the authenticated writer context instead.
Derived facts inherit the minimum trust of their authenticated source records.
"""
from __future__ import annotations

import os
import secrets
from contextlib import contextmanager
from contextvars import ContextVar

from commontrace import _jsonl, origin, paths

TRUST = {"external": 0, "tool": 1, "agent": 1, "user": 2, "operator": 3}
WRITER: ContextVar[tuple[str, str]] = ContextVar("commontrace_memory_writer", default=("local", "operator"))


@contextmanager
def writer(principal: str, authority: str):
    if authority not in TRUST or not isinstance(principal, str) or not principal:
        raise ValueError("invalid authenticated writer")
    token = WRITER.set((principal, authority))
    try:
        yield
    finally:
        WRITER.reset(token)


def _key(root: str, *, create: bool = False) -> bytes | None:
    filename = os.path.join(paths.memory_dir(root), ".origin-key")
    paths.enforce_boundary(root, filename)
    if create:
        paths.safe_prepare_output_path(filename)
        with _jsonl.locked(filename):
            try:
                fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(secrets.token_bytes(32))
                    fh.flush()
                    os.fsync(fh.fileno())
    try:
        if os.path.islink(filename):
            raise ValueError("origin key must not be a symlink")
        with open(filename, "rb") as fh:
            key = fh.read(33)
    except FileNotFoundError:
        return None
    if len(key) != 32:
        raise ValueError("invalid origin key")
    return key


def trace_record(trace: dict, *, context: str | None = None, solution: str | None = None) -> dict:
    """Bind content and metadata that determine evidence eligibility and outcomes."""
    return {"id": trace.get("id"),
            "context": trace.get("context_text", "") if context is None else context,
            "solution": trace.get("solution_text", "") if solution is None else solution,
            "scopes": trace.get("scopes", []), "outcome": trace.get("outcome", {}),
            "expires": trace.get("expires"), "expires_at": trace.get("expires_at"),
            "valid_from": trace.get("valid_from"),
            "valid_until": trace.get("valid_until"),
            "procedure": trace.get("extensions", {}).get("profile", {}).get("procedure", {})}


def fact_record(fact) -> dict:
    return {name: getattr(fact, name) for name in ("id", "statement", "category", "scopes", "valid_from",
            "valid_until", "expires_at", "source_traces", "memory_type")}


def verify(root: str, receipt: dict, record: dict | None = None) -> bool:
    key = _key(root)
    if key is None or receipt.get("authority") not in TRUST:
        return False
    principal = origin.Principal(receipt.get("principal", ""), "local-store", receipt["authority"], key)
    return (origin.verify(receipt, {principal.id: principal})
            and (record is None or receipt.get("record") == record))


def bind(root: str, record: dict, *, sources: list[str] | None = None) -> dict:
    principal_id, authority = WRITER.get()
    key = _key(root, create=True)
    assert key is not None
    ledger = os.path.join(paths.memory_dir(root), "origins.jsonl")
    with _jsonl.locked(ledger):
        prior = {r["record"].get("id"): r for r in _jsonl.read_rows(ledger) if verify(root, r)}
        inherited = [prior.get(source) for source in sources or []]
        if inherited:
            ranks = [TRUST[r["authority"]] if r else 0 for r in inherited]
            minimum = min(TRUST[authority], *ranks)
            authority = next(name for name in ("external", "agent", "user", "operator") if TRUST[name] == minimum)
        signed = origin.bind(record, origin.Principal(principal_id, "local-store", authority, key))
        _jsonl.append_row(ledger, signed)
    return signed


def permits(root: str, fact, *, action_class: str = "") -> bool:
    return permits_record(root, fact.origin, fact_record(fact), action_class=action_class)


def permits_record(root: str, receipt: dict, record: dict, *, action_class: str = "") -> bool:
    if not receipt and previously_bound(root, record.get("id", "")):
        return False  # Removing a receipt is not a migration back to unsigned legacy data.
    if receipt and not verify(root, receipt, record):
        return False
    if not action_class:
        return True
    import yaml

    filename = os.path.join(paths.memory_dir(root), "authority-policy.yaml")
    try:
        with open(filename, encoding="utf-8") as fh:
            policy = yaml.safe_load(fh)
    except FileNotFoundError:
        return True
    if not isinstance(policy, dict) or not isinstance(policy.get("actions"), dict):
        raise ValueError("authority policy requires an actions mapping")
    minimum = policy["actions"].get(action_class, policy.get("default", "operator"))
    if minimum not in TRUST:
        raise ValueError("unknown minimum origin authority")
    return TRUST.get(receipt.get("authority", "external"), 0) >= TRUST[minimum]


def previously_bound(root: str, record_id: str) -> bool:
    return any(row.get("record", {}).get("id") == record_id for row in _jsonl.read_rows(
               os.path.join(paths.memory_dir(root), "origins.jsonl")))
