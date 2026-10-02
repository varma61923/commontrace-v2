"""widen retrievals depth commons_hits counters to bigint"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '40d3f29cbb24'
down_revision: Union[str, None] = '126ec57affb6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('traces', 'retrievals',
               existing_type=sa.INTEGER(),
               type_=sa.BigInteger(),
               existing_nullable=False)
    op.alter_column('traces', 'depth',
               existing_type=sa.INTEGER(),
               type_=sa.BigInteger(),
               existing_nullable=False)
    op.alter_column('traces', 'commons_hits',
               existing_type=sa.INTEGER(),
               type_=sa.BigInteger(),
               existing_nullable=False,
               existing_server_default=sa.text('0'))


def downgrade() -> None:
    op.alter_column('traces', 'commons_hits',
               existing_type=sa.BigInteger(),
               type_=sa.INTEGER(),
               existing_nullable=False,
               existing_server_default=sa.text('0'))
    op.alter_column('traces', 'depth',
               existing_type=sa.BigInteger(),
               type_=sa.INTEGER(),
               existing_nullable=False)
    op.alter_column('traces', 'retrievals',
               existing_type=sa.BigInteger(),
               type_=sa.INTEGER(),
               existing_nullable=False)
