"""api key scopes (least-privilege workload tokens)

Revision ID: a1c4e70f9d52
Revises: b3f7a1e9c2d4
Create Date: 2026-09-11 00:00:00.000000

Before this column a Hub API key was one undifferentiated capability per
org: hold one and you could search, contribute, amend, delete a trace, and
schedule the org's own deletion. There was no way to hand a production
agent a credential that reads and writes but cannot destroy.

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a1c4e70f9d52'
down_revision: Union[str, None] = 'b3f7a1e9c2d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default is EVERY scope, deliberately. An existing key could do
    # all of this when it was issued, and a migration that silently narrowed
    # live credentials would break every running fleet at upgrade time --
    # the operator re-issues narrower keys when they choose to, which is
    # where that decision belongs. NOT NULL so no row can spell "nothing"
    # two different ways (hub/scopes.py:satisfies has to read a missing
    # grant list as "everything" for pre-column rows; nothing written from
    # here on should be ambiguous).
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
