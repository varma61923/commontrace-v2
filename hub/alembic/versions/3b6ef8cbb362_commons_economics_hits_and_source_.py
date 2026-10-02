"""commons economics: hits and source provenance"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '3b6ef8cbb362'
down_revision: Union[str, None] = '4b51f38d01b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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
