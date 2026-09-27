"""Each Hub process's in-memory copy of the Knowledge Base's matchable corpus.

WHY THIS EXISTS
---------------
`commons_overlap` and `commons_search` used to load every visible Knowledge
Base row -- full text, JSON-encoded signature and all -- on every call, then
compare against it. Measured at 20,000 entries (the scan cap):

    load full rows          ~1,390 ms
    load (id, signature)      ~530 ms   the JSONB decode is most of it
    load ids only             ~120 ms
    build the matrix           ~80 ms
    compare one signature       ~3 ms

So a query spent almost all of its time re-reading a corpus that changes
only when an operator curates it. This module keeps what the matcher needs
-- each entry's id, owner, agent type and signature -- as numpy arrays, and
reloads it only when `commons_corpus_state` says the corpus changed. A
query then costs one single-row version read, a mask, the comparison, and a
fetch of the few rows that actually matched.

WHAT IS CACHED, AND WHAT IS DELIBERATELY NOT
--------------------------------------------
Only columns that change when the corpus's SHAPE changes, which are exactly
the ones the version triggers watch (hub/models.py:COMMONS_CORPUS_COLUMNS).
Never trust, votes, hits, standing inputs or text: those change on every
vote and every matched query, and every matched row is re-read from the
database with the visibility filter re-applied, so what a caller receives
is always current even if this snapshot is a moment old.

HOW STALENESS IS BOUNDED
------------------------
The version is read before the rows, in its own statement. Under READ
COMMITTED a change that commits between the two can only make the rows
NEWER than the version they are tagged with, which costs one extra rebuild
on the next query -- never a snapshot older than the version it claims.
And because matched rows are re-fetched through commons_visible(), an
entry retracted after the snapshot was taken is dropped from the answer
rather than served.

The snapshot is the same for every organization: commons_visible() admits
only operator-curated rows, which every org may read (and which the traces
RLS policy admits via `shared_with_commons`). The per-caller part -- not
scoring a caller against its own rows, the optional agent_type filter --
is a mask applied per query, not a different snapshot.

Requires numpy. Without it the Hub keeps the direct path in hub/crud.py,
which is what numpy's absence already implied for the comparison itself.
"""

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
except ImportError:  # the Hub runs without numpy; see available()
    _np = None

logger = logging.getLogger(__name__)


def available() -> bool:
    return _np is not None


@dataclass(frozen=True)
class Snapshot:
    """The matchable corpus, in the scan order the direct path used:
    created_at DESC, id DESC -- so ties in similarity resolve to the same
    entry either way."""

    key: tuple
    ids: Any            # numpy object array of trace ids
    org_ids: Any        # numpy object array
    agent_types: Any    # numpy object array
    signatures: Any     # numpy uint64 array, shape (n, COMMONS_NUM_PERM)

    @property
    def size(self) -> int:
        return len(self.ids)


_snapshot: Snapshot | None = None


def invalidate() -> None:
    """Drop this process's snapshot. Tests use it; nothing else should need
    to, since the version triggers catch every change that matters."""
    global _snapshot
    _snapshot = None


async def _current_key(session: AsyncSession) -> tuple:
    # Through commons_corpus_key(), not a SELECT on the table: the function
    # runs as its owner, so a runtime role holding DML grants on nothing but
    # the tables it serves can still read the version (see the comment on
    # COMMONS_CORPUS_TRIGGER_DDL in hub/models.py).
    row = (await session.execute(text("SELECT version, changed_at FROM commons_corpus_key()"))).first()
    # No row yet means no Knowledge Base write has happened since the
    # triggers were installed. Still a valid key: the first write creates
    # the row and changes it.
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
        # A signature of the wrong width cannot be compared (see
        # hub/commons.py:_coerce_signature). The direct path raised on the
        # whole query when it met one; skipping the one row and saying so
        # keeps every other entry matchable.
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
    """The current snapshot, rebuilt only if the corpus changed.

    `visible` is hub/crud.py:commons_visible(), passed in rather than
    imported so this module cannot drift from the one definition of what
    the Knowledge Base is.
    """
    global _snapshot
    key = await _current_key(session)
    current = _snapshot
    if current is not None and current.key == key:
        return current
    # Two concurrent queries after a change may both rebuild. That is
    # duplicate work, not a correctness problem -- both build the same
    # thing -- and it avoids a lock that would have to span an await.
    built = await _build(session, key, visible)
    _snapshot = built
    return built


def select_for(snap: Snapshot, org_id: str, agent_type: str, cap: int) -> tuple[int, list[str], Any]:
    """The part of the snapshot one caller's query scans.

    Returns (total, ids, signatures): `total` counts every entry the
    caller's filters admit, `ids`/`signatures` are the first `cap` of them
    in scan order. That is exactly what the direct path's COUNT plus
    ORDER BY ... LIMIT produced, so truncation behaves identically.
    """
    mask = snap.org_ids != org_id
    if agent_type:
        mask &= snap.agent_types == agent_type
    idx = _np.flatnonzero(mask)
    total = int(idx.size)
    if total == snap.size and total <= cap:
        # The common case -- a customer querying a corpus it owns none of,
        # unfiltered -- scans the cached matrix itself, with no copy.
        return total, list(snap.ids), snap.signatures
    idx = idx[:cap]
    return total, list(snap.ids[idx]), snap.signatures[idx]
