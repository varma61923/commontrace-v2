"""The shipped deployment config serves as a role that cannot bypass RLS.

The audited P0 was a DEPLOYMENT fact, not a code fact. Migration
d5c8b3a91e77's tenant-isolation policies were correct; docker-compose.yml
pointed the serving process at the cluster superuser, and Postgres skips
every policy for such a role SILENTLY. Nothing that exercised the running
server could catch that -- hub/tests/test_row_level_security.py asserts how
the policies behave, and they behaved correctly for every role that was
subject to them.

So these tests read the shipped files instead, and they deliberately use no
database fixtures: they are about what a `docker compose up` would do, and
they should run (and fail) even on a machine with no Postgres at all.

Plain text slicing rather than a YAML/SQL parser: every assertion here is
about a specific string an operator would have to change deliberately, and
taking on a parser dependency to check a dozen lines would cost more than it
buys.
"""
from __future__ import annotations

import pathlib


class TestTheShippedStackServesAsANonBypassingRole:
    _ROOT = pathlib.Path(__file__).resolve().parents[2]

    def _compose(self) -> str:
        return (self._ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    def _service_block(self, name: str) -> str:
        """One service's YAML, by text slice. The services are top-level
        two-space keys in a known order, so this needs no YAML parser."""
        compose = self._compose()
        after = compose.split(f"\n  {name}:", 1)[1]
        for following in ("\n  db:", "\n  migrate:", "\n  hub:", "\nvolumes:"):
            if following in after:
                after = after.split(following, 1)[0]
        return after

    def _init_sql(self) -> str:
        return (self._ROOT / "hub" / "postgres-init" / "10-runtime-role.sql").read_text(
            encoding="utf-8"
        )

    def test_the_hub_service_connects_as_the_runtime_role_not_the_owner(self):
        """The exact regression: the serving process's HUB_DATABASE_URL
        naming the owner role is what made every policy inert in the shipped
        stack."""
        hub_block = self._service_block("hub")
        urls = [line for line in hub_block.splitlines() if "HUB_DATABASE_URL" in line]
        assert urls, "the hub service must configure HUB_DATABASE_URL"
        for line in urls:
            assert "commontrace_app:" in line, (
                "the Hub must serve as the non-superuser runtime role; a "
                f"HUB_DATABASE_URL naming the owner makes RLS inert: {line.strip()}"
            )

    def test_migrations_still_run_as_the_owner(self):
        """The other half, and the reason the check above is scoped to one
        service rather than the whole file: the runtime role has no DDL
        rights by design, so the migrate service must keep the owner's
        credentials. Flipped to the runtime role, `alembic upgrade head`
        would fail at the first CREATE TABLE."""
        migrate_block = self._service_block("migrate")
        urls = [line for line in migrate_block.splitlines() if "HUB_DATABASE_URL" in line]
        assert urls, "the migrate service must configure HUB_DATABASE_URL"
        for line in urls:
            assert "commontrace:" in line and "commontrace_app:" not in line

    def test_the_init_script_is_mounted_where_postgres_will_run_it(self):
        compose = self._compose()
        assert "./hub/postgres-init:/docker-entrypoint-initdb.d" in compose

    def test_the_runtime_role_is_created_without_the_attributes_that_skip_rls(self):
        """NOSUPERUSER and NOBYPASSRLS are the entire point of the role.
        Stated explicitly in the SQL (rather than relying on CREATE ROLE's
        defaults) precisely so this can check them."""
        sql = self._init_sql().upper()
        assert "NOSUPERUSER" in sql
        assert "NOBYPASSRLS" in sql

    def test_the_runtime_role_gets_no_ddl_rights(self):
        """USAGE on the schema, never CREATE: a role that can create objects
        owns them, and an owner is exempt from FORCE-less policies on its own
        tables."""
        sql = self._init_sql()
        assert "GRANT USAGE ON SCHEMA public TO commontrace_app" in sql
        assert "GRANT CREATE ON SCHEMA" not in sql
        assert "GRANT ALL" not in sql

    def test_future_migrations_are_covered_by_default_privileges(self):
        """The init script runs before any table exists, so a GRANT over
        today's tables would cover nothing and every future migration would
        silently create a table the runtime role cannot read."""
        sql = self._init_sql()
        assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public" in sql
        assert "ON TABLES TO commontrace_app" in sql
        assert "ON SEQUENCES TO commontrace_app" in sql

    def test_the_rate_limit_table_is_precreated_for_the_runtime_role(self):
        """hub/abuse.py's Postgres limiter cannot create it: a role without
        CREATE on the schema is refused `CREATE TABLE IF NOT EXISTS` even
        when the table is already there. The DDL must match the module's."""
        from hub.abuse import _RATE_LIMIT_DDL

        sql = self._init_sql()
        assert "CREATE TABLE IF NOT EXISTS hub_rate_limit_buckets" in sql
        for column in ("limiter_name", "bucket_key", "tokens", "last_refill"):
            assert column in sql, f"{column} is in _RATE_LIMIT_DDL but not the init script"
            assert column in _RATE_LIMIT_DDL
