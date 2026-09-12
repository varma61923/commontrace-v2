"""alert rules (threshold alerting on top of the existing webhook pipeline)

Revision ID: b4d9e12a6f37
Revises: a7c3e91f4b28
Create Date: 2026-09-12 07:00:00.000000

Audit §8.3: "No alerting, scheduled reports, BI export." Webhooks (6.3,
hub/events.py) already tell a receiver WHEN something happened; this adds
WHETHER a number has crossed a line an operator cares about, firing
through that same signed, at-least-once queue rather than a second
delivery mechanism.

Same row-level security every other org-scoped table in this Hub gets
(d5c8b3a91e77).
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4d9e12a6f37"
down_revision: Union[str, None] = "a7c3e91f4b28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UNSCOPED = "coalesce(current_setting('app.org_id', true), '') = ''"
_OWN_ROWS = "org_id::text = current_setting('app.org_id', true)"

_NEW_TABLES = ("alert_rules",)


def upgrade() -> None:
    op.create_table(
        "alert_rules",
        sa.Column("id", sa.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "org_id", sa.UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("comparator", sa.String(8), nullable=False),
        sa.Column("threshold", sa.Float, nullable=False),
        sa.Column("cooldown_minutes", sa.Integer, server_default="60", nullable=False),
        sa.Column("enabled", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("created_by", sa.String(128), server_default="", nullable=False),
    )
    op.create_index("ix_alert_rules_org_id", "alert_rules", ["org_id"])
    op.create_index(
        "ix_alert_rules_org_enabled", "alert_rules", ["org_id", "enabled"],
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
    for table in _NEW_TABLES:
        op.execute(f"DROP POLICY IF EXISTS org_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_alert_rules_org_enabled", table_name="alert_rules")
    op.drop_index("ix_alert_rules_org_id", table_name="alert_rules")
    op.drop_table("alert_rules")
