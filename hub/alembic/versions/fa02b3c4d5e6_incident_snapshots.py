"""Incident snapshots anchored to legal holds and Merkle history."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "fa02b3c4d5e6"
down_revision = "f9c1a2b3d4e5"
branch_labels = None
depends_on = None
_TABLES = ("incident_snapshots",)


def upgrade():
    op.create_table("incident_snapshots",
        sa.Column("org_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("valid_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hold_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("legal_holds.id"), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("anchor", postgresql.JSONB(), nullable=False))
    op.execute("ALTER TABLE incident_snapshots ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE incident_snapshots FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON incident_snapshots "
               "USING (org_id::text = current_setting('app.org_id', true)) "
               "WITH CHECK (org_id::text = current_setting('app.org_id', true))")


def downgrade():
    op.drop_table("incident_snapshots")
