"""cross-org commons: consent, rationale, signature

Revision ID: 4b51f38d01b8
Revises: 5707b1fc15d3
Create Date: 2026-08-21 01:25:10.248345

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '4b51f38d01b8'
down_revision: Union[str, None] = '5707b1fc15d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COMMONS_WHERE = sa.text('shared_with_commons AND NOT quarantined')


def upgrade() -> None:
    op.add_column('traces', sa.Column('shared_at', sa.DateTime(timezone=True), nullable=True))
    # server_default is NOT cosmetic here: `ADD COLUMN ... NOT NULL` with no
    # default fails outright on a table that already has rows, which is
    # every deployment that has ever accepted a trace. Alembic's
    # autogenerate does not add it, because it only sees the model's
    # Python-side default. Backfills existing rows with '' and keeps the
    # column NOT NULL.
    op.add_column(
        'traces',
        sa.Column('shared_rationale', sa.String(length=500), nullable=False, server_default=''),
    )
    op.add_column(
        'traces',
        sa.Column('commons_signature', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index(
        'ix_traces_commons', 'traces', ['shared_with_commons'],
        unique=False, postgresql_where=_COMMONS_WHERE,
    )


def downgrade() -> None:
    op.drop_index('ix_traces_commons', table_name='traces', postgresql_where=_COMMONS_WHERE)
    op.drop_column('traces', 'commons_signature')
    op.drop_column('traces', 'shared_rationale')
    op.drop_column('traces', 'shared_at')
