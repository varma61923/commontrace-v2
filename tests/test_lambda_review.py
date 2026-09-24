"""Lambda's per-proposal verdicts, persisted in each episode (roadmap P1,
benchmark/STATUS.md) and reported by `commontrace bench`.

`lesson_quality` sees only what was applied, so it could not tell a
proposal Lambda rejected from one it sent back for refinement -- two
different failures of Omega with two different fixes.
"""
from __future__ import annotations

import os

import measure_performance as bm

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_verdicts_are_counted_across_episodes():
    review = bm.compute_lambda_review([
        {"lambda_decisions": {"a": "ACCEPTED", "b": "REJECTED"}},
        {"lambda_decisions": {"c": "needs refinement", "d": "Accepted"}},
    ])
    assert review["n_episodes"] == 2 and review["n_proposals"] == 4
    assert review["counts"] == {"ACCEPTED": 2, "REJECTED": 1, "NEEDS_REFINEMENT": 1, "OTHER": 0}
    assert review["acceptance_rate"] == 0.5
    assert review["rejection_rate"] == review["refinement_rate"] == 0.25


def test_episodes_that_predate_the_field_are_skipped_not_counted_as_empty():
    review = bm.compute_lambda_review([
        {"lessons_validated_by_lambda": ["a"]},
        {"lambda_decisions": {}},
        {"lambda_decisions": {"x": "REJECTED"}},
    ])
    assert review["n_episodes"] == 1 and review["acceptance_rate"] == 0.0
    assert bm.compute_lambda_review([{"lessons_validated_by_lambda": ["a"]}]) is None


def test_an_unrecognised_verdict_is_visible_not_dropped():
    review = bm.compute_lambda_review([{"lambda_decisions": {"a": "ACCEPTED", "b": "MAYBE"}}])
    assert review["counts"]["OTHER"] == 1 and review["acceptance_rate"] == 0.5
    md = bm.render_markdown({**_minimal_report(), "lambda_review": review})
    assert "50.0% accepted" in md.replace("**", "") and "unrecognised verdict" in md


def test_the_field_survives_the_fallback_parser():
    """Stores without PyYAML parse frontmatter with parse_yaml_minimal."""
    text = "lambda_decisions: {lesson_a: ACCEPTED, lesson_b: NEEDS_REFINEMENT}\n"
    assert bm.parse_yaml_minimal(text)["lambda_decisions"] == {
        "lesson_a": "ACCEPTED", "lesson_b": "NEEDS_REFINEMENT"}
    block = "lambda_decisions:\n  lesson_a: ACCEPTED\n  lesson_b: REJECTED\n"
    assert bm.parse_yaml_minimal(block)["lambda_decisions"] == {
        "lesson_a": "ACCEPTED", "lesson_b": "REJECTED"}


def test_the_templates_ask_for_it():
    for path in ("SKILL.md", os.path.join("memory", "episodes", "episode_template.md")):
        with open(os.path.join(REPO, path), encoding="utf-8") as fh:
            assert "lambda_decisions:" in fh.read(), path


def _minimal_report():
    episodes = [{"name": "e", "lessons_proposed_by_omega": ["a", "b"],
                 "lessons_validated_by_lambda": ["a"]}]
    return {
        "schema_version": bm.SCHEMA_VERSION, "timestamp": "2026-01-01T00:00:00",
        "n_episodes": 1, "n_lessons": 0,
        "lesson_quality": {"value": 0.5, "n": 1},
        "implicit_retrieval": {"strict": None, "permissive": None, "n": 0},
        "transfer_gap": {"value": None, "n": 0, "untraceable": 0},
        "episodes": episodes, "extras": bm.compute_extras(episodes, {}),
        "operational_cost": bm.compute_operational_cost(),
        "semantic_duplicates": bm.compute_semantic_duplicates(),
        "freshness": {"value": None, "n": 0, "window_days": bm.FRESHNESS_WINDOW_DAYS},
        "skipped_unreadable_files": {"episodes": [], "lessons": []},
    }
