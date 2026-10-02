from __future__ import annotations

import pathlib


class TestTheShippedStackServesAsANonBypassingRole:
    _ROOT = pathlib.Path(__file__).resolve().parents[2]

    def _compose(self) -> str:
        return (self._ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    def _service_block(self, name: str) -> str:
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
        hub_block = self._service_block("hub")
        urls = [line for line in hub_block.splitlines() if "HUB_DATABASE_URL" in line]
        assert urls, "the hub service must configure HUB_DATABASE_URL"
        for line in urls:
            assert "commontrace_app:" in line, (
                "the Hub must serve as the non-superuser runtime role; a "
                f"HUB_DATABASE_URL naming the owner makes RLS inert: {line.strip()}"
            )

    def test_migrations_still_run_as_the_owner(self):
        migrate_block = self._service_block("migrate")
        urls = [line for line in migrate_block.splitlines() if "HUB_DATABASE_URL" in line]
        assert urls, "the migrate service must configure HUB_DATABASE_URL"
        for line in urls:
            assert "commontrace:" in line and "commontrace_app:" not in line

    def test_the_init_script_is_mounted_where_postgres_will_run_it(self):
        compose = self._compose()
        assert "./hub/postgres-init:/docker-entrypoint-initdb.d" in compose

    def test_the_runtime_role_is_created_without_the_attributes_that_skip_rls(self):
        sql = self._init_sql().upper()
        assert "NOSUPERUSER" in sql
        assert "NOBYPASSRLS" in sql

    def test_the_runtime_role_gets_no_ddl_rights(self):
        sql = self._init_sql()
        assert "GRANT USAGE ON SCHEMA public TO commontrace_app" in sql
        assert "GRANT CREATE ON SCHEMA" not in sql
        assert "GRANT ALL" not in sql

    def test_future_migrations_are_covered_by_default_privileges(self):
        sql = self._init_sql()
        assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public" in sql
        assert "ON TABLES TO commontrace_app" in sql
        assert "ON SEQUENCES TO commontrace_app" in sql

    def test_the_rate_limit_table_is_precreated_for_the_runtime_role(self):
        from hub.abuse import _RATE_LIMIT_DDL

        sql = self._init_sql()
        assert "CREATE TABLE IF NOT EXISTS hub_rate_limit_buckets" in sql
        for column in ("limiter_name", "bucket_key", "tokens", "last_refill"):
            assert column in sql, f"{column} is in _RATE_LIMIT_DDL but not the init script"
            assert column in _RATE_LIMIT_DDL
