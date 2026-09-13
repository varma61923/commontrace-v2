"""Tenant isolation survives a forgotten WHERE clause.

hub/models.py opens by describing the discipline this backstops: "Read
paths in hub/crud.py filter by org_id *in the SQL WHERE clause*, never by
fetching rows and filtering in Python -- that is the property
hub/tests/test_tenant_isolation.py asserts." That discipline is real and
it is tested. It is also discipline: it holds for every query somebody
remembered to write correctly, and a single omitted predicate in a single
future query is a cross-tenant read that no existing test would catch,
because the test suite can only check the queries it knows about.

These tests assert the property from the other direction. They run a
deliberately UNSAFE query -- one with no org_id predicate at all, the
mistake itself -- and require Postgres to return zero of the other
tenant's rows anyway.

The test database is built by `Base.metadata.create_all` rather than by
alembic (see conftest), so the policies the migration installs are not
present here by default. The fixture below applies them, building the SQL
from the migration module's own constants so the two cannot drift: if
someone changes the policy in the migration, these tests exercise the
changed policy rather than a stale copy of the old one.

THE SUPERUSER TRAP, which these tests found the hard way. Postgres skips
every policy for a superuser or a BYPASSRLS role, silently -- no error,
no warning. The first version of these tests connected as whatever role
the test URL named and passed locally, where that role happens to be
unprivileged; in CI the same role is the cluster's POSTGRES_USER and
therefore superuser, and all four enforcement tests failed while the
control still passed. That was the tests working: the deployment shape
CI uses is the one docker-compose.yml ships, so the shipped default
would have installed the policies and bypassed them.

So enforcement is asserted through `enforcing_factory`, which connects as
a purpose-made unprivileged role whenever the ambient one would bypass.
Asserting through a role that cannot be subject to RLS would be asserting
nothing.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from hub import auth

# The migration is the single source of truth for the policy text.
from hub.alembic.versions.d5c8b3a91e77_row_level_security import (  # noqa: E402
    _OWN_ROWS,
    _SCOPED_TABLES,
    _UNSCOPED,
)
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio

# The mistake this exists to survive: a query filtered only by content,
# with no tenant predicate anywhere in it.
_UNSAFE_QUERY = text("SELECT title FROM traces WHERE title LIKE :pat ORDER BY title")


@pytest_asyncio.fixture
async def rls(session_factory):
    """Install the migration's policies for the duration of one test."""
    async with session_scope(session_factory) as session:
        for table in _SCOPED_TABLES:
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
            for table in (*_SCOPED_TABLES, "traces"):
                await session.execute(text(f"DROP POLICY IF EXISTS org_isolation ON {table}"))
                await session.execute(text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
                await session.execute(text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))


_RLS_ROLE = "commontrace_rls_test"
_RLS_ROLE_PASSWORD = "rls-test-only"  # noqa: S105 - a throwaway local test role


@pytest_asyncio.fixture
async def enforcing_factory(session_factory, config):
    """A session factory whose connections are actually subject to RLS.

    When the ambient test role already cannot bypass (the usual local
    setup), this is just `session_factory` -- no extra role, no second
    engine. When it CAN bypass (CI, where POSTGRES_USER is the cluster
    superuser), a throwaway unprivileged role is created and granted only
    what the tests need, and a second engine connects as that role.

    Skips rather than silently passing if neither is possible: a test that
    cannot subject itself to the policy has not verified the policy.
    """
    import dataclasses

    from sqlalchemy.engine import make_url

    from hub.db import make_engine, make_session_factory, rls_status

    async with session_factory() as session:
        status = await rls_status(session)
    if not status["bypasses_rls"]:
        yield session_factory
        return

    tables = (*_SCOPED_TABLES, "traces")
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
    """The mirror image of `enforcing_factory`: a session factory whose
    connections DO bypass RLS, for the tests that assert the Hub refuses to
    start on exactly that.

    The ambient role is usually already this in CI (POSTGRES_USER is the
    cluster superuser, which is the whole point of the audited finding).
    Where it is not -- a local setup whose role is unprivileged -- there is
    no way to manufacture a bypassing role without superuser rights we do
    not have, so this skips rather than passing vacuously: a test that could
    not put the server in the dangerous state has not checked the refusal.
    """
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
    """Two orgs, each with one trace whose title carries a shared marker."""
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
        """The control. If this ever stops leaking, the test below proves
        nothing -- it would be passing because the query is harmless, not
        because RLS is working. Deliberately runs WITHOUT the `rls`
        fixture."""
        _a_id, _b_id, pattern = two_orgs
        assert len(await _titles(session_factory, pattern)) == 2

    async def test_with_rls_the_same_query_returns_only_the_callers_rows(
        self, rls, enforcing_factory, two_orgs
    ):
        """The guarantee: same unsafe SQL, scoped connection, zero rows
        belonging to anyone else."""
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
        """Symmetry, so a policy that happened to hide everything would not
        pass: B must still see B."""
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
        """hub/manage.py, the benchmarks and alembic legitimately act
        across orgs and never set the contextvar. Their behaviour must be
        exactly what it was before RLS existed, or this migration breaks
        every operator tool at once."""
        _a_id, _b_id, pattern = two_orgs
        assert len(await _titles(enforcing_factory, pattern)) == 2


class TestWritesCannotCrossTenants:
    async def test_writing_a_row_into_another_org_is_refused(
        self, rls, enforcing_factory, two_orgs
    ):
        """The WITH CHECK half. Reading another tenant is the famous
        failure; writing INTO one is the quieter and worse one, because it
        plants data that later reads then serve as that tenant's own."""
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


class TestTheKnowledgeBaseStillWorks:
    async def test_a_shared_trace_stays_readable_across_orgs(
        self, rls, enforcing_factory, two_orgs
    ):
        """The Knowledge Base is cross-org BY DESIGN -- commons_search and
        commons_overlap read what other orgs published. A policy that only
        allowed an org its own rows would silently empty it, and the
        emptiness would look like "no matches" rather than like a bug."""
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
        """Readable is not writable: marking a row shared must not let
        another org edit it."""
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
                # The USING clause lets A SEE the shared row; WITH CHECK is
                # what stops the write landing. Either zero rows matched or
                # the statement was refused -- both are correct, and which
                # one Postgres picks is not this test's business.
                assert result.rowcount == 0
        except Exception as exc:  # noqa: BLE001
            assert "row-level security" in str(exc).lower()
        finally:
            auth.current_org_id.reset(token)


class TestTheHubNoticesWhenRlsCannotBite:
    """The failure mode that makes RLS worse than nothing: policies that
    exist, are listed by pg_policies, satisfy an audit -- and are skipped
    silently because the connecting role is a superuser or has BYPASSRLS.

    This is not hypothetical. The Postgres image makes POSTGRES_USER the
    cluster superuser, this repo's docker-compose.yml pointed
    HUB_DATABASE_URL at exactly that role, and CI runs the same shape --
    so the shipped default would have installed these policies and
    bypassed every one of them without a word."""

    async def test_status_reports_whether_policies_can_actually_bite(
        self, rls, session_factory
    ):
        from hub.db import rls_status

        async with session_factory() as session:
            status = await rls_status(session)
        assert status["policy_count"] > 0, "the rls fixture installed policies"
        # enforced is exactly "policies exist AND this role cannot bypass",
        # which is the only combination that means anything.
        assert status["enforced"] == (not status["bypasses_rls"])

    async def test_a_bypassing_role_is_reported_as_not_enforced(
        self, rls, session_factory
    ):
        """Pinned from the database's own answer rather than from a mock,
        so it stays true if Postgres ever changes who is exempt."""
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
        """A diagnostic that can take the server down with it is worse than
        the thing it diagnoses, so an unreachable database returns None
        rather than raising -- and, crucially, keeps doing so under the
        strictest flags. "Cannot determine" is not "determined to be
        unsafe": refusing here would turn a transient blip at boot into a
        crash-loop, for a process that would have reported itself unready
        through /readyz anyway."""
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
    """The audited P0: the shipped stack installed tenant-isolation policies
    and served traffic as a role that bypasses them, so the policies were
    inert -- and the Hub's only response was one log line at startup.

    A silently-bypassed policy is worse than an absent one: it is a
    guarantee an operator believes in and does not have. So the default is
    now to refuse to start, with the unsafe configuration reachable only by
    naming it (HUB_ALLOW_RLS_BYPASS), exactly as HUB_ALLOW_INSECURE_HTTP
    gates serving credentials over plaintext."""

    async def test_installed_but_bypassed_policies_refuse_startup(
        self, rls, bypassing_factory
    ):
        from hub.db import RowLevelSecurityError, check_row_level_security

        with pytest.raises(RowLevelSecurityError, match="INSTALLED BUT INERT"):
            await check_row_level_security(bypassing_factory, allow_bypass=False)

    async def test_the_refusal_names_the_way_out(self, rls, bypassing_factory):
        """A refusal an operator cannot act on just moves the outage. The
        message has to name both remedies: the role to connect as, and the
        flag that says the current one is deliberate."""
        from hub.db import RowLevelSecurityError, check_row_level_security

        with pytest.raises(RowLevelSecurityError) as excinfo:
            await check_row_level_security(bypassing_factory, allow_bypass=False)
        message = str(excinfo.value)
        assert "HUB_ALLOW_RLS_BYPASS" in message
        assert "non-superuser" in message

    async def test_an_acknowledged_bypass_is_allowed_through(
        self, rls, bypassing_factory
    ):
        """The escape hatch has to actually work, or the only way to run the
        pre-existing shape is to not upgrade."""
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
        """The stronger, opt-in form. `allow_bypass` catches policies that
        cannot bite; it says nothing about policies that are not there at
        all -- a database nobody migrated, or one somebody dropped them
        from. Deliberately runs WITHOUT the `rls` fixture, so there is
        nothing installed to bypass."""
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
