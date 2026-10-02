"""self-service account deletion: request/confirm token on organizations"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "3c3adaa53a71"
down_revision: Union[str, None] = "79001d0e5699"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("deletion_token_hash", sa.String(length=200), nullable=True))
    op.add_column(
        "organizations", sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "organizations", sa.Column("deletion_expires_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("organizations", "deletion_expires_at")
    op.drop_column("organizations", "deletion_requested_at")
    op.drop_column("organizations", "deletion_token_hash")
