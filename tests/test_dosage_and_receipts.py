from __future__ import annotations

import datetime

import pytest

from commontrace import dosage, receipts, retrieval


def _candidate(slug: str, chars: int = 100, **kwargs) -> dosage.Candidate:
    return dosage.Candidate(slug=slug, text="x" * chars, **kwargs)


class TestTheBudget:
    def test_a_negative_budget_is_refused(self):
        with pytest.raises(ValueError, match="cannot be negative"):
            dosage.Budget(max_chars=-1)

    def test_zero_admits_nothing_rather_than_erroring(self):
        dose = dosage.select([_candidate("a")], dosage.Budget(max_lessons=0))
        assert dose.admitted == ()
        assert dose.dropped[0].reason == dosage.REASON_COUNT


class TestDosage:
    def test_core_lessons_are_admitted_before_matched_ones(self):
        dose = dosage.select(
            [
                _candidate("matched_a", relevance=0.9),
                _candidate("always_on", core=True),
                _candidate("matched_b", relevance=0.8),
            ],
            dosage.Budget(max_lessons=2, max_chars=10_000),
        )
        assert [c.slug for c in dose.admitted] == ["always_on", "matched_a"]

    def test_matched_lessons_keep_retrievals_order(self):
        dose = dosage.select(
            [_candidate("first", relevance=0.9), _candidate("second", relevance=0.1)],
            dosage.Budget(max_lessons=5, max_chars=10_000),
        )
        assert [c.slug for c in dose.admitted] == ["first", "second"]

    def test_core_lessons_order_among_themselves_by_importance(self):
        dose = dosage.select(
            [
                _candidate("minor", core=True, importance=1),
                _candidate("showstopper", core=True, importance=5),
            ],
            dosage.Budget(max_lessons=5, max_chars=10_000),
        )
        assert [c.slug for c in dose.admitted] == ["showstopper", "minor"]

    def test_core_is_still_budgeted(self):
        dose = dosage.select(
            [_candidate(f"core{i}", core=True, importance=3) for i in range(5)]
            + [_candidate("matched", relevance=0.9)],
            dosage.Budget(max_lessons=3, max_chars=10_000),
        )
        assert len(dose.admitted) == 3
        assert dose.core_dropped, "an over-budget core set must be visible as such"

    def test_an_oversized_lesson_does_not_truncate_everything_below_it(self):
        dose = dosage.select(
            [
                _candidate("huge", chars=9_000, relevance=0.9),
                _candidate("small", chars=100, relevance=0.8),
            ],
            dosage.Budget(max_lessons=5, max_chars=1_000),
        )
        assert [c.slug for c in dose.admitted] == ["small"]
        assert dose.dropped[0].slug == "huge"

    def test_nothing_is_dropped_silently(self):
        dose = dosage.select(
            [_candidate(f"l{i}") for i in range(5)],
            dosage.Budget(max_lessons=2, max_chars=10_000),
        )
        assert len(dose.admitted) == 2
        assert {d.slug for d in dose.dropped} == {"l2", "l3", "l4"}
        assert all(d.reason for d in dose.dropped)

    def test_the_gauge_reports_the_cost(self):
        dose = dosage.select(
            [_candidate("a", chars=200)], dosage.Budget(max_lessons=5, max_chars=1_000)
        )
        gauge = dose.gauge()
        assert "200" in gauge and "1,000" in gauge and "20%" in gauge

    def test_core_is_read_tolerantly_from_frontmatter(self):
        assert dosage.is_core({"core": True})
        assert dosage.is_core({"core": "yes"})
        assert dosage.is_core({"core": "TRUE"})
        assert not dosage.is_core({})
        assert not dosage.is_core({"core": False})
        assert not dosage.is_core({"core": "no"})


class TestRedundancySuppression:
    def _dup_candidate(self, slug: str, text: str, **kwargs) -> dosage.Candidate:
        return dosage.Candidate(slug=slug, text=text, compare_text=text, **kwargs)

    def test_off_by_default(self):
        same_text = "never retry a payment without an idempotency key"
        dose = dosage.select(
            [
                self._dup_candidate("a", same_text, relevance=0.9),
                self._dup_candidate("b", same_text, relevance=0.8),
            ],
            dosage.Budget(max_lessons=5, max_chars=10_000),
        )
        assert {c.slug for c in dose.admitted} == {"a", "b"}

    def test_a_restatement_is_dropped_and_names_what_it_duplicates(self):
        same_text = "never retry a payment without an idempotency key " * 5
        dose = dosage.select(
            [
                self._dup_candidate("first", same_text, relevance=0.9),
                self._dup_candidate("second", same_text, relevance=0.8),
            ],
            dosage.Budget(max_lessons=5, max_chars=10_000, redundancy_threshold=0.3),
        )
        assert [c.slug for c in dose.admitted] == ["first"]
        assert dose.dropped[0].slug == "second"
        assert dose.dropped[0].reason == dosage.redundant_reason("first")
        assert dose.redundant_dropped == dose.dropped

    def test_the_freed_slot_goes_to_the_next_distinct_candidate(self):
        same_text = "never retry a payment without an idempotency key " * 5
        dose = dosage.select(
            [
                self._dup_candidate("first", same_text, relevance=0.9),
                self._dup_candidate("duplicate", same_text, relevance=0.8),
                self._dup_candidate("distinct", "rotate credentials every 90 days", relevance=0.7),
            ],
            dosage.Budget(max_lessons=2, max_chars=10_000, redundancy_threshold=0.3),
        )
        assert [c.slug for c in dose.admitted] == ["first", "distinct"]

    def test_core_is_never_suppressed_but_is_noted(self):
        same_text = "never retry a payment without an idempotency key " * 5
        dose = dosage.select(
            [
                self._dup_candidate("core_a", same_text, core=True, importance=5),
                self._dup_candidate("core_b", same_text, core=True, importance=4),
            ],
            dosage.Budget(max_lessons=5, max_chars=10_000, redundancy_threshold=0.3),
        )
        assert {c.slug for c in dose.admitted} == {"core_a", "core_b"}
        assert dose.redundant_core
        assert dose.redundant_core[0].slug == "core_b"

    def test_distinct_lessons_are_unaffected(self):
        dose = dosage.select(
            [
                self._dup_candidate("payments", "idempotency keys prevent double charges"),
                self._dup_candidate("security", "rotate credentials every 90 days"),
            ],
            dosage.Budget(max_lessons=5, max_chars=10_000, redundancy_threshold=0.3),
        )
        assert {c.slug for c in dose.admitted} == {"payments", "security"}
        assert not dose.dropped

    def test_a_threshold_outside_zero_one_is_refused(self):
        with pytest.raises(ValueError, match="redundancy_threshold"):
            dosage.Budget(redundancy_threshold=1.5)
        with pytest.raises(ValueError, match="redundancy_threshold"):
            dosage.Budget(redundancy_threshold=-0.1)

    def test_candidate_falls_back_to_text_when_compare_text_is_unset(self):
        candidate = dosage.Candidate(slug="a", text="hello world")
        assert candidate.comparable == "hello world"


class TestReciprocalRankFusion:
    def test_agreement_across_arms_beats_one_arms_confidence(self):
        fused = retrieval.reciprocal_rank_fusion({
            "lexical": ["agreed", "lexical_only"],
            "semantic": ["agreed", "semantic_only"],
        })
        assert fused[0][0] == "agreed"

    def test_an_arm_that_missed_a_document_does_not_vote_against_it(self):
        fused = dict(retrieval.reciprocal_rank_fusion({
            "lexical": ["a"],
            "semantic": ["b"],
        }))
        assert fused["a"] == fused["b"]

    def test_only_position_matters_not_the_incoming_scores(self):
        one = retrieval.reciprocal_rank_fusion({"arm": ["x", "y", "z"]})
        assert [item for item, _ in one] == ["x", "y", "z"]

    def test_the_order_is_deterministic(self):
        arms = {"a": ["p", "q"], "b": ["q", "p"]}
        assert retrieval.reciprocal_rank_fusion(arms) == (
            retrieval.reciprocal_rank_fusion(arms)
        )

    def test_top_k_bounds_the_result(self):
        fused = retrieval.reciprocal_rank_fusion(
            {"a": ["1", "2", "3", "4"]}, top_k=2
        )
        assert len(fused) == 2

    def test_a_non_positive_k_is_refused(self):
        with pytest.raises(ValueError, match="must be positive"):
            retrieval.reciprocal_rank_fusion({"a": ["x"]}, k=0)

    def test_no_arms_fuses_to_nothing(self):
        assert retrieval.reciprocal_rank_fusion({}) == []

    def test_a_weight_shifts_which_arm_wins_a_disagreement(self):
        arms = {"lexical": ["x", "y"], "semantic": ["y", "x"]}
        assert retrieval.reciprocal_rank_fusion(arms)[0][0] == "x"
        lexical_heavy = retrieval.reciprocal_rank_fusion(
            arms, weights={"lexical": 3.0, "semantic": 1.0})
        semantic_heavy = retrieval.reciprocal_rank_fusion(
            arms, weights={"lexical": 1.0, "semantic": 3.0})
        assert lexical_heavy[0][0] == "x"
        assert semantic_heavy[0][0] == "y"

    def test_an_unnamed_arm_weighs_one(self):
        arms = {"a": ["x", "y"], "b": ["y", "x"]}
        assert (
            retrieval.reciprocal_rank_fusion(arms, weights={"a": 1.0})
            == retrieval.reciprocal_rank_fusion(arms)
        )


def _receipt(occasion="occ-1", *, visible=(("a", "rev-a"), ("b", "rev-b")),
             admitted=(("a", "rev-a", 1),), withheld=()) -> receipts.Receipt:
    return receipts.Receipt(
        occasion_id=occasion,
        at=datetime.datetime(2026, 5, 1, tzinfo=datetime.timezone.utc).isoformat(),
        visible=tuple(receipts.Visible(s, r) for s, r in visible),
        admitted=tuple(
            receipts.Admitted(slug=s, revision=r, rank=k) for s, r, k in admitted
        ),
        withheld=tuple(withheld),
        query="password reset", scorer="idf", floor=0.04,
        chars_used=120, max_chars=8000, max_lessons=10,
    )


class TestReceipts:
    def test_a_receipt_round_trips(self, tmp_path):
        written = receipts.record(str(tmp_path), _receipt())
        [stored] = receipts.read_all(str(tmp_path))
        assert stored == written

    def test_the_digest_identifies_the_candidate_set_by_content(self, tmp_path):
        same = _receipt(visible=(("a", "rev-a"), ("b", "rev-b")))
        reordered = _receipt(visible=(("b", "rev-b"), ("a", "rev-a")))
        assert same.digest == reordered.digest

    def test_a_changed_revision_changes_the_digest(self, tmp_path):
        before = _receipt(visible=(("a", "rev-one"),))
        after = _receipt(visible=(("a", "rev-two"),))
        assert before.digest != after.digest

    def test_a_lesson_that_was_never_a_candidate_is_distinguishable(self, tmp_path):
        receipts.record(str(tmp_path), _receipt(visible=(("a", "rev-a"),)))
        [stored] = receipts.read_all(str(tmp_path))
        assert "b" not in {v.slug for v in stored.visible}

    def test_crowded_out_is_not_the_same_as_nothing_matched(self, tmp_path):
        receipts.record(str(tmp_path), _receipt(
            visible=(("a", "r"), ("b", "r")), admitted=(),
            withheld=(("a", dosage.REASON_CHARS), ("b", dosage.REASON_CHARS)),
        ))
        found = receipts.coverage(str(tmp_path))
        assert found.n_crowded_out == 1
        assert found.n_with_any_admitted == 0


class TestUseIsRecordedSeparatelyFromInjection:
    def test_injected_is_not_used(self, tmp_path):
        receipts.record(str(tmp_path), _receipt(admitted=(("a", "rev-a", 1),)))
        found = receipts.coverage(str(tmp_path))
        assert found.n_with_any_admitted == 1
        assert found.n_with_any_used == 0
        assert found.never_used == ("a",)

    def test_a_reported_use_is_joined_to_its_receipt(self, tmp_path):
        receipts.record(str(tmp_path), _receipt(admitted=(("a", "rev-a", 1),)))
        receipts.record_use(str(tmp_path), "occ-1", ["a"], succeeded=True)
        found = receipts.coverage(str(tmp_path))
        assert found.n_with_any_used == 1
        assert found.never_used == ()
        assert found.used_rate == 1.0

    def test_using_nothing_is_a_real_answer_and_is_stored(self, tmp_path):
        receipts.record(str(tmp_path), _receipt())
        receipts.record_use(str(tmp_path), "occ-1", [], succeeded=False)
        [entry] = receipts.read_uses(str(tmp_path))
        assert entry["used"] == []
        assert entry["succeeded"] is False

    def test_a_use_is_a_separate_line_not_an_edit(self, tmp_path):
        receipts.record(str(tmp_path), _receipt())
        receipts.record_use(str(tmp_path), "occ-1", ["a"])
        assert len(receipts.read_all(str(tmp_path))) == 1
        assert len(receipts.read_uses(str(tmp_path))) == 1

    def test_the_used_rate_excludes_occasions_that_got_nothing(self, tmp_path):
        receipts.record(str(tmp_path), _receipt("occ-1", admitted=(("a", "r", 1),)))
        receipts.record(str(tmp_path), _receipt("occ-2", admitted=()))
        receipts.record_use(str(tmp_path), "occ-1", ["a"])
        found = receipts.coverage(str(tmp_path))
        assert found.n_occasions == 2
        assert found.used_rate == 1.0

    def test_a_corrupt_line_does_not_lose_the_rest(self, tmp_path):
        receipts.record(str(tmp_path), _receipt())
        with open(receipts.receipts_path(str(tmp_path)), "a", encoding="utf-8") as fh:
            fh.write("{ not json\n")
        receipts.record(str(tmp_path), _receipt("occ-2"))
        assert len(receipts.read_all(str(tmp_path))) == 2

    def test_an_empty_store_reports_zeroes_rather_than_dividing_by_them(self, tmp_path):
        found = receipts.coverage(str(tmp_path))
        assert found.n_occasions == 0
        assert found.admitted_rate == 0.0
        assert found.used_rate == 0.0
