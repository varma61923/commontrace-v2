"""Bi-temporal supersession: a superseded trace says so, on its own row"""
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
    op.create_index(
        "ix_traces_org_live", "traces", ["org_id"],
        postgresql_where=sa.text("superseded_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_traces_org_live", table_name="traces")
    op.drop_index("ix_traces_superseded_at", table_name="traces")
    op.drop_column("traces", "superseded_by_trace_id")
    op.drop_column("traces", "superseded_at")
