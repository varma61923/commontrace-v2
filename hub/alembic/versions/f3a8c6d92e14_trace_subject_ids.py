"""trace subject_ids (structured subject tagging for exact-match erasure)"""
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
    op.create_index(
        "ix_traces_subject_ids_gin", "traces", ["subject_ids"], unique=False, postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_traces_subject_ids_gin", table_name="traces", postgresql_using="gin")
    op.drop_column("traces", "subject_ids")
