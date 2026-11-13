"""Durable principal-scoped learning and assignment-time legacy metadata."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "ff69cbd3e507"
down_revision = "ff58bac2d4f6"
branch_labels = None
depends_on = None
_TABLES = ("learning_experiments", "learning_occasions", "learning_decisions", "learning_work")


def _org():
    return sa.Column(
        "org_id", pg.UUID(as_uuid=False), sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )


def _created():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)


def upgrade():
    op.create_table(
        "learning_experiments",
        _org(),
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("config", pg.JSONB(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("checkpoint", sa.String(64), nullable=True),
        sa.Column("report", pg.JSONB(), nullable=True),
        _created(),
    )
    op.create_index(
        "uq_learning_active_org", "learning_experiments", ["org_id"], unique=True, postgresql_where=sa.text("active")
    )
    op.create_table(
        "learning_occasions",
        _org(),
        sa.Column("experiment_id", sa.String(128), primary_key=True),
        sa.Column("principal", sa.String(256), primary_key=True),
        sa.Column("occasion_id", sa.String(256), primary_key=True),
        sa.Column("evidence", pg.JSONB(), nullable=False),
        _created(),
    )
    op.create_table(
        "learning_decisions",
        _org(),
        sa.Column("experiment_id", sa.String(128), primary_key=True),
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column("decision", pg.JSONB(), nullable=False),
        _created(),
    )
    op.create_table(
        "learning_work",
        _org(),
        sa.Column("experiment_id", sa.String(128), primary_key=True),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("failure", sa.String(512)),
    )
    op.create_index(
        "ix_learning_work_pending", "learning_work", ["org_id", "experiment_id"], postgresql_where=sa.text("pending")
    )
    for name in _TABLES:
        op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
        own = "org_id::text = current_setting('app.org_id', true)"
        # Only work routing metadata is visible to the trusted unscoped scheduler.
        # Assignment bodies, outcomes, configuration and decisions remain tenant-only.
        predicate = (
            f"({own} OR coalesce(current_setting('app.org_id', true), '') = '')" if name == "learning_work" else own
        )
        op.execute(f"CREATE POLICY tenant_isolation ON {name} USING ({predicate}) WITH CHECK ({predicate})")
    op.add_column("holdout_observations", sa.Column("assignment_rate", sa.Float(), nullable=True))
    op.add_column("holdout_observations", sa.Column("assignment_context", pg.JSONB(), nullable=True))


def downgrade():
    op.drop_column("holdout_observations", "assignment_context")
    op.drop_column("holdout_observations", "assignment_rate")
    for name in reversed(_TABLES):
        op.drop_table(name)
