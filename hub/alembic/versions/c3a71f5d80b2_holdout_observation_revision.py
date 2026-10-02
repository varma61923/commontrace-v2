"""holdout observation records which revision of the trace it measured"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3a71f5d80b2"
down_revision: Union[str, None] = "b7e4c91d2a08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "holdout_observations",
        sa.Column("trace_revision", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("holdout_observations", "trace_revision")
