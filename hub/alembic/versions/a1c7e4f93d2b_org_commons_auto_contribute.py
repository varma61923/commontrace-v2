"""Per-org opt-in to contributing traces back to the Knowledge Base"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1c7e4f93d2b"
down_revision: Union[str, None] = "37d2580be8db"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("organizations")}
    if "commons_auto_contribute" not in existing:
        op.add_column(
            "organizations",
            sa.Column(
                "commons_auto_contribute",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
        )


def downgrade() -> None:
    op.drop_column("organizations", "commons_auto_contribute")
