"""collaboration: comments, assignments, notifications

Revision ID: a7c3e91f4b28
Revises: c2f8a4d16e93
Create Date: 2026-09-12 06:30:00.000000

Audit §8.1: "No reviewer queue, comments, assignments, notification inbox,
ownership." hub/manage.py's Knowledge Base review queue is an OPERATOR
surface (cross-tenant, staff-only) -- this is the missing piece for a
customer's OWN team: discussing and dividing up work on their own traces.
Built on c2f8a4d16e93's human users, since none of this means anything for
a shared workload API key with no notion of who is behind it.

Same row-level security every other org-scoped table in this Hub gets
(d5c8b3a91e77).
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7c3e91f4b28"
down_revision: Union[str, None] = "c2f8a4d16e93"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UNSCOPED = "coalesce(current_setting('app.org_id', true), '') = ''"
_OWN_ROWS = "org_id::text = current_setting('app.org_id', true)"

_NEW_TABLES = ("comments", "assignments", "notifications")


def upgrade() -> None:
    op.create_table(
        "comments",
        sa.Column("id", sa.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "org_id", sa.UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "author_user_id", sa.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("body", sa.String(4000), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )
    op.create_index("ix_comments_org_id", "comments", ["org_id"])
    op.create_index(
        "ix_comments_target", "comments", ["org_id", "target_type", "target_id"],
    )

    op.create_table(
        "assignments",
        sa.Column("id", sa.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "org_id", sa.UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "assignee_user_id", sa.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "assigned_by_user_id", sa.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.UniqueConstraint(
            "org_id", "target_type", "target_id", name="uq_assignments_target",
        ),
    )
    op.create_index("ix_assignments_org_id", "assignments", ["org_id"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "org_id", sa.UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "user_id", sa.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_notifications_org_id", "notifications", ["org_id"])
    op.create_index(
        "ix_notifications_user_unread", "notifications",
        ["org_id", "user_id", "read_at"],
    )

    for table in _NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY org_isolation ON {table}
                AS PERMISSIVE FOR ALL
                USING ({_UNSCOPED} OR {_OWN_ROWS})
                WITH CHECK ({_UNSCOPED} OR {_OWN_ROWS})
            """
        )


def downgrade() -> None:
    for table in _NEW_TABLES:
        op.execute(f"DROP POLICY IF EXISTS org_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_notifications_user_unread", table_name="notifications")
    op.drop_index("ix_notifications_org_id", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_assignments_org_id", table_name="assignments")
    op.drop_table("assignments")
    op.drop_index("ix_comments_target", table_name="comments")
    op.drop_index("ix_comments_org_id", table_name="comments")
    op.drop_table("comments")
