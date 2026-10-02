"""plans: org plan column and usage counters"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '4e1e9ecbff1f'
down_revision: Union[str, None] = '3b6ef8cbb362'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('usage_counters',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('org_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('period', sa.String(length=7), nullable=False),
    sa.Column('metric', sa.String(length=64), nullable=False),
    sa.Column('n', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('org_id', 'period', 'metric', name='uq_usage_org_period_metric')
    )
    op.create_index(op.f('ix_usage_counters_org_id'), 'usage_counters', ['org_id'], unique=False)
    op.add_column(
        'organizations',
        sa.Column('plan', sa.String(length=32), nullable=False, server_default='free'),
    )


def downgrade() -> None:
    op.drop_column('organizations', 'plan')
    op.drop_index(op.f('ix_usage_counters_org_id'), table_name='usage_counters')
    op.drop_table('usage_counters')
