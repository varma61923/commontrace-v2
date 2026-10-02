"""traces: add scopes (GIN-indexed), valid_from, valid_until for enterprise routing and temporal validity"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2fa881205556"
down_revision: Union[str, None] = "c4f7a2d81e60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add routing scopes array with empty default
    op.add_column(
        "traces",
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.String(64)),
            nullable=False,
            server_default="{}",
        ),
    )
    # GIN index for O(log N) containment queries: scopes @> ARRAY['payments']
    op.create_index(
        "ix_traces_scopes_gin",
        "traces",
        ["scopes"],
        postgresql_using="gin",
    )

    # Bitemporal validity columns
    op.add_column(
        "traces",
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "traces",
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("traces", "valid_until")
    op.drop_column("traces", "valid_from")
    op.drop_index("ix_traces_scopes_gin", table_name="traces")
    op.drop_column("traces", "scopes")
