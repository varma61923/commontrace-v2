"""experiment pre-registration (what the run committed to measuring)

Revision ID: c8d31b74e2f0
Revises: a1c4e70f9d52
Create Date: 2026-09-11 00:00:00.000000

`experiment.plan` worked out what it takes to answer the question before a
run started, and nothing recorded that plan -- so the outcome measured, the
effect size treated as meaningful, and the point the run stopped at could
all be settled with the results already visible.

Nullable, and NULL means exactly "this experiment was not pre-registered",
which commontrace/prereg.py reports as a finding rather than treating as a
pass. Backfilling a registration onto runs that predate the column would be
the specific dishonesty this whole column exists to prevent.

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c8d31b74e2f0'
down_revision: Union[str, None] = 'a1c4e70f9d52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'organizations',
        sa.Column('holdout_prereg', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('organizations', 'holdout_prereg')
