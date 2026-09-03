"""Which of a query's terms are worth matching on -- the pure half.

`hub/search.py:choose_terms` decides, per query, which lexemes the tsquery
is built from. It is the mechanism that keeps a relaxed (OR) match from
being a firehose, and it needs no database, so it is tested here rather than
against Postgres -- these cases are the ones that decide whether a real
fleet's search is useful, and they should run in milliseconds and always.

The end-to-end behaviour is in `test_retrieval_relaxation.py`.
"""
from __future__ import annotations

from hub import search


class TestTermSelectionIsPure:
    """`choose_terms` needs no database, and these are the cases that decide
    whether a real fleet's search is useful or is a firehose."""

    def test_rarest_terms_are_kept_and_the_corpus_wide_one_is_dropped(self):
        chosen = search.choose_terms(
            [("retri", 64_001), ("storm", 320), ("pool", 12), ("checkout", 0)]
        )
        assert chosen.used == ("checkout", "pool", "storm")
        assert chosen.ignored == ("retri",)

    def test_a_term_over_budget_is_dropped_even_when_it_is_the_only_one(self):
        """The case that decides whether a bad query returns noise or nothing.

        Every result would share exactly one word, and that word is in
        essentially every trace the org has. An agent injects what it is
        given, so twenty unrelated incidents is worse than an empty answer.
        """
        chosen = search.choose_terms([("retri", 64_001)])
        assert chosen.used == ()
        assert chosen.ignored == ("retri",)

    def test_the_cumulative_sum_is_what_bounds_the_ranker(self):
        """A union is never larger than the sum of its parts, so keeping the
        sum under budget bounds rows ranked without counting the union."""
        chosen = search.choose_terms(
            [("a", 5_000), ("b", 4_000), ("c", 100)], budget=8_000
        )
        assert chosen.used == ("c", "b")  # 100 + 4000 fits; +5000 would not
        assert chosen.ignored == ("a",)

    def test_zero_frequency_terms_are_kept_so_used_does_not_lie(self):
        chosen = search.choose_terms([("zzz", 0), ("pool", 12)])
        assert chosen.used == ("zzz", "pool")
        assert chosen.ignored == ()

    def test_ties_break_alphabetically_so_the_query_is_reproducible(self):
        """Four equally common terms would otherwise be selected in whatever
        order the probe happened to return them, and the same search would
        compile to a different tsquery on different days."""
        one = search.choose_terms(
            [("d", 3_000), ("c", 3_000), ("b", 3_000), ("a", 3_000)], budget=8_000
        )
        two = search.choose_terms(
            [("a", 3_000), ("b", 3_000), ("c", 3_000), ("d", 3_000)], budget=8_000
        )
        assert one.used == two.used == ("a", "b")

    def test_all_terms_reports_every_lexeme_however_it_was_treated(self):
        chosen = search.choose_terms([("retri", 64_001), ("pool", 12)])
        assert set(chosen.all_terms) == {"retri", "pool"}
