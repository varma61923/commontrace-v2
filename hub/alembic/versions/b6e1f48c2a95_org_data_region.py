"""organizations.data_region: the region an org's data is pinned to"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "b6e1f48c2a95"
down_revision: Union[str, None] = "a7c3e91d4b20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("data_region", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("organizations", "data_region")
