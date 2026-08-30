"""Tests for the CommonTrace Knowledge Base.

Two things are being pinned here, and the second matters more than the first:

1. That the Knowledge Base works -- `commons_overlap` answers the coverage
   question correctly against operator-curated content, and gets sane
   numbers back.

2. That it does NOT create a cross-tenant leak. `commons_overlap` and
   `commons_search` are the only paths in hub/crud.py by which a query
   reaches content outside the caller's own org, and the only content they
   can ever reach is `commons_source == "seed"` -- rows written exclusively
   by the operator-run `hub/manage.py:commons_seed`, never by a customer.
   There is no customer-facing tool that sets `shared_with_commons` on a
   customer's own trace (see hub/plans.py "why there is no org-to-org
   sharing here"). Every way this boundary could fail is tested explicitly:
   an ordinary (non-seeded) trace, a quarantined seeded entry, and -- the
   defense-in-depth case -- a hypothetical row that is `shared_with_commons`
   but NOT `commons_source == "seed"` must never appear in a result.

hub/tests/test_tenant_isolation.py continues to pass unchanged, which is
the complementary half of the same claim: the ordinary read paths did not
loosen.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from hub import commons, crud
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def orgs(session_factory):
    """One operator org (the only one ever allowed to own Knowledge Base
    entries) and two ordinary customer orgs."""
    async with session_scope(session_factory) as session:
        made = {}
        for name in ("operator", "customer-a", "customer-b"):
            o = Organization(name=name)
            session.add(o)
            await session.flush()
            made[name] = o.id
        return made


async def _contribute(session_factory, config, org_id, title, context, solution, tags=None):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        return await crud.contribute_trace(
            session, org_id, config, rate_limiter,
            title=title, context_text=context, solution_text=solution,
            tags=tags or [], agent_type="code", actor="test",
        )


async def _seed(
    session_factory, operator_org_id, title, context, solution, tags=None, agent_type="code",
):
    """Put one entry directly into the Knowledge Base, the only way that
    ever happens in production: `hub/manage.py:commons_seed` constructing a
    row with `commons_source="seed"` under an operator org. Exercised here
    at the row level rather than via `manage.commons_seed` so each test can
    build exactly the fixture it needs without a JSONL file on disk --
    `TestShippedSeedCorpus` below exercises the real loader end to end.
    """
    tags = tags or []
    async with session_scope(session_factory) as session:
        trace = Trace(
            org_id=operator_org_id,
            title=title,
            context_text=context,
            solution_text=solution,
            tags=tags,
            agent_type=agent_type,
            shared_with_commons=True,
            shared_at=datetime.now(timezone.utc),
            shared_rationale="test fixture: operator-curated substrate knowledge",
            commons_signature=commons.signature_for(title, context, tags),
            commons_source="seed",
        )
        session.add(trace)
        await session.flush()
        return trace.id


def _sign(title, context, tags=None):
    """Sign a failure exactly as a client would, via the shared module."""
    return commons.signature_for(title, context, tags or [])


def _failure(label, title, context, tags=None):
    return {"label": label, "signature": _sign(title, context, tags)}


# --- 1. The algorithm must be identical on both sides -------------------


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestSignatureCompatibility:
    """Pure-function tests -- no DB, no event loop. The module-level
    `pytestmark` applies asyncio to every test in the file; these opt out of
    the resulting "marked asyncio but not async" warning rather than
    dropping the module-level mark that the other tests need."""

    def test_hub_and_client_produce_identical_signatures(self):
        """The whole Knowledge Base is meaningless if the Hub and the client
        draw different MinHash permutations -- estimate_jaccard would
        compare mismatched positions and return a confident, wrong number
        rather than failing. hub/commons.py imports the client's module
        precisely so this cannot drift; this test fails loudly if someone
        ever reimplements it locally."""
        from commontrace import overlap

        text = commons.matchable_text("Stripe webhook", "duplicate delivery", ["stripe"])
        assert commons.signature_for("Stripe webhook", "duplicate delivery", ["stripe"]) == \
            overlap.minhash(text, overlap.DEFAULT_NUM_PERM)

    def test_identical_text_matches_itself_perfectly(self):
        sig = _sign("Stripe webhook retries", "duplicate delivery on 500", ["stripe"])
        assert commons.estimate(sig, sig) == pytest.approx(1.0)

    def test_unrelated_text_does_not_match(self):
        a = _sign("Stripe webhook retries", "duplicate delivery on 500", ["stripe"])
        b = _sign("CUDA kernel launch", "grid dimensions exceeded device limit", ["cuda"])
        assert commons.estimate(a, b) < commons.DEFAULT_COMMONS_THRESHOLD


# --- 2. The Knowledge Base boundary must not leak ------------------------


class TestCommonsDoesNotLeak:
    async def test_an_ordinary_customer_trace_never_appears(self, session_factory, config, orgs):
        """A trace nobody ever ran commons_seed on -- the entire population
        of customer traces -- must never surface in a Knowledge Base query,
        because there is no customer-facing path that could have put it
        there."""
        await _contribute(
            session_factory, config, orgs["customer-a"],
            "Stripe webhook retries", "duplicate delivery on 500", "SECRET-SOLUTION",
        )
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["customer-b"],
                [_failure("f1", "Stripe webhook retries", "duplicate delivery on 500")],
            )
        assert report["n_commons_traces"] == 0
        assert report["n_covered"] == 0
        assert report["matches"] == []

    async def test_a_quarantined_seed_entry_is_excluded(self, session_factory, orgs):
        """Belt and braces: even operator-curated content must be excluded
        once quarantined."""
        tid = await _seed(
            session_factory, orgs["operator"],
            "Stripe webhook retries", "duplicate delivery on 500", "use an idempotency key",
        )
        async with session_scope(session_factory) as session:
            row = await session.get(Trace, tid)
            row.quarantined = True

        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["customer-a"],
                [_failure("f1", "Stripe webhook retries", "duplicate delivery on 500")],
            )
        assert report["n_commons_traces"] == 0
        assert report["n_covered"] == 0

    async def test_a_shared_row_that_is_not_seed_sourced_is_still_invisible(
        self, session_factory, orgs
    ):
        """THE defense-in-depth property. There is no customer-facing tool
        that can set `shared_with_commons` on a customer's own trace today
        -- but if one ever did (a bug, a future regression), the
        `commons_source == "seed"` filter must still keep it out of every
        other customer's results. This is what makes "no org-to-org
        sharing" a guarantee rather than a policy that merely holds because
        nothing currently violates it."""
        async with session_scope(session_factory) as session:
            rogue = Trace(
                org_id=orgs["customer-a"],
                title="Customer A's proprietary escalation policy",
                context_text="internal pricing logic",
                solution_text="internal solution",
                tags=[],
                agent_type="code",
                shared_with_commons=True,  # hypothetically set by a bug
                commons_signature=commons.signature_for(
                    "Customer A's proprietary escalation policy", "internal pricing logic", []
                ),
                commons_source="org",  # NOT "seed"
            )
            session.add(rogue)
            await session.flush()

        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["customer-b"],
                [_failure("f1", "Customer A's proprietary escalation policy", "internal pricing logic")],
            )
        assert report["n_commons_traces"] == 0
        assert report["n_covered"] == 0

        async with session_scope(session_factory) as session:
            search_result = await crud.commons_search(
                session, orgs["customer-b"],
                _sign("Customer A's proprietary escalation policy", "internal pricing logic"),
            )
        assert search_result["n_candidates"] == 0


# --- 3. The number itself -----------------------------------------------


class TestCoverageNumber:
    async def test_matching_failure_is_covered_and_returns_the_solution(
        self, session_factory, orgs
    ):
        """The payoff: a match hands back the Knowledge Base entry's
        substrate content -- title/context/solution/tags/agent_type/trust,
        the narrow projection (see TestCommonsCrossOrgProjection below)."""
        await _seed(
            session_factory, orgs["operator"],
            "Stripe webhook retries", "duplicate delivery on 500 response",
            "Use an idempotency key on the handler", tags=["stripe", "webhooks"],
        )
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["customer-a"],
                [_failure("our-f1", "Stripe webhook retries", "duplicate delivery on 500 response",
                          ["stripe", "webhooks"])],
            )

        assert report["n_covered"] == 1
        assert report["covered_fraction"] == pytest.approx(1.0)
        match = report["matches"][0]
        assert match["failure_label"] == "our-f1"
        assert match["similarity"] >= commons.DEFAULT_COMMONS_THRESHOLD
        assert "idempotency key" in match["trace"]["solution_text"]

    async def test_unrelated_failure_is_not_covered(self, session_factory, orgs):
        await _seed(
            session_factory, orgs["operator"],
            "Stripe webhook retries", "duplicate delivery on 500", "idempotency key",
        )
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["customer-a"],
                [_failure("f1", "CUDA kernel launch failure",
                          "grid dimensions exceeded the device limit", ["cuda"])],
            )
        assert report["n_covered"] == 0
        assert report["covered_fraction"] == pytest.approx(0.0)

    async def test_coverage_fraction_is_the_ratio_covered(self, session_factory, orgs):
        await _seed(
            session_factory, orgs["operator"],
            "Stripe webhook retries", "duplicate delivery on 500", "idempotency key",
        )
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["customer-a"],
                [
                    _failure("hit", "Stripe webhook retries", "duplicate delivery on 500"),
                    _failure("miss1", "CUDA kernel launch", "grid dimensions exceeded"),
                    _failure("miss2", "DNS resolution flake", "intermittent NXDOMAIN in CI"),
                ],
            )
        assert report["n_failures"] == 3
        assert report["n_covered"] == 1
        assert report["covered_fraction"] == pytest.approx(1 / 3)

    async def test_aggregates_across_multiple_entries(self, session_factory, orgs):
        await _seed(
            session_factory, orgs["operator"],
            "Stripe webhook retries", "duplicate delivery on 500", "idempotency key",
        )
        await _seed(
            session_factory, orgs["operator"],
            "CUDA kernel launch failure", "grid dimensions exceeded the device limit", "clamp the grid",
        )
        probe = [
            _failure("f1", "Stripe webhook retries", "duplicate delivery on 500"),
            _failure("f2", "CUDA kernel launch failure", "grid dimensions exceeded the device limit"),
        ]
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(session, orgs["customer-a"], probe)

        assert report["n_commons_traces"] == 2
        assert report["n_covered"] == 2
        assert report["covered_fraction"] == pytest.approx(1.0)

    async def test_include_matches_false_returns_the_number_without_content(
        self, session_factory, orgs
    ):
        """A prospect evaluating whether to engage can get the headline
        number without pulling any content."""
        await _seed(
            session_factory, orgs["operator"],
            "Stripe webhook retries", "duplicate delivery on 500", "idempotency key",
        )
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["customer-a"],
                [_failure("f1", "Stripe webhook retries", "duplicate delivery on 500")],
                include_matches=False,
            )
        assert report["n_covered"] == 1
        assert report["matches"] == []

    async def test_empty_knowledge_base_says_so_rather_than_reporting_zero_percent(
        self, session_factory, orgs
    ):
        """0% against an empty corpus is not a finding, and this number is
        exactly the kind that gets quoted once and repeated forever."""
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["customer-a"], [_failure("f1", "anything", "at all")],
            )
        assert report["n_covered"] == 0
        assert "no entries yet" in report["note"].lower()


class TestScanDoesNotBlockTheEventLoop:
    """commons.best_matches is a CPU-bound MinHash comparison loop -- up to
    MAX_SUBMITTED_FAILURES (500) signatures against up to max_corpus_scan()
    (20,000) corpus rows. Run inline on the request coroutine, that stalls
    the single-threaded asyncio event loop for its full duration, starving
    every other request the process is concurrently serving. crud.commons_overlap
    must run it via asyncio.to_thread instead of calling it directly."""

    async def test_best_matches_runs_off_the_event_loop_thread(
        self, session_factory, orgs, monkeypatch
    ):
        await _seed(
            session_factory, orgs["operator"],
            "Stripe webhook retries", "duplicate delivery on 500 response",
            "Use an idempotency key on the handler",
        )

        import threading

        main_thread = threading.current_thread()
        seen_threads = []
        real_best_matches = commons.best_matches

        def _tracking_best_matches(*args, **kwargs):
            seen_threads.append(threading.current_thread())
            return real_best_matches(*args, **kwargs)

        monkeypatch.setattr(crud.commons, "best_matches", _tracking_best_matches)

        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["customer-a"],
                [_failure("f1", "Stripe webhook retries", "duplicate delivery on 500 response")],
            )
        assert report["n_covered"] == 1
        assert len(seen_threads) == 1
        assert seen_threads[0] is not main_thread


class TestCommonsCrossOrgProjection:
    """A Knowledge Base entry's title/context/solution/tags is what a
    lookup needs -- that is not the same as exposing every column on the
    row. commons_overlap's matches used to hand back crud._to_wire(hit)
    in full, which included `contributor` (routinely an email/name),
    `extensions`/`outcome` (freeform JSON that can carry internal project
    ids or cost data), `watch_condition`, and `review_after` -- operational
    bookkeeping fields with no meaning on curated content and no business
    being on the wire to any caller. The projection must carry the
    substrate content a requester actually needs and nothing else."""

    async def test_private_fields_are_excluded_from_a_commons_match(
        self, session_factory, orgs
    ):
        tid = await _seed(
            session_factory, orgs["operator"],
            "Stripe webhook retries", "duplicate delivery on 500",
            "Use an idempotency key", tags=["stripe"],
        )
        async with session_scope(session_factory) as session:
            row = await session.get(Trace, tid)
            row.contributor = "alice@example.com"
            row.extensions = {"internal_project_id": "proj-42", "cost_usd": 1337}
            row.outcome = {"resolved": True, "notes": "internal escalation notes"}
            row.watch_condition = "if error_rate > 5%"
            row.review_after = "2027-01-01"

        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["customer-a"],
                [_failure("f1", "Stripe webhook retries", "duplicate delivery on 500", ["stripe"])],
            )

        assert report["n_covered"] == 1
        match_trace = report["matches"][0]["trace"]
        for private_field in (
            "contributor", "extensions", "outcome", "watch_condition",
            "review_after", "retrievals", "depth", "supersedes_trace_id",
            "votes", "related", "quarantined", "quarantine_reason",
            "shared_with_commons",
        ):
            assert private_field not in match_trace, f"{private_field!r} leaked to a caller"
        # What a requester actually needs to judge and use the match:
        assert match_trace["title"] == "Stripe webhook retries"
        assert match_trace["solution_text"] == "Use an idempotency key"
        assert match_trace["tags"] == ["stripe"]
        assert "trust" in match_trace
        assert "created_at" in match_trace


# --- 4. Scaling: the fast path must not diverge from the reference ------


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestMatcherPaths:
    """commons.best_matches has a numpy fast path and a pure-Python
    fallback. They must return identical results -- a fast path that
    quietly disagreed would produce wrong coverage numbers only on hosts
    that happen to have numpy installed, which is close to undebuggable."""

    def _random_sigs(self, n, seed):
        import random

        rnd = random.Random(seed)
        return [
            [rnd.randrange(0, 1 << 61) for _ in range(commons.COMMONS_NUM_PERM)]
            for _ in range(n)
        ]

    def _pure_python(self, submitted, corpus):
        out = []
        for _label, sig in submitted:
            best_idx, best_sim = -1, 0.0
            for i, cand in enumerate(corpus):
                sim = commons.estimate(sig, cand)
                if sim > best_sim:
                    best_idx, best_sim = i, sim
            out.append((best_idx, best_sim))
        return out

    def test_both_paths_agree_on_random_signatures(self):
        corpus = self._random_sigs(40, seed=1)
        submitted = [(f"f{i}", s) for i, s in enumerate(self._random_sigs(5, seed=2))]
        assert commons.best_matches(submitted, corpus) == self._pure_python(submitted, corpus)

    def test_both_paths_agree_when_a_real_match_exists(self):
        """The case that actually matters: a planted near-duplicate must be
        found at the same index with the same similarity by both paths."""
        corpus = self._random_sigs(30, seed=3)
        planted = corpus[7][:]
        submitted = [("hit", planted)]
        fast = commons.best_matches(submitted, corpus)
        assert fast == self._pure_python(submitted, corpus)
        assert fast[0][0] == 7
        assert fast[0][1] == pytest.approx(1.0)

    def test_empty_corpus_is_handled_by_both_paths(self):
        submitted = [("f", self._random_sigs(1, seed=4)[0])]
        assert commons.best_matches(submitted, []) == [(-1, 0.0)]


class TestBoundedCorpusScan:
    async def test_a_truncated_scan_is_reported_as_a_lower_bound(
        self, session_factory, orgs, monkeypatch
    ):
        """Silently truncating would under-report the coverage figure. It
        must be flagged and framed as a lower bound instead."""
        monkeypatch.setattr(commons, "max_corpus_scan", lambda: 1)
        for i in range(3):
            await _seed(session_factory, orgs["operator"], f"Shared substrate {i}", f"context {i}", "fix")

        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["customer-a"], [_failure("f", "Shared substrate 0", "context 0")],
            )

        assert report["corpus_truncated"] is True
        assert report["n_commons_traces"] == 1
        assert report["n_commons_traces_total"] == 3
        assert "lower bound" in report["note"].lower()

    async def test_an_untruncated_scan_is_not_flagged(self, session_factory, orgs):
        await _seed(session_factory, orgs["operator"], "Shared", "ctx", "fix")
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["customer-a"], [_failure("f", "Shared", "ctx")],
            )
        assert report["corpus_truncated"] is False
        assert report["n_commons_traces"] == report["n_commons_traces_total"] == 1


class TestAgentTypePrefilter:
    async def test_narrowing_by_agent_type_excludes_other_fleets(self, session_factory, orgs):
        """Not an approximation -- a support fleet's failures genuinely
        should not be scored against another agent type's substrate."""
        await _seed(
            session_factory, orgs["operator"],
            "Stripe webhook retries", "duplicate delivery on 500", "idempotency key",
        )
        probe = [_failure("f", "Stripe webhook retries", "duplicate delivery on 500")]

        async with session_scope(session_factory) as session:
            matching = await crud.commons_overlap(
                session, orgs["customer-a"], probe, agent_type="code",
            )
        assert matching["n_covered"] == 1

        async with session_scope(session_factory) as session:
            other = await crud.commons_overlap(
                session, orgs["customer-a"], probe, agent_type="support",
            )
        assert other["n_commons_traces"] == 0
        assert other["n_covered"] == 0


# --- 5. Operator view: is the Knowledge Base content actually good? -----


class TestKbStats:
    """There is no network-effect question to ask here (see hub/plans.py
    "why there is no org-to-org sharing here") -- the operator's question
    is a content-quality one: is the corpus actually answering real
    questions, and who is using it."""

    async def test_empty_knowledge_base_reports_cleanly(self, session_factory, orgs, capsys):
        from hub import manage

        await manage.kb_stats(session_factory=session_factory)
        assert "no entries yet" in capsys.readouterr().out.lower()

    async def test_reports_entry_count_and_total_hits(self, session_factory, orgs, capsys):
        from hub import manage

        tid = await _seed(
            session_factory, orgs["operator"],
            "Stripe webhook retries", "duplicate delivery on 500", "idempotency key",
        )
        async with session_scope(session_factory) as session:
            await crud.commons_overlap(
                session, orgs["customer-a"],
                [_failure("f", "Stripe webhook retries", "duplicate delivery on 500")],
            )
        async with session_scope(session_factory) as session:
            assert (await session.get(Trace, tid)).commons_hits == 1

        await manage.kb_stats(session_factory=session_factory)
        out = capsys.readouterr().out
        assert "knowledge base entries:  1" in out
        assert "total hits delivered:    1" in out

    async def test_says_so_when_nothing_has_matched_yet(self, session_factory, orgs, capsys):
        from hub import manage

        await _seed(session_factory, orgs["operator"], "t", "c", "s")
        await manage.kb_stats(session_factory=session_factory)
        assert "has covered anyone's failure yet" in capsys.readouterr().out

    async def test_counts_distinct_querying_orgs(self, session_factory, orgs, capsys):
        from hub import manage

        await _seed(session_factory, orgs["operator"], "Stripe webhook retries", "ctx", "fix")
        probe = [_failure("f", "Stripe webhook retries", "ctx")]
        for org in ("customer-a", "customer-b", "customer-a"):  # a repeats
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(session, orgs[org], probe)

        await manage.kb_stats(session_factory=session_factory)
        # Two DISTINCT orgs queried, even though customer-a queried twice --
        # this is an adoption count, not a query-volume count.
        assert "queried by:              2 of 3 org(s)" in capsys.readouterr().out

    async def test_an_empty_submission_does_not_count_as_a_query(self, session_factory, orgs, capsys):
        """An empty submission compares nothing and is never metered (see
        commons_overlap) -- it must not register as adoption either."""
        from hub import manage

        await _seed(session_factory, orgs["operator"], "t", "c", "s")
        async with session_scope(session_factory) as session:
            await crud.commons_overlap(session, orgs["customer-a"], [])

        await manage.kb_stats(session_factory=session_factory)
        assert "queried by:              0 of 3 org(s)" in capsys.readouterr().out

    async def test_flags_a_corpus_that_mostly_never_matches(self, session_factory, orgs, capsys):
        from hub import manage

        await _seed(session_factory, orgs["operator"], "Matched entry", "ctx", "fix")
        for i in range(3):
            await _seed(session_factory, orgs["operator"], f"Dead entry {i}", f"ctx {i}", "fix")
        async with session_scope(session_factory) as session:
            await crud.commons_overlap(
                session, orgs["customer-a"], [_failure("f", "Matched entry", "ctx")],
            )

        await manage.kb_stats(session_factory=session_factory)
        out = capsys.readouterr().out
        assert "never matched anything:  3 of 4" in out
        assert "never matched a" in out.lower()


class TestValueLedger:
    """Trace.commons_hits is the operator's quality signal for its own
    curated content -- "this entry actually covered a real recurring
    failure" -- so it has to be counted correctly."""

    async def test_a_covered_failure_increments_the_hit_count(self, session_factory, orgs):
        tid = await _seed(
            session_factory, orgs["operator"],
            "Stripe webhook retries", "duplicate delivery on 500", "idempotency key",
        )
        async with session_scope(session_factory) as session:
            row = await session.get(Trace, tid)
        assert row.commons_hits == 0

        async with session_scope(session_factory) as session:
            await crud.commons_overlap(
                session, orgs["customer-a"],
                [_failure("f", "Stripe webhook retries", "duplicate delivery on 500")],
            )

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, tid)
        assert row.commons_hits == 1

    async def test_a_miss_credits_nothing(self, session_factory, orgs):
        tid = await _seed(
            session_factory, orgs["operator"],
            "Stripe webhook retries", "duplicate delivery on 500", "idempotency key",
        )
        async with session_scope(session_factory) as session:
            await crud.commons_overlap(
                session, orgs["customer-a"],
                [_failure("f", "CUDA kernel launch", "grid dimensions exceeded")],
            )
        async with session_scope(session_factory) as session:
            row = await session.get(Trace, tid)
        assert row.commons_hits == 0

    async def test_hits_accumulate_across_separate_customers(self, session_factory, orgs):
        tid = await _seed(
            session_factory, orgs["operator"],
            "Stripe webhook retries", "duplicate delivery on 500", "idempotency key",
        )
        probe = [_failure("f", "Stripe webhook retries", "duplicate delivery on 500")]
        for customer in ("customer-a", "customer-b"):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(session, orgs[customer], probe)

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, tid)
        assert row.commons_hits == 2

    async def test_two_failures_in_one_query_hitting_the_same_entry_both_count(
        self, session_factory, orgs
    ):
        """The batch case `test_hits_accumulate_across_separate_customers`
        doesn't cover: TWO submitted failures in the SAME commons_overlap
        call both best-matching the SAME entry -- a fleet hitting one
        substrate failure across several tasks and submitting them
        together, exactly the batch workflow this API exists to support.

        A naive `UPDATE ... WHERE id IN (hit_ids)` credits the row once per
        UPDATE STATEMENT regardless of how many times its id repeats in the
        IN-list -- Postgres does not re-apply the SET clause per duplicate.
        That silently under-counts this case, and two distinct failures
        genuinely covered is two hits, not one."""
        tid = await _seed(
            session_factory, orgs["operator"],
            "Stripe webhook retries", "duplicate delivery on 500", "idempotency key",
        )
        probe = [
            _failure("first-occurrence", "Stripe webhook retries", "duplicate delivery on 500"),
            _failure("second-occurrence", "Stripe webhook retries", "duplicate delivery on 500"),
        ]
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(session, orgs["customer-a"], probe)
        assert report["n_covered"] == 2

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, tid)
        assert row.commons_hits == 2, (
            "two failures covered in one call must count as two hits, not one"
        )

    async def test_duplicate_signature_farming_is_capped_per_query(self, session_factory, orgs):
        """Nothing on the wire stops a caller from submitting the identical
        signature many times in one request (up to
        commons.MAX_SUBMITTED_FAILURES). Without a cap, one repeated
        signature could inflate one entry's quality signal into looking far
        more useful than it actually is. commons.MAX_HITS_PER_TRACE_PER_QUERY
        bounds how much a single call can credit one entry, while leaving
        small genuine multi-task batches (like the 2-failure case above)
        fully credited."""
        tid = await _seed(
            session_factory, orgs["operator"],
            "Stripe webhook retries", "duplicate delivery on 500", "idempotency key",
        )
        probe = [
            _failure(f"occurrence-{i}", "Stripe webhook retries", "duplicate delivery on 500")
            for i in range(commons.MAX_HITS_PER_TRACE_PER_QUERY + 30)
        ]
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(session, orgs["customer-a"], probe)
        # The caller's own coverage report is unaffected by the cap -- every
        # submitted failure it asked about really was covered.
        assert report["n_covered"] == len(probe)

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, tid)
        assert row.commons_hits == commons.MAX_HITS_PER_TRACE_PER_QUERY, (
            "one query repeating one signature must not credit an entry past the per-query cap"
        )

    async def test_counting_survives_concurrent_queries(self, session_factory, orgs):
        """The increment is an atomic in-database UPDATE, not a
        read-modify-write: this is the operator's only quality signal for
        its own content, so lost counts under concurrency would corrupt it."""
        import asyncio

        tid = await _seed(
            session_factory, orgs["operator"],
            "Stripe webhook retries", "duplicate delivery on 500", "idempotency key",
        )
        probe = [_failure("f", "Stripe webhook retries", "duplicate delivery on 500")]

        async def _query():
            async with session_scope(session_factory) as session:
                return await crud.commons_overlap(session, orgs["customer-a"], probe)

        await asyncio.gather(*[_query() for _ in range(12)])

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, tid)
        assert row.commons_hits == 12, "concurrent queries must not lose hit credit"


# --- 6. Untrusted input --------------------------------------------------


class TestSubmittedInputIsValidated:
    async def test_rejects_a_wrong_width_signature(self, session_factory, orgs):
        """A mismatched width is not salvageable -- comparing it would
        return a confident, meaningless number."""
        with pytest.raises(commons.CommonsInputError):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(
                    session, orgs["customer-a"], [{"label": "f", "signature": [1, 2, 3]}],
                )

    async def test_rejects_too_many_failures(self, session_factory, orgs):
        oversized = [
            {"label": f"f{i}", "signature": [0] * commons.COMMONS_NUM_PERM}
            for i in range(commons.MAX_SUBMITTED_FAILURES + 1)
        ]
        with pytest.raises(commons.CommonsInputError):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(session, orgs["customer-a"], oversized)

    async def test_rejects_non_integer_signature_values(self, session_factory, orgs):
        with pytest.raises(commons.CommonsInputError):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(
                    session, orgs["customer-a"],
                    [{"label": "f", "signature": ["x"] * commons.COMMONS_NUM_PERM}],
                )

    async def test_rejects_a_non_list_payload(self, session_factory, orgs):
        with pytest.raises(commons.CommonsInputError):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(session, orgs["customer-a"], "not a list")

    async def test_empty_submission_is_not_an_error(self, session_factory, orgs):
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(session, orgs["customer-a"], [])
        assert report["n_failures"] == 0
        assert report["covered_fraction"] == 0.0

    async def test_rejects_negative_signature_values(self, session_factory, orgs):
        """A negative value passes isinstance(v, int) but is outside the
        uint64 domain MinHash signatures live in -- on numpy hosts,
        converting it (`np.array(..., dtype=uint64)`) raises OverflowError,
        surfacing as an unhandled 500 instead of a clean 400."""
        with pytest.raises(commons.CommonsInputError):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(
                    session, orgs["customer-a"],
                    [{"label": "f", "signature": [-1] * commons.COMMONS_NUM_PERM}],
                )

    async def test_rejects_signature_values_above_uint64_max(self, session_factory, orgs):
        with pytest.raises(commons.CommonsInputError):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(
                    session, orgs["customer-a"],
                    [{"label": "f", "signature": [2**64] * commons.COMMONS_NUM_PERM}],
                )

    async def test_rejects_non_finite_threshold(self, session_factory, orgs):
        """max(0.0, min(nan, 1.0)) silently clamps NaN to 0.0 rather than
        rejecting it -- permissive, not a crash, but a threshold of 0.0
        matches everything, which is not what a caller who passed NaN
        intended."""
        probe = [_failure("f", "Stripe webhook retries", "duplicate delivery on 500")]
        with pytest.raises(commons.CommonsInputError):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(session, orgs["customer-a"], probe, threshold=float("nan"))


# --- 7. The shipped seed corpus -------------------------------------------


SEED_CORPUS = Path(__file__).resolve().parents[2] / "commons" / "seed" / "substrate-v1.jsonl"


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestShippedSeedCorpus:
    """`commons-seed` is a mechanism; commons/seed/substrate-v1.jsonl is the
    corpus that actually ships with it. A mechanism with no corpus leaves
    every first customer looking at a 0% coverage report, so the corpus is
    part of the product and is tested like it.

    What is pinned here is the corpus being *loadable and honest*, not the
    coverage number it produces -- that is measured separately and on
    held-out data (commons/eval/), because a coverage figure computed
    against the same file that produced the corpus would be meaningless.
    """

    def test_every_line_is_loadable_by_commons_seed(self):
        """`commons_seed` silently skips lines it cannot use. A corpus that
        ships with skipped lines is a corpus nobody checked, so assert the
        preconditions the loader enforces, line by line."""
        assert SEED_CORPUS.exists(), f"the shipped corpus is missing: {SEED_CORPUS}"
        titles = set()
        for i, raw in enumerate(SEED_CORPUS.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            rec = json.loads(raw)  # a parse failure here is the test failing
            assert rec.get("title"), f"line {i}: commons_seed requires a title"
            assert rec.get("solution_text"), f"line {i}: commons_seed requires solution_text"
            assert isinstance(rec.get("tags"), list) and rec["tags"], f"line {i}: needs tags"
            key = rec["title"].strip().lower()
            assert key not in titles, f"line {i}: duplicate title {rec['title']!r}"
            titles.add(key)
        assert len(titles) >= 40, "a corpus this small will not move anyone's coverage number"

    def test_every_record_cites_where_it_came_from(self):
        """Curated knowledge is stored as `shared_rationale`, which is the
        only provenance a customer ever sees. A row without a citation is
        indistinguishable from something we made up."""
        for i, raw in enumerate(SEED_CORPUS.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            rec = json.loads(raw)
            assert rec.get("source"), f"line {i}: no provenance for {rec.get('title')!r}"

    async def test_loads_into_a_hub_and_answers_a_query(self, session_factory, orgs):
        """End to end: the shipped file goes in, and a customer org gets a
        real answer out of it."""
        from hub import manage

        await manage.commons_seed(
            str(SEED_CORPUS), orgs["operator"], session_factory=session_factory,
        )
        async with session_scope(session_factory) as session:
            rows = (await session.execute(select(Trace))).scalars().all()
            assert len(rows) >= 40
            assert all(r.commons_source == "seed" for r in rows)
            assert all(r.shared_with_commons for r in rows)
            assert all(r.commons_signature for r in rows)
            assert all(r.shared_rationale for r in rows)

            probe = rows[0]
            report = await crud.commons_overlap(
                session, orgs["customer-a"],
                [_failure("f", probe.title, probe.context_text, probe.tags)],
            )
        assert report["n_covered"] == 1

    async def test_loading_it_reports_the_right_entry_count(self, session_factory, orgs, capsys):
        """The corpus loads as Knowledge Base content, checkable via
        kb-stats -- the operator's own view of what it curated."""
        from hub import manage

        await manage.commons_seed(
            str(SEED_CORPUS), orgs["operator"], session_factory=session_factory,
        )
        capsys.readouterr()
        await manage.kb_stats(session_factory=session_factory)
        out = capsys.readouterr().out
        n = len(SEED_CORPUS.read_text(encoding="utf-8").splitlines())
        assert f"knowledge base entries:  {n}" in out


# --- 8. The held-out coverage evaluation -------------------------------


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestHeldOutEvaluation:
    """The Knowledge Base ships a coverage percentage, and a coverage
    percentage nobody validated is worse than none -- it gets quoted.
    commons/eval/ measures it against probes the corpus was not built from;
    these tests keep that measurement runnable and keep its conclusions
    from silently drifting.

    Deliberately NOT asserted: an exact recall figure. The measured value
    is recorded in commons/eval/RESULTS.md, and pinning it here would make
    any corpus improvement look like a test failure. What is asserted is
    the property that must not regress -- the matcher does not report
    coverage it does not have."""

    def _evaluate(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_commons_eval_run", SEED_CORPUS.parent.parent / "eval" / "run.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_evaluation_still_runs(self):
        r = self._evaluate().evaluate()
        assert r["n_positive"] >= 40 and r["n_negative"] >= 20
        assert r["threshold"] == commons.DEFAULT_COMMONS_THRESHOLD

    def test_no_absent_failure_is_reported_as_covered(self):
        """The negative controls are the half of this evaluation that
        author bias cannot flatter, and they are what makes the shipped
        number safe to quote. If the corpus or the matcher ever starts
        claiming one of the 22 deliberately-absent failures -- including
        the near misses -- the coverage report has begun over-claiming and
        that is worse than low recall."""
        r = self._evaluate().evaluate()
        assert r["false_positive_rate"] == 0.0, (
            "the Knowledge Base is now reporting coverage it does not have: "
            + ", ".join(
                x["label"] for x in r["results"]
                if x["expect"] == "uncovered" and x["matched"]
            )
        )

    def test_matches_point_at_the_right_record(self):
        """A match that lands on the wrong record is a customer opening a
        trace that does not solve their problem -- worse than no match,
        because it spends their trust."""
        r = self._evaluate().evaluate()
        assert r["right_row_rate"] == 1.0

    def test_results_are_recorded_with_their_limits(self):
        """The measured numbers are only safe to quote alongside what
        produced them. If RESULTS.md ever loses the limitations section,
        the numbers start travelling on their own."""
        text = (SEED_CORPUS.parent.parent / "eval" / "RESULTS.md").read_text(encoding="utf-8")
        assert "Limits of this evaluation" in text
        assert "Same author" in text

    async def test_the_returned_note_says_the_number_is_a_floor(self, session_factory, orgs):
        """The evaluation's finding has to reach the person reading the
        number, not just the repository. A coverage figure that a customer
        reads as an estimate, when measured recall says it is a floor, is
        the number that gets quoted and then falls apart."""
        await _seed(session_factory, orgs["operator"], "Some failure", "ctx", "fix")
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["customer-a"], [_failure("f", "Some failure", "ctx")],
            )
        assert "FLOOR" in report["note"]
        assert "misses are not evidence of absence" in report["note"]

    async def test_a_measurement_that_measured_nothing_is_not_qualified(self, session_factory, orgs):
        """An empty Knowledge Base has no number to qualify -- appending the
        recall caveat there would imply a real comparison happened."""
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["customer-a"], [_failure("f", "anything", "ctx")],
            )
        assert "by construction, not by finding" in report["note"]
        assert "FLOOR" not in report["note"]


# --- 9. The two retrieval tiers make opposite trades --------------------


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestRetrievalTiersDiffer:
    """STRATEGY.md §12.4.3 claimed per-org retrieval shared the Knowledge
    Base matcher's recall defect because it shares a tokenizer. Measurement
    (commons/eval/retrieval_tiers.py) showed the opposite, and §12.7 records
    the correction. These pin the property that correction rests on, so the
    strategy document cannot quietly drift away from its own evidence.

    What is asserted is the SHAPE of each tier's trade, not exact figures --
    pinning 84.8% would make any corpus improvement look like a failure."""

    def _evaluate(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_retrieval_tiers", SEED_CORPUS.parent.parent / "eval" / "retrieval_tiers.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.evaluate()

    def test_per_org_retrieval_has_high_recall(self):
        """It ranks instead of thresholding, so a paraphrase still surfaces.
        If this drops toward the Knowledge Base's coverage figure, the
        per-org product's core loop is broken and §12.7's conclusion no
        longer holds."""
        r = self._evaluate()
        assert r["recall"][5] > 0.7, f"per-org recall@5 collapsed to {r['recall'][5]:.1%}"

    def test_per_org_retrieval_pays_for_that_with_no_precision(self):
        """The other half of the trade, asserted so it is never mistaken for
        a coverage signal: with no threshold, failures the corpus cannot
        answer still come back with a result."""
        r = self._evaluate()
        assert r["neg_returns_something"][1] > 0.5, (
            "negative controls stopped returning results -- if a threshold was "
            "added to rank_lessons, §12.7's analysis needs redoing"
        )

    def test_the_two_tiers_are_not_interchangeable(self):
        """The finding in one line: same tokenizer, opposite outcomes. A
        change that made these converge would invalidate the reasoning in
        both RESULTS.md and STRATEGY.md §12.7."""
        r = self._evaluate()
        assert r["recall"][1] > 0.5, "per-org tier should rank, not threshold"
        assert r["recall_anywhere"] > 0.9, (
            "the corpus should contain findable answers for nearly every probe -- "
            "if not, the Knowledge Base's problem really is the corpus after all"
        )
