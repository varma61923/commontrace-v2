"""organization trace_count (maintained counter, replaces per-write COUNT(*))"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '9c14a2e6d8f0'
down_revision: Union[str, None] = 'e2a5c9f47b13'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'organizations',
        sa.Column('trace_count', sa.Integer(), nullable=False, server_default='0'),
    )
    op.execute(
        """
        UPDATE organizations o
        SET trace_count = t.n
        FROM (SELECT org_id, count(*) AS n FROM traces GROUP BY org_id) t
        WHERE o.id = t.org_id
        """
    )
    op.alter_column('organizations', 'trace_count', server_default=None)


def downgrade() -> None:
    op.drop_column('organizations', 'trace_count')
