"""rank_candidates: the pure ranking primitive, no database.

Split from hub/tests/test_commons_search.py because that module carries a
module-level asyncio mark and these are synchronous. The property they
guard is the same one hub/tests/test_commons.py pins for best_matches: the
numpy fast path and the pure-Python reference path must agree exactly, or
the commons returns confident wrong similarity numbers on whichever hosts
happen to have numpy.
"""
from __future__ import annotations

import pytest

from hub import commons

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


class TestRankCandidatesUnit:
    """rank_candidates has a numpy path and a pure-Python path, and they must
    agree exactly -- the same property hub/tests/test_commons.py pins for
    best_matches. A fast path that quietly diverges produces confident wrong
    similarity numbers, the worst failure mode available here."""

    def test_both_implementations_agree(self, monkeypatch):
        pytest.importorskip("numpy")
        query = commons.signature_for("payment webhook delivered twice", "", [])
        corpus = [
            commons.signature_for(*args[:2], args[3])
            for args in (WEBHOOK, POOL)
        ]
        with_numpy = commons.rank_candidates(query, corpus, 5)
        monkeypatch.setattr(commons, "_np", None)
        without_numpy = commons.rank_candidates(query, corpus, 5)
        assert [i for i, _ in with_numpy] == [i for i, _ in without_numpy]
        for (_, a), (_, b) in zip(with_numpy, without_numpy):
            assert a == pytest.approx(b)

    def test_an_empty_corpus_returns_nothing(self):
        assert commons.rank_candidates([1] * commons.COMMONS_NUM_PERM, [], 5) == []

    def test_a_nonpositive_top_k_returns_nothing(self):
        query = commons.signature_for("x", "", [])
        assert commons.rank_candidates(query, [query], 0) == []
