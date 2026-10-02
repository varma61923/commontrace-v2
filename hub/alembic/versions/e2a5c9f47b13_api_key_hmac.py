"""api key hmac (fast-path verification, replacing per-request Argon2)"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e2a5c9f47b13'
down_revision: Union[str, None] = 'c3a71f5d80b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('api_keys', sa.Column('key_hmac', sa.String(length=64), nullable=True))
    op.create_index(op.f('ix_api_keys_key_hmac'), 'api_keys', ['key_hmac'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_api_keys_key_hmac'), table_name='api_keys')
    op.drop_column('api_keys', 'key_hmac')
