"""traces: commons sharing grants other orgs SELECT only, never UPDATE or DELETE

The original policy put `shared_with_commons` in a FOR ALL USING clause. USING
also decides which rows UPDATE and DELETE may touch, and DELETE has no WITH
CHECK, so any org could delete another org's shared traces through a query that
forgot its own org filter. Sharing is now a separate SELECT-only policy.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "c1d4e8f2a9b6"
down_revision: Union[str, None] = "b8e3f1a2c7d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UNSCOPED = "coalesce(current_setting('app.org_id', true), '') = ''"
_OWN_ROWS = "org_id::text = current_setting('app.org_id', true)"
OWN_POLICY = (f"CREATE POLICY org_isolation ON traces AS PERMISSIVE FOR ALL "
              f"USING ({_UNSCOPED} OR {_OWN_ROWS}) WITH CHECK ({_UNSCOPED} OR {_OWN_ROWS})")
SHARED_READ_POLICY = "CREATE POLICY commons_read ON traces AS PERMISSIVE FOR SELECT USING (shared_with_commons)"


def upgrade() -> None:
    op.execute("DROP POLICY IF EXISTS org_isolation ON traces")
    op.execute(OWN_POLICY)
    op.execute(SHARED_READ_POLICY)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS commons_read ON traces")
    op.execute("DROP POLICY IF EXISTS org_isolation ON traces")
    op.execute(
        f"CREATE POLICY org_isolation ON traces AS PERMISSIVE FOR ALL "
        f"USING ({_UNSCOPED} OR {_OWN_ROWS} OR shared_with_commons) WITH CHECK ({_UNSCOPED} OR {_OWN_ROWS})"
    )
