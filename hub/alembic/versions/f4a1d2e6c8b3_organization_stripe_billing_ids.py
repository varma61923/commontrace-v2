"""organization stripe_customer_id, stripe_subscription_id (self-serve billing)"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f4a1d2e6c8b3'
down_revision: Union[str, None] = '9c14a2e6d8f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('organizations', sa.Column('stripe_customer_id', sa.String(length=255), nullable=True))
    op.add_column('organizations', sa.Column('stripe_subscription_id', sa.String(length=255), nullable=True))
    op.create_index(
        op.f('ix_organizations_stripe_customer_id'), 'organizations', ['stripe_customer_id'], unique=True
    )
    op.create_index(
        op.f('ix_organizations_stripe_subscription_id'), 'organizations', ['stripe_subscription_id'], unique=True
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_organizations_stripe_subscription_id'), table_name='organizations')
    op.drop_index(op.f('ix_organizations_stripe_customer_id'), table_name='organizations')
    op.drop_column('organizations', 'stripe_subscription_id')
    op.drop_column('organizations', 'stripe_customer_id')
