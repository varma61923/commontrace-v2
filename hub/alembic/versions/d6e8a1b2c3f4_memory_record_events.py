"""Versioned memory events and full-text projections under fail-closed tenant RLS."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d6e8a1b2c3f4"
down_revision = "c1d4e8f2a9b6"
branch_labels = None
depends_on = None


_NEW_TABLES = ("memory_record_events", "memory_records", "memory_vector_erasures")
_POLICY = "org_id::text = current_setting('app.org_id', true)"


def upgrade():
    # Freeze the migration independently of future ORM model changes.
    op.create_table(
        "memory_record_events",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("id", sa.String(256), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("record", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "memory_records",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("id", sa.String(256), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', coalesce(record->'payload'->>'text',''))", persisted=True),
        ),
    )
    op.create_index("ix_memory_records_search", "memory_records", ["search_vector"], postgresql_using="gin")
    op.create_index("ix_memory_records_scope", "memory_records", ["scope"], postgresql_using="gin")
    op.create_table(
        "memory_vector_erasures",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("record_id", sa.String(256), primary_key=True),
    )
    for table in _NEW_TABLES:
        op.execute("ALTER TABLE " + table + " ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE " + table + " FORCE ROW LEVEL SECURITY")
        op.execute(
            "CREATE POLICY org_isolation ON " + table + " FOR ALL USING (" + _POLICY + ") WITH CHECK (" + _POLICY + ")"
        )


def downgrade():
    op.drop_table("memory_vector_erasures")
    op.drop_table("memory_records")
    op.drop_table("memory_record_events")
