"""trace agent_id + agents-under-management index"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7c4a1d92e5b8"
down_revision: Union[str, None] = "40d3f29cbb24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "agent_id" not in {c["name"] for c in inspector.get_columns("traces")}:
        op.add_column(
            "traces",
            sa.Column("agent_id", sa.String(length=128), nullable=False, server_default=""),
        )
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_traces_org_created_agent",
            table_name="traces",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.create_index(
            "ix_traces_org_created_agent",
            "traces",
            ["org_id", "created_at", "agent_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_traces_org_created_agent",
            table_name="traces",
            postgresql_concurrently=True,
            if_exists=True,
        )
    op.drop_column("traces", "agent_id")
