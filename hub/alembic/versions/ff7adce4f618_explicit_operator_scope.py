"""Require a tenant or explicit control-plane scope, including keys and relations."""

from alembic import op

revision = "ff7adce4f618"
down_revision = "ff69cbd3e507"
branch_labels = None
depends_on = None

TABLES = (
    "traces",
    "votes",
    "holdout_observations",
    "kb_submissions",
    "usage_counters",
    "users",
    "comments",
    "assignments",
    "notifications",
    "alert_rules",
    "connectors",
    "connector_deliveries",
    "connector_pending_outcomes",
    "retention_policies",
    "legal_holds",
    "webhook_endpoints",
    "webhook_deliveries",
    "scim_groups",
    "scim_group_memberships",
)
OWN = "org_id::text = current_setting('app.org_id', true)"
OPERATOR = "current_setting('app.operator_scope', true) = 'all_orgs'"
LEGACY = "coalesce(current_setting('app.org_id', true), '') = ''"


def _policy(table: str, name: str, predicate: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {name} ON {table}")
    op.execute(f"CREATE POLICY {name} ON {table} USING ({predicate}) WITH CHECK ({predicate})")


def upgrade() -> None:
    for table in (*TABLES, "api_keys"):
        _policy(table, "org_isolation", f"{OWN} OR {OPERATOR}")
    _policy("learning_work", "tenant_isolation", f"{OWN} OR {OPERATOR}")
    op.execute("DROP POLICY IF EXISTS commons_read ON traces")
    op.execute(
        "CREATE POLICY commons_read ON traces FOR SELECT USING "
        "(shared_with_commons AND nullif(current_setting('app.org_id', true), '') IS NOT NULL)"
    )
    related = (
        "EXISTS (SELECT 1 FROM traces t WHERE t.id = trace_relations.trace_id "
        "AND t.org_id::text = current_setting('app.org_id', true))"
    )
    _policy("trace_relations", "org_isolation", f"{OPERATOR} OR {related}")


def downgrade() -> None:
    for table in TABLES:
        _policy(table, "org_isolation", f"{OWN} OR {LEGACY}")
    _policy("learning_work", "tenant_isolation", f"{OWN} OR {LEGACY}")
    op.execute("DROP POLICY IF EXISTS commons_read ON traces")
    op.execute("CREATE POLICY commons_read ON traces FOR SELECT USING (shared_with_commons)")
    for table in ("api_keys", "trace_relations"):
        op.execute(f"DROP POLICY IF EXISTS org_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
