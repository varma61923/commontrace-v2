"""Tests for the opt-in cross-org commons.

Two things are being pinned here, and the second matters more than the first:

1. That the commons works -- an org can contribute, withdraw, and ask the
   coverage question, and gets sane numbers back.

2. That adding it did NOT create a cross-tenant leak. The commons is the
   only path in hub/crud.py by which a row can cross an org boundary, so
   every way it could over-share is tested explicitly: unshared traces,
   quarantined traces, withdrawn traces, and traces belonging to the caller
   itself must never appear in a commons result.

hub/tests/test_tenant_isolation.py continues to pass unchanged, which is
the complementary half of the same claim: the ordinary read paths did not
loosen.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from hub import commons, crud
from hub.abuse import TraceRejected, make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def orgs(session_factory):
    """Three orgs: a contributor, a second contributor, and a consumer."""
    async with session_scope(session_factory) as session:
        made = {}
        for name in ("contributor-a", "contributor-b", "consumer"):
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
    dropping the module-level mark that the other 21 tests need."""

    def test_hub_and_client_produce_identical_signatures(self):
        """The whole commons is meaningless if the Hub and the client draw
        different MinHash permutations -- estimate_jaccard would compare
        mismatched positions and return a confident, wrong number rather
        than failing. hub/commons.py imports the client's module precisely
        so this cannot drift; this test fails loudly if someone ever
        reimplements it locally."""
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


# --- 2. Contribution is explicit and revocable -------------------------


class TestSharingIsOptIn:
    async def test_contributed_traces_are_private_by_default(self, session_factory, config, orgs):
        trace = await _contribute(
            session_factory, config, orgs["contributor-a"], "t", "c", "s"
        )
        async with session_scope(session_factory) as session:
            row = await session.get(Trace, trace["id"])
        assert row.shared_with_commons is False
        assert row.commons_signature is None
        assert row.shared_at is None

    async def test_share_marks_it_and_computes_a_signature(self, session_factory, config, orgs):
        trace = await _contribute(
            session_factory, config, orgs["contributor-a"],
            "Stripe webhook retries", "duplicate delivery on 500", "use an idempotency key",
        )
        async with session_scope(session_factory) as session:
            result = await crud.share_trace(
                session, orgs["contributor-a"], trace["id"], rationale="substrate: payment API semantics",
            )
        assert result["shared_with_commons"] is True

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, trace["id"])
        assert row.shared_with_commons is True
        assert row.shared_at is not None
        assert row.shared_rationale == "substrate: payment API semantics"
        assert len(row.commons_signature) == commons.COMMONS_NUM_PERM

    async def test_an_org_cannot_share_a_trace_it_does_not_own(self, session_factory, config, orgs):
        """Same 404-not-403 shape as get_trace: returning a permission error
        would confirm the id exists in another org."""
        trace = await _contribute(session_factory, config, orgs["contributor-a"], "t", "c", "s")
        async with session_scope(session_factory) as session:
            result = await crud.share_trace(session, orgs["consumer"], trace["id"])
        assert result is None

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, trace["id"])
        assert row.shared_with_commons is False, "a foreign org's call must not have shared it"

    async def test_quarantined_traces_cannot_be_shared(self, session_factory, config, orgs):
        """Quarantine exists to contain suspect content; letting it into a
        corpus other orgs read would propagate exactly what it contains."""
        trace = await _contribute(session_factory, config, orgs["contributor-a"], "t", "c", "s")
        async with session_scope(session_factory) as session:
            row = await session.get(Trace, trace["id"])
            row.quarantined = True
            row.quarantine_reason = "suspected spam"

        with pytest.raises(TraceRejected):
            async with session_scope(session_factory) as session:
                await crud.share_trace(session, orgs["contributor-a"], trace["id"])

    async def test_unshare_withdraws_it_and_clears_the_signature(self, session_factory, config, orgs):
        trace = await _contribute(session_factory, config, orgs["contributor-a"], "t", "c", "s")
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, orgs["contributor-a"], trace["id"])
        async with session_scope(session_factory) as session:
            result = await crud.unshare_trace(session, orgs["contributor-a"], trace["id"])
        assert result["shared_with_commons"] is False

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, trace["id"])
        assert row.shared_with_commons is False
        assert row.commons_signature is None, "a withdrawn trace must stop matching immediately"

    async def test_sharing_is_audited(self, session_factory, config, orgs):
        from hub.models import AuditLogEntry

        trace = await _contribute(session_factory, config, orgs["contributor-a"], "t", "c", "s")
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, orgs["contributor-a"], trace["id"], rationale="because")
        async with session_scope(session_factory) as session:
            rows = (await session.execute(select(AuditLogEntry))).scalars().all()
        actions = {r.action for r in rows}
        assert "share_trace" in actions
        blob = " ".join(r.summary for r in rows)
        assert "because" not in blob, "the audit summary must stay content-free"


# --- 3. The commons boundary must not leak -----------------------------


class TestCommonsDoesNotLeak:
    async def test_unshared_traces_never_appear(self, session_factory, config, orgs):
        await _contribute(
            session_factory, config, orgs["contributor-a"],
            "Stripe webhook retries", "duplicate delivery on 500", "SECRET-SOLUTION",
        )
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["consumer"],
                [_failure("f1", "Stripe webhook retries", "duplicate delivery on 500")],
            )
        assert report["n_commons_traces"] == 0
        assert report["n_covered"] == 0
        assert report["matches"] == []

    async def test_a_withdrawn_trace_stops_matching(self, session_factory, config, orgs):
        trace = await _contribute(
            session_factory, config, orgs["contributor-a"],
            "Stripe webhook retries", "duplicate delivery on 500", "use an idempotency key",
        )
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, orgs["contributor-a"], trace["id"])

        probe = [_failure("f1", "Stripe webhook retries", "duplicate delivery on 500")]
        async with session_scope(session_factory) as session:
            before = await crud.commons_overlap(session, orgs["consumer"], probe)
        assert before["n_covered"] == 1

        async with session_scope(session_factory) as session:
            await crud.unshare_trace(session, orgs["contributor-a"], trace["id"])
        async with session_scope(session_factory) as session:
            after = await crud.commons_overlap(session, orgs["consumer"], probe)
        assert after["n_covered"] == 0
        assert after["matches"] == []

    async def test_a_quarantined_shared_trace_is_excluded(self, session_factory, config, orgs):
        """Belt and braces: even if a trace was shared before being
        quarantined, the commons query must still exclude it."""
        trace = await _contribute(
            session_factory, config, orgs["contributor-a"],
            "Stripe webhook retries", "duplicate delivery on 500", "use an idempotency key",
        )
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, orgs["contributor-a"], trace["id"])
        async with session_scope(session_factory) as session:
            row = await session.get(Trace, trace["id"])
            row.quarantined = True

        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["consumer"],
                [_failure("f1", "Stripe webhook retries", "duplicate delivery on 500")],
            )
        assert report["n_commons_traces"] == 0
        assert report["n_covered"] == 0

    async def test_your_own_shared_traces_are_excluded_from_your_own_number(
        self, session_factory, config, orgs
    ):
        """The headline is "what would I GAIN from everyone else". Counting
        your own contributions would inflate it into something useless for
        exactly the decision it informs."""
        trace = await _contribute(
            session_factory, config, orgs["contributor-a"],
            "Stripe webhook retries", "duplicate delivery on 500", "use an idempotency key",
        )
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, orgs["contributor-a"], trace["id"])

        probe = [_failure("f1", "Stripe webhook retries", "duplicate delivery on 500")]

        # The contributor asking about its own trace: no credit.
        async with session_scope(session_factory) as session:
            own = await crud.commons_overlap(session, orgs["contributor-a"], probe)
        assert own["n_commons_traces"] == 0
        assert own["n_covered"] == 0

        # A different org asking the same question: full credit.
        async with session_scope(session_factory) as session:
            other = await crud.commons_overlap(session, orgs["consumer"], probe)
        assert other["n_commons_traces"] == 1
        assert other["n_covered"] == 1


# --- 4. The number itself -----------------------------------------------


class TestCoverageNumber:
    async def test_matching_failure_is_covered_and_returns_the_solution(
        self, session_factory, config, orgs
    ):
        """The payoff: a match hands back the shared trace in full, because
        its owner explicitly put it in the commons."""
        trace = await _contribute(
            session_factory, config, orgs["contributor-a"],
            "Stripe webhook retries", "duplicate delivery on 500 response",
            "Use an idempotency key on the handler", tags=["stripe", "webhooks"],
        )
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, orgs["contributor-a"], trace["id"])

        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["consumer"],
                [_failure("our-f1", "Stripe webhook retries", "duplicate delivery on 500 response",
                          ["stripe", "webhooks"])],
            )

        assert report["n_covered"] == 1
        assert report["covered_fraction"] == pytest.approx(1.0)
        match = report["matches"][0]
        assert match["failure_label"] == "our-f1"
        assert match["similarity"] >= commons.DEFAULT_COMMONS_THRESHOLD
        assert "idempotency key" in match["trace"]["solution_text"]

    async def test_unrelated_failure_is_not_covered(self, session_factory, config, orgs):
        trace = await _contribute(
            session_factory, config, orgs["contributor-a"],
            "Stripe webhook retries", "duplicate delivery on 500", "idempotency key",
        )
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, orgs["contributor-a"], trace["id"])

        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["consumer"],
                [_failure("f1", "CUDA kernel launch failure",
                          "grid dimensions exceeded the device limit", ["cuda"])],
            )
        assert report["n_covered"] == 0
        assert report["covered_fraction"] == pytest.approx(0.0)

    async def test_coverage_fraction_is_the_ratio_covered(self, session_factory, config, orgs):
        shared = await _contribute(
            session_factory, config, orgs["contributor-a"],
            "Stripe webhook retries", "duplicate delivery on 500", "idempotency key",
        )
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, orgs["contributor-a"], shared["id"])

        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["consumer"],
                [
                    _failure("hit", "Stripe webhook retries", "duplicate delivery on 500"),
                    _failure("miss1", "CUDA kernel launch", "grid dimensions exceeded"),
                    _failure("miss2", "DNS resolution flake", "intermittent NXDOMAIN in CI"),
                ],
            )
        assert report["n_failures"] == 3
        assert report["n_covered"] == 1
        assert report["covered_fraction"] == pytest.approx(1 / 3)

    async def test_aggregates_across_multiple_contributing_orgs(self, session_factory, config, orgs):
        """The network effect, minimally: two contributors cover more than
        either does alone."""
        a = await _contribute(
            session_factory, config, orgs["contributor-a"],
            "Stripe webhook retries", "duplicate delivery on 500", "idempotency key",
        )
        b = await _contribute(
            session_factory, config, orgs["contributor-b"],
            "CUDA kernel launch failure", "grid dimensions exceeded the device limit", "clamp the grid",
        )
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, orgs["contributor-a"], a["id"])
            await crud.share_trace(session, orgs["contributor-b"], b["id"])

        probe = [
            _failure("f1", "Stripe webhook retries", "duplicate delivery on 500"),
            _failure("f2", "CUDA kernel launch failure", "grid dimensions exceeded the device limit"),
        ]
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(session, orgs["consumer"], probe)

        assert report["n_commons_traces"] == 2
        assert report["n_covered"] == 2
        assert report["covered_fraction"] == pytest.approx(1.0)

    async def test_include_matches_false_returns_the_number_without_content(
        self, session_factory, config, orgs
    ):
        """A prospect evaluating whether to engage can get the headline
        number without pulling any content."""
        trace = await _contribute(
            session_factory, config, orgs["contributor-a"],
            "Stripe webhook retries", "duplicate delivery on 500", "idempotency key",
        )
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, orgs["contributor-a"], trace["id"])

        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["consumer"],
                [_failure("f1", "Stripe webhook retries", "duplicate delivery on 500")],
                include_matches=False,
            )
        assert report["n_covered"] == 1
        assert report["matches"] == []

    async def test_empty_commons_says_so_rather_than_reporting_zero_percent(
        self, session_factory, config, orgs
    ):
        """0% against an empty corpus is not a finding, and this number is
        exactly the kind that gets quoted once and repeated forever."""
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["consumer"], [_failure("f1", "anything", "at all")],
            )
        assert report["n_covered"] == 0
        assert "no other org has contributed" in report["note"].lower()


# --- 5. Scaling: the fast path must not diverge from the reference ------


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
        self, session_factory, config, orgs, monkeypatch
    ):
        """Silently truncating would under-report the one number this
        product's strategy rests on. It must be flagged and framed as a
        lower bound instead."""
        monkeypatch.setattr(commons, "max_corpus_scan", lambda: 1)
        for i in range(3):
            t = await _contribute(
                session_factory, config, orgs["contributor-a"],
                f"Shared substrate {i}", f"context {i}", "fix",
            )
            async with session_scope(session_factory) as session:
                await crud.share_trace(session, orgs["contributor-a"], t["id"])

        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["consumer"], [_failure("f", "Shared substrate 0", "context 0")],
            )

        assert report["corpus_truncated"] is True
        assert report["n_commons_traces"] == 1
        assert report["n_commons_traces_total"] == 3
        assert "lower bound" in report["note"].lower()

    async def test_an_untruncated_scan_is_not_flagged(self, session_factory, config, orgs):
        t = await _contribute(
            session_factory, config, orgs["contributor-a"], "Shared", "ctx", "fix",
        )
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, orgs["contributor-a"], t["id"])
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["consumer"], [_failure("f", "Shared", "ctx")],
            )
        assert report["corpus_truncated"] is False
        assert report["n_commons_traces"] == report["n_commons_traces_total"] == 1


class TestAgentTypePrefilter:
    async def test_narrowing_by_agent_type_excludes_other_fleets(
        self, session_factory, config, orgs
    ):
        """Not an approximation -- a support fleet's failures genuinely
        should not be scored against another agent type's substrate."""
        t = await _contribute(
            session_factory, config, orgs["contributor-a"],
            "Stripe webhook retries", "duplicate delivery on 500", "idempotency key",
        )
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, orgs["contributor-a"], t["id"])

        probe = [_failure("f", "Stripe webhook retries", "duplicate delivery on 500")]

        async with session_scope(session_factory) as session:
            matching = await crud.commons_overlap(
                session, orgs["consumer"], probe, agent_type="code",
            )
        assert matching["n_covered"] == 1

        async with session_scope(session_factory) as session:
            other = await crud.commons_overlap(
                session, orgs["consumer"], probe, agent_type="support",
            )
        assert other["n_commons_traces"] == 0
        assert other["n_covered"] == 0


# --- 6. Operator view: is the network effect real yet? ------------------


class TestCommonsStats:
    """Corpus size alone is vanity. The operator needs to know how many
    DISTINCT orgs contribute, because that is what a network effect is --
    and needs to be told plainly when the answer is "one"."""

    async def _share(self, session_factory, config, org_id, n, prefix):
        for i in range(n):
            t = await _contribute(
                session_factory, config, org_id, f"{prefix} {i}", f"ctx {i}", "fix",
            )
            async with session_scope(session_factory) as session:
                await crud.share_trace(session, org_id, t["id"])

    async def test_empty_commons_says_nothing_compounds_yet(
        self, session_factory, orgs, capsys
    ):
        from hub import manage

        await manage.commons_stats(session_factory=session_factory)
        out = capsys.readouterr().out
        assert "commons traces (org):  0" in out
        assert "nothing compounds" in out.lower()

    async def test_single_contributor_is_called_out_not_celebrated(
        self, session_factory, config, orgs, capsys
    ):
        from hub import manage

        await self._share(session_factory, config, orgs["contributor-a"], 3, "A")
        await manage.commons_stats(session_factory=session_factory)
        out = capsys.readouterr().out
        assert "contributing orgs:     1 of 3" in out
        assert "not yet a network" in out.lower()
        assert "concentration risk" in out.lower()

    async def test_reports_concentration_when_one_org_dominates(
        self, session_factory, config, orgs, capsys
    ):
        from hub import manage

        await self._share(session_factory, config, orgs["contributor-a"], 9, "A")
        await self._share(session_factory, config, orgs["contributor-b"], 1, "B")
        await manage.commons_stats(session_factory=session_factory)
        out = capsys.readouterr().out
        assert "contributing orgs:     2 of 3" in out
        assert "90%" in out
        # the warning wraps across lines, so normalise whitespace first
        assert "network effect as unproven" in " ".join(out.lower().split())

    async def test_balanced_contribution_gets_no_warning(
        self, session_factory, config, orgs, capsys
    ):
        from hub import manage

        await self._share(session_factory, config, orgs["contributor-a"], 5, "A")
        await self._share(session_factory, config, orgs["contributor-b"], 5, "B")
        await manage.commons_stats(session_factory=session_factory)
        out = capsys.readouterr().out
        assert "contributing orgs:     2 of 3" in out
        assert "unproven" not in out.lower()

    async def test_quarantined_shared_traces_are_not_counted(
        self, session_factory, config, orgs, capsys
    ):
        """Quarantined traces are excluded from commons queries, so counting
        them here would overstate the corpus an org can actually reach."""
        from hub import manage

        await self._share(session_factory, config, orgs["contributor-a"], 2, "A")
        async with session_scope(session_factory) as session:
            rows = (
                await session.execute(
                    select(Trace).where(Trace.org_id == orgs["contributor-a"])
                )
            ).scalars().all()
            rows[0].quarantined = True

        await manage.commons_stats(session_factory=session_factory)
        assert "commons traces (org):  1" in capsys.readouterr().out


# --- 7. Economics: contribution has to be measurably worth it -----------


class TestValueLedger:
    """A commons where contributing is pure altruism fills with filler
    (STRATEGY.md §3). Trace.commons_hits makes the value a contributor
    DELIVERS measurable, which is both the incentive and the pricing
    denominator -- so it has to be counted correctly."""

    async def _share_one(self, session_factory, config, org_id, title, ctx):
        t = await _contribute(session_factory, config, org_id, title, ctx, "the fix")
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, org_id, t["id"])
        return t["id"]

    async def test_a_covered_failure_credits_the_contributor(
        self, session_factory, config, orgs
    ):
        tid = await self._share_one(
            session_factory, config, orgs["contributor-a"],
            "Stripe webhook retries", "duplicate delivery on 500",
        )
        async with session_scope(session_factory) as session:
            row = await session.get(Trace, tid)
        assert row.commons_hits == 0

        async with session_scope(session_factory) as session:
            await crud.commons_overlap(
                session, orgs["consumer"],
                [_failure("f", "Stripe webhook retries", "duplicate delivery on 500")],
            )

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, tid)
        assert row.commons_hits == 1

    async def test_a_miss_credits_nobody(self, session_factory, config, orgs):
        tid = await self._share_one(
            session_factory, config, orgs["contributor-a"],
            "Stripe webhook retries", "duplicate delivery on 500",
        )
        async with session_scope(session_factory) as session:
            await crud.commons_overlap(
                session, orgs["consumer"],
                [_failure("f", "CUDA kernel launch", "grid dimensions exceeded")],
            )
        async with session_scope(session_factory) as session:
            row = await session.get(Trace, tid)
        assert row.commons_hits == 0

    async def test_hits_accumulate_across_separate_consumers(
        self, session_factory, config, orgs
    ):
        tid = await self._share_one(
            session_factory, config, orgs["contributor-a"],
            "Stripe webhook retries", "duplicate delivery on 500",
        )
        probe = [_failure("f", "Stripe webhook retries", "duplicate delivery on 500")]
        for consumer in ("consumer", "contributor-b"):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(session, orgs[consumer], probe)

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, tid)
        assert row.commons_hits == 2

    async def test_two_failures_in_one_query_hitting_the_same_trace_both_count(
        self, session_factory, config, orgs
    ):
        """The batch case `test_hits_accumulate_across_separate_consumers`
        doesn't cover: TWO submitted failures in the SAME commons_overlap
        call both best-matching the SAME shared trace -- a fleet hitting one
        substrate failure across several tasks and submitting them together,
        which is exactly the batch workflow this API exists to support.

        A naive `UPDATE ... WHERE id IN (hit_ids)` credits the row once per
        UPDATE STATEMENT regardless of how many times its id repeats in the
        IN-list -- Postgres does not re-apply the SET clause per duplicate.
        That silently under-counts this case by construction, contradicting
        this class's whole premise: value delivered has to be counted
        correctly, and two distinct failures genuinely covered is two hits,
        not one."""
        tid = await self._share_one(
            session_factory, config, orgs["contributor-a"],
            "Stripe webhook retries", "duplicate delivery on 500",
        )
        probe = [
            _failure("first-occurrence", "Stripe webhook retries", "duplicate delivery on 500"),
            _failure("second-occurrence", "Stripe webhook retries", "duplicate delivery on 500"),
        ]
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(session, orgs["consumer"], probe)
        assert report["n_covered"] == 2

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, tid)
        assert row.commons_hits == 2, (
            "two failures covered in one call must count as two hits, not one"
        )

    async def test_duplicate_signature_farming_is_capped_per_query(
        self, session_factory, config, orgs
    ):
        """Nothing on the wire stops a caller from submitting the identical
        signature many times in one request (up to
        commons.MAX_SUBMITTED_FAILURES). Without a cap, the "N submitted
        failures matching the same trace = N hits" rule the batch test above
        depends on turns one repeated signature into hundreds of
        query-credit hits for whichever trace it matches -- a colluding
        querying org could mint effectively unlimited query allowance for a
        sharing org just by repeating one signature. commons.
        MAX_HITS_PER_TRACE_PER_QUERY bounds how much a single call can credit
        one trace, while leaving small genuine multi-task batches (like the
        2-failure case above) fully credited."""
        tid = await self._share_one(
            session_factory, config, orgs["contributor-a"],
            "Stripe webhook retries", "duplicate delivery on 500",
        )
        probe = [
            _failure(f"occurrence-{i}", "Stripe webhook retries", "duplicate delivery on 500")
            for i in range(commons.MAX_HITS_PER_TRACE_PER_QUERY + 30)
        ]
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(session, orgs["consumer"], probe)
        # The caller's own coverage report is unaffected by the cap -- every
        # submitted failure it asked about really was covered.
        assert report["n_covered"] == len(probe)

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, tid)
        assert row.commons_hits == commons.MAX_HITS_PER_TRACE_PER_QUERY, (
            "one query repeating one signature must not credit a trace past the per-query cap"
        )

    async def test_counting_survives_concurrent_queries(self, session_factory, config, orgs):
        """The increment is an atomic in-database UPDATE, not a
        read-modify-write: contributor standing is the basis for pricing,
        so lost counts under concurrency would be lost revenue."""
        import asyncio

        tid = await self._share_one(
            session_factory, config, orgs["contributor-a"],
            "Stripe webhook retries", "duplicate delivery on 500",
        )
        probe = [_failure("f", "Stripe webhook retries", "duplicate delivery on 500")]

        async def _query():
            async with session_scope(session_factory) as session:
                return await crud.commons_overlap(session, orgs["consumer"], probe)

        await asyncio.gather(*[_query() for _ in range(12)])

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, tid)
        assert row.commons_hits == 12, "concurrent queries must not lose contributor credit"


class TestSeedProvenance:
    """Seeding breaks the cold start, but seeded rows must never be
    counted as evidence of a network effect -- that would make the one
    metric that decides the company's direction quietly self-referential."""

    async def _seed(self, session_factory, config, org_id, n=2):
        for i in range(n):
            t = await _contribute(
                session_factory, config, org_id, f"Seeded substrate {i}", f"ctx {i}", "fix",
            )
            async with session_scope(session_factory) as session:
                await crud.share_trace(session, org_id, t["id"])
                row = await session.get(Trace, t["id"])
                row.commons_source = "seed"

    async def test_seeded_traces_still_answer_queries(self, session_factory, config, orgs):
        """Seed content is real value to a querying fleet -- it just isn't
        a network effect. It must still match."""
        await self._seed(session_factory, config, orgs["contributor-a"], n=1)
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["consumer"], [_failure("f", "Seeded substrate 0", "ctx 0")],
            )
        assert report["n_covered"] == 1

    async def test_seeded_traces_do_not_count_as_contributing_orgs(
        self, session_factory, config, orgs, capsys
    ):
        from hub import manage

        await self._seed(session_factory, config, orgs["contributor-a"], n=3)
        await manage.commons_stats(session_factory=session_factory)
        out = capsys.readouterr().out
        assert "commons traces (org):  0" in out
        assert "commons traces (seed): 3" in out
        assert "contributing orgs:     0 of 3" in out
        assert "not a network effect" in out.lower()

    async def test_org_and_seed_are_reported_separately_by_value(
        self, session_factory, config, orgs, capsys
    ):
        from hub import manage

        await self._seed(session_factory, config, orgs["contributor-a"], n=2)
        t = await _contribute(
            session_factory, config, orgs["contributor-b"], "Real contribution", "ctx", "fix",
        )
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, orgs["contributor-b"], t["id"])

        await manage.commons_value(session_factory=session_factory)
        out = capsys.readouterr().out
        assert "org-contributed" in out
        assert "operator-seeded" in out


class TestPricingDenominator:
    async def test_reports_delivered_value_per_org(self, session_factory, config, orgs, capsys):
        from hub import manage

        t = await _contribute(
            session_factory, config, orgs["contributor-a"],
            "Stripe webhook retries", "duplicate delivery on 500", "idempotency key",
        )
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, orgs["contributor-a"], t["id"])
        async with session_scope(session_factory) as session:
            await crud.commons_overlap(
                session, orgs["consumer"],
                [_failure("f", "Stripe webhook retries", "duplicate delivery on 500")],
            )

        await manage.commons_value(session_factory=session_factory)
        out = capsys.readouterr().out
        assert "contributor-a" in out
        assert "delivered" in out.lower()

    async def test_says_so_when_nothing_has_been_delivered_yet(
        self, session_factory, config, orgs, capsys
    ):
        from hub import manage

        t = await _contribute(session_factory, config, orgs["contributor-a"], "t", "c", "s")
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, orgs["contributor-a"], t["id"])
        await manage.commons_value(session_factory=session_factory)
        assert "has covered anyone's failure yet" in capsys.readouterr().out

    async def test_empty_commons_reports_cleanly(self, session_factory, orgs, capsys):
        from hub import manage

        await manage.commons_value(session_factory=session_factory)
        assert "no value to attribute" in capsys.readouterr().out


# --- 8. Untrusted input -------------------------------------------------


class TestSubmittedInputIsValidated:
    async def test_rejects_a_wrong_width_signature(self, session_factory, config, orgs):
        """A mismatched width is not salvageable -- comparing it would
        return a confident, meaningless number."""
        with pytest.raises(commons.CommonsInputError):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(
                    session, orgs["consumer"], [{"label": "f", "signature": [1, 2, 3]}],
                )

    async def test_rejects_too_many_failures(self, session_factory, config, orgs):
        oversized = [
            {"label": f"f{i}", "signature": [0] * commons.COMMONS_NUM_PERM}
            for i in range(commons.MAX_SUBMITTED_FAILURES + 1)
        ]
        with pytest.raises(commons.CommonsInputError):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(session, orgs["consumer"], oversized)

    async def test_rejects_non_integer_signature_values(self, session_factory, config, orgs):
        with pytest.raises(commons.CommonsInputError):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(
                    session, orgs["consumer"],
                    [{"label": "f", "signature": ["x"] * commons.COMMONS_NUM_PERM}],
                )

    async def test_rejects_a_non_list_payload(self, session_factory, config, orgs):
        with pytest.raises(commons.CommonsInputError):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(session, orgs["consumer"], "not a list")

    async def test_empty_submission_is_not_an_error(self, session_factory, config, orgs):
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(session, orgs["consumer"], [])
        assert report["n_failures"] == 0
        assert report["covered_fraction"] == 0.0

    async def test_rejects_negative_signature_values(self, session_factory, config, orgs):
        """A negative value passes isinstance(v, int) but is outside the
        uint64 domain MinHash signatures live in -- on numpy hosts,
        converting it (`np.array(..., dtype=uint64)`) raises OverflowError,
        surfacing as an unhandled 500 instead of a clean 400."""
        with pytest.raises(commons.CommonsInputError):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(
                    session, orgs["consumer"],
                    [{"label": "f", "signature": [-1] * commons.COMMONS_NUM_PERM}],
                )

    async def test_rejects_signature_values_above_uint64_max(self, session_factory, config, orgs):
        with pytest.raises(commons.CommonsInputError):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(
                    session, orgs["consumer"],
                    [{"label": "f", "signature": [2**64] * commons.COMMONS_NUM_PERM}],
                )

    async def test_rejects_non_finite_threshold(self, session_factory, config, orgs):
        """max(0.0, min(nan, 1.0)) silently clamps NaN to 0.0 rather than
        rejecting it -- permissive, not a crash, but a threshold of 0.0
        matches everything, which is not what a caller who passed NaN
        intended."""
        probe = [_failure("f", "Stripe webhook retries", "duplicate delivery on 500")]
        with pytest.raises(commons.CommonsInputError):
            async with session_scope(session_factory) as session:
                await crud.commons_overlap(session, orgs["consumer"], probe, threshold=float("nan"))


# --- 9. The shipped seed corpus -----------------------------------------


SEED_CORPUS = Path(__file__).resolve().parents[2] / "commons" / "seed" / "substrate-v1.jsonl"


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestShippedSeedCorpus:
    """`commons-seed` is a mechanism; commons/seed/substrate-v1.jsonl is the
    corpus that actually ships with it. A mechanism with no corpus leaves
    every first prospect looking at a 0% coverage report, so the corpus is
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
        """Seeded knowledge is stored as `shared_rationale`, which is the
        only provenance a customer ever sees. A row without a citation is
        indistinguishable from something we made up."""
        for i, raw in enumerate(SEED_CORPUS.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            rec = json.loads(raw)
            assert rec.get("source"), f"line {i}: no provenance for {rec.get('title')!r}"

    async def test_loads_into_a_hub_and_answers_a_query(self, session_factory, orgs):
        """End to end: the shipped file goes in, and a *different* org gets
        a real answer out of it."""
        from hub import manage

        await manage.commons_seed(
            str(SEED_CORPUS), orgs["contributor-a"], session_factory=session_factory,
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
                session, orgs["consumer"],
                [_failure("f", probe.title, probe.context_text, probe.tags)],
            )
        assert report["n_covered"] == 1

    async def test_loading_it_does_not_inflate_the_network_effect_metric(
        self, session_factory, orgs, capsys
    ):
        """The honesty invariant, asserted against the real file rather
        than a fixture: after seeding the shipped corpus, the number that
        decides the company's direction is still zero."""
        from hub import manage

        await manage.commons_seed(
            str(SEED_CORPUS), orgs["contributor-a"], session_factory=session_factory,
        )
        capsys.readouterr()
        await manage.commons_stats(session_factory=session_factory)
        out = capsys.readouterr().out
        assert "commons traces (org):  0" in out
        assert "contributing orgs:     0 of 3" in out


# --- 10. The held-out coverage evaluation -------------------------------


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestHeldOutEvaluation:
    """The commons ships a coverage percentage, and a coverage percentage
    nobody validated is worse than none -- it gets quoted. commons/eval/
    measures it against probes the corpus was not built from; these tests
    keep that measurement runnable and keep its conclusions from silently
    drifting.

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
            "the commons is now reporting coverage it does not have: "
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

    async def test_the_returned_note_says_the_number_is_a_floor(
        self, session_factory, config, orgs
    ):
        """The evaluation's finding has to reach the person reading the
        number, not just the repository. A coverage figure that a customer
        reads as an estimate, when measured recall says it is a floor, is
        the number that gets quoted and then falls apart."""
        t = await _contribute(
            session_factory, config, orgs["contributor-a"], "Some failure", "ctx", "fix",
        )
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, orgs["contributor-a"], t["id"])
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["consumer"], [_failure("f", "Some failure", "ctx")],
            )
        assert "FLOOR" in report["note"]
        assert "misses are not evidence of absence" in report["note"]

    async def test_a_measurement_that_measured_nothing_is_not_qualified(
        self, session_factory, orgs
    ):
        """An empty commons has no number to qualify -- appending the
        recall caveat there would imply a real comparison happened."""
        async with session_scope(session_factory) as session:
            report = await crud.commons_overlap(
                session, orgs["consumer"], [_failure("f", "anything", "ctx")],
            )
        assert "by construction, not by finding" in report["note"]
        assert "FLOOR" not in report["note"]


# --- 11. The two retrieval tiers make opposite trades --------------------


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestRetrievalTiersDiffer:
    """STRATEGY.md §12.4.3 claimed per-org retrieval shared the commons
    matcher's recall defect because it shares a tokenizer. Measurement
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
        If this drops toward the commons figure, the per-org product's core
        loop is broken and §12.7's conclusion no longer holds."""
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
            "if not, the commons' problem really is the corpus after all"
        )
