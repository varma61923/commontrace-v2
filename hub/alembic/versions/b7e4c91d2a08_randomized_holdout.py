"""randomized holdout: per-org experiment config + observations"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7e4c91d2a08"
down_revision: Union[str, None] = "8f2b40c17ade"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("holdout_rate", sa.Float(), server_default="0", nullable=False),
    )
    op.add_column(
        "organizations",
        sa.Column("holdout_salt", sa.String(length=64), server_default="", nullable=False),
    )
    op.create_table(
        "holdout_observations",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trace_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("occasion_id", sa.String(length=128), nullable=False),
        sa.Column("injected", sa.Boolean(), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=True),
        sa.Column("salt", sa.String(length=64), server_default="", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "org_id", "salt", "trace_id", "occasion_id", name="uq_holdout_org_salt_trace_occasion"
        ),
    )
    op.create_index("ix_holdout_observations_org_id", "holdout_observations", ["org_id"])
    op.create_index("ix_holdout_observations_trace_id", "holdout_observations", ["trace_id"])
    op.create_index("ix_holdout_org_occasion", "holdout_observations", ["org_id", "occasion_id"])
    op.create_index("ix_holdout_org_salt", "holdout_observations", ["org_id", "salt"])


def downgrade() -> None:
    op.drop_index("ix_holdout_org_salt", table_name="holdout_observations")
    op.drop_index("ix_holdout_org_occasion", table_name="holdout_observations")
    op.drop_index("ix_holdout_observations_trace_id", table_name="holdout_observations")
    op.drop_index("ix_holdout_observations_org_id", table_name="holdout_observations")
    op.drop_table("holdout_observations")
    op.drop_column("organizations", "holdout_salt")
    op.drop_column("organizations", "holdout_rate")
