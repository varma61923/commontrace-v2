"""organizations.data_region: the region an org's data is pinned to

Revision ID: b6e1f48c2a95
Revises: a7c3e91d4b20
Create Date: 2026-10-01 00:00:00.000000

Nullable and unset for every existing org, which keeps today's behaviour exactly: an org with no region is served
by any deployment. Set one (`hub.manage set-region`) and a deployment that declares a different HUB_DATA_REGION
stops authenticating that org's keys (hub/auth.py), so a misrouted client cannot write EU data into a US
deployment, or the reverse.
"""
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
