"""Answer grading retries failed questions instead of dropping them or aborting the run."""
from __future__ import annotations

import pytest

from benchmarks import conversation_bench as bench
from commontrace import llm


def test_a_transient_failure_is_retried_and_every_row_is_graded():
    failures = {"q2": 2}

    def grade(question):
        if failures.get(question, 0):
            failures[question] -= 1
            raise llm.LLMUnavailable("503")
        return {"correct": True, "answer": question}

    rows = [{} for _ in range(4)]
    bench.grade_all([(row, {"question": f"q{n}"}) for n, row in enumerate(rows)], 3, grade=grade, pause_s=0)
    assert [r["answer"] for r in rows] == ["q0", "q1", "q2", "q3"]


def test_a_question_that_never_grades_fails_the_run_rather_than_vanishing():
    def grade(question):
        if question == "bad":
            raise llm.LLMUnavailable("500")
        return {"correct": True}

    rows = [{}, {}]
    with pytest.raises(RuntimeError, match="1 question"):
        bench.grade_all([(rows[0], {"question": "ok"}), (rows[1], {"question": "bad"})], 2, grade=grade,
                        rounds=2, pause_s=0)
    assert rows[0] == {"correct": True} and rows[1] == {}


def test_other_errors_are_not_swallowed():
    def grade(question):
        raise KeyError(question)

    with pytest.raises(KeyError):
        bench.grade_all([({}, {"question": "x"})], 1, grade=grade, pause_s=0)
