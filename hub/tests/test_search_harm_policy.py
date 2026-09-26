"""search_traces can withdraw a trace the org's experiment measured making
outcomes WORSE (commontrace/harm.py, Organization.harm_policy).

These pin what an org opts into -- the trace stops being returned, is named
with its evidence where it would have appeared, and its near-duplicates go
with it -- and the ways it must not disturb the experiment: nothing moves
under the default, nothing is acted on while the evidence is unreadable, a
withdrawn trace is never assigned an arm and is not counted as retrieved.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from hub import crud, manage
from hub.db import session_scope
from hub.models import AuditLogEntry, HoldoutObservation, Organization, Trace
from hub.tests.test_search_evidence import TAG, _drive, _lessons

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_evidence_cache():
    crud._evidence_cache.clear()
    yield
    crud._evidence_cache.clear()


@pytest_asyncio.fixture
async def harmed(session_factory):
    """An org whose experiment has established that BAD hurts, and a GOOD
    trace created after it (so GOOD sorts first on a tag-only search)."""
    async with session_scope(session_factory) as session:
        o = Organization(name="fleet", holdout_rate=0.5, holdout_salt="harm-salt")
        session.add(o)
        await session.flush()
        org_id = o.id
    bad, good = await _lessons(session_factory, org_id, ["paste the reset link", "verify the owner"])
    # Ground truth, seeded: an occasion succeeds exactly when BAD is withheld.
    await _drive(session_factory, org_id, bad, 200, 0.0, 1.0, "bad")
    return org_id, bad, good


async def _search(session_factory, org_id, **kw):
    async with session_scope(session_factory) as session:
        return await crud.search_traces(session, org_id, tags=[TAG], **kw)


async def _set_policy(session_factory, org_id, policy):
    async with session_scope(session_factory) as session:
        (await session.get(Organization, org_id)).harm_policy = policy


async def test_the_fixture_establishes_harm(session_factory, harmed):
    org_id, bad, good = harmed
    by_id = {t["id"]: t for t in (await _search(session_factory, org_id))["traces"]}
    assert by_id[bad]["evidence"]["verdict"] == "HURTS"


async def test_by_default_it_is_still_returned_with_its_verdict(session_factory, harmed):
    org_id, bad, _good = harmed
    async with session_scope(session_factory) as session:
        assert (await session.get(Organization, org_id)).harm_policy == "inform"
    result = await _search(session_factory, org_id)
    assert bad in {t["id"] for t in result["traces"]}
    assert "withdrawn" not in result


async def test_withdraw_stops_returning_it_and_names_it(session_factory, harmed):
    org_id, bad, good = harmed
    await _set_policy(session_factory, org_id, "withdraw")
    result = await _search(session_factory, org_id)

    assert [t["id"] for t in result["traces"]] == [good]
    [entry] = result["withdrawn"]
    assert entry["id"] == bad
    assert entry["reason"] == "measured_harm"
    assert entry["title"] == "paste the reset link"
    assert entry["evidence"]["verdict"] == "HURTS"
    assert entry["evidence"]["effect"] < 0
    # Named, not handed over.
    assert "solution_text" not in entry and "context_text" not in entry
    assert "HURTS" in result["withdrawn_note"]


async def test_it_is_named_only_on_the_page_it_would_have_appeared_on(session_factory, harmed):
    """Tag-only search sorts newest first: GOOD, then BAD. With one result
    per page, BAD would have been on page two -- so page one does not
    claim to have kept it out, and page two does."""
    org_id, bad, good = harmed
    await _set_policy(session_factory, org_id, "withdraw")

    first = await _search(session_factory, org_id, limit=1, offset=0)
    assert [t["id"] for t in first["traces"]] == [good]
    assert "withdrawn" not in first
    assert first["has_more"] is False

    second = await _search(session_factory, org_id, limit=1, offset=1)
    assert second["traces"] == []
    assert [w["id"] for w in second["withdrawn"]] == [bad]


async def test_its_near_duplicates_go_with_it(session_factory, harmed):
    """The holdout measured BAD's near-duplicate cluster as one unit under
    BAD's id, so the verdict is the cluster's. A re-telling left in the
    results would hand the same content straight back."""
    org_id, bad, good = harmed
    async with session_scope(session_factory) as session:
        original = await session.get(Trace, bad)
        copy = Trace(org_id=org_id, title=original.title, context_text=original.context_text,
                     solution_text=original.solution_text, tags=[TAG], agent_type="code")
        session.add(copy)
        await session.flush()
        copy_id = copy.id
    await _set_policy(session_factory, org_id, "withdraw")

    result = await _search(session_factory, org_id)
    assert [t["id"] for t in result["traces"]] == [good]
    by_id = {w["id"]: w for w in result["withdrawn"]}
    assert by_id[copy_id]["reason"] == "near_duplicate_of_withdrawn"
    assert by_id[copy_id]["duplicate_of"] == bad
    assert by_id[copy_id]["evidence"]["verdict"] == "HURTS"


async def test_an_amendment_is_not_withdrawn_with_the_text_it_replaced(session_factory, harmed):
    """An amendment is a near-duplicate of what it superseded by
    construction, and usually the fix. Withdrawing it on the strength of
    the old text would block the one remedy for a harmful lesson; it has
    its own id and gets its own trial."""
    org_id, bad, good = harmed
    async with session_scope(session_factory) as session:
        original = await session.get(Trace, bad)
        original.superseded_at = datetime.now(timezone.utc)
        fixed = Trace(org_id=org_id, title=original.title, context_text=original.context_text,
                      solution_text=original.solution_text + " Only after verifying the owner.",
                      tags=[TAG], agent_type="code", supersedes_trace_id=bad)
        session.add(fixed)
        await session.flush()
        fixed_id = fixed.id
    await _set_policy(session_factory, org_id, "withdraw")

    result = await _search(session_factory, org_id)
    assert fixed_id in {t["id"] for t in result["traces"]}
    assert fixed_id not in {w["id"] for w in result.get("withdrawn", [])}


async def test_a_query_with_nothing_searchable_still_answers(session_factory, harmed):
    """The branch that skips the search entirely must not trip over the
    policy it never got to apply."""
    org_id, _bad, _good = harmed
    await _set_policy(session_factory, org_id, "withdraw")
    async with session_scope(session_factory) as session:
        result = await crud.search_traces(session, org_id, query="the and of")
    assert result["traces"] == []
    assert "withdrawn" not in result


async def test_a_withdrawn_trace_is_never_assigned_an_arm(session_factory, harmed):
    org_id, bad, good = harmed
    await _set_policy(session_factory, org_id, "withdraw")
    async with session_scope(session_factory) as session:
        result = await crud.search_traces(session, org_id, tags=[TAG])
        await crud.holdout_for_results(session, org_id, result["traces"], "after-withdraw")
    async with session_scope(session_factory) as session:
        assigned = set((await session.execute(
            select(HoldoutObservation.trace_id).where(
                HoldoutObservation.org_id == org_id,
                HoldoutObservation.occasion_id == "after-withdraw",
            )
        )).scalars().all())
    assert assigned == {good}


async def test_a_withdrawn_trace_is_not_counted_as_retrieved(session_factory, harmed):
    org_id, bad, _good = harmed
    async with session_scope(session_factory) as session:
        before = (await session.get(Trace, bad)).retrievals
    await _set_policy(session_factory, org_id, "withdraw")
    await _search(session_factory, org_id)
    async with session_scope(session_factory) as session:
        assert (await session.get(Trace, bad)).retrievals == before


async def test_unreadable_evidence_withdraws_nothing(session_factory, harmed, monkeypatch):
    """A COMPROMISED experiment yields no evidence, and effects a named
    mechanism is biasing must not steer what the fleet is given."""
    org_id, bad, _good = harmed
    await _set_policy(session_factory, org_id, "withdraw")

    async def compromised(session, org_id):
        return {"available": False, "reason": "COMPROMISED", "measured_at": "", "by_trace": {}}

    monkeypatch.setattr(crud, "causal_evidence", compromised)
    result = await _search(session_factory, org_id)
    assert bad in {t["id"] for t in result["traces"]}
    assert "withdrawn" not in result


async def test_a_new_experiment_gives_it_a_second_trial(session_factory, harmed):
    org_id, bad, _good = harmed
    await _set_policy(session_factory, org_id, "withdraw")
    async with session_scope(session_factory) as session:
        (await session.get(Organization, org_id)).holdout_salt = "fresh-salt"
    result = await _search(session_factory, org_id)
    assert bad in {t["id"] for t in result["traces"]}
    assert "withdrawn" not in result


async def test_it_never_reaches_another_org(session_factory, harmed):
    org_id, bad, _good = harmed
    await _set_policy(session_factory, org_id, "withdraw")
    async with session_scope(session_factory) as session:
        other = Organization(name="other", holdout_rate=0.5, holdout_salt="other-salt",
                             harm_policy="withdraw")
        session.add(other)
        await session.flush()
        other_id = other.id
    [theirs] = await _lessons(session_factory, other_id, ["paste the reset link"])
    result = await _search(session_factory, other_id)
    assert [t["id"] for t in result["traces"]] == [theirs]
    assert "withdrawn" not in result


class TestTheOperatorCommand:
    async def test_it_shows_and_sets_the_policy_and_audits_the_change(
        self, session_factory, harmed, capsys
    ):
        org_id, _bad, _good = harmed
        assert await manage.harm_policy(org_id, session_factory=session_factory)
        assert "harm policy: inform" in capsys.readouterr().out

        assert await manage.harm_policy(org_id, "withdraw", session_factory=session_factory)
        async with session_scope(session_factory) as session:
            assert (await session.get(Organization, org_id)).harm_policy == "withdraw"
            events = (await session.execute(
                select(AuditLogEntry).where(AuditLogEntry.action == "set_harm_policy")
            )).scalars().all()
        assert [e.summary for e in events] == ["inform -> withdraw"]

    async def test_an_unknown_policy_is_refused(self, session_factory, harmed):
        org_id, _bad, _good = harmed
        assert not await manage.harm_policy(org_id, "delete", session_factory=session_factory)
        async with session_scope(session_factory) as session:
            assert (await session.get(Organization, org_id)).harm_policy == "inform"
