"""trace subject_ids (structured subject tagging for exact-match erasure)

Revision ID: f3a8c6d92e14
Revises: b4d9e12a6f37
Create Date: 2026-09-12 11:30:00.000000

Audit §2.2: "a customer who needs subject-level erasure over trace content
must locate the traces themselves; there is no field this system could
search on to do it for them." `search_trace_content` (2.2's first half,
b4d9e12a6f37's predecessor commit) closed the "no field at all" gap with a
free-text scan, but could not honestly claim "provably complete" -- a
free-text match proves presence, never absence.

This is the structured half that free text can never be: an OPTIONAL,
explicit array a curator populates when a trace is known to concern a
specific end user or customer. Once tagged, `find_traces_by_subject`/
`purge_traces_by_subject` (hub/crud.py) are EXACT array-membership queries,
not a fuzzy scan a human still has to review -- for tagged content, this
really is provably complete. Untagged or historical content still needs
`search_trace_content`; this does not retroactively fix that, and does not
claim to.

Same row-level security every column on `traces` already gets -- an
existing, already-protected table, not a new one, so no new RLS policy is
created here (unlike a new table's own migration, e.g. b4d9e12a6f37).
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f3a8c6d92e14"
down_revision: Union[str, None] = "b4d9e12a6f37"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "traces",
        sa.Column(
            "subject_ids", postgresql.ARRAY(sa.String(256)),
            server_default="{}", nullable=False,
        ),
    )
    # GIN, not btree -- same reasoning as ix_traces_tags_gin: this is
    # queried for array MEMBERSHIP (a subject_id is one of possibly many
    # tagged onto a trace), which a btree index on the array column cannot
    # support at all.
    op.create_index(
        "ix_traces_subject_ids_gin", "traces", ["subject_ids"], unique=False, postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_traces_subject_ids_gin", table_name="traces", postgresql_using="gin")
    op.drop_column("traces", "subject_ids")
