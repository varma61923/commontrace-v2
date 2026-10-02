"""Each Hub process's in-memory copy of the Knowledge Base's matchable corpus."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from hub import commons
from hub.models import Trace

try:  # pragma: no cover - exercised by whichever path the environment has
    import numpy as _np
except ImportError:
    _np = None

logger = logging.getLogger(__name__)


def available() -> bool:
    return _np is not None


@dataclass(frozen=True)
class Snapshot:
    key: tuple
    ids: Any
    org_ids: Any
    agent_types: Any
    signatures: Any

    @property
    def size(self) -> int:
        return len(self.ids)


_snapshot: Snapshot | None = None


def invalidate() -> None:
    global _snapshot
    _snapshot = None


async def _current_key(session: AsyncSession) -> tuple:
    row = (await session.execute(text("SELECT version, changed_at FROM commons_corpus_key()"))).first()
    if row is None or row[0] is None:
        return (0, None)
    return (row[0], row[1])


async def _build(session: AsyncSession, key: tuple, visible: list) -> Snapshot:
    rows = (
        await session.execute(
            select(Trace.id, Trace.org_id, Trace.agent_type, Trace.commons_signature)
            .where(*visible, Trace.commons_signature.isnot(None))
            .order_by(Trace.created_at.desc(), Trace.id.desc())
        )
    ).all()
    width = commons.COMMONS_NUM_PERM
    kept = [r for r in rows if isinstance(r[3], list) and len(r[3]) == width]
    if len(kept) != len(rows):
        logger.warning(
            "commons_cache: skipped %d Knowledge Base row(s) with a malformed signature",
            len(rows) - len(kept),
        )
    signatures = _np.array([r[3] for r in kept], dtype=_np.uint64).reshape(len(kept), width)
    return Snapshot(
        key=key,
        ids=_np.array([r[0] for r in kept], dtype=object),
        org_ids=_np.array([r[1] for r in kept], dtype=object),
        agent_types=_np.array([r[2] or "" for r in kept], dtype=object),
        signatures=signatures,
    )


async def snapshot(session: AsyncSession, visible: list) -> Snapshot:
    """The current snapshot, rebuilt only if the corpus changed."""
    global _snapshot
    key = await _current_key(session)
    current = _snapshot
    if current is not None and current.key == key:
        return current
    built = await _build(session, key, visible)
    _snapshot = built
    return built


def select_for(snap: Snapshot, org_id: str, agent_type: str, cap: int) -> tuple[int, list[str], Any]:
    """The part of the snapshot one caller's query scans."""
    mask = snap.org_ids != org_id
    if agent_type:
        mask &= snap.agent_types == agent_type
    idx = _np.flatnonzero(mask)
    total = int(idx.size)
    if total == snap.size and total <= cap:
        return total, list(snap.ids), snap.signatures
    idx = idx[:cap]
    return total, list(snap.ids[idx]), snap.signatures[idx]
