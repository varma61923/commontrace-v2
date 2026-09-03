"""kb_submissions table + organizations.bonus_commons_queries

The community-contribution channel: an org can propose a Knowledge Base
entry (kb_submissions, status='pending'), and hub/manage.py
review-submission is the one deliberate operator action that can approve
it into a new Trace row with commons_source='seed' -- never automatic,
never triggered by the act of submitting alone. Approval also awards
bonus_commons_queries, a permanent addition to that org's monthly
Knowledge Base query allowance (hub/plans.py:query_allowance). See
hub/models.py:KnowledgeBaseSubmission for why review (not opt-in) is what
keeps this from repeating the adverse-selection failure STRATEGY.md §3
already ruled out.

Revision ID: 79001d0e5699
Revises: 7c4a1d92e5b8
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "79001d0e5699"
down_revision: Union[str, None] = "7c4a1d92e5b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("bonus_commons_queries", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "kb_submissions",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("context_text", sa.Text(), nullable=False),
        sa.Column("solution_text", sa.Text(), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.String(length=128)), nullable=False, server_default="{}"),
        sa.Column("agent_type", sa.String(length=64), nullable=False),
        sa.Column("rationale", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("rejection_reason", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("resulting_trace_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("credit_awarded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_kb_submissions_status"),
        sa.UniqueConstraint("org_id", "idempotency_key", name="uq_kb_submissions_org_idempotency_key"),
    )
    op.create_index("ix_kb_submissions_org_id", "kb_submissions", ["org_id"])
    op.create_index("ix_kb_submissions_org_created_at", "kb_submissions", ["org_id", "created_at"])
    op.create_index(
        "ix_kb_submissions_pending",
        "kb_submissions",
        ["status"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_kb_submissions_pending", table_name="kb_submissions")
    op.drop_index("ix_kb_submissions_org_created_at", table_name="kb_submissions")
    op.drop_index("ix_kb_submissions_org_id", table_name="kb_submissions")
    op.drop_table("kb_submissions")
    op.drop_column("organizations", "bonus_commons_queries")
