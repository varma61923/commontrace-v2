"""commons economics: hits and source provenance

Revision ID: 3b6ef8cbb362
Revises: 4b51f38d01b8
Create Date: 2026-08-21 02:11:39.642671

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '3b6ef8cbb362'
down_revision: Union[str, None] = '4b51f38d01b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default on both, for the same reason as revision 4b51f38d01b8:
    # `ADD COLUMN ... NOT NULL` with no default fails outright on a table
    # that already has rows, and autogenerate does not add it because it
    # only sees the model's Python-side default. Existing rows backfill to
    # 0 hits and source 'org' -- correct, since every pre-existing commons
    # entry was contributed by an org, not operator-seeded.
    op.add_column(
        'traces',
        sa.Column('commons_hits', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'traces',
        sa.Column('commons_source', sa.String(length=16), nullable=False, server_default='org'),
    )


def downgrade() -> None:
    op.drop_column('traces', 'commons_source')
    op.drop_column('traces', 'commons_hits')
