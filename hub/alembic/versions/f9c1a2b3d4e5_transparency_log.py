"""Per-organization signed Merkle transparency histories."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f9c1a2b3d4e5"
down_revision = "f8a0b3c4d5e6"
branch_labels = None
depends_on = None
_TABLES = ("transparency_heads", "transparency_entries")


def upgrade():
    for name, extra in (
        ("transparency_heads", [sa.Column("tree_size", sa.BigInteger(), nullable=False),
                                sa.Column("root_hash", sa.String(64), nullable=False),
                                sa.Column("signed_head", postgresql.JSONB(), nullable=False)]),
        ("transparency_entries", [sa.Column("leaf_index", sa.BigInteger(), primary_key=True),
                                  sa.Column("leaf_hash", sa.String(64), nullable=False),
                                  sa.Column("statement", postgresql.JSONB(), nullable=False)]),
    ):
        op.create_table(name, sa.Column("org_id", postgresql.UUID(as_uuid=False),
                        sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True), *extra)
        op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {name} "
                   "USING (org_id::text = current_setting('app.org_id', true)) "
                   "WITH CHECK (org_id::text = current_setting('app.org_id', true))")


def downgrade():
    op.drop_table("transparency_entries")
    op.drop_table("transparency_heads")
