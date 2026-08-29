"""trace agent_id + agents-under-management index

Adds the agent IDENTITY that `agent_type` was never able to carry:
agent_type is a category ("support"), so a fleet of 25 support agents
shares one value and the number of agents an org runs was not computable
from anything stored. See hub/models.py:Trace.agent_id and hub/plans.py.

Backfill is a server_default of '' applied to every existing row, so this
migration is safe on a live table with no application coordination: old
clients that never send an agent_id keep writing, and their traces are
attributed to the single UNATTRIBUTED sentinel agent per org rather than
being rejected or silently uncounted.

Revision ID: 7c4a1d92e5b8
Revises: 40d3f29cbb24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7c4a1d92e5b8"
down_revision: Union[str, None] = "40d3f29cbb24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default='' rather than nullable=True: every pre-existing row
    # gets a concrete value in one pass, so no COUNT(DISTINCT agent_id)
    # downstream has to special-case NULL. Postgres 11+ adds a non-volatile
    # DEFAULT without rewriting the table, so this stays cheap on a large
    # traces table.
    op.add_column(
        "traces",
        sa.Column("agent_id", sa.String(length=128), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_traces_org_created_agent",
        "traces",
        ["org_id", "created_at", "agent_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_traces_org_created_agent", table_name="traces")
    op.drop_column("traces", "agent_id")
