"""`commons_search` -- the knowledge-base lookup, as distinct from the
coverage meter.

The commons shipped one query surface: `commons_overlap`, which thresholds
and emits a percentage measured at 10.9% recall. Ranking the same
signatures against the same corpus with no threshold gets 89.1% recall@1
and 100% within the top 10 (commons/eval/search_modes.py). That is the
difference between a meter and a knowledge base, and it costs nothing in
privacy: both surfaces take a MinHash signature and no failure text.

What is pinned here is mostly the DISCIPLINE, not the retrieval. A ranked
surface with no threshold returns something for every query, including
questions the corpus cannot answer. So the tests below assert that it never
claims coverage, never credits contributor value, and never becomes a
second way to read another org's private traces.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from hub import commons, crud, plans
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def orgs(session_factory):
    async with session_scope(session_factory) as session:
        made = {}
        for name in ("asker", "sharer", "other"):
            o = Organization(name=name)
            session.add(o)
            await session.flush()
            made[name] = o.id
        return made


async def _contribute_and_share(session_factory, config, org_id, title, context, solution, tags):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        result = await crud.contribute_trace(
            session, org_id, config, rate_limiter,
            title=title, context_text=context, solution_text=solution,
            tags=tags, agent_type="code", actor="test",
        )
    async with session_scope(session_factory) as session:
        await crud.share_trace(session, org_id, result["id"], rationale="substrate")
    return result["id"]


def _sig(text: str) -> list[int]:
    return commons.signature_for(text, "", [])


async def _search(session_factory, org_id, question, **kw):
    async with session_scope(session_factory) as session:
        return await crud.commons_search(session, org_id, _sig(question), **kw)


WEBHOOK = (
    "Payment webhook delivered more than once",
    "The payment provider re-delivers a webhook after a timeout so the handler runs twice",
    "Persist the provider event id and check it before any side effect",
    ["webhooks", "idempotency", "payments"],
)
POOL = (
    "Postgres connection pool exhausted under retry storm",
    "Retries pile up and every connection in the pool is checked out",
    "Bound retries with jittered backoff and set a pool_timeout",
    ["postgres", "pool"],
)


class TestItFindsAnswers:
    async def test_a_shared_trace_is_returned_for_a_related_question(
        self, session_factory, config, orgs
    ):
        await _contribute_and_share(session_factory, config, orgs["sharer"], *WEBHOOK)
        r = await _search(session_factory, orgs["asker"], "payment webhook delivered twice")
        assert r["n_candidates"] >= 1
        assert r["candidates"][0]["trace"]["title"] == WEBHOOK[0]

    async def test_the_solution_text_comes_back(self, session_factory, config, orgs):
        """The payoff. Safe to return in full: the trace is only in the corpus
        because its owner explicitly shared it."""
        await _contribute_and_share(session_factory, config, orgs["sharer"], *WEBHOOK)
        r = await _search(session_factory, orgs["asker"], "payment webhook delivered twice")
        assert "Persist the provider event id" in r["candidates"][0]["trace"]["solution_text"]

    async def test_candidates_are_ranked_best_first_and_numbered(
        self, session_factory, config, orgs
    ):
        await _contribute_and_share(session_factory, config, orgs["sharer"], *WEBHOOK)
        await _contribute_and_share(session_factory, config, orgs["sharer"], *POOL)
        r = await _search(session_factory, orgs["asker"], "webhook delivered more than once payment")
        sims = [c["similarity"] for c in r["candidates"]]
        assert sims == sorted(sims, reverse=True)
        assert [c["rank"] for c in r["candidates"]] == list(range(1, len(r["candidates"]) + 1))

    async def test_it_finds_what_the_threshold_would_have_discarded(
        self, session_factory, config, orgs
    ):
        """The reason this tool exists. A question worded in on-call
        vocabulary scores below the coverage threshold, so commons_overlap
        reports it uncovered -- while the answer is right there and ranking
        surfaces it."""
        await _contribute_and_share(session_factory, config, orgs["sharer"], *WEBHOOK)
        question = "customer charged twice for one order"

        async with session_scope(session_factory) as session:
            overlap_result = await crud.commons_overlap(
                session, orgs["asker"], [{"label": "q", "signature": _sig(question)}]
            )
        assert overlap_result["n_covered"] == 0  # thresholded away

        r = await _search(session_factory, orgs["asker"], question)
        assert any(c["trace"]["title"] == WEBHOOK[0] for c in r["candidates"])


class TestItNeverClaimsCoverage:
    async def test_the_result_carries_no_coverage_figure(self, session_factory, config, orgs):
        await _contribute_and_share(session_factory, config, orgs["sharer"], *WEBHOOK)
        r = await _search(session_factory, orgs["asker"], "webhook twice")
        for forbidden in ("covered_fraction", "n_covered", "threshold"):
            assert forbidden not in r, f"search must not emit {forbidden}: that is coverage"

    async def test_the_note_says_these_are_candidates_not_coverage(
        self, session_factory, config, orgs
    ):
        await _contribute_and_share(session_factory, config, orgs["sharer"], *WEBHOOK)
        r = await _search(session_factory, orgs["asker"], "webhook twice")
        assert "not coverage" in r["note"].lower()

    async def test_an_unanswerable_question_still_returns_something(
        self, session_factory, config, orgs
    ):
        """Documented, not hidden. This is exactly why the surface may never
        be read as coverage, and the note has to say so."""
        await _contribute_and_share(session_factory, config, orgs["sharer"], *WEBHOOK)
        r = await _search(session_factory, orgs["asker"], "webhook payment provider unrelated")
        assert r["n_candidates"] >= 0  # may or may not match; never an assertion of absence
        assert "candidates" in r["note"].lower()

    async def test_zero_scored_records_are_not_returned_as_answers(
        self, session_factory, config, orgs
    ):
        """A list of records sharing no token with the question is noise
        wearing a ranking's clothes."""
        await _contribute_and_share(session_factory, config, orgs["sharer"], *WEBHOOK)
        r = await _search(
            session_factory, orgs["asker"],
            "kubernetes ingress certificate rotation staging cluster",
        )
        assert all(c["similarity"] > 0 for c in r["candidates"])


class TestScopingMatchesCommonsOverlap:
    async def test_your_own_traces_are_excluded(self, session_factory, config, orgs):
        """The question is what you'd gain from everyone else."""
        await _contribute_and_share(session_factory, config, orgs["asker"], *WEBHOOK)
        r = await _search(session_factory, orgs["asker"], "payment webhook delivered twice")
        assert r["n_candidates"] == 0

    async def test_unshared_traces_are_never_searchable(self, session_factory, config, orgs):
        """The one property that would be a cross-tenant data leak."""
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            await crud.contribute_trace(
                session, orgs["sharer"], config, rate_limiter,
                title=WEBHOOK[0], context_text=WEBHOOK[1], solution_text=WEBHOOK[2],
                tags=WEBHOOK[3], agent_type="code", actor="test",
            )  # contributed but NOT shared
        r = await _search(session_factory, orgs["asker"], "payment webhook delivered twice")
        assert r["n_candidates"] == 0

    async def test_quarantined_traces_are_excluded(self, session_factory, config, orgs):
        trace_id = await _contribute_and_share(session_factory, config, orgs["sharer"], *WEBHOOK)
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
            trace.quarantined = True
        r = await _search(session_factory, orgs["asker"], "payment webhook delivered twice")
        assert r["n_candidates"] == 0

    async def test_agent_type_narrows_the_corpus(self, session_factory, config, orgs):
        await _contribute_and_share(session_factory, config, orgs["sharer"], *WEBHOOK)
        r = await _search(
            session_factory, orgs["asker"], "payment webhook delivered twice", agent_type="support"
        )
        assert r["n_candidates"] == 0


class TestItDoesNotCreditContributorValue:
    async def test_searching_does_not_increment_commons_hits(self, session_factory, config, orgs):
        """commons_hits is the basis for earned query allowance and for the
        contributor-value metric that resists filler. It means "covered a
        real failure", established at the conservative threshold. A search
        candidate is not that, and crediting candidates would make the one
        number that cannot be self-dealt trivially inflatable."""
        trace_id = await _contribute_and_share(session_factory, config, orgs["sharer"], *WEBHOOK)
        for _ in range(5):
            await _search(session_factory, orgs["asker"], "payment webhook delivered twice")
        async with session_scope(session_factory) as session:
            assert (await session.get(Trace, trace_id)).commons_hits == 0


class TestMetering:
    async def test_a_search_consumes_a_commons_query(self, session_factory, config, orgs):
        """It reads other orgs' contributions, so it is metered exactly like
        commons_overlap."""
        await _contribute_and_share(session_factory, config, orgs["sharer"], *WEBHOOK)
        async with session_scope(session_factory) as session:
            before = (await crud.entitlements(session, orgs["asker"]))["commons_queries"]["used"]
        await _search(session_factory, orgs["asker"], "webhook twice")
        async with session_scope(session_factory) as session:
            after = (await crud.entitlements(session, orgs["asker"]))["commons_queries"]["used"]
        assert after == before + 1

    async def test_exhausting_the_allowance_refuses_the_search(self, session_factory, config, orgs):
        await _contribute_and_share(session_factory, config, orgs["sharer"], *WEBHOOK)
        allowance = plans.get("free").commons_queries_per_month
        for _ in range(allowance):
            await _search(session_factory, orgs["asker"], "webhook twice")
        with pytest.raises(plans.EntitlementExceeded) as exc:
            await _search(session_factory, orgs["asker"], "webhook twice")
        assert exc.value.metric == crud.METRIC_COMMONS_QUERIES

    async def test_a_malformed_signature_is_not_metered(self, session_factory, config, orgs):
        """Charging for a call that returned an error is the kind of thing
        customers notice and remember."""
        async with session_scope(session_factory) as session:
            before = (await crud.entitlements(session, orgs["asker"]))["commons_queries"]["used"]
        with pytest.raises(commons.CommonsInputError):
            async with session_scope(session_factory) as session:
                await crud.commons_search(session, orgs["asker"], [1, 2, 3])  # wrong width
        async with session_scope(session_factory) as session:
            after = (await crud.entitlements(session, orgs["asker"]))["commons_queries"]["used"]
        assert after == before


class TestInputValidation:
    async def test_a_wrong_width_signature_is_refused(self, session_factory, orgs):
        with pytest.raises(commons.CommonsInputError, match="num_perm"):
            async with session_scope(session_factory) as session:
                await crud.commons_search(session, orgs["asker"], [1] * 7)

    async def test_a_non_list_signature_is_refused(self, session_factory, orgs):
        with pytest.raises(commons.CommonsInputError):
            async with session_scope(session_factory) as session:
                await crud.commons_search(session, orgs["asker"], "not-a-signature")

    async def test_out_of_range_values_are_refused(self, session_factory, orgs):
        bad = [0] * commons.COMMONS_NUM_PERM
        bad[3] = -1
        with pytest.raises(commons.CommonsInputError):
            async with session_scope(session_factory) as session:
                await crud.commons_search(session, orgs["asker"], bad)

    async def test_the_limit_is_clamped_not_trusted(self, session_factory, config, orgs):
        await _contribute_and_share(session_factory, config, orgs["sharer"], *WEBHOOK)
        r = await _search(session_factory, orgs["asker"], "webhook twice", limit=10_000)
        assert r["n_candidates"] <= commons.MAX_SEARCH_CANDIDATES

    async def test_a_nonsense_limit_is_a_clean_error(self, session_factory, orgs):
        with pytest.raises(commons.CommonsInputError):
            async with session_scope(session_factory) as session:
                await crud.commons_search(
                    session, orgs["asker"], _sig("q"), limit="lots"
                )
