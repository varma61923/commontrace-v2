"""lesson marketplace: publisher keys and verified listings, readable across orgs"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b8e3f1a2c7d4"
down_revision: Union[str, None] = "2fa881205556"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UNSCOPED = "coalesce(current_setting('app.org_id', true), '') = ''"
_OWN_ROWS = "org_id::text = current_setting('app.org_id', true)"

# The catalog is public to every org, but only for SELECT: a FOR ALL policy with
# a public USING clause would also let any org UPDATE or DELETE another's rows.
_READ = {"market_publishers": "true", "market_listings": "status = 'listed'"}


def upgrade() -> None:
    op.create_table(
        "market_publishers",
        sa.Column("id", sa.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.UUID(as_uuid=False),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("principal_id", sa.String(128), nullable=False, unique=True),
        sa.Column("organization", sa.String(128), nullable=False),
        sa.Column("public_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_market_publishers_org_id", "market_publishers", ["org_id"])
    op.create_table(
        "market_listings",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.UUID(as_uuid=False),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("publisher_principal", sa.String(128), nullable=False),
        sa.Column("publisher_org", sa.String(128), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, server_default="", nullable=False),
        sa.Column("tags", postgresql.JSONB, server_default="[]", nullable=False),
        sa.Column("lesson_sha256", sa.String(64), nullable=False),
        sa.Column("lift_effect", sa.Float, nullable=False),
        sa.Column("lift_ci_low", sa.Float, nullable=False),
        sa.Column("lift_ci_high", sa.Float, nullable=False),
        sa.Column("organizations", sa.Integer, nullable=False),
        sa.Column("licence_id", sa.String(128), nullable=False),
        sa.Column("price_usd", sa.Float, nullable=True),
        sa.Column("price_per", sa.String(16), nullable=True),
        sa.Column("listing", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.String(16), server_default="listed", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('listed', 'withdrawn')", name="ck_market_listing_status"),
    )
    op.create_index("ix_market_listings_org_id", "market_listings", ["org_id"])
    op.create_index("ix_market_listings_lesson_sha256", "market_listings", ["lesson_sha256"])
    op.create_index("ix_market_listings_status_created", "market_listings", ["status", "created_at"])
    for table, read in _READ.items():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY org_isolation ON {table}
                AS PERMISSIVE FOR ALL
                USING ({_UNSCOPED} OR {_OWN_ROWS})
                WITH CHECK ({_UNSCOPED} OR {_OWN_ROWS})
        """)
        op.execute(f"CREATE POLICY market_public_read ON {table} AS PERMISSIVE FOR SELECT USING ({read})")


def downgrade() -> None:
    for table in _READ:
        op.execute(f"DROP POLICY IF EXISTS market_public_read ON {table}")
        op.execute(f"DROP POLICY IF EXISTS org_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_market_listings_status_created", table_name="market_listings")
    op.drop_index("ix_market_listings_lesson_sha256", table_name="market_listings")
    op.drop_index("ix_market_listings_org_id", table_name="market_listings")
    op.drop_table("market_listings")
    op.drop_index("ix_market_publishers_org_id", table_name="market_publishers")
    op.drop_table("market_publishers")
