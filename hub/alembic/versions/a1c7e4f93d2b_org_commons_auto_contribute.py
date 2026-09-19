"""Per-org opt-in to contributing traces back to the Knowledge Base

Adds `organizations.commons_auto_contribute`. When true, hub/crud.py's
contribute_trace also proposes the new trace to the Knowledge Base, so an
org that has decided to participate does not have to remember to propose
each entry by hand.

Defaults FALSE, in the column default and in the server default, and the
backfill is therefore a no-op by construction: this flag is what lets an
org's own incident text leave its tenant, so every existing organization
must come out of this migration in exactly the state it was in before --
not participating. A server_default is set (rather than relying on the
ORM's default alone) precisely so rows inserted by anything that does not
go through SQLAlchemy -- psql, a restore, a future migration -- also land
on the safe value rather than NULL.

Revision ID: a1c7e4f93d2b
Revises: 37d2580be8db
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1c7e4f93d2b"
down_revision: Union[str, None] = "37d2580be8db"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Guarded the same way 8f2b40c17ade guards its own add_column calls:
    # op.add_column is not idempotent, so a retry after an interrupted
    # first attempt would otherwise fail with a duplicate-column error
    # rather than completing.
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("organizations")}
    if "commons_auto_contribute" not in existing:
        op.add_column(
            "organizations",
            sa.Column(
                "commons_auto_contribute",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
        )


def downgrade() -> None:
    op.drop_column("organizations", "commons_auto_contribute")
