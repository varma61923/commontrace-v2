"""Persistent incremental materialization contracts for optional vector engines.

Private staged changes become visible through one fenced publication. Vector
versions use half-open revision intervals, as in multiversion concurrency
control (Bernstein and Goodman, ACM Computing Surveys 1981,
doi:10.1145/356842.356846). A live read lease protects its snapshot from garbage
collection; a build lease grants publication authority only against its base.
These are consistency mechanisms, not claims of algorithmic novelty.
"""
from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from commontrace.vector_store import MAX_BATCH, VectorHit, VectorRecord, _generation, _keys, _label, _vector


@dataclass(frozen=True)
class SnapshotHead:
    revision: int
    generation: str | None


@dataclass(frozen=True)
class BuildLease:
    token: str
    fence: int
    base_revision: int
    target_revision: int
    generation: str
    full: bool


@dataclass(frozen=True)
class ReadLease:
    token: str
    revision: int
    generation: str


class SnapshotConflict(RuntimeError):
    """A lease expired, a publication lost its CAS, or a snapshot was retired."""


class SnapshotBackend(Protocol):
    async def initialize(self) -> None: ...
    async def head(self) -> SnapshotHead: ...
    async def begin(self, target_revision: int, generation: str, *, full: bool = False,
                    lease_seconds: float = 60) -> BuildLease | None: ...
    async def stage(self, lease: BuildLease, records: Iterable[VectorRecord],
                    deleted_keys: Iterable[str] = ()) -> int: ...
    async def publish(self, lease: BuildLease) -> bool: ...
    async def abort(self, lease: BuildLease) -> None: ...
    async def pin(self, generation: str, *, lease_seconds: float = 60) -> ReadLease: ...
    async def renew(self, lease: ReadLease) -> None: ...
    async def release(self, lease: ReadLease) -> None: ...
    async def search(self, lease: ReadLease, vector: Sequence[float], *, top_k: int = 10,
                     allowed_ids: Sequence[str] | None = None) -> list[VectorHit]: ...
    async def collect_garbage(self, *, limit: int = 1000) -> int: ...


def lease_duration(seconds: float) -> float:
    if isinstance(seconds, bool) or not math.isfinite(seconds) or not 1 <= seconds <= 300:
        raise ValueError("lease_seconds must be finite and between 1 and 300")
    return float(seconds)


def target_revision(revision: int, generation: str) -> None:
    if isinstance(revision, bool) or not isinstance(revision, int) or not 0 <= revision < 2**63:
        raise ValueError("target revision must be a nonnegative signed 64-bit integer")
    _label(_generation(generation), "snapshot generation")


def snapshot_records(records: Iterable[VectorRecord], dimension: int) -> list[tuple[str, tuple[float, ...], str]]:
    rows: list[tuple[str, tuple[float, ...], str]] = []
    for record in records:
        if len(rows) >= MAX_BATCH:
            raise ValueError(f"one snapshot batch may contain at most {MAX_BATCH} records")
        key, vector = _label(record.key, "key"), _vector(record.vector, dimension)
        checksum = record.checksum or hashlib.sha256(struct.pack(f"<{dimension}f", *vector)).hexdigest()
        rows.append((key, vector, _label(checksum, "checksum")))
    return rows


def snapshot_deletes(keys: Iterable[str]) -> list[str]:
    return _keys(keys)
