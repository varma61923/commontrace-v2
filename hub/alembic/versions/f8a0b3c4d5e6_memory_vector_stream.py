"""Tenant-ordered transactional replay, embedding cache, and projection cursors."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f8a0b3c4d5e6"
down_revision = "e7f9a2b3c4d5"
branch_labels = None
depends_on = None

_TABLES = ("memory_change_clocks", "memory_change_events", "memory_projection_states", "memory_embedding_cache")
_POLICY = "org_id::text = current_setting('app.org_id', true)"


def _owner():
    return sa.Column(
        "org_id", postgresql.UUID(as_uuid=False),
        sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True,
    )


def upgrade():
    # Block canonical mutation during the one-time deterministic current-state
    # baseline. Historical revision events remain in memory_record_events.
    op.execute("LOCK TABLE memory_records IN SHARE ROW EXCLUSIVE MODE")
    op.add_column("memory_records", sa.Column("change_seq", sa.BigInteger(), nullable=False, server_default="0"))
    op.create_table(
        "memory_change_clocks", _owner(),
        sa.Column("sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "source_id", postgresql.UUID(as_uuid=False), nullable=False, server_default=sa.text("gen_random_uuid()"),
        ),
        sa.CheckConstraint("sequence >= 0", name="ck_memory_change_clock_sequence"),
    )
    op.create_table(
        "memory_change_events", _owner(),
        sa.Column("sequence", sa.BigInteger(), primary_key=True),
        sa.Column("record_id", sa.String(256), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("record", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.CheckConstraint("sequence > 0 AND version > 0", name="ck_memory_change_event_revision"),
        sa.CheckConstraint(
            "(operation = 'upsert' AND record IS NOT NULL) OR (operation = 'delete' AND record IS NULL)",
            name="ck_memory_change_event_operation",
        ),
    )
    op.create_index("ix_memory_change_events_record", "memory_change_events", ["org_id", "record_id"])
    op.create_table(
        "memory_projection_states", _owner(),
        sa.Column("projection", sa.String(64), primary_key=True),
        sa.Column("namespace", sa.String(256), nullable=False),
        sa.Column("model", sa.String(256), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("cursor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("generation", sa.String(128), nullable=True),
        sa.CheckConstraint("length(projection) = 64", name="ck_memory_projection_identity"),
        sa.CheckConstraint("dimension > 0 AND cursor >= 0", name="ck_memory_projection_position"),
    )
    op.create_table(
        "memory_embedding_cache", _owner(),
        sa.Column("projection", sa.String(64), primary_key=True),
        sa.Column("record_id", sa.String(256), primary_key=True),
        sa.Column("checksum", sa.String(64), primary_key=True),
        sa.Column("vector", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("length(projection) = 64 AND length(checksum) = 64", name="ck_memory_embedding_identity"),
        sa.CheckConstraint("jsonb_typeof(vector) = 'array'", name="ck_memory_embedding_array"),
        sa.CheckConstraint(
            "jsonb_array_length(vector) > 0 AND NOT jsonb_path_exists(vector, '$[*] ? (@.type() != \"number\")')",
            name="ck_memory_embedding_numeric",
        ),
    )
    # The previous migration FORCEs RLS on memory_records. Iterate tenants and
    # restore the caller's setting so a non-BYPASSRLS schema owner can migrate
    # without ever disabling canonical policies or exposing another tenant.
    op.execute("""
        DO $$
        DECLARE owner uuid; previous_scope text := current_setting('app.org_id', true);
        BEGIN
            FOR owner IN SELECT id FROM organizations ORDER BY id LOOP
                PERFORM set_config('app.org_id', owner::text, true);
                INSERT INTO memory_change_clocks(org_id, sequence)
                    SELECT owner, count(*) FROM memory_records WHERE org_id = owner;
                INSERT INTO memory_change_events(org_id, sequence, record_id, version, operation, record)
                    SELECT org_id, row_number() OVER (ORDER BY id, version), id, version, 'upsert', record
                    FROM memory_records WHERE org_id = owner;
                UPDATE memory_records AS current SET change_seq = event.sequence
                    FROM memory_change_events AS event
                    WHERE current.org_id = owner AND event.org_id = owner AND current.id = event.record_id;
            END LOOP;
            PERFORM set_config('app.org_id', coalesce(previous_scope, ''), true);
        END $$
    """)
    for table in _TABLES:
        op.execute("ALTER TABLE " + table + " ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE " + table + " FORCE ROW LEVEL SECURITY")
        op.execute(
            "CREATE POLICY org_isolation ON " + table + " FOR ALL USING (" + _POLICY + ") WITH CHECK (" + _POLICY + ")"
        )


def downgrade():
    for table in reversed(_TABLES):
        op.drop_table(table)
    op.drop_column("memory_records", "change_seq")
