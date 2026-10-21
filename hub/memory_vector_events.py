"""Transactional outbox for hosted, replayable vector projections.

Like the local fact-store outbox and Hindsight's ``fire_event_with_conn``,
enqueue on the caller's canonical transaction. Do not commit independently or
swallow enqueue errors: the snapshot and its canonical mutation are atomic.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert

from hub.models import MemoryChangeClock, MemoryChangeEvent, MemoryEmbeddingCache
from unimem.memory_record import MemoryRecord


async def append_change(
    session,
    org_id: str,
    record: MemoryRecord | None,
    *,
    record_id: str | None = None,
    version: int | None = None,
) -> int:
    """Append one snapshot/tombstone without releasing the canonical transaction.

    Canonical callers hold the tenant advisory lock. The clock's atomic UPSERT
    also serializes direct concurrent appenders, with rollback-safe allocation.
    """
    if record is not None:
        if record.scope.get("tenant", org_id) != org_id:
            raise ValueError("record tenant cannot override authenticated organization")
        if record_id is not None and record_id != record.id or version is not None and version != record.version:
            raise ValueError("record identity and revision must match the snapshot")
        record_id, version = record.id, record.version
    if not isinstance(record_id, str) or not record_id or len(record_id) > 256:
        raise ValueError("change event requires a record identity")
    if type(version) is not int or version < 1:
        raise ValueError("change event requires a positive record version")
    allocated = await session.execute(
        insert(MemoryChangeClock)
        .values(org_id=org_id, sequence=1)
        .on_conflict_do_update(
            index_elements=["org_id"], set_={"sequence": MemoryChangeClock.sequence + 1}
        )
        .returning(MemoryChangeClock.sequence)
    )
    sequence = allocated.scalar_one()
    await session.execute(insert(MemoryChangeEvent).values(
        org_id=org_id,
        sequence=sequence,
        record_id=record_id,
        version=version,
        operation="upsert" if record is not None else "delete",
        record=record.to_dict() if record is not None else None,
    ))
    return sequence


async def redact_changes(session, org_id: str, identities: Iterable[str]) -> None:
    """Erase all prior payloads/cached vectors while preserving ordered removals.

    The caller holds the same tenant lock as writers and projection publication.
    Tombstones are retained indefinitely; a lagging/new projection still sees
    removal even when no currently configured projection acknowledged it.
    """
    identities = list(dict.fromkeys(identities))
    # Stay below asyncpg's parameter limit for large source/derived cascades.
    for start in range(0, len(identities), 500):
        batch = identities[start:start + 500]
        await session.execute(
            update(MemoryChangeEvent).where(
                MemoryChangeEvent.org_id == org_id, MemoryChangeEvent.record_id.in_(batch)
            ).values(operation="delete", record=None)
        )
        await session.execute(
            delete(MemoryEmbeddingCache).where(
                MemoryEmbeddingCache.org_id == org_id, MemoryEmbeddingCache.record_id.in_(batch)
            )
        )
