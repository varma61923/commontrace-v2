"""The unified sequential rule and the namespaced evidence cache.

Sequential: analyze(..., sequential=True) judges every lesson by ONE rule —
its anytime-valid confidence interval (HELPS above zero, HURTS below, else
the power gate between UNDERPOWERED and NO_MEASURABLE_EFFECT). Fixed-horizon:
analyze(..., fixed_horizon=True), the explicit spelling of the legacy
sequential=False reading, keeps the fixed-sample BH rule for one look at a
finished run.
"""

from __future__ import annotations

import json

import pytest
import yaml

from commontrace import evidence
from commontrace import experiment as ex
from commontrace.cli import main


def _obs(slug, n, injected, success_rate, offset=0):
    n_success = round(n * success_rate)
    return [
        ex.HoldoutObservation(
            lesson_slug=slug,
            occasion_id=f"{slug}-{'i' if injected else 'w'}-{offset + i}",
            injected=injected,
            succeeded=i < n_success,
        )
        for i in range(n)
    ]


def _borderline():
    # Fixed-sample significant (z=2.24, p=0.025) but the anytime interval at
    # this n still spans zero: exactly where the two readings must disagree.
    return _obs("l", 60, True, 0.70) + _obs("l", 60, False, 0.50)


class TestUnifiedSequentialRule:
    def test_a_clear_help_reports_the_anytime_interval_that_drove_it(self):
        (effect,) = ex.analyze(_obs("l", 100, True, 0.90) + _obs("l", 100, False, 0.50), sequential=True)
        assert effect.verdict == ex.VERDICT_HELPS
        assert effect.significant and effect.ci_low > 0

        baseline = (90 + 50) / (100 + 100)
        target = ex.required_n_per_arm(ex.SEQUENTIAL_TARGET_EFFECT_MULTIPLE * 0.10, baseline)
        lo, hi = ex.anytime_confidence_interval(90, 100, 50, 100, target_n_per_arm=target)
        assert (effect.ci_low, effect.ci_high) == (round(lo, 4), round(hi, 4))

    def test_anytime_interval_remains_bounded_and_tunes_with_target_horizon(self):
        narrow = ex.anytime_confidence_interval(90, 100, 50, 100, target_n_per_arm=100)
        broad = ex.anytime_confidence_interval(90, 100, 50, 100, target_n_per_arm=4)
        assert -1.0 <= narrow[0] <= narrow[1] <= 1.0
        assert -1.0 <= broad[0] <= broad[1] <= 1.0
        assert narrow != broad

    def test_negative_arm_sizes_are_rejected(self):
        with pytest.raises(ValueError, match="must not be negative"):
            ex.two_proportion_test(0, -1, 0, 1)

    def test_a_clear_hurt_is_hurts(self):
        (effect,) = ex.analyze(_obs("l", 100, True, 0.40) + _obs("l", 100, False, 0.85), sequential=True)
        assert effect.verdict == ex.VERDICT_HURTS
        assert effect.significant and effect.ci_high < 0

    def test_a_fixed_significant_but_watched_result_is_not_yet(self):
        assert ex.analyze(_borderline(), fixed_horizon=True)[0].verdict == ex.VERDICT_HELPS
        sequential = ex.analyze(_borderline(), sequential=True)[0]
        assert sequential.verdict == ex.VERDICT_UNDERPOWERED
        assert "running experiment" in sequential.note

    def test_fixed_horizon_is_the_legacy_fixed_reading(self):
        obs = _borderline()
        legacy = ex.analyze(obs, sequential=False)[0]
        explicit = ex.analyze(obs, fixed_horizon=True)[0]
        assert (explicit.verdict, explicit.p_value, explicit.ci_low, explicit.ci_high) == (
            legacy.verdict, legacy.p_value, legacy.ci_low, legacy.ci_high,
        )

    def test_contradictory_modes_are_rejected(self):
        with pytest.raises(ValueError, match="fixed_horizon=True together with sequential=True"):
            ex.analyze(_borderline(), sequential=True, fixed_horizon=True)

    def test_the_target_multiple_default_is_unchanged(self):
        # The magic 2.0*detectable multiplier is now named; the default keeps
        # the historical behavior it had as a literal.
        assert ex.SEQUENTIAL_TARGET_EFFECT_MULTIPLE == 2.0
        assert ex.analyze(_borderline(), sequential=True)[0].verdict == ex.analyze(
            _borderline(), sequential=True, target_effect_multiple=2.0
        )[0].verdict

    def test_the_target_multiple_retunes_the_boundary(self):
        narrow = ex.analyze(_borderline(), sequential=True, target_effect_multiple=4.0)[0]
        wide = ex.analyze(_borderline(), sequential=True, target_effect_multiple=1.0)[0]
        assert (narrow.ci_low, narrow.ci_high) != (wide.ci_low, wide.ci_high)

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan")])
    def test_a_non_positive_target_multiple_is_rejected(self, bad):
        with pytest.raises(ValueError, match="target_effect_multiple"):
            ex.analyze(_borderline(), sequential=True, target_effect_multiple=bad)

    def test_an_unknown_spending_shape_is_rejected_but_a_known_one_is_ignored(self):
        with pytest.raises(ValueError, match="unknown alpha-spending shape"):
            ex.analyze(_borderline(), sequential=True, spending_shape="made-up")
        ok = ex.analyze(_borderline(), sequential=True, spending_shape=ex.SPEND_POCOCK)[0]
        assert ok.verdict == ex.analyze(_borderline(), sequential=True)[0].verdict

    @pytest.mark.parametrize("kwargs", [{"alpha": 0.0}, {"alpha": 1.5}, {"detectable": 0.0}, {"detectable": 2.0}])
    def test_degenerate_thresholds_are_rejected(self, kwargs):
        with pytest.raises(ValueError):
            ex.analyze(_borderline(), **kwargs)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    main(["init", "--agent-type", "code", "--dest", str(tmp_path)])
    return tmp_path


@pytest.fixture(autouse=True)
def _fresh_cache():
    evidence._cache.clear()
    yield
    evidence._cache.clear()


def _write_episode(store, name, verdict, day):
    (store / "memory" / "episodes" / f"2026-01-{day:02d}_{name}.md").write_text(
        "---\n" + yaml.safe_dump({"name": name, "verdict": verdict}) + "---\n\nbody\n",
        encoding="utf-8",
    )


def _seed_two_lessons(store):
    from commontrace.commands.experiment_cmd import holdout_log_path

    lines = []
    for lesson in ("a", "b"):
        for i in range(30):
            occ = f"{lesson}-{i}"
            lines.append(json.dumps({
                "occasion_id": occ, "lesson": lesson, "injected": i % 2 == 0,
                "rate": 0.5, "salt": "default",
            }))
            _write_episode(store, occ, "CONFORM" if i % 3 else "ABANDON", (i % 28) + 1)
    with open(holdout_log_path(str(store)), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


class TestEvidenceCache:
    def test_ttl_is_a_module_constant(self):
        assert evidence.TTL_SECONDS == 300.0

    def test_a_subset_query_is_namespaced_from_the_full_payload(self, store):
        _seed_two_lessons(store)
        full = evidence.for_lessons(str(store))
        assert set(full["by_lesson"]) == {"a", "b"}

        sub = evidence.for_lessons(str(store), lesson_slugs=["a"])
        assert set(sub["by_lesson"]) == {"a"}

        # The subset query neither poisoned nor reused the full entry.
        again = evidence.for_lessons(str(store))
        assert set(again["by_lesson"]) == {"a", "b"}
        assert again == full

    def test_repeated_queries_hit_the_cache_per_namespace(self, store, monkeypatch):
        _seed_two_lessons(store)
        calls = {"n": 0}
        real = evidence.analyse

        def counted(root):
            calls["n"] += 1
            return real(root)

        monkeypatch.setattr(evidence, "analyse", counted)
        evidence.for_lessons(str(store))
        evidence.for_lessons(str(store))
        assert calls["n"] == 1

        evidence.for_lessons(str(store), lesson_slugs=["a"])
        assert calls["n"] == 2
        evidence.for_lessons(str(store), lesson_slugs=["a"])
        assert calls["n"] == 2
