"""Tests for commontrace/redundancy.py — near-duplicate detection.

Three things need guarding here, and they are different in kind:

1. **The similarity primitive is correct.** `jaccard` and `token_set` are
   the ground truth every other function in this module and in
   `commontrace/dosage.py` builds on.
2. **`find_near_duplicates` finds the same pairs with and without the LSH
   fast path.** The banded path is a performance optimization over items
   below `LSH_MIN_ITEMS`; it must not change the ANSWER, only how fast it
   is reached. This is checked by forcing `LSH_MIN_ITEMS` down rather than
   building a 200-item fixture.
3. **What is compared matches what the docstring promises** -- tags/domain
   excluded, description/applies_when/do_not_apply_when/body included.
"""
from __future__ import annotations

import pytest

from commontrace import redundancy


class TestTokenSetAndJaccard:
    def test_identical_text_is_similarity_one(self):
        text = "never retry a payment without an idempotency key"
        assert redundancy.jaccard(redundancy.token_set(text), redundancy.token_set(text)) == 1.0

    def test_disjoint_text_is_zero(self):
        a = redundancy.token_set("alpha bravo charlie delta")
        b = redundancy.token_set("xray yankee zulu whiskey")
        assert redundancy.jaccard(a, b) == 0.0

    def test_empty_text_is_similar_to_nothing_including_itself(self):
        """The degenerate alternative (empty == empty == 1.0) would make
        every content-free lesson a duplicate of every other one -- the same
        failure overlap.minhash's own docstring documents avoiding."""
        empty = redundancy.token_set("")
        assert redundancy.jaccard(empty, empty) == 0.0
        assert redundancy.jaccard(empty, redundancy.token_set("something")) == 0.0

    def test_stopwords_and_single_characters_are_excluded(self):
        tokens = redundancy.token_set("the a of to I x")
        assert tokens == frozenset()

    def test_shares_vocabulary_with_the_retriever(self):
        """A corpus this module calls redundant should be redundant in the
        same vocabulary the local retriever ranks in -- see the module
        docstring on why a second, drifting stopword list would be a real
        failure mode here."""
        from commontrace._lexical import STOPWORDS

        assert "the" in STOPWORDS
        assert redundancy.token_set("the") == frozenset()


class TestComparableText:
    def test_uses_description_applies_when_do_not_apply_when_and_body(self):
        fm = {
            "description": "Payment webhook delivered twice",
            "applies_when": "A webhook is retried after a timeout",
            "do_not_apply_when": "The provider guarantees exactly-once delivery",
            "tags": ["payments", "webhooks"],
            "domain": "idempotency",
        }
        text = redundancy.comparable_text(fm, body="Persist the event id before any side effect")
        assert "Payment webhook delivered twice" in text
        assert "A webhook is retried after a timeout" in text
        assert "The provider guarantees exactly-once delivery" in text
        assert "Persist the event id" in text

    def test_tags_and_domain_are_excluded(self):
        """Grouping metadata is shared by construction among lessons in the
        same area; including it would add a constant similarity floor to
        exactly the pairs most likely to be compared."""
        fm = {"description": "x", "tags": ["unique_tag_xyz"], "domain": "unique_domain_xyz"}
        text = redundancy.comparable_text(fm)
        assert "unique_tag_xyz" not in text
        assert "unique_domain_xyz" not in text

    def test_missing_fields_do_not_raise(self):
        assert redundancy.comparable_text({}) == ""


class TestFindNearDuplicates:
    def test_finds_an_obvious_restatement(self):
        items = [
            ("a", "never retry a payment without an idempotency key"),
            ("b", "do not retry a payment without an idempotency key"),
            ("c", "rotate credentials every ninety days"),
        ]
        pairs = redundancy.find_near_duplicates(items, threshold=0.5)
        assert len(pairs) == 1
        assert {pairs[0].a, pairs[0].b} == {"a", "b"}

    def test_a_single_item_finds_nothing(self):
        assert redundancy.find_near_duplicates([("a", "text")]) == []

    def test_empty_input_finds_nothing(self):
        assert redundancy.find_near_duplicates([]) == []

    def test_pairs_are_ordered_most_similar_first(self):
        items = [
            ("a", "alpha bravo charlie delta echo foxtrot"),
            ("b", "alpha bravo charlie delta echo golf"),  # 5/7 overlap with a
            ("c", "alpha bravo hotel india juliet kilo"),  # 2/10 overlap with a
        ]
        pairs = redundancy.find_near_duplicates(items, threshold=0.1)
        assert pairs[0].similarity >= pairs[-1].similarity

    def test_pair_labels_are_sorted_for_stability(self):
        items = [("z", "same text here"), ("a", "same text here")]
        pairs = redundancy.find_near_duplicates(items, threshold=0.5)
        assert pairs[0].a == "a" and pairs[0].b == "z"

    def test_a_nonpositive_threshold_is_refused(self):
        with pytest.raises(ValueError):
            redundancy.find_near_duplicates([("a", "x"), ("b", "y")], threshold=0.0)

    def test_lsh_path_agrees_with_exact_comparison(self, monkeypatch):
        """The banded fast path must find the same pairs as brute force --
        it is a performance optimization, not a different algorithm."""
        monkeypatch.setattr(redundancy, "LSH_MIN_ITEMS", 5)
        items = [
            ("dup_a", "never retry a payment without an idempotency key here"),
            ("dup_b", "never retry a payment without an idempotency key there"),
            ("distinct_1", "rotate credentials every ninety days for compliance"),
            ("distinct_2", "cache invalidation on write not on read completion"),
            ("distinct_3", "commit offsets after processing not on message fetch"),
            ("distinct_4", "shard by tenant id not by request timestamp value"),
        ]
        exact = redundancy.find_near_duplicates(items, threshold=0.4, num_perm=64)
        monkeypatch.setattr(redundancy, "LSH_MIN_ITEMS", 200)
        lsh = redundancy.find_near_duplicates(items, threshold=0.4, num_perm=64)
        assert {(p.a, p.b) for p in exact} == {(p.a, p.b) for p in lsh}

    def test_custom_similarity_function_is_used_instead_of_lexical(self):
        """Callers drive this from the optional semantic layer by passing
        their own metric -- the LSH fast path must not silently override it."""
        calls = []

        def always_similar(a, b):
            calls.append((a, b))
            return 1.0

        pairs = redundancy.find_near_duplicates(
            [("a", "x"), ("b", "y")], threshold=0.5, similarity=always_similar,
        )
        assert len(pairs) == 1
        assert calls  # the custom function was actually invoked


class TestClosest:
    def test_finds_the_best_match_above_threshold(self):
        match = redundancy.closest(
            "never retry a payment without an idempotency key",
            [
                ("unrelated", "rotate credentials every ninety days"),
                ("close", "do not retry a payment without an idempotency key"),
            ],
            threshold=0.5,
        )
        assert match is not None
        assert match.a == "close"

    def test_none_when_nothing_clears_the_threshold(self):
        match = redundancy.closest(
            "never retry a payment without an idempotency key",
            [("unrelated", "rotate credentials every ninety days")],
            threshold=0.5,
        )
        assert match is None

    def test_empty_query_matches_nothing(self):
        assert redundancy.closest("", [("a", "some text")], threshold=0.1) is None


class TestCalibration:
    """The default threshold is a measured constant -- see
    commontrace/reference/measure_redundancy.py and the module docstring's
    'HOW THE DEFAULT THRESHOLD WAS CHOSEN' section. This just pins that no
    genuinely distinct pair in the shipped field fixtures crosses it, so a
    change to either the fixtures or the default is caught rather than
    silently drifting apart."""

    def test_default_threshold_is_silent_on_the_field_fixtures(self):
        import glob
        import json
        import os

        fixtures_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "commontrace", "fixtures", "fields",
        )
        items = []
        for path in sorted(glob.glob(os.path.join(fixtures_dir, "*.json"))):
            with open(path, encoding="utf-8") as fh:
                field = json.load(fh)
            for lesson in field.get("lessons", []):
                items.append((
                    f"{field['field']}/{lesson['name']}",
                    redundancy.comparable_text(lesson),
                ))
        pairs = redundancy.find_near_duplicates(items, threshold=redundancy.DEFAULT_THRESHOLD)
        assert pairs == [], (
            f"default threshold {redundancy.DEFAULT_THRESHOLD} flags genuinely distinct "
            f"fixture lessons as duplicates: {pairs}"
        )
