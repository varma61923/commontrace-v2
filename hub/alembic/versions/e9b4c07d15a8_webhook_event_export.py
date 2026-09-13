"""webhook event export (endpoints and a durable delivery queue)

Revision ID: e9b4c07d15a8
Revises: d7f2a63b9c41
Create Date: 2026-09-12 00:00:00.000000

Everything this Hub knew was readable only by polling it, so a fleet that
wanted to open a ticket on a quarantine or gate a deploy on an experiment
verdict had to cron `hub/manage.py` and diff the output against last time.

Two tables:

  webhook_endpoints   where an org wants to be told, and which events it
                      subscribed to. Note there is NO SECRET COLUMN: unlike
                      an API key, which is only ever verified and so can be
                      an argon2 hash, a webhook secret has to be USED on
                      every delivery. It is derived per endpoint from the
                      deployment's signing key plus id and key_version
                      (hub/events.py), so a full dump of this table yields
                      no ability to forge an event, and rotation bumps an
                      integer rather than rewriting a stored secret.

  webhook_deliveries  one durable, retried attempt to say one thing. The
                      moments worth a webhook are precisely the ones a
                      receiver cannot afford to miss because their load
                      balancer was restarting, so this is a queue rather
                      than a fire-and-forget call. Delivery is
                      at-least-once and the envelope carries a stable
                      event_id for deduplication.

`payload` holds only the fields its event type declares -- ids, counts and
verdicts, never trace content. A webhook is egress to a third party and
that column is the one place where "just this once" would become permanent.

Both carry org_id and get the row-level security d5c8b3a91e77 established.

"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e9b4c07d15a8"
down_revision: Union[str, None] = "d7f2a63b9c41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UNSCOPED = "coalesce(current_setting('app.org_id', true), '') = ''"
_OWN_ROWS = "org_id::text = current_setting('app.org_id', true)"

_NEW_TABLES = ("webhook_endpoints", "webhook_deliveries")


def upgrade() -> None:
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "org_id", sa.UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("url", sa.String(2000), nullable=False),
        # The subscribed subset, stored in full rather than as an empty
        # "all" sentinel: adding a new event type must never silently start
        # delivering it to endpoints that predate it.
        sa.Column(
            "events", postgresql.ARRAY(sa.String(64)),
            server_default="{}", nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("key_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )
    op.create_index("ix_webhook_endpoints_org_id", "webhook_endpoints", ["org_id"])

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "org_id", sa.UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("endpoint_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}", nullable=False,
        ),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.String(500), server_default="", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_webhook_deliveries_org_id", "webhook_deliveries", ["org_id"])
    op.create_index(
        "ix_webhook_deliveries_endpoint_id", "webhook_deliveries", ["endpoint_id"]
    )
    # The drain query, exactly: what is pending and due, oldest first.
    op.create_index(
        "ix_webhook_deliveries_due", "webhook_deliveries",
        ["status", "next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
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
    op.drop_index("ix_webhook_deliveries_due", table_name="webhook_deliveries")
    op.drop_index(
        "ix_webhook_deliveries_endpoint_id", table_name="webhook_deliveries"
    )
    op.drop_index("ix_webhook_deliveries_org_id", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_webhook_endpoints_org_id", table_name="webhook_endpoints")
    op.drop_table("webhook_endpoints")
