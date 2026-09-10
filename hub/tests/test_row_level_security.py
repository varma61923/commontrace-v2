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
        self, rls, session_factory, two_orgs
    ):
        """The guarantee: same unsafe SQL, scoped connection, zero rows
        belonging to anyone else."""
        a_id, _b_id, pattern = two_orgs
        token = auth.current_org_id.set(a_id)
        try:
            titles = await _titles(session_factory, pattern)
        finally:
            auth.current_org_id.reset(token)
        assert titles == [t for t in titles if t.endswith("owned by A")]
        assert len(titles) == 1

    async def test_the_other_org_sees_its_own_row_and_not_the_first(
        self, rls, session_factory, two_orgs
    ):
        """Symmetry, so a policy that happened to hide everything would not
        pass: B must still see B."""
        _a_id, b_id, pattern = two_orgs
        token = auth.current_org_id.set(b_id)
        try:
            titles = await _titles(session_factory, pattern)
        finally:
            auth.current_org_id.reset(token)
        assert len(titles) == 1 and titles[0].endswith("owned by B")


class TestOperatorPathsAreUnaffected:
    async def test_an_unscoped_connection_still_sees_every_org(
        self, rls, session_factory, two_orgs
    ):
        """hub/manage.py, the benchmarks and alembic legitimately act
        across orgs and never set the contextvar. Their behaviour must be
        exactly what it was before RLS existed, or this migration breaks
        every operator tool at once."""
        _a_id, _b_id, pattern = two_orgs
        assert len(await _titles(session_factory, pattern)) == 2


class TestWritesCannotCrossTenants:
    async def test_writing_a_row_into_another_org_is_refused(
        self, rls, session_factory, two_orgs
    ):
        """The WITH CHECK half. Reading another tenant is the famous
        failure; writing INTO one is the quieter and worse one, because it
        plants data that later reads then serve as that tenant's own."""
        a_id, b_id, _pattern = two_orgs
        token = auth.current_org_id.set(a_id)
        try:
            with pytest.raises(Exception) as exc_info:
                async with session_scope(session_factory) as session:
                    session.add(Trace(org_id=b_id, title="forged", context_text="x",
                                      solution_text="x", agent_type="code"))
        finally:
            auth.current_org_id.reset(token)
        assert "row-level security" in str(exc_info.value).lower()


class TestTheKnowledgeBaseStillWorks:
    async def test_a_shared_trace_stays_readable_across_orgs(
        self, rls, session_factory, two_orgs
    ):
        """The Knowledge Base is cross-org BY DESIGN -- commons_search and
        commons_overlap read what other orgs published. A policy that only
        allowed an org its own rows would silently empty it, and the
        emptiness would look like "no matches" rather than like a bug."""
        a_id, b_id, pattern = two_orgs
        async with session_scope(session_factory) as session:
            await session.execute(
                text("UPDATE traces SET shared_with_commons = true WHERE org_id = :o"),
                {"o": b_id},
            )
        token = auth.current_org_id.set(a_id)
        try:
            titles = await _titles(session_factory, pattern)
        finally:
            auth.current_org_id.reset(token)
        assert len(titles) == 2, "B's published Knowledge Base entry became invisible to A"

    async def test_shared_does_not_also_grant_write_access(
        self, rls, session_factory, two_orgs
    ):
        """Readable is not writable: marking a row shared must not let
        another org edit it."""
        a_id, b_id, _pattern = two_orgs
        async with session_scope(session_factory) as session:
            await session.execute(
                text("UPDATE traces SET shared_with_commons = true WHERE org_id = :o"),
                {"o": b_id},
            )
        token = auth.current_org_id.set(a_id)
        try:
            async with session_scope(session_factory) as session:
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
