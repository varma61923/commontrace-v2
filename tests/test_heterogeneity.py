"""Subgroup (heterogeneous) lesson effects from the randomized holdout."""
import json
import random

import pytest

from commontrace import experiment, heterogeneity, holdout_io, trace_io
from commontrace.cli import main


@pytest.mark.parametrize("x,df,expected", [
    (3.841458820694124, 1, 0.05), (5.991464547107979, 2, 0.05), (11.070497693516351, 5, 0.05),
    (0.0, 3, 1.0), (1.0, 1, 0.31731050786291404), (30.0, 10, 0.0008566412),
])
def test_chi2_sf_matches_reference_values(x, df, expected):
    assert heterogeneity.chi2_sf(x, df) == pytest.approx(expected, rel=1e-6, abs=1e-9)


def _obs(rng, lesson, groups, n=200):
    """groups: {label: (baseline, effect)}; independent 50% randomization per occasion."""
    rows, group_of = [], {}
    for label, (base, effect) in groups.items():
        for i in range(n):
            occ = f"{label}-{i}"
            injected = rng.random() < 0.5
            p = base + effect if injected else base
            rows.append(experiment.HoldoutObservation(lesson, occ, injected, rng.random() < p))
            group_of[occ] = label
    return rows, group_of


def test_crossing_effect_is_flagged_even_when_the_average_is_null():
    rows, group_of = _obs(random.Random(7), "L", {"support": (0.4, 0.35), "sales": (0.6, -0.35)}, n=300)
    overall = experiment.analyze(rows, fixed_horizon=True)[0]
    assert abs(overall.effect) < 0.1  # the fleet-wide average hides both effects
    [h] = heterogeneity.analyze(rows, group_of)
    assert h.flag == heterogeneity.FLAG_CROSSING and h.q_p_value < 1e-6
    by = {g.group: g for g in h.groups}
    assert by["support"].effect > 0.2 and by["sales"].effect < -0.2


def test_consistent_effect_is_not_flagged():
    rows, group_of = _obs(random.Random(3), "L", {"a": (0.4, 0.2), "b": (0.4, 0.2), "c": (0.4, 0.2)})
    [h] = heterogeneity.analyze(rows, group_of)
    assert h.flag == heterogeneity.FLAG_CONSISTENT and h.degrees_of_freedom == 2


def test_null_effects_rarely_flag_heterogeneity():
    flagged = 0
    for seed in range(40):
        rows, group_of = _obs(random.Random(seed), "L", {"a": (0.5, 0.0), "b": (0.5, 0.0)}, n=120)
        [h] = heterogeneity.analyze(rows, group_of)
        flagged += h.flag in (heterogeneity.FLAG_CROSSING, heterogeneity.FLAG_HETEROGENEOUS)
    assert flagged <= 6  # ~5% expected at alpha 0.05


def test_sparse_and_unlabelled_occasions():
    rows, group_of = _obs(random.Random(1), "L", {"a": (0.5, 0.1)}, n=100)
    rows.append(experiment.HoldoutObservation("L", "unlabelled", True, True))
    rows += [experiment.HoldoutObservation("L", f"tiny-{i}", i % 2 == 0, True) for i in range(6)]
    group_of.update({f"tiny-{i}": "tiny" for i in range(6)})
    [h] = heterogeneity.analyze(rows, group_of)
    assert h.flag == heterogeneity.FLAG_UNTESTABLE and h.untested_groups == ["tiny"]
    assert sum(g.n_injected + g.n_withheld for g in h.groups) == 100


def test_covariate_file_validation(tmp_path):
    good = tmp_path / "c.jsonl"
    good.write_text('{"occasion_id": "o1", "group": "tier-a"}\n\n{"occasion_id": "o2", "group": "tier-b"}\n')
    assert heterogeneity.occasion_groups_from_file(str(good)) == {"o1": "tier-a", "o2": "tier-b"}
    bad = tmp_path / "d.jsonl"
    bad.write_text('{"occasion_id": "o1", "group": "a"}\n{"occasion_id": "o1", "group": "b"}\n')
    with pytest.raises(ValueError, match="two different groups"):
        heterogeneity.occasion_groups_from_file(str(bad))
    with pytest.raises(ValueError, match="pre-treatment"):
        heterogeneity.occasion_groups_from_traces(str(tmp_path), "tags")


def test_cli_by_agent_type_end_to_end(tmp_path, capsys):
    root = str(tmp_path)
    assert main(["init", "--dest", root]) == 0
    config = holdout_io.configure(root, rate=0.5)
    rng = random.Random(11)
    for agent_type, base, effect in (("support", 0.4, 0.4), ("sales", 0.6, -0.4)):
        for i in range(160):
            occ = f"{agent_type}-{i}"
            withheld = holdout_io.assign_and_log(root, ["L"], occasion_id=occ, rate=0.5, salt=config.salt)
            trace_io.write_new(root, title="t", context="c", solution="s", tags=[], agent_type=agent_type,
                               trace_id=occ)
            p = base if "L" in withheld else base + effect
            holdout_io.record_outcome(root, occ, rng.random() < p)
    capsys.readouterr()
    assert main(["experiment", "--by", "agent_type", "--json", "--dest", root]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["exploratory"] is True and out["labelled_occasions"] == 320
    [lesson] = out["lessons"]
    assert lesson["flag"] == "CROSSING"
    assert main(["experiment", "--by", "agent_type", "--strict", "--dest", root]) == 1
    assert "--draft-revisions" in capsys.readouterr().out
    assert main(["experiment", "--by", "agent_type", "--covariates", "x", "--dest", root]) == 2
