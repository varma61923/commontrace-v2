"""Function kits: any function is a validated spec, and nothing branches on which."""
import json
import os

import pytest

from commontrace import experiment, functions, holdout_io, paths
from commontrace.cli import main
from commontrace.commands import experiment_cmd

KITS = functions.builtin_kits()


GOOD = {
    "key": "warehouse", "title": "Warehouse picking",
    "occasion": {"label": "pick order", "example": "PO-1"},
    "outcome": {"success": "picked correctly with no re-pick", "window_days": 2,
                "signals": ["from_threshold", "from_no_reversal"], "combine": "all"},
    "planning": {"baseline": 0.9, "effect": 0.03, "holdout_rate": 0.5},
}


def _effects(root):
    rows, _salt, _other = experiment_cmd.scope_to_current_salt(root, experiment_cmd._load(root)[0])
    return {e.lesson_slug: e for e in experiment.analyze(experiment_cmd._observations(rows))}


def test_the_built_in_kits_cover_the_functions_the_product_claims():
    assert set(KITS) == {"support", "sales", "hr", "coding", "marketing", "robotics",
                         "legal", "finance", "clinical"}
    assert KITS["coding"].agent_type == "code"
    assert {k for k, v in KITS.items() if v.regulated} == {"legal", "finance", "clinical"}


@pytest.mark.parametrize("key", sorted(KITS))
def test_every_built_in_kit_is_valid_and_round_trips(key):
    kit = KITS[key]
    assert functions.from_dict(kit.to_dict()) == kit
    assert set(kit.outcome.signals) <= set(functions.detector_names())


@pytest.mark.parametrize("key", sorted(KITS))
def test_demo_data_recovers_help_harm_and_no_effect_for_every_function(tmp_path, key):
    root = str(tmp_path)
    functions.seed_demo(root, KITS[key])
    effects = _effects(root)
    assert effects["demo-helpful-memory"].verdict == experiment.VERDICT_HELPS
    assert effects["demo-harmful-memory"].verdict == experiment.VERDICT_HURTS
    assert effects["demo-neutral-memory"].verdict not in (experiment.VERDICT_HELPS, experiment.VERDICT_HURTS)
    assert functions.is_demo_store(root)


def test_demo_data_is_reproducible(tmp_path):
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    for root in (a, b):
        functions.seed_demo(root, KITS["sales"], seed=3, occasions=300)
    assert open(holdout_io.holdout_log_path(a)).read().count("\n") == open(holdout_io.holdout_log_path(b)).read().count("\n")
    assert {s: (e.n_injected, e.rate_injected) for s, e in _effects(a).items()} == \
           {s: (e.n_injected, e.rate_injected) for s, e in _effects(b).items()}


def test_demo_data_never_goes_into_a_store_holding_real_data(tmp_path):
    root = str(tmp_path)
    config = holdout_io.configure(root, rate=0.5)
    holdout_io.assign_and_log(root, ["real"], occasion_id="T-1", rate=0.5, salt=config.salt)
    with pytest.raises(functions.KitError, match="real holdout data"):
        functions.seed_demo(root, KITS["support"], occasions=30)


def test_a_new_function_is_just_a_file(tmp_path):
    path = tmp_path / "kit.json"
    path.write_text(json.dumps(GOOD))
    assert main(["function", "check", str(path)]) == 0
    root = str(tmp_path / "store")
    assert main(["init", "--kit", str(path), "--dest", root]) == 0
    assert paths.store_agent_type(root) == "warehouse"
    assert functions.store_kit(root).occasion_label == "pick order"
    assert main(["function", "forecast", "--daily", "500", "--dest", root]) == 0
    assert main(["function", "demo", "--dest", root]) == 0
    assert _effects(root)["demo-helpful-memory"].verdict == experiment.VERDICT_HELPS


@pytest.mark.parametrize("change, message", [
    (lambda s: s.update(key="Bad Key"), "key must be a lowercase slug"),
    (lambda s: s.update(surprise=1), "unknown field 'surprise'"),
    (lambda s: s["outcome"].update(signals=["from_vibes"]), "not a detector"),
    (lambda s: s["outcome"].update(combine="single"), "exactly one signal"),
    (lambda s: s["outcome"].update(window_days=-1), "outcome.window_days"),
    (lambda s: s["planning"].update(baseline=1.0), "planning.baseline"),
    (lambda s: s["planning"].update(effect=0), "planning.effect"),
    (lambda s: s["planning"].update(baseline=True), "planning.baseline"),
    (lambda s: s["occasion"].pop("example"), "occasion.example"),
    (lambda s: s.pop("planning"), "planning is required"),
    (lambda s: s.update(domains=["Not A Slug"]), "domains"),
])
def test_a_bad_kit_is_refused_with_the_reason(change, message):
    spec = json.loads(json.dumps(GOOD))
    change(spec)
    with pytest.raises(functions.KitError, match=message):
        functions.from_dict(spec)


def test_every_problem_is_reported_at_once():
    with pytest.raises(functions.KitError) as exc:
        functions.from_dict({"key": "x", "bogus": 1})
    assert str(exc.value).count(";") >= 3


def test_forecast_arithmetic_matches_the_planner():
    f = functions.forecast(KITS["support"], 40)
    design = experiment.plan(effect=0.05, baseline=0.70, rate=0.5)
    assert f.design.occasions_needed == design.occasions_needed == 2638
    assert f.days_to_collect == 66 and f.days_to_verdict == 66 + 7
    assert f.assumed_baseline
    assert not functions.forecast(KITS["support"], 40, baseline=0.8).assumed_baseline


def test_forecast_says_when_no_volume_can_meet_the_horizon():
    f = functions.forecast(KITS["finance"], 1000, within_days=30)  # the outcome window IS 30 days
    assert f.daily_needed is None
    assert "No volume delivers" in functions.render_forecast(f)
    assert functions.forecast(KITS["support"], 40, within_days=30).daily_needed == 115


def test_a_low_volume_forecast_says_it_is_too_slow():
    assert "too low to answer in a quarter" in functions.render_forecast(functions.forecast(KITS["sales"], 5))


@pytest.mark.parametrize("bad", [0, -3, float("nan"), float("inf")])
def test_forecast_refuses_a_nonsense_volume(bad):
    with pytest.raises(ValueError):
        functions.forecast(KITS["support"], bad)


def test_init_with_each_function_stamps_its_agent_type_and_carries_the_kit(tmp_path):
    for key, kit in KITS.items():
        root = str(tmp_path / key)
        assert main(["init", "--function", key, "--dest", root]) == 0
        assert paths.store_agent_type(root) == kit.agent_type
        assert functions.store_kit(root) == kit
        index = open(paths.index_path(root), encoding="utf-8").read()
        assert kit.domains[0].replace("-", " ").title() in index


def test_init_refuses_function_and_kit_together_and_unknown_functions(tmp_path, capsys):
    path = tmp_path / "kit.json"
    path.write_text(json.dumps(GOOD))
    assert main(["init", "--function", "sales", "--kit", str(path), "--dest", str(tmp_path / "a")]) == 2
    assert main(["init", "--function", "astrology", "--dest", str(tmp_path / "b")]) == 2
    err = capsys.readouterr().err
    assert "not both" in err and "no function kit 'astrology'" in err
    assert not os.path.isdir(tmp_path / "a" / "memory")


def test_show_prints_the_regulated_notice_only_where_it_applies():
    assert "not whether the underlying decision was correct" in functions.render_kit(KITS["clinical"])
    assert "Regulated" not in functions.render_kit(KITS["support"])


@pytest.mark.parametrize("key", ["support", "sales", "robotics"])
def test_a_demo_report_is_sound_not_merely_significant(tmp_path, key):
    from commontrace import integrity

    root = str(tmp_path)
    functions.seed_demo(root, KITS[key], occasions=600)
    rows, _salt, _other = experiment_cmd.scope_to_current_salt(root, experiment_cmd._load(root)[0])
    assert integrity.audit(rows).verdict == integrity.VERDICT_SOUND
