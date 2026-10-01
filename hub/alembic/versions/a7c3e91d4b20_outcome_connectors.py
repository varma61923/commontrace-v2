"""outcome connectors: links to systems of record, a replay ledger, pending outcomes

Revision ID: a7c3e91d4b20
Revises: d8b2e5f71a36
Create Date: 2026-10-01 00:00:00.000000

Three tables, all org-scoped under the row-level security d5c8b3a91e77 set up:

  connectors                     one org's link to Zendesk/GitHub/...; the vendor's
                                 signing secret, sealed; created in dry-run
  connector_deliveries           the replay ledger, unique per (connector, delivery
                                 id, dry_run)
  connector_pending_outcomes     a candidate success waiting out its window
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c3e91d4b20"
down_revision: Union[str, None] = "d8b2e5f71a36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UNSCOPED = "coalesce(current_setting('app.org_id', true), '') = ''"
_OWN_ROWS = "org_id::text = current_setting('app.org_id', true)"

_NEW_TABLES = ("connectors", "connector_deliveries", "connector_pending_outcomes")


def _org_fk() -> sa.Column:
    return sa.Column(
        "org_id", sa.UUID(as_uuid=False),
        sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
    )


def _connector_fk() -> sa.Column:
    return sa.Column(
        "connector_id", sa.UUID(as_uuid=False),
        sa.ForeignKey("connectors.id", ondelete="CASCADE"), nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "connectors",
        sa.Column("id", sa.UUID(as_uuid=False), primary_key=True),
        _org_fk(),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("name", sa.String(100), server_default="", nullable=False),
        sa.Column("secret", sa.String(2000), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("dry_run", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_connectors_org_id", "connectors", ["org_id"])

    op.create_table(
        "connector_deliveries",
        sa.Column("id", sa.UUID(as_uuid=False), primary_key=True),
        _org_fk(),
        _connector_fk(),
        sa.Column("delivery_id", sa.String(255), nullable=False),
        sa.Column("dry_run", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("outcome", sa.String(500), server_default="", nullable=False),
        sa.UniqueConstraint("connector_id", "delivery_id", "dry_run", name="uq_connector_delivery"),
    )
    op.create_index("ix_connector_deliveries_org_id", "connector_deliveries", ["org_id"])
    op.create_index("ix_connector_deliveries_connector_id", "connector_deliveries", ["connector_id"])

    op.create_table(
        "connector_pending_outcomes",
        sa.Column("id", sa.UUID(as_uuid=False), primary_key=True),
        _org_fk(),
        _connector_fk(),
        sa.Column("occasion_id", sa.String(255), nullable=False),
        sa.Column("ref", sa.String(255), server_default="", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("mature_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("connector_id", "occasion_id", name="uq_connector_pending_occasion"),
    )
    op.create_index("ix_connector_pending_outcomes_org_id", "connector_pending_outcomes", ["org_id"])
    op.create_index(
        "ix_connector_pending_outcomes_connector_id", "connector_pending_outcomes", ["connector_id"],
    )
    op.create_index("ix_connector_pending_outcomes_mature_at", "connector_pending_outcomes", ["mature_at"])
    op.create_index(
        "ix_connector_pending_connector_ref", "connector_pending_outcomes", ["connector_id", "ref"],
    )

    for table in _NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY org_isolation ON {table}
                AS PERMISSIVE FOR ALL
                USING ({_UNSCOPED} OR {_OWN_ROWS})
                WITH CHECK ({_UNSCOPED} OR {_OWN_ROWS})
            """
        )


def downgrade() -> None:
    for table in reversed(_NEW_TABLES):
        op.execute(f"DROP POLICY IF EXISTS org_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_table("connector_pending_outcomes")
    op.drop_table("connector_deliveries")
    op.drop_table("connectors")
