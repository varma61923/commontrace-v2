"""UniMem signed pooled contributions with tenant isolation."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "fc14d6e8a0b2"
down_revision = "fb13c5d7e9a1"
branch_labels = None
depends_on = None
_TABLES = ("pooled_contributions",)


def upgrade():
    op.create_table("pooled_contributions",
        sa.Column("org_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("release_id", sa.String(128), primary_key=True),
        sa.Column("unit_hash", sa.String(64), nullable=False),
        sa.Column("contribution", postgresql.JSONB(), nullable=False),
        sa.Column("anchor", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_pooled_contributions_unit", "pooled_contributions", ["org_id", "unit_hash"])
    op.execute("ALTER TABLE pooled_contributions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE pooled_contributions FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON pooled_contributions "
               "USING (org_id::text = current_setting('app.org_id', true)) "
               "WITH CHECK (org_id::text = current_setting('app.org_id', true))")


def downgrade():
    op.drop_table("pooled_contributions")
