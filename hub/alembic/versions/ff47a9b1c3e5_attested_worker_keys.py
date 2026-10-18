"""Log-approved measurements and tenant-scoped one-use worker challenges."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ff47a9b1c3e5"
down_revision = "fe36f8a0b2d4"
branch_labels = None
depends_on = None

_TABLES = ("attested_measurements", "attestation_challenges")


def upgrade():
    op.create_table("attested_measurements",
        sa.Column("org_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("measurement", sa.String(64), primary_key=True),
        sa.Column("profile", postgresql.JSONB(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("receipt", postgresql.JSONB(), nullable=False))
    op.create_table("attestation_challenges",
        sa.Column("org_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("nonce", sa.String(64), primary_key=True),
        sa.Column("request", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed", sa.Boolean(), nullable=False, server_default="false"))
    op.create_index("ix_attestation_challenges_expiry", "attestation_challenges", ["org_id", "expires_at"])
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} "
            "USING (org_id::text = current_setting('app.org_id', true)) "
            "WITH CHECK (org_id::text = current_setting('app.org_id', true))")


def downgrade():
    op.drop_table("attestation_challenges")
    op.drop_table("attested_measurements")
