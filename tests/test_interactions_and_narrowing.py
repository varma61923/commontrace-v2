"""Factorial interaction readings and evidence-drafted narrowing of CROSSING lessons."""
import os
import random

from commontrace import experiment, frontmatter, heterogeneity, interactions, lesson_io, paths, templates


def _world(n=4000, interaction=-0.4, seed=7):
    rng = random.Random(seed)
    obs = []
    for i in range(n):
        a, b = rng.random() < 0.5, rng.random() < 0.5
        p = 0.4 + 0.2 * a + 0.2 * b + interaction * (a and b)
        ok = rng.random() < p
        occasion = f"o{i}"
        obs += [experiment.HoldoutObservation("lesson-a", occasion, a, ok),
                experiment.HoldoutObservation("lesson-b", occasion, b, ok)]
        if i % 3 == 0:  # a third lesson that rides along on some occasions with no effect
            obs.append(experiment.HoldoutObservation("lesson-c", occasion, rng.random() < 0.5, ok))
    return obs


def test_interference_is_detected_and_ranked_first():
    results = interactions.analyze(_world())
    top = results[0]
    assert (top.lesson_a, top.lesson_b, top.flag) == ("lesson-a", "lesson-b", interactions.FLAG_INTERFERENCE)
    assert -0.5 < top.interaction < -0.3 and top.ci_high < 0
    assert 0.15 < top.effect_a_without_b < 0.25 and top.effect_a_with_b < -0.1
    assert "INTERFERENCE" in interactions.render(results)


def test_additive_pairs_are_not_flagged():
    results = interactions.analyze(_world(interaction=0.0))
    assert all(p.flag == interactions.FLAG_ADDITIVE for p in results)


def test_sparse_cells_are_skipped():
    assert interactions.analyze(_world(n=30), min_cell=20) == []


def _crossing(slug):
    g = heterogeneity.SubgroupEffect
    helps = g("coder", 300, 300, 0.7, 0.5, 0.2, 0.12, 0.28, 0.0001, True)
    hurts = g("support", 300, 300, 0.4, 0.55, -0.15, -0.23, -0.07, 0.0002, True)
    flat = g("ops", 40, 40, 0.5, 0.5, 0.0, -0.2, 0.2, 0.9, False)
    return heterogeneity.LessonHeterogeneity(slug, heterogeneity.FLAG_CROSSING, 31.2, 2, 1e-7, [helps, hurts, flat])


def test_crossing_lesson_gets_a_narrowed_review_draft(tmp_path):
    root = str(tmp_path)
    os.makedirs(paths.lessons_dir(root))
    fm = templates.lesson_frontmatter(
        slug="retry", description="Retry flaky calls", agent_type="coder", domain="ops", tags=["retry"],
        applies_when="a call fails transiently.", do_not_apply_when="the call mutates state.", importance=3,
        importance_rationale="saves reruns", source_traces=[], status="active")
    lesson_io.write_lesson(os.path.join(paths.lessons_dir(root), "lesson_retry.md"), fm, "## Rule\nRetry.\n",
                           root=root, actor="t", reason="t")
    path = heterogeneity.draft_narrowing(root, _crossing("retry"), "agent_type")
    draft, body = frontmatter.read(path)
    assert draft["status"] == "review" and draft["revises"] == "retry"
    assert draft["narrowed_to"] == {"by": "agent_type", "include": ["coder"], "exclude": ["support"]}
    assert "Only when agent_type is one of: coder" in draft["applies_when"]
    assert "Not when agent_type is one of: support" in draft["do_not_apply_when"]
    assert "hurts: support" in body and "other: ops" in body
    original, _ = frontmatter.read(os.path.join(paths.lessons_dir(root), "lesson_retry.md"))
    assert original["status"] == "active"  # nothing changes until a human approves
    assert heterogeneity.draft_narrowing(root, _crossing("retry"), "agent_type") is None  # one draft at a time
    consistent = _crossing("retry")
    consistent.flag = heterogeneity.FLAG_CONSISTENT
    assert heterogeneity.draft_narrowing(root, consistent, "agent_type") is None
