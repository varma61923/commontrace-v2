"""organization stripe_customer_id, stripe_subscription_id (self-serve billing)

Revision ID: f4a1d2e6c8b3
Revises: 9c14a2e6d8f0
Create Date: 2026-09-08 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f4a1d2e6c8b3'
down_revision: Union[str, None] = '9c14a2e6d8f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Both nullable, both unset for every existing org (nothing here has
    # ever gone through a Stripe Checkout Session before this migration) --
    # no backfill needed, unlike trace_count in the prior migration, which
    # had to reconstruct a value from data that already existed.
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
