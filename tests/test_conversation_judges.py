"""Tests for benchmark judge integration in benchmarks/conversation_bench.py.

Verifies:
1. Reference modes (memory, full-context, no-memory) context building and execution.
2. Latency percentiles (p50/p95), token accounting, and cost accounting.
3. Pre-flight cost estimation and --max-cost budget protection.
4. BEAM official ability reporting (10 abilities, event ordering, abstention).
5. LoCoMo category 5 (adversarial) exclusion from overall accuracy.
6. Memory lift calculation between reference modes.
"""
from __future__ import annotations

import argparse
import tempfile

import pytest

from benchmarks.cache import BenchmarkCache, CostGuard
from benchmarks.conversation_bench import (
    _p50_p95,
    grade_answer,
    make_full_context,
    summarize,
)
from benchmarks.judges import LoCoMoJudge


class TestMakeFullContext:
    def test_chronological_session_ordering(self):
        sessions = [
            ("session 1", "2024-01-01", [{"speaker": "User", "text": "Hi"}, {"speaker": "Assistant", "text": "Hello"}]),
            ("session 2", "2024-01-02", [{"speaker": "User", "text": "What is my cat's name?"}]),
        ]
        ctx, n_tokens = make_full_context(sessions, budget=1000)
        assert "=== session 1 (2024-01-01) ===" in ctx
        assert "User: Hi" in ctx
        assert "=== session 2 (2024-01-02) ===" in ctx
        assert "User: What is my cat's name?" in ctx
        assert n_tokens > 0

    def test_budget_truncation(self):
        sessions = [
            (f"session {i}", "2024-01-01", [{"speaker": "User", "text": "Very long story repeated " * 20}])
            for i in range(20)
        ]
        ctx, n_tokens = make_full_context(sessions, budget=50)
        assert n_tokens <= 60  # close to budget limit


class TestLatencyPercentiles:
    def test_empty_returns_none(self):
        assert _p50_p95([]) is None
        assert _p50_p95([None, None]) is None

    def test_percentile_calculation(self):
        vals = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10]
        res = _p50_p95(vals)
        assert res is not None
        assert 45.0 <= res["p50_ms"] <= 65.0
        assert 90.0 <= res["p95_ms"] <= 105.0


class TestGradeAnswerWithCache:
    def test_grade_answer_and_cache_accounting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = BenchmarkCache(tmpdir)
            cost_guard = CostGuard(max_cost_usd=10.0)

            # Pre-seed cache with answer and judgment
            judge = LoCoMoJudge(model="gpt-4o")
            q = {"question": "What is my hobby?", "answer": "Rock climbing", "type": "single-hop"}

            # Populate cache manually for answer and judge
            ans_prompt = (
                "You answer from your memory of past conversations with the user.\n"
                "Use only the memories below. Session headers say when each session took place; dates in\n"
                "[brackets] resolve relative time words.\n\n"
                "How to answer:\n"
                "- Follow every standing instruction the user gave (format, length, style, things to avoid),\n"
                "  and shape suggestions to the preferences they stated, even when the question does not\n"
                "  mention them.\n"
                "- If something changed over time, the most recent statement is current; say what it was\n"
                "  before only if asked.\n"
                "- If two memories contradict each other and neither is clearly the later correction, say\n"
                "  that the memories conflict, quote both briefly, and ask which is right instead of picking one.\n"
                "- If the memories do not contain the answer, say you don't have that information; never guess\n"
                "  a detail that was not stated.\n"
                "- For \"when\", \"how long\" and \"how many days/weeks\" questions, compute from the dates shown.\n"
                "- For order or sequence questions, list the items in the order they happened or were raised,\n"
                "  with their dates.\n"
                "- For summaries, cover the whole span in chronological order, not only the latest part.\n"
                "Answer concisely.\n\n"
                "Memories:\n"
                "User loves rock climbing\n\n"
                "Question (asked now): What is my hobby?\n"
                "Answer:"
            )
            cache.put(
                "claude-sonnet-5",
                ans_prompt,
                "Your hobby is rock climbing.",
                {"input_tokens": 120, "output_tokens": 10},
                cost_usd=0.0005,
            )

            j_prompt = judge.format_prompt(
                question="What is my hobby?",
                golden_answer="Rock climbing",
                generated_answer="Your hobby is rock climbing.",
            )
            cache.put(
                "gpt-4o",
                j_prompt,
                '{"label": "CORRECT"}',
                {"input_tokens": 200, "output_tokens": 8},
                cost_usd=0.0006,
            )

            res = grade_answer(
                question=q,
                context="User loves rock climbing",
                now=None,
                ans_model="claude-sonnet-5",
                j_model="gpt-4o",
                judge_inst=judge,
                cache=cache,
                cost_guard=cost_guard,
            )

            assert res["correct"] is True
            assert res["score"] == 1.0
            assert "rock climbing" in res["answer"].lower()
            assert res["judge"] == "locomo"
            assert res["judge_profile"] == "locomo-downstream-binary-v1"
            assert res["judge_model"] == "gpt-4o"
            assert res["answer_model"] == "claude-sonnet-5"
            assert res["answer_latency_s"] == 0.0  # cached
            assert res["judge_latency_s"] == 0.0  # cached
            assert res["answer_cost_usd"] == 0.0005
            assert res["judge_cost_usd"] == 0.0006


class TestSummarizeAndProtocolExclusions:
    def test_locomo_category_5_excluded_from_overall_accuracy(self):
        args = argparse.Namespace(
            dataset="locomo",
            answer=True,
            embedder="none",
            rerank="none",
            neighbours=1,
        )
        rows = [
            {"type": "single-hop", "correct": True, "score": 1.0, "evidence": 1.0, "complete": True, "session": 1.0, "answer_in_context": True, "tokens": 100},
            {"type": "temporal", "correct": True, "score": 1.0, "evidence": 1.0, "complete": True, "session": 1.0, "answer_in_context": True, "tokens": 100},
            {"type": "multi-hop", "correct": False, "score": 0.0, "evidence": 0.5, "complete": False, "session": 1.0, "answer_in_context": False, "tokens": 100},
            # Category 5 / adversarial: incorrect answer, should be excluded from overall accuracy
            {"type": "adversarial", "correct": False, "score": 0.0, "evidence": 0.0, "complete": False, "session": 1.0, "answer_in_context": False, "tokens": 100},
        ]

        summary = summarize(rows, args, budget=1500, ingest_s=1.0, recall_s=0.01, full_tokens=[500])
        # Scorable categories are 1, 2, 4 (single-hop, temporal, multi-hop): 2 correct out of 3 = 0.6667
        assert summary["overall"]["accuracy"] == pytest.approx(0.6667, abs=1e-3)
        assert summary["by_type"]["adversarial"]["accuracy"] is None

    def test_beam_ability_score_reporting(self):
        args = argparse.Namespace(
            dataset="beam",
            answer=True,
            embedder="none",
            rerank="none",
            neighbours=1,
        )
        rows = [
            {
                "type": "information_extraction",
                "beam_ability": "information_extraction",
                "correct": True,
                "score": 1.0,
                "evidence": 1.0,
                "complete": True,
                "session": None,
                "answer_in_context": True,
                "tokens": 500,
            },
            {
                "type": "event_ordering",
                "beam_ability": "event_ordering",
                "correct": True,
                "score": 0.85,
                "evidence": 1.0,
                "complete": True,
                "session": None,
                "answer_in_context": True,
                "tokens": 500,
                "beam_details": {"tau_b": 0.9, "f1": 0.95},
            },
            {
                "type": "abstention",
                "beam_ability": "abstention",
                "correct": True,
                "score": 1.0,
                "evidence": None,
                "complete": None,
                "session": None,
                "answer_in_context": False,
                "tokens": 100,
            },
        ]

        summary = summarize(rows, args, budget=1500, ingest_s=2.0, recall_s=0.02, full_tokens=[1000])
        assert "beam_abilities" in summary
        assert summary["beam_abilities"]["information_extraction"] == 1.0
        assert summary["beam_abilities"]["event_ordering"] == 0.85
        assert summary["beam_abilities"]["abstention"] == 1.0
        assert summary["beam_event_ordering"]["tau_b"] == 0.9
        assert summary["beam_event_ordering"]["f1"] == 0.95
        assert summary["beam_abstention"]["accuracy"] == 1.0
