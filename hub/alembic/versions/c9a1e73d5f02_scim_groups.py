"""scim groups (permission-inert membership tracking for SCIM /Groups)

Revision ID: c9a1e73d5f02
Revises: f3a8c6d92e14
Create Date: 2026-09-12 18:00:00.000000

Audit §1.2 named "SCIM Groups" as a declined gap: a real Groups API needs
many-to-many membership, and this Hub gives one `User` exactly one `role`
(hub/rbac.py) -- no additive permission surface a group could plug into.

This closes the data-model half honestly, without inventing a second
authorization system: `scim_groups`/`scim_group_memberships` track an IdP's
group roster faithfully (what `/scim/v2/Groups` needs to exist at all), and
deliberately grant NOTHING -- nothing in `hub/rbac.py` or `hub/server.py`'s
tool gating reads either table. A "group" here is a label plus a membership
list, not a bundle of permissions.

Same row-level security every other org-scoped table in this Hub gets
(d5c8b3a91e77). `scim_group_memberships.org_id` is denormalized from the
owning group rather than joined, the same choice `comments`/`assignments`
already made, so RLS has a column on the row itself to scope against.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "c9a1e73d5f02"
down_revision: Union[str, None] = "f3a8c6d92e14"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UNSCOPED = "coalesce(current_setting('app.org_id', true), '') = ''"
_OWN_ROWS = "org_id::text = current_setting('app.org_id', true)"

_NEW_TABLES = ("scim_groups", "scim_group_memberships")


def upgrade() -> None:
    op.create_table(
        "scim_groups",
        sa.Column("id", sa.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "org_id", sa.UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("external_id", sa.String(255), server_default="", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.UniqueConstraint("org_id", "display_name", name="uq_scim_groups_org_display_name"),
    )
    op.create_index("ix_scim_groups_org_id", "scim_groups", ["org_id"])

    op.create_table(
        "scim_group_memberships",
        sa.Column("id", sa.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "org_id", sa.UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "group_id", sa.UUID(as_uuid=False),
            sa.ForeignKey("scim_groups.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "user_id", sa.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.UniqueConstraint("group_id", "user_id", name="uq_scim_group_memberships"),
    )
    op.create_index("ix_scim_group_memberships_org_id", "scim_group_memberships", ["org_id"])
    op.create_index("ix_scim_group_memberships_group_id", "scim_group_memberships", ["group_id"])
    op.create_index("ix_scim_group_memberships_user_id", "scim_group_memberships", ["user_id"])

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
    op.drop_index("ix_scim_group_memberships_user_id", table_name="scim_group_memberships")
    op.drop_index("ix_scim_group_memberships_group_id", table_name="scim_group_memberships")
    op.drop_index("ix_scim_group_memberships_org_id", table_name="scim_group_memberships")
    op.drop_table("scim_group_memberships")
    op.drop_index("ix_scim_groups_org_id", table_name="scim_groups")
    op.drop_table("scim_groups")
