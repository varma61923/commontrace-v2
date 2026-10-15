"""Index hosted per-session pending buffers without scanning tenant history."""

from alembic import op

revision = "e7f9a2b3c4d5"
down_revision = "d6e8a1b2c3f4"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE INDEX ix_memory_records_session ON memory_records "
        "(org_id, (record->'payload'->>'session_id'), (record->>'created_at'))"
    )


def downgrade():
    op.drop_index("ix_memory_records_session", table_name="memory_records")
