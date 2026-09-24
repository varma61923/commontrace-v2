"""search_traces carries each lesson's causal evidence.

Before this, a search returned a lesson proven to help, one never
measured, and one measured to make outcomes WORSE in exactly the same
shape. The holdout knew the difference; only working_set -- which shows
the winners by construction -- ever said so. These tests pin that the
verdict now travels with the result, that it is always the SAME verdict
causal_effects reports, that it is withheld when the experiment is
compromised, and that the per-org cache behind it neither serves stale
evidence nor recomputes on every call.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from hub import crud
from hub.db import session_scope
from hub.models import Organization, Trace
from hub.tests.test_holdout import _assign, _resolve

pytestmark = pytest.mark.asyncio

TAG = "evidence-probe"


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="fleet", holdout_rate=0.5, holdout_salt="salt-one")
        session.add(o)
        await session.flush()
        return o.id


@pytest_asyncio.fixture
async def other_org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="other", holdout_rate=0.5, holdout_salt="salt-other")
        session.add(o)
        await session.flush()
        return o.id


@pytest.fixture(autouse=True)
def _fresh_evidence_cache():
    crud._evidence_cache.clear()
    yield
    crud._evidence_cache.clear()


async def _lessons(session_factory, org_id, titles):
    ids = []
    async with session_scope(session_factory) as session:
        for title in titles:
            t = Trace(org_id=org_id, title=title, context_text=f"context for {title}",
                      solution_text=f"solution for {title}", tags=[TAG], agent_type="code")
            session.add(t)
            await session.flush()
            ids.append(t.id)
    return ids


async def _drive(session_factory, org_id, trace, n, p_injected, p_withheld, prefix):
    """The same seeded two-arm run test_holdout.py uses, through the real
    assign/record path."""
    injected_seen = withheld_seen = 0
    for i in range(n):
        occ = f"{prefix}-{i}"
        result = await _assign(session_factory, org_id, [trace], occ)
        if result["inject"]:
            injected_seen += 1
            ok = (injected_seen % 100) < int(p_injected * 100)
        else:
            withheld_seen += 1
            ok = (withheld_seen % 100) < int(p_withheld * 100)
        await _resolve(session_factory, org_id, occ, ok)


async def _search(session_factory, org_id):
    async with session_scope(session_factory) as session:
        return await crud.search_traces(session, org_id, tags=[TAG], limit=20)


async def _causal(session_factory, org_id):
    async with session_scope(session_factory) as session:
        return await crud.causal_effects(session, org_id)


class TestTheVerdictTravelsWithTheResult:
    async def test_helps_hurts_and_unmeasured_are_told_apart(self, session_factory, org):
        helps, hurts, untested = await _lessons(
            session_factory, org, ["proven lesson", "harmful lesson", "untested lesson"]
        )
        await _drive(session_factory, org, helps, 400, 0.80, 0.40, "h")
        await _drive(session_factory, org, hurts, 400, 0.35, 0.75, "x")

        result = await _search(session_factory, org)
        by_id = {t["id"]: t for t in result["traces"]}
        assert by_id[helps]["evidence"]["verdict"] == "HELPS"
        assert by_id[hurts]["evidence"]["verdict"] == "HURTS"
        assert by_id[untested]["evidence"] == {"verdict": "NOT_MEASURED"}
        assert result["evidence"]["available"] is True

        helps_evidence = by_id[helps]["evidence"]
        assert helps_evidence["effect"] > 0.2
        assert helps_evidence["ci_95"][0] > 0
        assert helps_evidence["n_injected"] > 0 and helps_evidence["n_withheld"] > 0

    async def test_it_is_the_same_verdict_causal_effects_reports(self, session_factory, org):
        """One analysis, two surfaces. A search that said HELPS while the
        experiment report said UNDERPOWERED would be worse than saying
        nothing."""
        ids = await _lessons(session_factory, org, ["lesson a", "lesson b"])
        await _drive(session_factory, org, ids[0], 60, 0.9, 0.3, "a")
        await _drive(session_factory, org, ids[1], 400, 0.8, 0.4, "b")

        report = await _causal(session_factory, org)
        expected = {e["trace_id"]: e["verdict"] for e in report["effects"]}
        result = await _search(session_factory, org)
        got = {t["id"]: t["evidence"]["verdict"] for t in result["traces"]}
        assert got == expected

    async def test_an_org_that_never_ran_an_experiment_gets_no_extra_fields(
        self, session_factory, org
    ):
        """Every field costs the calling agent tokens on every search;
        "not measured" on every row of an org that never measured anything
        says nothing."""
        await _lessons(session_factory, org, ["plain lesson"])
        result = await _search(session_factory, org)
        assert "evidence" not in result
        assert all("evidence" not in t for t in result["traces"])

    async def test_evidence_never_crosses_orgs(self, session_factory, org, other_org):
        [mine] = await _lessons(session_factory, org, ["mine"])
        await _drive(session_factory, org, mine, 400, 0.8, 0.4, "m")
        await _lessons(session_factory, other_org, ["theirs"])

        result = await _search(session_factory, other_org)
        assert "evidence" not in result
        assert all(t["id"] != mine for t in result["traces"])


class TestACompromisedExperimentShowsNoNumbers:
    async def test_the_reporting_gap_case(self, session_factory, org):
        """Built the way test_holdout.py builds it: every injected occasion
        reported, most withheld ones not. The effects are biased by a named
        mechanism, so none are shown -- only why."""
        [trace] = await _lessons(session_factory, org, ["biased lesson"])
        for i in range(120):
            result = await _assign(session_factory, org, [trace], f"g-{i}")
            if result["inject"] or i % 4 == 0:
                await _resolve(session_factory, org, f"g-{i}", i % 2 == 0)

        assert (await _causal(session_factory, org))["integrity"]["effects_readable"] is False
        result = await _search(session_factory, org)
        assert result["evidence"]["available"] is False
        assert "COMPROMISED" in result["evidence"]["reason"]
        assert all("evidence" not in t for t in result["traces"])


class TestTheCache:
    async def _counting(self, monkeypatch):
        calls = {"n": 0}
        real = crud.causal_effects

        async def counted(session, org_id, *a, **kw):
            calls["n"] += 1
            return await real(session, org_id, *a, **kw)

        monkeypatch.setattr(crud, "causal_effects", counted)
        return calls

    async def test_repeated_searches_do_not_recompute(self, session_factory, org, monkeypatch):
        [trace] = await _lessons(session_factory, org, ["cached lesson"])
        await _drive(session_factory, org, trace, 40, 0.8, 0.4, "c")
        calls = await self._counting(monkeypatch)
        for _ in range(5):
            await _search(session_factory, org)
        assert calls["n"] == 1

    async def test_a_new_outcome_recomputes(self, session_factory, org, monkeypatch):
        [trace] = await _lessons(session_factory, org, ["moving lesson"])
        await _drive(session_factory, org, trace, 40, 0.8, 0.4, "d")
        calls = await self._counting(monkeypatch)
        await _search(session_factory, org)
        await _assign(session_factory, org, [trace], "d-new")
        await _search(session_factory, org)
        assert calls["n"] == 2, "a new assignment must invalidate the evidence"
        await _resolve(session_factory, org, "d-new", True)
        await _search(session_factory, org)
        assert calls["n"] == 3, "a newly recorded outcome must invalidate the evidence"

    async def test_a_new_experiment_recomputes(self, session_factory, org, monkeypatch):
        [trace] = await _lessons(session_factory, org, ["rotated lesson"])
        await _drive(session_factory, org, trace, 40, 0.8, 0.4, "e")
        calls = await self._counting(monkeypatch)
        await _search(session_factory, org)
        async with session_scope(session_factory) as session:
            (await session.get(Organization, org)).holdout_salt = "salt-two"
        result = await _search(session_factory, org)
        assert calls["n"] == 1, "no data under the new salt: answered without the analysis"
        assert "evidence" not in result

    async def test_it_expires_with_time_alone(self, session_factory, org, monkeypatch):
        """The validity audit judges pending occasions by their age, so its
        answer can change with no new data at all."""
        [trace] = await _lessons(session_factory, org, ["aging lesson"])
        await _drive(session_factory, org, trace, 40, 0.8, 0.4, "f")
        calls = await self._counting(monkeypatch)
        await _search(session_factory, org)
        monkeypatch.setattr(crud, "_EVIDENCE_TTL_SECONDS", 0.0)
        await _search(session_factory, org)
        assert calls["n"] == 2
