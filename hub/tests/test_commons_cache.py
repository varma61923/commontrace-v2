"""The Knowledge Base corpus cache: fast, and never wrong.

hub/commons_cache.py keeps each process's copy of the matchable corpus in
memory and reloads it only when `commons_corpus_state` moves. Two things
have to be true for that to be safe, and each is tested directly rather
than through the matching tools, because the matching tools re-check
visibility on every matched row (hub/crud.py:_commons_rows) -- which would
let a broken trigger hide behind that second check in any end-to-end test.

  1. The version moves on every change that can alter what the matcher
     sees, and on nothing else. Too few triggers is stale results; too
     many is a reload on every vote and every query, which is the cost
     this exists to remove.
  2. The cached path returns exactly what the direct path returns, on
     corpora with every complication at once: the caller's own entries,
     retracted, quarantined, superseded and non-seed rows, agent_type
     filtering, and a scan cap that truncates.
"""
from __future__ import annotations

import importlib.util
import pathlib
import random
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, update

from hub import commons, commons_cache, crud
from hub.db import session_scope
from hub.models import COMMONS_CORPUS_TRIGGER_DDL, Organization, Trace

pytestmark = pytest.mark.asyncio

WORDS = (
    "stripe webhook retry timeout pool connection postgres hydration react date "
    "idempotency cache redis lock deadlock migration schema index vacuum kubernetes "
    "crashloop dns tls certificate token jwt refresh cors bundle vite import cycle"
).split()


@pytest_asyncio.fixture
async def orgs(session_factory):
    async with session_scope(session_factory) as session:
        made = {}
        for name in ("operator", "reader", "other"):
            o = Organization(name=f"{name}-{uuid.uuid4().hex[:6]}", plan="operator" if name == "operator" else "scale")
            session.add(o)
            await session.flush()
            made[name] = o.id
        return made


@pytest.fixture(autouse=True)
def _fresh_cache():
    commons_cache.invalidate()
    yield
    commons_cache.invalidate()


async def _seed(session_factory, org_id, title, *, context="", agent_type="code", **kw):
    async with session_scope(session_factory) as session:
        t = Trace(
            org_id=org_id, title=title, context_text=context, solution_text="fix: " + title,
            tags=[], agent_type=agent_type, shared_with_commons=True,
            shared_at=datetime.now(timezone.utc), shared_rationale="seed",
            commons_source="seed", commons_signature=commons.signature_for(title, context, []),
            **kw,
        )
        session.add(t)
        await session.flush()
        return t.id


async def _key(session_factory):
    async with session_scope(session_factory) as session:
        return await commons_cache._current_key(session)


async def _set(session_factory, trace_id, **values):
    async with session_scope(session_factory) as session:
        await session.execute(update(Trace).where(Trace.id == trace_id).values(**values))


class TestTheTriggersTheDatabaseRuns:
    async def test_the_migration_installs_exactly_what_the_models_do(self):
        """Tests build the schema from the models; deployed databases get it
        from the migration. Two copies of the same SQL is a drift waiting to
        happen, so they are compared token for token."""
        path = (
            pathlib.Path(__file__).resolve().parents[1]
            / "alembic" / "versions" / "c3e8a1f05b92_commons_corpus_version.py"
        )
        spec = importlib.util.spec_from_file_location("commons_corpus_migration", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        def tokens(sql):
            return " ".join(sql.replace("(", " ( ").replace(")", " ) ").replace(",", " , ").split())

        assert [tokens(s) for s in mod.TRIGGER_DDL] == [tokens(s) for s in COMMONS_CORPUS_TRIGGER_DDL]


class TestTheVersionMovesExactlyWhenTheCorpusDoes:
    async def test_adding_an_entry(self, session_factory, orgs):
        before = await _key(session_factory)
        await _seed(session_factory, orgs["operator"], "stripe webhook retried twice")
        assert await _key(session_factory) != before

    @pytest.mark.parametrize(
        "change",
        [
            {"commons_retracted_at": datetime.now(timezone.utc)},
            {"quarantined": True},
            {"superseded_at": datetime.now(timezone.utc)},
            {"shared_with_commons": False},
            {"commons_source": "org"},
            {"agent_type": "support"},
            {"commons_signature": commons.signature_for("entirely different text", "", [])},
        ],
        ids=lambda c: next(iter(c)),
    )
    async def test_every_change_that_alters_what_the_matcher_sees(self, session_factory, orgs, change):
        tid = await _seed(session_factory, orgs["operator"], "postgres pool exhausted")
        before = await _key(session_factory)
        await _set(session_factory, tid, **change)
        assert await _key(session_factory) != before

    async def test_restoring_a_retracted_entry(self, session_factory, orgs):
        tid = await _seed(session_factory, orgs["operator"], "tls certificate expired")
        await _set(session_factory, tid, commons_retracted_at=datetime.now(timezone.utc))
        before = await _key(session_factory)
        await _set(session_factory, tid, commons_retracted_at=None)
        assert await _key(session_factory) != before

    async def test_deleting_an_entry(self, session_factory, orgs):
        tid = await _seed(session_factory, orgs["operator"], "dns lookup failed")
        before = await _key(session_factory)
        async with session_scope(session_factory) as session:
            await session.execute(delete(Trace).where(Trace.id == tid))
        assert await _key(session_factory) != before

    @pytest.mark.parametrize(
        "change",
        [{"commons_hits": 41}, {"trust": 0.9, "commons_votes": 7}, {"retrievals": 3},
         {"solution_text": "a better fix"}],
        ids=lambda c: next(iter(c)),
    )
    async def test_not_on_the_counters_that_change_on_every_query_and_vote(
        self, session_factory, orgs, change
    ):
        """These are never cached -- matched rows are read fresh -- so a
        bump here would reload the whole corpus on every request for nothing."""
        tid = await _seed(session_factory, orgs["operator"], "redis lock deadlock")
        before = await _key(session_factory)
        await _set(session_factory, tid, **change)
        assert await _key(session_factory) == before

    async def test_not_on_ordinary_customer_traces(self, session_factory, orgs):
        """The overwhelming majority of writes. Bumping one shared row on
        each of them would serialize every customer's capture."""
        before = await _key(session_factory)
        async with session_scope(session_factory) as session:
            session.add(Trace(org_id=orgs["reader"], title="private", context_text="x", solution_text="y",
                              tags=[], agent_type="code"))
        assert await _key(session_factory) == before

    async def test_a_no_op_update_does_not_count(self, session_factory, orgs):
        """UPDATE OF fires when a column is merely named in SET; the WHEN
        clause is what stops an unchanged value from reloading the corpus."""
        tid = await _seed(session_factory, orgs["operator"], "cors preflight blocked")
        before = await _key(session_factory)
        await _set(session_factory, tid, agent_type="code")
        assert await _key(session_factory) == before


class TestTheSnapshot:
    async def test_it_is_reused_until_the_corpus_changes(self, session_factory, orgs):
        await _seed(session_factory, orgs["operator"], "vite import cycle")
        async with session_scope(session_factory) as session:
            first = await commons_cache.snapshot(session, crud.commons_visible())
            again = await commons_cache.snapshot(session, crud.commons_visible())
        assert again is first
        await _seed(session_factory, orgs["operator"], "jwt refresh race")
        async with session_scope(session_factory) as session:
            rebuilt = await commons_cache.snapshot(session, crud.commons_visible())
        assert rebuilt is not first
        assert rebuilt.size == first.size + 1

    async def test_an_entry_retracted_after_the_snapshot_is_not_served(self, session_factory, orgs):
        """The window the version cannot close by itself: a snapshot taken a
        moment before a retraction. Forced here by pinning the stale snapshot
        as current, and required to be caught by the fresh re-read of each
        matched row."""
        title, ctx = "stripe webhook delivered more than once", "handler ran twice after timeout"
        tid = await _seed(session_factory, orgs["operator"], title, context=ctx)
        async with session_scope(session_factory) as session:
            stale = await commons_cache.snapshot(session, crud.commons_visible())
        await _set(session_factory, tid, commons_retracted_at=datetime.now(timezone.utc))
        key_now = await _key(session_factory)
        commons_cache._snapshot = commons_cache.Snapshot(
            key=key_now, ids=stale.ids, org_ids=stale.org_ids,
            agent_types=stale.agent_types, signatures=stale.signatures,
        )
        failure = {"label": "f", "signature": commons.signature_for(title, ctx, [])}
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(session, orgs["reader"], [failure])
            search = await crud.commons_search(session, orgs["reader"], failure["signature"])
        assert report["n_covered"] == 0 and report["matches"] == []
        assert tid not in [c["trace"]["id"] for c in search["candidates"]]


def _strip_volatile(result):
    """Drop fields that legitimately differ between two calls made one after
    the other: timestamps, and hit counts that the first call itself raised."""
    def scrub(obj):
        if isinstance(obj, dict):
            return {k: scrub(v) for k, v in obj.items() if k not in ("commons_hits", "hits")}
        if isinstance(obj, list):
            return [scrub(v) for v in obj]
        return obj
    return scrub(result)


class TestTheCachedPathMatchesTheDirectPathExactly:
    """The claim the whole change rests on, checked on corpora built to hit
    every rule at once, with the scan cap forced low enough to truncate."""

    @pytest.mark.parametrize("seed", [1, 2, 3])
    async def test_identical_results(self, session_factory, orgs, monkeypatch, seed):
        rng = random.Random(seed)

        def text():
            return " ".join(rng.choice(WORDS) for _ in range(rng.randint(4, 9)))

        owners = [orgs["operator"], orgs["operator"], orgs["operator"], orgs["reader"], orgs["other"]]
        for _ in range(60):
            extra = rng.choice([
                {}, {}, {}, {},
                {"commons_retracted_at": datetime.now(timezone.utc)},
                {"quarantined": True},
                {"superseded_at": datetime.now(timezone.utc)},
            ])
            tid = await _seed(session_factory, rng.choice(owners), text(), context=text(),
                              agent_type=rng.choice(["code", "code", "support"]), **extra)
            if rng.random() < 0.1:
                await _set(session_factory, tid, commons_source="org")  # shared but not operator-curated

        failures = [{"label": f"f{i}", "signature": commons.signature_for(text(), "", [])} for i in range(12)]
        query = failures[0]["signature"]
        monkeypatch.setattr(commons, "max_corpus_scan", lambda: 9)

        async def run(cached: bool):
            monkeypatch.setattr(commons_cache, "available", lambda: cached)
            commons_cache.invalidate()
            out = []
            for agent_type in ("", "code"):
                async with session_scope(session_factory) as session:
                    out.append(await crud.commons_overlap(
                        session, orgs["reader"], failures, threshold=0.05, agent_type=agent_type))
                async with session_scope(session_factory) as session:
                    out.append(await crud.commons_search(
                        session, orgs["reader"], query, limit=10, agent_type=agent_type))
            return _strip_volatile(out)

        direct = await run(cached=False)
        cached = await run(cached=True)
        assert cached == direct
        # And the comparison is not vacuous: the corpus was truncated, and
        # both paths found real matches in it.
        assert direct[0]["corpus_truncated"] is True
        assert direct[0]["n_covered"] > 0
        assert direct[1]["n_candidates"] > 0
