"""UniMem bounded optimistic team concurrency primitives, without I/O.

Read-set validation follows optimistic concurrency control (Kung and Robinson,
1981, doi:10.1145/319566.319567). Serializable mode protects declared point
reads, not undeclared predicate reads. Snapshot mode checks write/write
conflicts; sequenced mode additionally requires a live fenced work-item lease.
Contradictions must be tested before dedup (MemClaw, arXiv:2606.24535).
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

LEVELS = ("none", "snapshot", "serializable", "sequenced")
REQUEST: ContextVar[dict[str, Any] | None] = ContextVar("unimem_team_request", default=None)
MAX_READS = 256


def transaction_time(record: Mapping[str, Any]) -> str:
    """Bind transaction timestamp and exact body, without changing record hashes."""
    raw = json.dumps(dict(record), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    if len(raw) > 256 * 1024:
        raise ValueError("UniMem team fact exceeds 256 KiB")
    return "tx1:" + hashlib.sha256(raw).hexdigest()


def read_set(value: Any) -> dict[str, str]:
    if not isinstance(value, list) or len(value) > MAX_READS:
        raise ValueError("UniMem read_set must contain at most 256 version pairs")
    result: dict[str, str] = {}
    for pair in value:
        if not isinstance(pair, dict) or set(pair) != {"id", "transaction_time"}:
            raise ValueError("UniMem read_set entries require id and transaction_time")
        identity, version = pair["id"], pair["transaction_time"]
        if not isinstance(identity, str) or not 0 < len(identity) <= 256 or any(ord(c) < 32 for c in identity):
            raise ValueError("UniMem invalid read_set fact ID")
        if not isinstance(version, str) or re.fullmatch(r"tx1:[0-9a-f]{64}", version) is None:
            raise ValueError("UniMem invalid transaction_time")
        if identity in result:
            raise ValueError("UniMem read_set repeats a fact")
        result[identity] = version
    return result


def stale(declared: Mapping[str, str], current: Mapping[str, str]) -> list[str]:
    return sorted(identity for identity, version in declared.items() if current.get(identity) != version)


class StaleGeneration(ValueError):
    """A reviewable conflict, whose public details have already been scoped."""

    def __init__(self, superseding: list[dict[str, Any]] | None = None, *, reason: str = "read_set_changed") -> None:
        self.details: dict[str, Any] = {"code": "STALE_GENERATION", "reason": reason,
                                      "superseding_facts": superseding or [], "options": ["regenerate", "merge"]}
        super().__init__("STALE_GENERATION: regenerate from the current authorized facts or submit a reviewable merge")
