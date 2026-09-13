"""human users and roles (identity distinct from the workload API key)

Revision ID: c2f8a4d16e93
Revises: e9b4c07d15a8
Create Date: 2026-09-12 00:00:00.000000

Every request into this Hub resolved to an ORGANIZATION, via one shared
workload API key -- there was no notion of which person on that org's team
was acting, so nobody could be individually deprovisioned, and nothing
distinguished a curator from a validator from an owner. hub/auth.py's own
module docstring names "Human users, SSO and RBAC" as an explicit,
not-yet-built follow-up; this is that table.

`role` is one of hub/rbac.py's named roles, checked per MCP tool call in
ADDITION to the org's existing API-key scope -- this table does not replace
scoped API keys, it adds a second, finer-grained, per-person gate for the
subset of callers who authenticate as a person rather than a workload.

Deprovisioning is `disabled_at`, never a delete: the row is exactly what an
auditor asks about later (who had access, with what role, when it was
revoked), and it is checked on every authenticated call rather than only at
token issuance, so revoking access takes effect immediately rather than at
the token's next natural expiry.

Same row-level security every other org-scoped table in this Hub gets
(d5c8b3a91e77).

"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "c2f8a4d16e93"
down_revision: Union[str, None] = "e9b4c07d15a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UNSCOPED = "coalesce(current_setting('app.org_id', true), '') = ''"
_OWN_ROWS = "org_id::text = current_setting('app.org_id', true)"

_NEW_TABLES = ("users",)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "org_id", sa.UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(200), server_default="", nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("issuer", sa.String(500), server_default="", nullable=False),
        sa.Column("external_subject", sa.String(255), server_default="", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("created_by", sa.String(128), server_default="", nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("org_id", "email", name="uq_users_org_email"),
    )
    op.create_index("ix_users_org_id", "users", ["org_id"])
    # Partial: rows with no SSO linked yet both carry ("", "") and must not
    # collide -- only an ACTUAL linked identity has to be globally unique.
    op.create_index(
        "ix_users_issuer_subject", "users", ["issuer", "external_subject"],
        unique=True, postgresql_where=sa.text("external_subject != ''"),
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
    op.drop_index("ix_users_issuer_subject", table_name="users")
    op.drop_index("ix_users_org_id", table_name="users")
    op.drop_table("users")
