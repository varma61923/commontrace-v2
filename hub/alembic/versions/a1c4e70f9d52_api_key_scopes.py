"""api key scopes (least-privilege workload tokens)"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a1c4e70f9d52'
down_revision: Union[str, None] = 'b3f7a1e9c2d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'api_keys',
        sa.Column(
            'scopes',
            postgresql.ARRAY(sa.String(length=32)),
            nullable=False,
            server_default='{read,write,admin}',
        ),
    )


def downgrade() -> None:
    op.drop_column('api_keys', 'scopes')
