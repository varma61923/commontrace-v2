"""stripe webhook event idempotency ledger

Revision ID: 37d2580be8db
Revises: c9a1e73d5f02
Create Date: 2026-09-18 00:00:00.000000

hub/billing.py's `apply_webhook_event` is idempotent against Stripe's
at-least-once webhook delivery today only by accident: every branch is a
pure `org.plan = plan` overwrite, never an increment, so replaying the
same event twice happens to land on the same state. Nothing previously
checked whether an event had already been applied before re-running its
handler, so a future edit that made any branch additive (crediting
something per event, say) would silently reintroduce double-application.

`processed_webhook_events` makes "already handled" a check the webhook
route makes before re-running anything, independent of whether the
handler underneath happens to be idempotent on its own. Keyed by Stripe's
own event id, which is globally unique by construction.

Not org-scoped and NOT given the row-level-security policy every
org-scoped table in this Hub gets (d5c8b3a91e77) -- this row has to be
checked BEFORE any org is resolved from the event body, for event types
this integration does not otherwise act on at all. Same exclusion as
`organizations`/`api_keys`/`audit_log`, for the same reason.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "37d2580be8db"
down_revision: Union[str, None] = "c9a1e73d5f02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "processed_webhook_events",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(500), server_default="", nullable=False),
        sa.Column(
            "processed_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("processed_webhook_events")
