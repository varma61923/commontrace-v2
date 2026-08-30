"""The Hub finds a memory when the task is described in the operator's own
words -- and can tell an operator when it stops doing so.

This file pins the correction documented in `hub/search.py`. Before it,
`search_traces` built its tsquery with `plainto_tsquery`, which ANDs every
lexeme, so a natural-language task description required a trace containing
EVERY content word in it. `hub/bench_retrieval.py` measured the result on
the shipped substrate corpus: 0.0% recall@1 and a 100% zero-result rate,
against 84.8% for the local file tier on the same 46 records.

The defect survived a 1300-test suite because nothing in it ever issued a
query longer than two words. `TestQueryTermsAreOrNotAnd` is the class whose
absence allowed that, so it comes first.

It also survived because it is silent in production: an unmatched search is
`{"traces": []}` with HTTP 200, and `Trace.retrievals` counts rows RETURNED,
so a search that matched nothing incremented nothing anywhere. The counters
in `TestRetrievalHealthTelemetry` exist so that a deployment with this class
of defect is visible to an operator without anyone thinking to look.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from hub import crud
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="relaxation-org")
        session.add(o)
        await session.flush()
        return o.id


@pytest_asyncio.fixture
async def other_org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="relaxation-other-org")
        session.add(o)
        await session.flush()
        return o.id


async def _contribute(session_factory, config, org_id, title, context, solution, tags=None):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        return await crud.contribute_trace(
            session, org_id, config, rate_limiter,
            title=title, context_text=context, solution_text=solution,
            tags=tags or [], agent_type="code", actor="test",
        )


async def _search(session_factory, org_id, **kwargs):
    async with session_scope(session_factory) as session:
        return await crud.search_traces(session, org_id, **kwargs)


# The trace every test in the first class is trying to find, and the query
# an agent would actually issue for it. Not one word of the query appears in
# the trace by accident: "charged twice" / "duplicate charge" and "checkout"
# / "payment" are the paraphrase gap a real fleet's vocabulary creates.
WEBHOOK_TITLE = "Payment webhook delivered more than once"
WEBHOOK_CONTEXT = (
    "The provider re-delivers a webhook after a timeout, so the handler runs "
    "twice and the customer is billed for a duplicate charge"
)
WEBHOOK_SOLUTION = "Persist the provider event id and check it before any side effect"

REAL_QUERY = (
    "customer charged twice for one order: two identical charges appear on "
    "the card for a single checkout, our handler ran twice"
)


class TestQueryTermsAreOrNotAnd:
    async def test_a_natural_language_description_finds_the_trace(
        self, session_factory, config, org
    ):
        """The regression this whole change exists for.

        Under `plainto_tsquery` this returned zero rows: the query contains
        'identical', 'card' and 'checkout', none of which appear in the
        trace, and one absent lexeme is enough to fail a conjunction.
        """
        await _contribute(
            session_factory, config, org, WEBHOOK_TITLE, WEBHOOK_CONTEXT, WEBHOOK_SOLUTION
        )
        page = await _search(session_factory, org, query=REAL_QUERY)
        assert [t["title"] for t in page["traces"]] == [WEBHOOK_TITLE]

    async def test_one_shared_word_is_enough_to_surface_a_trace(
        self, session_factory, config, org
    ):
        """STRATEGY.md §12.7's sentence about the local tier, asserted of the
        Hub. It is the whole contract: a ranked top-k list must not hide the
        answer, and a threshold -- boolean or numeric -- is what hides it."""
        await _contribute(session_factory, config, org, "Cache stampede", "many workers", "add jitter")
        page = await _search(session_factory, org, query="stampede across the fleet at midnight")
        assert len(page["traces"]) == 1

    async def test_a_trace_matching_more_terms_ranks_above_one_matching_fewer(
        self, session_factory, config, org
    ):
        """Relaxation without ranking would be a firehose. This is the
        property that makes returning everything safe rather than useless,
        and it is what `ts_rank` is doing in `hub/search.py:relevance`."""
        await _contribute(
            session_factory, config, org,
            "Connection pool exhausted during a retry storm",
            "a spike of retries acquires every connection in the pool and requests time out",
            "bound the retry budget and set an acquire timeout",
        )
        await _contribute(
            session_factory, config, org,
            "Weekly report formatting",
            "the report renders a stray currency symbol",
            "pin the locale",
        )
        page = await _search(
            session_factory, org,
            query="every request is timing out waiting to acquire a database connection under load",
        )
        assert page["traces"], "relaxed matching must return something here"
        assert page["traces"][0]["title"].startswith("Connection pool exhausted")

    async def test_an_unrelated_query_still_returns_nothing(
        self, session_factory, config, org
    ):
        """Relaxed is not unconditional. A query sharing no lexeme with any
        trace matches no rows -- otherwise `searches_empty` below could never
        be non-zero and the health signal would be dead."""
        await _contribute(session_factory, config, org, "Cache stampede", "many workers", "add jitter")
        page = await _search(session_factory, org, query="photosynthesis chlorophyll")
        assert page["traces"] == []


class TestQueryConstructionIsSafe:
    async def test_a_lexeme_containing_an_ampersand_still_matches(
        self, session_factory, config, org
    ):
        """The trap in the obvious implementation.

        `replace(plainto_tsquery(...)::text, '&', '|')` looks like it turns
        the conjunction into a disjunction. Postgres tokenizes a URL into
        lexemes that CONTAIN '&' ('a.com/x&y=1'), so that replace edits the
        lexeme rather than the operator. It stays inside the quoted literal,
        so nothing is injected -- the query simply stops matching, silently,
        for exactly the identifiers engineers search by.
        """
        await _contribute(
            session_factory, config, org,
            "Callback URL rejected",
            "the gateway posted to http://example.com/hook?a=1&b=2 and got a 404",
            "register the path",
        )
        page = await _search(session_factory, org, query="http://example.com/hook?a=1&b=2")
        assert [t["title"] for t in page["traces"]] == ["Callback URL rejected"]

    @pytest.mark.parametrize(
        "hostile",
        [
            "!!! & | context ???",
            "' | 'x') OR 1=1 --",
            "a <-> b",
            "'; DROP TABLE traces; --",
            ":*",
            "\\",
        ],
    )
    async def test_tsquery_syntax_in_user_input_is_data_not_syntax(
        self, session_factory, config, org, hostile
    ):
        """`to_tsquery` DOES raise on malformed syntax, unlike the
        `plainto_tsquery` this replaced -- which is why every lexeme goes
        through `quote_literal` before it gets there. A raise here would be a
        500 on arbitrary user text; a match on `traces` after a dropped table
        would be worse."""
        await _contribute(session_factory, config, org, "t", "some context", "some solution")
        page = await _search(session_factory, org, query=hostile)
        assert isinstance(page["traces"], list)

    async def test_non_english_text_is_searchable(self, session_factory, config, org):
        await _contribute(
            session_factory, config, org, "Rapport d'incident", "le café était résumé", "corrigé"
        )
        page = await _search(session_factory, org, query="résumé du café")
        assert len(page["traces"]) == 1


class TestReportedTerms:
    async def test_terms_reports_the_stemmed_lexemes(self, session_factory, config, org):
        await _contribute(session_factory, config, org, "t", "c", "s")
        page = await _search(session_factory, org, query="the deployments were retrying")
        assert set(page["terms"]) == {"deploy", "retri"}

    async def test_a_stopword_only_query_reports_no_terms(self, session_factory, config, org):
        """The distinction that makes an empty result readable. Without
        `terms`, "your corpus has no answer" and "you asked for nothing
        searchable" are the same response."""
        await _contribute(session_factory, config, org, "t", "c", "s")
        page = await _search(session_factory, org, query="the of and to")
        assert page["terms"] == []
        assert page["traces"] == []

    async def test_no_query_reports_no_terms_and_still_lists_traces(
        self, session_factory, config, org
    ):
        await _contribute(session_factory, config, org, "t", "c", "s")
        page = await _search(session_factory, org, query="")
        assert page["terms"] == []
        assert len(page["traces"]) == 1


class TestRetrievalHealthTelemetry:
    async def test_a_hit_and_a_miss_are_counted_separately(
        self, session_factory, config, org
    ):
        await _contribute(session_factory, config, org, "Cache stampede", "many workers", "add jitter")
        await _search(session_factory, org, query="stampede")
        await _search(session_factory, org, query="photosynthesis")
        async with session_scope(session_factory) as session:
            health = await crud.search_health(session, org)
        assert health["searches"] == 2
        assert health["empty"] == 1
        assert health["no_terms"] == 0
        assert health["miss_rate"] == pytest.approx(0.5)

    async def test_a_query_with_no_searchable_terms_is_not_a_retrieval_miss(
        self, session_factory, config, org
    ):
        """Folding these together would let a client sending junk manufacture
        -- or mask -- a retrieval problem in the operator's report."""
        await _contribute(session_factory, config, org, "Cache stampede", "many workers", "add jitter")
        await _search(session_factory, org, query="stampede")
        await _search(session_factory, org, query="the of and to")
        async with session_scope(session_factory) as session:
            health = await crud.search_health(session, org)
        assert health["searches"] == 2
        assert health["no_terms"] == 1
        assert health["empty"] == 0
        # One searchable search, and it hit.
        assert health["searches_with_terms"] == 1
        assert health["miss_rate"] == pytest.approx(0.0)

    async def test_paging_a_result_set_counts_as_one_search(
        self, session_factory, config, org
    ):
        """Retrieval is one act by the agent however many pages it reads. If
        each page counted, an org that pages deeply would look like it
        searched more successfully than one that does not."""
        for i in range(5):
            await _contribute(session_factory, config, org, f"stampede {i}", "many workers", "jitter")
        await _search(session_factory, org, query="stampede", limit=2, offset=0)
        await _search(session_factory, org, query="stampede", limit=2, offset=2)
        await _search(session_factory, org, query="stampede", limit=2, offset=4)
        async with session_scope(session_factory) as session:
            health = await crud.search_health(session, org)
        assert health["searches"] == 1

    async def test_an_empty_query_is_not_counted_as_a_search(
        self, session_factory, config, org
    ):
        """Listing recent traces is browsing, not retrieval. Counting it
        would dilute the miss rate with calls that cannot miss."""
        await _contribute(session_factory, config, org, "t", "c", "s")
        await _search(session_factory, org, query="")
        async with session_scope(session_factory) as session:
            health = await crud.search_health(session, org)
        assert health["searches"] == 0
        assert health["miss_rate"] is None

    async def test_miss_rate_is_none_rather_than_zero_with_no_searches(
        self, session_factory, config, org
    ):
        """0.0 would read as 'retrieval is perfect' on an org that has never
        searched, which is the opposite of what the absence of data means."""
        async with session_scope(session_factory) as session:
            health = await crud.search_health(session, org)
        assert health["miss_rate"] is None
        assert health["traces"] == 0

    async def test_counters_are_per_org(self, session_factory, config, org, other_org):
        await _contribute(session_factory, config, org, "Cache stampede", "many workers", "jitter")
        await _search(session_factory, org, query="stampede")
        await _search(session_factory, other_org, query="stampede")
        async with session_scope(session_factory) as session:
            mine = await crud.search_health(session, org)
            theirs = await crud.search_health(session, other_org)
        assert mine["searches"] == 1 and mine["empty"] == 0
        assert theirs["searches"] == 1 and theirs["empty"] == 1
        assert theirs["traces"] == 0

    async def test_health_counts_the_orgs_own_traces(self, session_factory, config, org):
        await _contribute(session_factory, config, org, "a", "c", "s")
        await _contribute(session_factory, config, org, "b", "c", "s")
        async with session_scope(session_factory) as session:
            health = await crud.search_health(session, org)
        assert health["traces"] == 2


class TestTermSelectionEndToEnd:
    async def test_a_term_in_every_trace_does_not_drag_the_corpus_back(
        self, session_factory, config, org
    ):
        """The measured defect, in miniature.

        `budget=1` makes any term present in more than one trace
        non-discriminating, which is what a term in 100% of a 64,000-trace
        corpus is. Without term selection this query returns all three
        traces on the strength of the shared word alone.
        """
        for i in range(3):
            await _contribute(
                session_factory, config, org,
                f"Incident {i}", "the service degraded during a retry storm", "bounded the retry"
            )
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(
                session, org, query="retry", limit=10
            )
        # Sanity: with the shipped budget this common word is fine at n=3.
        assert len(page["traces"]) == 3
        assert page["terms_ignored"] == []

    async def test_a_query_whose_every_term_is_too_common_returns_nothing_and_says_why(
        self, session_factory, config, org, monkeypatch
    ):
        from hub import search

        monkeypatch.setattr(search, "RANK_BUDGET", 1)
        for i in range(3):
            await _contribute(
                session_factory, config, org,
                f"Incident {i}", "the service degraded during a retry storm", "bounded the retry"
            )
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org, query="retry storm", limit=10)
        assert page["traces"] == []
        assert set(page["terms_ignored"]) == {"retri", "storm"}
        assert set(page["terms"]) == {"retri", "storm"}

    async def test_a_selective_term_survives_beside_a_dropped_one(
        self, session_factory, config, org, monkeypatch
    ):
        """The property that makes this a search improvement and not just a
        cost bound: the rare word still finds its trace after the common one
        is discarded."""
        from hub import search

        monkeypatch.setattr(search, "RANK_BUDGET", 2)
        for i in range(3):
            await _contribute(
                session_factory, config, org,
                f"Incident {i}", "the service degraded during a retry storm", "bounded the retry"
            )
        await _contribute(
            session_factory, config, org,
            "Hydration mismatch", "server-rendered timestamps disagreed with the client", "pinned the locale"
        )
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(
                session, org, query="retry storm hydration", limit=10
            )
        assert [t["title"] for t in page["traces"]] == ["Hydration mismatch"]
        assert "hydrat" in page["terms"]
        assert "hydrat" not in page["terms_ignored"]

    async def test_a_pasted_log_does_not_probe_unboundedly(
        self, session_factory, config, org, monkeypatch
    ):
        """One caller must not be able to turn a single search into hundreds
        of index scans by pasting a stack trace."""
        from hub import search

        monkeypatch.setattr(search, "MAX_QUERY_TERMS", 5)
        await _contribute(session_factory, config, org, "t", "some context here", "a solution")
        flood = " ".join(f"lexeme{i}word" for i in range(200))
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org, query=flood, limit=10)
        assert len(page["terms"]) == 5
        assert isinstance(page["traces"], list)
