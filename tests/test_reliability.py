"""Tests for commontrace/reliability.py.

The behavior worth protecting here is not "it produces numbers" but that it
produces the *right diagnosis*: a rule that is wrong and a rule that merely
fires too often need opposite remedies, and conflating them sends people to
rewrite correct rules.
"""
import pytest

from commontrace import reliability as rel
from commontrace.reliability import Evidence


class TestWilsonLowerBound:
    def test_more_evidence_at_the_same_rate_raises_confidence(self):
        assert rel.wilson_lower_bound(9, 10) < rel.wilson_lower_bound(90, 100)

    def test_a_single_lucky_hit_does_not_outrank_a_long_track_record(self):
        """The whole reason the raw ratio is not used: 1/1 is 100% and 45/50
        is 90%, but the second is obviously the more trustworthy lesson."""
        assert rel.wilson_lower_bound(45, 50) > rel.wilson_lower_bound(1, 1)

    def test_zero_successes_never_claims_a_positive_rate(self):
        assert rel.wilson_lower_bound(0, 10) == 0.0

    def test_no_trials_is_zero_not_a_division_error(self):
        assert rel.wilson_lower_bound(0, 0) == 0.0

    def test_bound_never_exceeds_the_point_estimate(self):
        for k, n in ((1, 1), (5, 10), (99, 100), (3, 7)):
            assert rel.wilson_lower_bound(k, n) <= k / n


class TestVerdicts:
    def test_reliable_when_it_helps_and_there_is_enough_evidence(self):
        ev = [Evidence(f"e{i}", ["good"], ["good"], True) for i in range(10)]
        r = rel.score_lessons(ev)[0]
        assert r.verdict == rel.VERDICT_RELIABLE

    def test_unproven_below_the_evidence_floor_even_if_perfect(self):
        """Two perfect hits is not evidence. Refusing to call this reliable
        is the point -- a corpus that calls everything reliable on first
        contact is worse than no scoring at all."""
        ev = [Evidence(f"e{i}", ["new"], ["new"], True) for i in range(2)]
        r = rel.score_lessons(ev, min_evidence=5)[0]
        assert r.verdict == rel.VERDICT_UNPROVEN
        assert "noise" in r.rationale

    def test_miscalibrated_when_it_fires_constantly_but_rarely_helps(self):
        """The activation condition is too broad. The rule may be perfectly
        correct -- which is why this is not HARMFUL."""
        ev = [Evidence(f"e{i}", ["broad"], [], True) for i in range(12)]
        r = rel.score_lessons(ev)[0]
        assert r.verdict == rel.VERDICT_MISCALIBRATED
        assert "activation-condition" in r.rationale

    def test_harmful_when_tasks_go_worse_with_it_injected(self):
        # baseline succeeds often; this lesson's occasions mostly fail
        ev = [Evidence(f"ok{i}", ["other"], ["other"], True) for i in range(12)]
        ev += [Evidence(f"bad{i}", ["bad"], ["bad"], False) for i in range(8)]
        by = {r.slug: r for r in rel.score_lessons(ev)}
        assert by["bad"].verdict == rel.VERDICT_HARMFUL
        assert by["bad"].lift is not None and by["bad"].lift < 0

    def test_harmful_and_miscalibrated_are_distinguished(self):
        """The central diagnostic claim. A high-hit-rate lesson whose tasks
        still fail is a *wrong rule*; a low-hit-rate lesson whose tasks
        succeed is a *broad trigger*. They must not collapse together."""
        ev = [Evidence(f"base{i}", ["ok"], ["ok"], True) for i in range(12)]
        ev += [Evidence(f"h{i}", ["wrong_rule"], ["wrong_rule"], False) for i in range(8)]
        ev += [Evidence(f"m{i}", ["broad_trigger"], [], True) for i in range(12)]
        by = {r.slug: r for r in rel.score_lessons(ev)}
        assert by["wrong_rule"].verdict == rel.VERDICT_HARMFUL
        assert by["broad_trigger"].verdict == rel.VERDICT_MISCALIBRATED

    def test_a_hit_without_a_retrieval_is_not_counted(self):
        """Otherwise a retro pass that credits a lesson it never injected
        yields precision above 1.0."""
        ev = [Evidence("e1", ["a"], ["a", "never_retrieved"], True)]
        slugs = {r.slug for r in rel.score_lessons(ev)}
        assert "never_retrieved" not in slugs

    def test_precision_can_never_exceed_one(self):
        ev = [Evidence(f"e{i}", ["a"], ["a", "a"], True) for i in range(8)]
        assert rel.score_lessons(ev)[0].precision <= 1.0

    def test_unknown_outcomes_are_excluded_from_lift_not_treated_as_failure(self):
        ev = [Evidence(f"e{i}", ["a"], ["a"], None) for i in range(8)]
        r = rel.score_lessons(ev)[0]
        assert r.lift is None and r.success_rate is None

    def test_worst_offenders_are_listed_first(self):
        ev = [Evidence(f"g{i}", ["good"], ["good"], True) for i in range(10)]
        ev += [Evidence(f"b{i}", ["broad"], [], True) for i in range(12)]
        assert rel.score_lessons(ev)[0].slug == "broad"


class TestPolarity:
    def test_prescriptive_and_prohibitive_have_opposite_signs(self):
        assert rel.polarity("Always retry the request") > 0
        assert rel.polarity("Never retry the request") < 0

    def test_neutral_text_is_zero(self):
        assert rel.polarity("the deployment pipeline runs nightly") == 0.0

    def test_empty_text_does_not_raise(self):
        assert rel.polarity("") == 0.0


def _lesson(slug, desc, applies, tags=None, status="active"):
    return {
        "name": slug, "description": desc, "applies_when": applies,
        "tags": tags or [], "domain": "other", "status": status,
    }


class TestContradictions:
    def test_detects_opposite_rules_on_the_same_trigger(self):
        lessons = [
            _lesson("always", "Always retry a failed webhook delivery",
                    "a webhook delivery to the payments provider fails transiently", ["webhooks"]),
            _lesson("never", "Never retry a failed webhook delivery",
                    "a webhook delivery to the payments provider fails transiently", ["webhooks"]),
        ]
        found = rel.find_contradictions(lessons)
        assert len(found) == 1
        assert {found[0].slug_a, found[0].slug_b} == {"always", "never"}

    def test_opposite_rules_on_unrelated_triggers_are_not_flagged(self):
        """Two lessons that never fire in the same situation cannot conflict
        in practice, however opposed their wording."""
        lessons = [
            _lesson("a", "Always retry the request", "a webhook to the payment provider fails", ["webhooks"]),
            _lesson("b", "Never retry the request", "a cuda kernel returns nondeterministic tensors", ["cuda"]),
        ]
        assert rel.find_contradictions(lessons) == []

    def test_archived_lessons_are_ignored(self):
        """Only lessons that can actually be injected together matter."""
        lessons = [
            _lesson("a", "Always retry the webhook", "a webhook delivery fails transiently", ["webhooks"]),
            _lesson("b", "Never retry the webhook", "a webhook delivery fails transiently",
                    ["webhooks"], status="archived"),
        ]
        assert rel.find_contradictions(lessons) == []

    def test_agreeing_lessons_on_the_same_trigger_are_not_flagged(self):
        lessons = [
            _lesson("a", "Always retry the webhook delivery", "a webhook delivery fails transiently", ["webhooks"]),
            _lesson("b", "Always use exponential backoff", "a webhook delivery fails transiently", ["webhooks"]),
        ]
        assert rel.find_contradictions(lessons) == []

    def test_opposite_measured_effect_is_a_high_severity_signal(self):
        """Empirical divergence outranks the lexical heuristic: it cannot be
        fooled by phrasing."""
        lessons = [
            _lesson("a", "Handle the webhook this way", "a webhook delivery fails transiently", ["webhooks"]),
            _lesson("b", "Handle the webhook that way", "a webhook delivery fails transiently", ["webhooks"]),
        ]
        scores = [
            rel.LessonReliability("a", 10, 8, 0.8, 0.5, 0.8, 0.30, rel.VERDICT_RELIABLE, ""),
            rel.LessonReliability("b", 10, 2, 0.2, 0.1, 0.2, -0.30, rel.VERDICT_HARMFUL, ""),
        ]
        found = rel.find_contradictions(lessons, reliability=scores)
        assert len(found) == 1
        assert found[0].severity == "high"
        assert any("measured effect" in s for s in found[0].signals)


class TestRender:
    def test_renders_without_data(self):
        assert "Lesson Reliability Report" in rel.render([], [], 5)

    def test_flags_actionable_lessons_in_the_report(self):
        ev = [Evidence(f"e{i}", ["broad"], [], True) for i in range(12)]
        out = rel.render(rel.score_lessons(ev), [], 5)
        assert "Needs attention" in out and "MISCALIBRATED" in out

    def test_states_its_own_limitations(self):
        """A report that drives decisions must carry its caveats with it."""
        out = rel.render([], [], 5)
        assert "lexical" in out and "Wilson" in out

    def test_says_out_loud_that_lift_is_correlational(self):
        """The report puts `lift` in a table next to a HARMFUL verdict. Without
        this caveat a reader takes it as a causal claim, and it is not one --
        the lesson fired *because* the situation matched it. The pointer to
        the experiment command has to travel with the number."""
        out = rel.render([], [], 5)
        assert "correlational, not causal" in out
        assert "commontrace experiment" in out


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestReliabilityCLI:
    def test_no_evidence_explains_what_is_missing_rather_than_failing(self, store, capsys):
        from commontrace.cli import main

        main(["init", "--agent-type", "code", "--dest", str(store)])
        capsys.readouterr()
        assert main(["reliability", "--dest", str(store)]) == 0
        assert "no retrieval evidence" in capsys.readouterr().err

    def test_scores_lessons_from_episode_evidence(self, store, capsys):
        import yaml

        from commontrace.cli import main

        main(["init", "--agent-type", "code", "--dest", str(store)])
        eps = store / "memory" / "episodes"
        eps.mkdir(parents=True, exist_ok=True)
        for i in range(10):
            fm = {
                "name": f"ep{i}", "verdict": "CONFORM",
                "lessons_retrieved_by_alpha": ["lesson_x"], "lessons_hit": ["lesson_x"],
            }
            (eps / f"2026-01-{i + 1:02d}_ep{i}.md").write_text(
                "---\n" + yaml.safe_dump(fm) + "---\n\nbody\n", encoding="utf-8"
            )
        capsys.readouterr()
        assert main(["reliability", "--dest", str(store), "--json"]) == 0
        import json

        data = json.loads(capsys.readouterr().out)
        assert data["lessons"][0]["slug"] == "lesson_x"
        assert data["lessons"][0]["verdict"] == rel.VERDICT_RELIABLE

    def test_strict_exits_nonzero_on_a_harmful_lesson(self, store, capsys):
        import yaml

        from commontrace.cli import main

        main(["init", "--agent-type", "code", "--dest", str(store)])
        eps = store / "memory" / "episodes"
        eps.mkdir(parents=True, exist_ok=True)
        for i in range(12):
            fm = {"name": f"ok{i}", "verdict": "CONFORM",
                  "lessons_retrieved_by_alpha": ["fine"], "lessons_hit": ["fine"]}
            (eps / f"2026-01-{i + 1:02d}_ok{i}.md").write_text(
                "---\n" + yaml.safe_dump(fm) + "---\n\nbody\n", encoding="utf-8")
        for i in range(8):
            fm = {"name": f"bad{i}", "verdict": "ABANDON",
                  "lessons_retrieved_by_alpha": ["bad"], "lessons_hit": ["bad"]}
            (eps / f"2026-02-{i + 1:02d}_bad{i}.md").write_text(
                "---\n" + yaml.safe_dump(fm) + "---\n\nbody\n", encoding="utf-8")
        capsys.readouterr()
        assert main(["reliability", "--dest", str(store), "--strict"]) == 1
        assert main(["reliability", "--dest", str(store)]) == 0


class TestRankingAdjustments:
    """rel.ranking_adjustments -- the one place a verdict becomes a number
    commontrace/retrieval.py's optional reliability_weight can use."""

    def test_harmful_gets_the_largest_penalty(self):
        scores = [rel.LessonReliability(
            slug="x", n_retrieved=10, n_hit=2, precision=0.2, precision_lower=0.05,
            success_rate=0.3, lift=-0.4, verdict=rel.VERDICT_HARMFUL, rationale="",
        )]
        assert rel.ranking_adjustments(scores) == {"x": -1.0}

    def test_miscalibrated_penalty_is_real_but_smaller_than_harmful(self):
        """A rule that fires too often still helps sometimes -- it must not
        rank behind one that measurably makes tasks worse, by the same
        amount (see the module docstring on why these are two verdicts)."""
        scores = [rel.LessonReliability(
            slug="x", n_retrieved=10, n_hit=2, precision=0.2, precision_lower=0.05,
            success_rate=None, lift=None, verdict=rel.VERDICT_MISCALIBRATED, rationale="",
        )]
        adj = rel.ranking_adjustments(scores)["x"]
        assert -1.0 < adj < 0.0

    def test_unproven_is_neutral(self):
        scores = [rel.LessonReliability(
            slug="x", n_retrieved=2, n_hit=2, precision=1.0, precision_lower=0.2,
            success_rate=None, lift=None, verdict=rel.VERDICT_UNPROVEN, rationale="",
        )]
        assert rel.ranking_adjustments(scores) == {"x": 0.0}

    def test_reliable_gets_the_largest_boost(self):
        scores = [rel.LessonReliability(
            slug="x", n_retrieved=10, n_hit=9, precision=0.9, precision_lower=0.6,
            success_rate=0.9, lift=0.1, verdict=rel.VERDICT_RELIABLE, rationale="",
        )]
        assert rel.ranking_adjustments(scores) == {"x": 1.0}

    def test_a_slug_with_no_verdict_is_simply_absent(self):
        """No evidence is not evidence of harm -- retrieval.rank_lessons
        reads a missing slug as 0.0, the same as UNPROVEN, and this
        function must not manufacture an entry to say so."""
        assert rel.ranking_adjustments([]) == {}
