"""UniMem shared capability replay fences and transactional lineage publication."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "fd25e7f9a1c3"
down_revision = "fc14d6e8a0b2"
branch_labels = None
depends_on = None

_TABLES = ("capability_nonces", "capability_attestations")


def upgrade():
    for name in _TABLES:
        columns = [sa.Column("org_id", postgresql.UUID(as_uuid=False),
                             sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True)]
        if name == "capability_nonces":
            columns.extend([sa.Column("nonce_id", sa.String(64), primary_key=True),
                            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False)])
        else:
            columns.extend([sa.Column("event_id", sa.String(64), primary_key=True),
                            sa.Column("body", postgresql.JSONB(), nullable=False),
                            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)])
        op.create_table(name, *columns)
        op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {name} "
                   "USING (org_id::text = current_setting('app.org_id', true)) "
                   "WITH CHECK (org_id::text = current_setting('app.org_id', true))")
    op.create_index("ix_capability_nonces_expiry", "capability_nonces", ["org_id", "expires_at"])


def downgrade():
    op.drop_table("capability_attestations")
    op.drop_table("capability_nonces")
