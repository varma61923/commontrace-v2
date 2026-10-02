"""retention policies and legal holds"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "d7f2a63b9c41"
down_revision: Union[str, None] = "c8d31b74e2f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UNSCOPED = "coalesce(current_setting('app.org_id', true), '') = ''"
_OWN_ROWS = "org_id::text = current_setting('app.org_id', true)"

_NEW_TABLES = ("retention_policies", "legal_holds")


def upgrade() -> None:
    op.create_table(
        "retention_policies",
        sa.Column("id", sa.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "org_id", sa.UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("object_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), server_default="any", nullable=False),
        sa.Column("max_age_days", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(500), server_default="", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.UniqueConstraint(
            "org_id", "object_type", "status", name="uq_retention_org_type_status"
        ),
    )
    op.create_index(
        "ix_retention_policies_org_id", "retention_policies", ["org_id"]
    )

    op.create_table(
        "legal_holds",
        sa.Column("id", sa.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "org_id", sa.UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("object_type", sa.String(32), server_default="", nullable=False),
        sa.Column("target_id", sa.String(64), server_default="", nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("placed_by", sa.String(128), nullable=False),
        sa.Column(
            "placed_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(500), server_default="", nullable=False),
    )
    op.create_index("ix_legal_holds_org_id", "legal_holds", ["org_id"])
    op.create_index(
        "ix_legal_holds_active", "legal_holds", ["org_id", "object_type"],
        postgresql_where=sa.text("released_at IS NULL"),
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
    op.drop_index("ix_legal_holds_active", table_name="legal_holds")
    op.drop_index("ix_legal_holds_org_id", table_name="legal_holds")
    op.drop_table("legal_holds")
    op.drop_index("ix_retention_policies_org_id", table_name="retention_policies")
    op.drop_table("retention_policies")
