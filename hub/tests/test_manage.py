"""Tests for hub/manage.py's monitoring/admin commands: stats, the
quarantine review queue, and permanent deletion (purge-trace/purge-org --
the deletion path DATA_RETENTION.md previously documented as
unimplemented)."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from hub import auth, crud, manage
from hub.abuse import make_rate_limiter
from hub.crud import amend_trace, contribute_trace
from hub.db import session_scope
from hub.models import Organization, Trace, TraceRelation

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def two_orgs(session_factory):
    async with session_scope(session_factory) as session:
        org_a = Organization(name="org-a")
        org_b = Organization(name="org-b")
        session.add_all([org_a, org_b])
        await session.flush()
        return {"org_a": org_a.id, "org_b": org_b.id}


async def test_stats_reports_zero_on_empty_db(session_factory, capsys):
    await manage.stats(session_factory=session_factory)
    out = capsys.readouterr().out
    assert "organizations:      0" in out
    assert "mean trust:          n/a" in out


async def test_stats_computes_real_counts_and_mean_trust(session_factory, config, two_orgs, capsys):
    """Regression test for switching from Python len()/fmean() over fully
    loaded ORM objects to SQL-side COUNT()/AVG() -- pins that the actual
    numbers still come out right, not just that the query doesn't crash."""
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        t1 = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="t1", context_text="c", solution_text="s", tags=[], agent_type="code", actor="test",
        )
        t2 = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="t2", context_text="c", solution_text="s", tags=[], agent_type="code", actor="test",
        )
    async with session_scope(session_factory) as session:
        await crud.vote_trace(session, two_orgs["org_a"], t1["id"], "up", actor="test")
        await crud.vote_trace(session, two_orgs["org_a"], t2["id"], "down", actor="test")

    capsys.readouterr()
    await manage.stats(session_factory=session_factory)
    out = capsys.readouterr().out
    assert "organizations:      2" in out
    assert "traces (total):     2" in out
    assert "votes:               2" in out
    # trust=1.0 and trust=0.0 -> mean 0.5
    assert "mean trust:          0.500" in out


async def test_list_quarantined_reports_none_cleanly(session_factory, capsys, two_orgs):
    await manage.list_quarantined(session_factory=session_factory)
    assert "no quarantined traces" in capsys.readouterr().out


async def test_release_quarantine_clears_flag(session_factory, config, two_orgs):
    rate_limiter = make_rate_limiter(config)
    spam_urls = " ".join(f"http://spam{i}.example.com" for i in range(config.suspect_url_threshold + 1))
    async with session_scope(session_factory) as session:
        result = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="spammy", context_text=spam_urls, solution_text="s", tags=[], agent_type="code",
        )
    assert result["quarantined"] is True

    await manage.release_quarantine(result["id"], session_factory=session_factory)

    async with session_scope(session_factory) as session:
        trace = await session.get(Trace, result["id"])
    assert trace.quarantined is False
    assert trace.quarantine_reason == ""


async def test_list_quarantined_shows_pending_review(session_factory, config, two_orgs, capsys):
    rate_limiter = make_rate_limiter(config)
    spam_urls = " ".join(f"http://spam{i}.example.com" for i in range(config.suspect_url_threshold + 1))
    async with session_scope(session_factory) as session:
        result = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="spammy title", context_text=spam_urls, solution_text="s", tags=[], agent_type="code",
        )

    capsys.readouterr()
    await manage.list_quarantined(session_factory=session_factory)
    out = capsys.readouterr().out
    assert result["id"] in out
    assert "spammy title" in out

    # filtering to the *other* org must not show org_a's quarantined trace
    capsys.readouterr()
    await manage.list_quarantined(two_orgs["org_b"], session_factory=session_factory)
    assert "no quarantined traces" in capsys.readouterr().out


async def test_release_quarantine_unknown_id_reports_error(session_factory, capsys):
    result = await manage.release_quarantine(
        "00000000-0000-0000-0000-000000000000", session_factory=session_factory
    )
    assert "no such trace" in capsys.readouterr().err
    # False (not just the stderr message) is what makes `main()` exit
    # non-zero for a failed destructive op -- see test_main_command_exit_codes.
    assert result is False


async def test_purge_trace_deletes_it_permanently(session_factory, config, two_orgs):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        result = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="t", context_text="c", solution_text="s", tags=[], agent_type="code",
        )

    await manage.purge_trace(result["id"], session_factory=session_factory)

    async with session_scope(session_factory) as session:
        trace = await session.get(Trace, result["id"])
    assert trace is None


async def test_purge_trace_cleans_dangling_relation_referencing_it(session_factory, config, two_orgs):
    """A relation row's related_trace_id is a plain column, not an FK -- it
    would otherwise survive the referenced trace being purged. purge_trace
    must clean that up explicitly (see its own docstring)."""
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        original = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="original", context_text="c", solution_text="s", tags=[], agent_type="code",
        )
        amended = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="amended", context_text="c", solution_text="s", tags=[], agent_type="code",
        )
        # Simulate what amend_trace's relation bookkeeping produces: a
        # SUPERSEDED_BY edge on `original` pointing at `amended`.
        session.add(
            TraceRelation(
                trace_id=original["id"], related_trace_id=amended["id"], relationship_type="SUPERSEDED_BY"
            )
        )

    await manage.purge_trace(amended["id"], session_factory=session_factory)

    async with session_scope(session_factory) as session:
        dangling = (
            await session.execute(select(TraceRelation).where(TraceRelation.related_trace_id == amended["id"]))
        ).scalars().all()
    assert dangling == []


async def test_purge_trace_on_an_amended_original_also_removes_the_amendment(
    session_factory, config, two_orgs
):
    """Regression test: purge_trace used to delete only the exact id it was
    given. amend_trace carries most content forward into a NEW row rather
    than mutating in place, so purging the ORIGINAL id left the amended
    row -- holding the same (or superset) content -- fully intact. A
    deletion request against one link in a chain must remove the whole
    logical trace, not just that link."""
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        original = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="MARKER-ORIGINAL", context_text="c", solution_text="s", tags=[], agent_type="code",
        )
    async with session_scope(session_factory) as session:
        amended = await amend_trace(
            session, two_orgs["org_a"], original["id"], config, rate_limiter,
            title="MARKER-AMENDED", actor="test",
        )

    await manage.purge_trace(original["id"], session_factory=session_factory)

    async with session_scope(session_factory) as session:
        original_row = await session.get(Trace, original["id"])
        amended_row = await session.get(Trace, amended["id"])
    assert original_row is None
    assert amended_row is None, "amending, then purging the ORIGINAL id, must also remove the amendment"


async def test_purge_trace_on_the_amendment_also_removes_the_original(session_factory, config, two_orgs):
    """Same chain, purged from the other end: deleting the newest version
    must also remove the older version it superseded."""
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        original = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="MARKER-ORIGINAL-2", context_text="c", solution_text="s", tags=[], agent_type="code",
        )
    async with session_scope(session_factory) as session:
        amended = await amend_trace(
            session, two_orgs["org_a"], original["id"], config, rate_limiter,
            title="MARKER-AMENDED-2", actor="test",
        )

    await manage.purge_trace(amended["id"], session_factory=session_factory)

    async with session_scope(session_factory) as session:
        original_row = await session.get(Trace, original["id"])
        amended_row = await session.get(Trace, amended["id"])
    assert amended_row is None
    assert original_row is None, "purging the newest version in a chain must also remove the original"


async def test_purge_trace_unrelated_traces_survive(session_factory, config, two_orgs):
    """The chain walk must not over-reach: an unrelated trace (never
    amended, no supersedes link) must survive purging a completely
    different trace."""
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        target = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="target", context_text="c", solution_text="s", tags=[], agent_type="code",
        )
        bystander = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="bystander", context_text="c", solution_text="s", tags=[], agent_type="code",
        )

    await manage.purge_trace(target["id"], session_factory=session_factory)

    async with session_scope(session_factory) as session:
        bystander_row = await session.get(Trace, bystander["id"])
    assert bystander_row is not None


async def test_purge_trace_unknown_id_reports_error(session_factory, capsys):
    result = await manage.purge_trace("00000000-0000-0000-0000-000000000000", session_factory=session_factory)
    assert "no such trace" in capsys.readouterr().err
    assert result is False


async def test_purge_org_cascades_to_its_traces(session_factory, config, two_orgs):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        result = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="t", context_text="c", solution_text="s", tags=[], agent_type="code",
        )

    await manage.purge_org(two_orgs["org_a"], session_factory=session_factory)

    async with session_scope(session_factory) as session:
        org = await session.get(Organization, two_orgs["org_a"])
        trace = await session.get(Trace, result["id"])
        other_org_still_there = await session.get(Organization, two_orgs["org_b"])
    assert org is None
    assert trace is None
    assert other_org_still_there is not None


async def test_purge_org_unknown_id_reports_error(session_factory, capsys):
    result = await manage.purge_org("00000000-0000-0000-0000-000000000000", session_factory=session_factory)
    assert "no such organization" in capsys.readouterr().err
    assert result is False


async def test_argument_count_validation():
    assert manage.main(["purge-trace"]) == 2
    assert manage.main(["purge-trace", "a", "b"]) == 2
    assert manage.main(["list-quarantined", "a", "b"]) == 2  # takes 0 or 1, not 2


async def test_auth_import_is_used():
    # sanity: hub/manage.py's existing key-issuance commands are untouched
    assert auth.generate_raw_key().startswith("ct_live_")


@pytest.mark.filterwarnings("ignore:.*is marked with '@pytest.mark.asyncio'.*:pytest.PytestWarning")
def test_malformed_uuid_reports_a_clean_error_not_a_traceback(config, _schema, monkeypatch, capsys):
    """Regression test for a real bug: `main()` only caught (ValueError,
    LookupError), but a malformed id (`revoke-key not-a-uuid`) is rejected
    by the UUID column type itself -- asyncpg raises that as a driver-level
    error (sqlalchemy.exc.DBAPIError, a SQLAlchemyError, not a ValueError or
    LookupError) that fell through uncaught and dumped a raw traceback for
    the same kind of operator typo the branch above was meant to handle
    cleanly. `main()` builds its own session_factory from HUB_DATABASE_URL
    (not the session_factory fixture) and drives it with asyncio.run(), so
    this has to be a plain sync test -- calling main() from inside a
    already-running async test's event loop would itself raise.
    """
    monkeypatch.setenv("HUB_DATABASE_URL", config.database_url)
    exit_code = manage.main(["revoke-key", "not-a-uuid"])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "Traceback" not in err


@pytest.mark.filterwarnings("ignore:.*is marked with '@pytest.mark.asyncio'.*:pytest.PytestWarning")
def test_main_exits_nonzero_when_a_destructive_op_fails(config, monkeypatch, capsys):
    """The actual bug this fixes: revoke-key/release-quarantine/purge-trace/
    purge-org/commons-seed/set-plan/usage all print "error: ..." to stderr
    and return False on a failed lookup, but nothing about that is a raised
    exception -- there is nothing wrong with the CLI, the id just didn't
    resolve. Before main() checked the command's return value, every one of
    these failures still exited 0, so an automated incident script checking
    $? after e.g. `purge-org <id>` (to confirm a GDPR deletion actually
    happened) would see success on a no-op."""
    monkeypatch.setenv("HUB_DATABASE_URL", config.database_url)
    for command, unknown_id in (
        ("revoke-key", "00000000-0000-0000-0000-000000000000"),
        ("release-quarantine", "00000000-0000-0000-0000-000000000000"),
        ("purge-trace", "00000000-0000-0000-0000-000000000000"),
        ("purge-org", "00000000-0000-0000-0000-000000000000"),
    ):
        exit_code = manage.main([command, unknown_id])
        assert exit_code == 2, f"{command} on an unknown id must exit non-zero, got {exit_code}"
        assert capsys.readouterr().err.startswith("error:")

    exit_code = manage.main(["set-plan", "00000000-0000-0000-0000-000000000000", "free"])
    assert exit_code == 2
    capsys.readouterr()

    exit_code = manage.main(["set-plan", "00000000-0000-0000-0000-000000000000", "not-a-real-plan"])
    assert exit_code == 2


@pytest.mark.filterwarnings("ignore:.*is marked with '@pytest.mark.asyncio'.*:pytest.PytestWarning")
def test_main_dispatch_treats_only_false_as_failure():
    """Isolates main()'s own dispatch logic (no DB needed): a command
    returning False fails the CLI, True or None (every command with no
    failure path) succeeds."""

    async def _fake_fail(*args):
        return False

    async def _fake_ok(*args):
        return True

    async def _fake_none(*args):
        return None

    original = dict(manage._COMMANDS)
    try:
        manage._COMMANDS["__test_fail__"] = (_fake_fail, 0, 0)
        manage._COMMANDS["__test_ok__"] = (_fake_ok, 0, 0)
        manage._COMMANDS["__test_none__"] = (_fake_none, 0, 0)
        assert manage.main(["__test_fail__"]) == 2
        assert manage.main(["__test_ok__"]) == 0
        assert manage.main(["__test_none__"]) == 0
    finally:
        manage._COMMANDS.clear()
        manage._COMMANDS.update(original)
