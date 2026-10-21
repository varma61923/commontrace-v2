"""Runtime completeness grading shares the benchmark's pure logic."""
from __future__ import annotations

from benchmarks import completeness as bench
from unimem import completeness


def test_the_benchmark_reexports_the_shared_functions_unchanged():
    assert bench.grade_context_completeness is completeness.grade_context_completeness
    assert bench.bucket_counts is completeness.bucket_counts
    assert (bench.COMPLETE, bench.PARTIAL, bench.INSUFFICIENT) == ("COMPLETE", "PARTIAL", "INSUFFICIENT")
    assert bench.PRESENCE_THRESHOLD == 0.5


def test_benchmark_grades_are_pinned():
    graded = bench.grade_context_completeness("Ana adopted a beagle named Biscuit in Lisbon",
                                              {"t1": "adopted a beagle", "t2": "moved to Porto in March"})
    assert graded == {"bucket": "PARTIAL", "score": 0.5, "present": ["t1"], "missing": ["t2"]}
    assert bench.grade_context_completeness("", None, answer="") == {"bucket": None, "score": None,
                                                                    "present": [], "missing": []}
    assert bench.grade_context_completeness("biscuit", None, answer="Biscuit the beagle")["missing"] == ["beagle"]
    assert bench.bucket_counts(["COMPLETE", "PARTIAL", None, "COMPLETE"]) == {
        "complete": 0.6667, "partial": 0.3333, "insufficient": 0.0}


def test_runtime_grade_uses_the_questions_own_terms():
    assert completeness.question_terms("Where does Ana keep her bike helmet?") == ["keep", "bike", "helmet"]
    full = completeness.grade_question("Where does Ana keep her bike helmet?",
                                       "Ana said she will keep the bike helmet in the hall.")
    assert full == {"bucket": "COMPLETE", "score": 1.0, "missing": [], "basis": "question-terms"}
    part = completeness.grade_question("Where does Ana keep her bike helmet?", "The bike is in the shed.")
    assert part["bucket"] == "PARTIAL" and part["missing"] == ["keep", "helmet"] and part["score"] == 0.3333
    assert completeness.grade_question("bike helmet", "")["bucket"] == "INSUFFICIENT"
    assert completeness.grade_question("what is it?", "anything")["bucket"] is None


def test_inflections_of_a_question_term_count_as_present():
    from unimem import completeness

    context = "The billing database failed over to the replica and recovered in nine minutes."
    graded = completeness.grade_question("what should I do when the billing database fails over?", context)
    assert graded["missing"] == [] and graded["bucket"] == completeness.COMPLETE
    # A term that really is absent is still reported.
    graded = completeness.grade_question("what should I do when the billing database fails over?", "billing database")
    assert graded["bucket"] == completeness.PARTIAL and "fails" in graded["missing"]


def test_the_benchmark_grader_keeps_exact_token_matching():
    from unimem import completeness

    exact = completeness.grade_context_completeness("failed", {"e": "fails"})
    assert exact["bucket"] == completeness.INSUFFICIENT  # no stemming unless a caller opts in
    from unimem._stem import stem

    folded = completeness.grade_context_completeness("failed", {"e": "fails"}, normalize=stem)
    assert folded["bucket"] == completeness.COMPLETE  # the opt-in used by the runtime grade
