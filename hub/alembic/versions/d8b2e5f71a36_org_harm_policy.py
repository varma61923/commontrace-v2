"""Per-org harm policy for search"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d8b2e5f71a36"
down_revision: Union[str, None] = "c3e8a1f05b92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("harm_policy", sa.String(length=16), server_default="inform", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("organizations", "harm_policy")
