"""Row-level security: tenant isolation enforced by Postgres, not by care"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "d5c8b3a91e77"
down_revision: Union[str, None] = "f4a1d2e6c8b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SCOPED_TABLES = ("votes", "holdout_observations", "kb_submissions", "usage_counters")

_UNSCOPED = "coalesce(current_setting('app.org_id', true), '') = ''"
_OWN_ROWS = "org_id::text = current_setting('app.org_id', true)"


def upgrade() -> None:
    for table in _SCOPED_TABLES:
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

    op.execute("ALTER TABLE traces ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE traces FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY org_isolation ON traces
            AS PERMISSIVE FOR ALL
            USING ({_UNSCOPED} OR {_OWN_ROWS} OR shared_with_commons)
            WITH CHECK ({_UNSCOPED} OR {_OWN_ROWS})
        """
    )


def downgrade() -> None:
    for table in (*_SCOPED_TABLES, "traces"):
        op.execute(f"DROP POLICY IF EXISTS org_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
