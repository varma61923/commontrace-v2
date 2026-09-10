"""Row-level security: tenant isolation enforced by Postgres, not by care

Today isolation is a hand-written `WHERE org_id = ...` in every query, and
hub/models.py says so at the top of the file: "Read paths in hub/crud.py
filter by org_id *in the SQL WHERE clause* ... that is the property
hub/tests/test_tenant_isolation.py asserts." That is a real discipline and
it is tested, but it is discipline: one forgotten predicate in one new
query is a cross-tenant read, and the test suite can only catch the cases
somebody thought to write.

This adds Postgres row-level security UNDERNEATH those predicates, so the
predicates stay exactly as they are and a missing one returns zero rows
instead of another tenant's data. Defence in depth, not a replacement --
the WHERE clauses are still the thing doing the work on every normal path.

HOW THE ORG IS PASSED
    hub/db.py:session_scope issues
    `SELECT set_config('app.org_id', :org, true)` at the start of the
    transaction, taking the org from the `auth.current_org_id` contextvar
    the auth middleware already sets. `set_config(..., true)` is the
    transaction-local form -- the parameterised equivalent of SET LOCAL,
    which cannot take a bind parameter. That distinction is the reason
    this migration's docstring mentions it at all: interpolating the org
    id into a `SET LOCAL` string is the documented footgun in this pattern
    (it is a SQL injection sink fed by a value that came from a request),
    and set_config is how it is avoided.

WHY "UNSET" MEANS UNSCOPED
    Operator paths -- hub/manage.py, the benchmarks, alembic itself --
    legitimately act across every org and never set the contextvar. The
    policies therefore treat an unset `app.org_id` as "no scoping", which
    is exactly today's behaviour, so this migration changes nothing for
    them. The isolation is gained on the authenticated request path, which
    always sets it. A deployment that wants the stronger "deny by default"
    posture should run its operator tooling under a separate role and
    remove the unset branch.

WHY `traces` HAS A THIRD BRANCH
    The Knowledge Base is cross-org BY DESIGN: `commons_search` and
    `commons_overlap` read entries other orgs published
    (`shared_with_commons`). A policy that only allowed an org its own
    rows would silently empty the Knowledge Base. The read policy
    therefore admits shared rows; the WRITE policy does not, so no caller
    can create or alter a row in another org's name even by marking it
    shared.

KNOWN LIMITS, stated because RLS is easy to over-trust
  * FK and UNIQUE constraint checks run outside the policy, so they remain
    a side channel: a UNIQUE violation can reveal that a row exists in
    another tenant without revealing its contents.
  * RLS does not sanitise logs. A query filtered to zero rows still
    appears verbatim in pg_stat_statements and slow-query logs.
  * Views do not inherit RLS unless created WITH (security_invoker = true)
    (Postgres 15+). No view is used on these paths today; a future one
    must set it.
  * Logical replication does not respect RLS -- a subscriber sees every
    row unless per-publication filters are configured.
  * FORCE is required because the application role owns these tables, and
    a table owner is otherwise exempt from its own policies.

Revision ID: d5c8b3a91e77
Revises: f4a1d2e6c8b3
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "d5c8b3a91e77"
down_revision: Union[str, None] = "f4a1d2e6c8b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables whose every row carries a non-null org_id and which are never read
# before the caller is authenticated.
#
# Deliberately NOT included:
#   api_keys       read by auth.verify_api_key to DISCOVER which org is
#                  calling, i.e. necessarily before app.org_id can be set.
#                  Scoping it would make authentication impossible.
#   organizations  read on the same pre-auth path (entitlements, signup).
#   audit_log      org_id is nullable by design; a null-org row is a
#                  system event, and a policy comparing null to the setting
#                  would silently hide those from every reader.
#   trace_relations  has no org_id column; its rows are reachable only
#                  through traces, which is protected.
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

    # traces: readable if unscoped, own, or published to the Knowledge
    # Base; writable only if unscoped or own. See the module docstring.
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
