from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from hub import auth
from hub.alembic.versions.a7c3e91d4b20_outcome_connectors import (  # noqa: E402
    _NEW_TABLES as _CONNECTOR_TABLES,
)
from hub.alembic.versions.d5c8b3a91e77_row_level_security import (  # noqa: E402
    _OWN_ROWS,
    _SCOPED_TABLES,
    _UNSCOPED,
)
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio

_UNSAFE_QUERY = text("SELECT title FROM traces WHERE title LIKE :pat ORDER BY title")


@pytest_asyncio.fixture
async def rls(session_factory):
    async with session_scope(session_factory) as session:
        for table in (*_SCOPED_TABLES, *_CONNECTOR_TABLES):
            await session.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            await session.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
            await session.execute(text(
                f"CREATE POLICY org_isolation ON {table} AS PERMISSIVE FOR ALL "
                f"USING ({_UNSCOPED} OR {_OWN_ROWS}) "
                f"WITH CHECK ({_UNSCOPED} OR {_OWN_ROWS})"
            ))
        await session.execute(text("ALTER TABLE traces ENABLE ROW LEVEL SECURITY"))
        await session.execute(text("ALTER TABLE traces FORCE ROW LEVEL SECURITY"))
        await session.execute(text(
            f"CREATE POLICY org_isolation ON traces AS PERMISSIVE FOR ALL "
            f"USING ({_UNSCOPED} OR {_OWN_ROWS} OR shared_with_commons) "
            f"WITH CHECK ({_UNSCOPED} OR {_OWN_ROWS})"
        ))
    try:
        yield
    finally:
        async with session_scope(session_factory) as session:
            for table in (*_SCOPED_TABLES, *_CONNECTOR_TABLES, "traces"):
                await session.execute(text(f"DROP POLICY IF EXISTS org_isolation ON {table}"))
                await session.execute(text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
                await session.execute(text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))


_RLS_ROLE = "commontrace_rls_test"
_RLS_ROLE_PASSWORD = "rls-test-only"  # noqa: S105 - a throwaway local test role


@pytest_asyncio.fixture
async def enforcing_factory(session_factory, config):
    import dataclasses

    from sqlalchemy.engine import make_url

    from hub.db import make_engine, make_session_factory, rls_status

    async with session_factory() as session:
        status = await rls_status(session)
    if not status["bypasses_rls"]:
        yield session_factory
        return

    tables = (*_SCOPED_TABLES, *_CONNECTOR_TABLES, "traces")
    try:
        async with session_scope(session_factory) as session:
            await session.execute(text(
                f"DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
                f"'{_RLS_ROLE}') THEN CREATE ROLE {_RLS_ROLE} LOGIN PASSWORD "
                f"'{_RLS_ROLE_PASSWORD}'; END IF; END$$"
            ))
            await session.execute(text(f"GRANT USAGE ON SCHEMA public TO {_RLS_ROLE}"))
            for table in tables:
                await session.execute(text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {_RLS_ROLE}"
                ))
            await session.execute(text(
                f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {_RLS_ROLE}"
            ))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"role {status['role']!r} bypasses RLS and an unprivileged role could not "
            f"be created to test enforcement through: {exc}"
        )

    url = make_url(config.database_url).set(
        username=_RLS_ROLE, password=_RLS_ROLE_PASSWORD
    )
    engine = make_engine(dataclasses.replace(config, database_url=url.render_as_string(
        hide_password=False)))
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def bypassing_factory(session_factory):
    from hub.db import rls_status

    async with session_factory() as session:
        status = await rls_status(session)
    if not status["bypasses_rls"]:
        pytest.skip(
            f"role {status['role']!r} cannot bypass RLS, so the inert-policy state "
            "this asserts a refusal on is not reachable here"
        )
    return session_factory


@pytest_asyncio.fixture
async def two_orgs(session_factory):
    marker = uuid.uuid4().hex[:10]
    async with session_scope(session_factory) as session:
        a, b = Organization(name=f"rls-a-{marker}"), Organization(name=f"rls-b-{marker}")
        session.add_all([a, b])
        await session.flush()
        a_id, b_id = str(a.id), str(b.id)
        session.add_all([
            Trace(org_id=a_id, title=f"{marker} owned by A", context_text="a",
                  solution_text="a", agent_type="code"),
            Trace(org_id=b_id, title=f"{marker} owned by B", context_text="b",
                  solution_text="b", agent_type="code"),
        ])
    return a_id, b_id, f"%{marker}%"


async def _titles(session_factory, pattern: str) -> list[str]:
    async with session_scope(session_factory) as session:
        return [r[0] for r in (await session.execute(_UNSAFE_QUERY, {"pat": pattern})).all()]


class TestAForgottenPredicateIsNotABreach:
    async def test_without_rls_the_unsafe_query_really_does_leak(
        self, session_factory, two_orgs
    ):
        _a_id, _b_id, pattern = two_orgs
        assert len(await _titles(session_factory, pattern)) == 2

    async def test_with_rls_the_same_query_returns_only_the_callers_rows(
        self, rls, enforcing_factory, two_orgs
    ):
        a_id, _b_id, pattern = two_orgs
        token = auth.current_org_id.set(a_id)
        try:
            titles = await _titles(enforcing_factory, pattern)
        finally:
            auth.current_org_id.reset(token)
        assert titles == [t for t in titles if t.endswith("owned by A")]
        assert len(titles) == 1

    async def test_the_other_org_sees_its_own_row_and_not_the_first(
        self, rls, enforcing_factory, two_orgs
    ):
        _a_id, b_id, pattern = two_orgs
        token = auth.current_org_id.set(b_id)
        try:
            titles = await _titles(enforcing_factory, pattern)
        finally:
            auth.current_org_id.reset(token)
        assert len(titles) == 1 and titles[0].endswith("owned by B")


class TestOperatorPathsAreUnaffected:
    async def test_an_unscoped_connection_still_sees_every_org(
        self, rls, enforcing_factory, two_orgs
    ):
        _a_id, _b_id, pattern = two_orgs
        assert len(await _titles(enforcing_factory, pattern)) == 2


class TestWritesCannotCrossTenants:
    async def test_writing_a_row_into_another_org_is_refused(
        self, rls, enforcing_factory, two_orgs
    ):
        a_id, b_id, _pattern = two_orgs
        token = auth.current_org_id.set(a_id)
        try:
            with pytest.raises(Exception) as exc_info:
                async with session_scope(enforcing_factory) as session:
                    session.add(Trace(org_id=b_id, title="forged", context_text="x",
                                      solution_text="x", agent_type="code"))
        finally:
            auth.current_org_id.reset(token)
        assert "row-level security" in str(exc_info.value).lower()


class TestConnectorTablesAreIsolated:
    async def _connectors(self, session_factory, a_id, b_id):
        from hub.models import Connector

        async with session_scope(session_factory) as session:
            session.add_all([
                Connector(org_id=a_id, provider="zendesk", secret="sealed-a"),
                Connector(org_id=b_id, provider="github", secret="sealed-b"),
            ])

    async def test_a_query_with_no_org_predicate_returns_only_the_callers_connectors(
        self, rls, enforcing_factory, session_factory, two_orgs
    ):
        a_id, b_id, _ = two_orgs
        await self._connectors(session_factory, a_id, b_id)
        token = auth.current_org_id.set(a_id)
        try:
            async with session_scope(enforcing_factory) as session:
                seen = [r[0] for r in (await session.execute(
                    text("SELECT secret FROM connectors ORDER BY secret"))).all()]
                ledger = (await session.execute(text("SELECT count(*) FROM connector_deliveries"))).scalar_one()
        finally:
            auth.current_org_id.reset(token)
        assert seen == ["sealed-a"] and ledger == 0

    async def test_writing_a_connector_into_another_org_is_refused(
        self, rls, enforcing_factory, two_orgs
    ):
        from hub.models import Connector

        a_id, b_id, _ = two_orgs
        token = auth.current_org_id.set(a_id)
        try:
            with pytest.raises(Exception) as exc_info:
                async with session_scope(enforcing_factory) as session:
                    session.add(Connector(org_id=b_id, provider="zendesk", secret="forged"))
        finally:
            auth.current_org_id.reset(token)
        assert "row-level security" in str(exc_info.value).lower()


class TestTheKnowledgeBaseStillWorks:
    async def test_a_shared_trace_stays_readable_across_orgs(
        self, rls, enforcing_factory, two_orgs
    ):
        a_id, b_id, pattern = two_orgs
        async with session_scope(enforcing_factory) as session:
            await session.execute(
                text("UPDATE traces SET shared_with_commons = true WHERE org_id = :o"),
                {"o": b_id},
            )
        token = auth.current_org_id.set(a_id)
        try:
            titles = await _titles(enforcing_factory, pattern)
        finally:
            auth.current_org_id.reset(token)
        assert len(titles) == 2, "B's published Knowledge Base entry became invisible to A"

    async def test_shared_does_not_also_grant_write_access(
        self, rls, enforcing_factory, two_orgs
    ):
        a_id, b_id, _pattern = two_orgs
        async with session_scope(enforcing_factory) as session:
            await session.execute(
                text("UPDATE traces SET shared_with_commons = true WHERE org_id = :o"),
                {"o": b_id},
            )
        token = auth.current_org_id.set(a_id)
        try:
            async with session_scope(enforcing_factory) as session:
                result = await session.execute(
                    text("UPDATE traces SET title = 'hijacked' WHERE org_id = :o"),
                    {"o": b_id},
                )
                assert result.rowcount == 0
        except Exception as exc:  # noqa: BLE001
            assert "row-level security" in str(exc).lower()
        finally:
            auth.current_org_id.reset(token)


class TestTheHubNoticesWhenRlsCannotBite:
    async def test_status_reports_whether_policies_can_actually_bite(
        self, rls, session_factory
    ):
        from hub.db import rls_status

        async with session_factory() as session:
            status = await rls_status(session)
        assert status["policy_count"] > 0, "the rls fixture installed policies"
        assert status["enforced"] == (not status["bypasses_rls"])

    async def test_a_bypassing_role_is_reported_as_not_enforced(
        self, rls, session_factory
    ):
        from hub.db import rls_status

        async with session_scope(session_factory) as session:
            bypasses = bool((await session.execute(text(
                "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
            ))).scalar_one())
        async with session_factory() as session:
            status = await rls_status(session)
        assert status["bypasses_rls"] == bypasses
        if bypasses:
            assert status["enforced"] is False

    async def test_the_check_never_breaks_startup_on_a_dead_database(self):
        import dataclasses

        from hub.config import HubConfig
        from hub.db import check_row_level_security, make_engine, make_session_factory

        cfg = dataclasses.replace(
            HubConfig(database_url="postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nope"),
            db_statement_timeout_ms=0,
        )
        engine = make_engine(cfg)
        try:
            factory = make_session_factory(engine)
            assert await check_row_level_security(factory) is None
            assert await check_row_level_security(
                factory, allow_bypass=False, require=True
            ) is None
        finally:
            await engine.dispose()


class TestTheHubRefusesToStartWhenRlsCannotBite:
    async def test_installed_but_bypassed_policies_refuse_startup(
        self, rls, bypassing_factory
    ):
        from hub.db import RowLevelSecurityError, check_row_level_security

        with pytest.raises(RowLevelSecurityError, match="INSTALLED BUT INERT"):
            await check_row_level_security(bypassing_factory, allow_bypass=False)

    async def test_the_refusal_names_the_way_out(self, rls, bypassing_factory):
        from hub.db import RowLevelSecurityError, check_row_level_security

        with pytest.raises(RowLevelSecurityError) as excinfo:
            await check_row_level_security(bypassing_factory, allow_bypass=False)
        message = str(excinfo.value)
        assert "HUB_ALLOW_RLS_BYPASS" in message
        assert "non-superuser" in message

    async def test_an_acknowledged_bypass_is_allowed_through(
        self, rls, bypassing_factory
    ):
        from hub.db import check_row_level_security

        status = await check_row_level_security(bypassing_factory, allow_bypass=True)
        assert status is not None
        assert status["enforced"] is False

    async def test_an_enforcing_connection_starts_normally(self, rls, enforcing_factory):
        from hub.db import check_row_level_security

        status = await check_row_level_security(enforcing_factory, allow_bypass=False)
        assert status is not None
        assert status["enforced"] is True

    async def test_require_rls_refuses_when_no_policies_are_installed(
        self, session_factory
    ):
        from hub.db import RowLevelSecurityError, check_row_level_security

        with pytest.raises(RowLevelSecurityError, match="HUB_REQUIRE_RLS"):
            await check_row_level_security(session_factory, require=True)

    async def test_require_rls_accepts_an_enforcing_connection(
        self, rls, enforcing_factory
    ):
        from hub.db import check_row_level_security

        status = await check_row_level_security(
            enforcing_factory, allow_bypass=False, require=True
        )
        assert status["enforced"] is True
