"""organizations.share_generation: revoke every Proof share link at once

Revision ID: c4f7a2d81e60
Revises: b6e1f48c2a95
Create Date: 2026-10-01 00:00:00.000000

Every share link records the generation it was minted under; the shared view
refuses a link whose generation is not the org's current one. Existing rows
start at 0, which is what links minted before this column existed carry
implicitly (hub/console.py reads a missing `gen` as 0), so no live link is
broken by the upgrade.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4f7a2d81e60"
down_revision: Union[str, None] = "b6e1f48c2a95"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("share_generation", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("organizations", "share_generation")
