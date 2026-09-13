"""Bi-temporal supersession: a superseded trace says so, on its own row

crud.py:amend_trace creates a NEW row and never mutates the one it amends
-- correct, because history should not be rewritten in place. But it left
the ORIGINAL carrying no signal that a correction exists: search_traces
had no predicate able to tell a live trace from an amended-away one, so a
search could return the stale original -- sometimes instead of its
correction, when the old wording happened to rank higher. Reproduced
against a live Hub before this migration existed: amend a trace, search
for the old wording, get the old wording back.

The fix is the idea, not the code, adapted from Zep/Graphiti's bi-temporal
fact model -- a superseded fact is invalidated, never deleted, and the
invalidation is itself a timestamped, queryable event, not merely
inferable by joining forward through every other row's own backward
pointer. Two columns:

    superseded_at            NULL means "this is the current head" --
                              still the right thing for search to return.
    superseded_by_trace_id   the forward pointer supersedes_trace_id
                              never had: which trace superseded this one,
                              so the chain is walkable in both directions
                              without a self-join.

Both set atomically, in the same transaction as the amending INSERT
(hub/crud.py:amend_trace), on the row being amended.

Not a ForeignKey, matching supersedes_trace_id's own precedent: the trace
a row points to may later be purged (hub/manage.py:purge_trace's
amendment-chain walk), and a dangling FK would block that deletion rather
than let the chain be cleaned up.

Nothing is backfilled: every existing row predates this column and is
therefore, correctly, a "current head" (superseded_at IS NULL) until the
next amendment sets it -- which is exactly what NULL already means for a
trace that was never amended, so there is no distinction to backfill.

Revision ID: b3f7a1e9c2d4
Revises: d5c8b3a91e77
Create Date: 2026-09-10 00:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3f7a1e9c2d4"
down_revision: Union[str, None] = "d5c8b3a91e77"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "traces",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "traces",
        sa.Column("superseded_by_trace_id", sa.UUID(as_uuid=False), nullable=True),
    )
    op.create_index(
        "ix_traces_superseded_at", "traces", ["superseded_at"],
    )
    # search_traces's new default predicate (org_id, superseded_at IS
    # NULL). A superseded trace is expected to eventually be a minority of
    # any active org's rows -- same reasoning as the existing
    # ix_traces_commons partial index -- so indexing only the live ones
    # keeps the common case (search an org) off the full table.
    op.create_index(
        "ix_traces_org_live", "traces", ["org_id"],
        postgresql_where=sa.text("superseded_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_traces_org_live", table_name="traces")
    op.drop_index("ix_traces_superseded_at", table_name="traces")
    op.drop_column("traces", "superseded_by_trace_id")
    op.drop_column("traces", "superseded_at")
