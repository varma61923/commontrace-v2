"""Tenant-bound finite policy activation with forced row-level security."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "fe36f8a0b2d4"
down_revision = "fd25e7f9a1c3"
branch_labels = None
depends_on = None
_TABLES = ("memory_policy_states",)


def upgrade():
    op.create_table("memory_policy_states",
        sa.Column("org_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("policy", postgresql.JSONB(), nullable=True),
        sa.Column("activation", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint("generation >= 0", name="ck_memory_policy_generation"))
    op.execute("ALTER TABLE memory_policy_states ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memory_policy_states FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON memory_policy_states "
               "USING (org_id::text = current_setting('app.org_id', true)) "
               "WITH CHECK (org_id::text = current_setting('app.org_id', true))")


def downgrade():
    op.drop_table("memory_policy_states")
