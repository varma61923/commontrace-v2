"""organization trace_count (maintained counter, replaces per-write COUNT(*))

Revision ID: 9c14a2e6d8f0
Revises: e2a5c9f47b13
Create Date: 2026-09-07 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9c14a2e6d8f0'
down_revision: Union[str, None] = 'e2a5c9f47b13'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'organizations',
        sa.Column('trace_count', sa.Integer(), nullable=False, server_default='0'),
    )
    # Backfill existing orgs from the real count -- a fresh column defaults
    # every row to 0 regardless of how many traces it actually already has,
    # and every mutation from this point on only ever changes the count by
    # a relative +/-N, so a wrong starting value here would never self-heal.
    # Only executed once, at migration time; every future write maintains it.
    op.execute(
        """
        UPDATE organizations o
        SET trace_count = t.n
        FROM (SELECT org_id, count(*) AS n FROM traces GROUP BY org_id) t
        WHERE o.id = t.org_id
        """
    )
    # server_default was only for this migration's own ADD COLUMN backfill
    # step (so existing rows populate before the code that maintains the
    # value going forward starts running); drop it so a driver-level
    # default never masks an application bug that forgot to set it.
    op.alter_column('organizations', 'trace_count', server_default=None)


def downgrade() -> None:
    op.drop_column('organizations', 'trace_count')
