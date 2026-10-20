from __future__ import annotations

import json
import math
import random

import pytest

from commontrace import aipw, eprocess, experiment, holdout_io, value


def test_seeded_joint_peeking_with_dependent_memories():
    """100 replicated trials; identical task outcomes induce perfect dependence.

    This is a regression simulation, not a proof. MC tolerance is 4 percentage
    points; the sharper seeded result is recorded without asserting universality.
    """
    rng = random.Random(731)
    e_rejections = bh_rejections = 0
    for _ in range(100):
        observations = []
        e_crossed = bh_crossed = False
        for t in range(1, 241):
            observations.append(aipw.Observation(rng.random() < .5, rng.random() < .5, .5))
            if t % 20:
                continue
            current = eprocess.process(observations)
            # Identical e-values and p-values for four dependent memories.
            e_crossed |= any(eprocess.ebh([current.maximum] * 4))
            treated = [o for o in observations if o.injected]
            control = [o for o in observations if not o.injected]
            _, p = experiment.two_proportion_test(sum(o.succeeded for o in treated), len(treated),
                                                   sum(o.succeeded for o in control), len(control))
            bh_crossed |= any(experiment.benjamini_hochberg([p] * 4))
        e_rejections += e_crossed
        bh_rejections += bh_crossed
    assert e_rejections <= 9
    assert bh_rejections > 10


def test_power_and_independent_deployment_combination():
    rng = random.Random(15)
    e_power = bh_power = combined_null = 0
    for _ in range(80):
        positive = [aipw.Observation(a := rng.random() < .5, rng.random() < (.85 if a else .15), .5)
                    for _ in range(240)]
        e_power += eprocess.process(positive).e_value >= 20
        arms = [[o for o in positive if o.injected == arm] for arm in (True, False)]
        bh_power += experiment.two_proportion_test(sum(o.succeeded for o in arms[0]), len(arms[0]),
                                                  sum(o.succeeded for o in arms[1]), len(arms[1]))[1] < .05
        deployments = [eprocess.process([aipw.Observation(rng.random() < .5, rng.random() < .5, .5)
                                         for _ in range(120)]).e_value for _ in range(2)]
        combined_null += eprocess.combine(deployments, independent=True) >= 20
    assert e_power >= 72 and bh_power >= e_power
    assert combined_null <= 8
    assert eprocess.combine([2, 4]) == 3


def test_propensities_and_predictable_covariates_are_used():
    rng = random.Random(6)
    obs = []
    for i in range(2000):
        rate = .15 if i < 1000 else .85
        obs.append(aipw.Observation(rng.random() > rate, rng.random() < (.9 if i < 1000 else .1), rate,
                                    "early" if i < 1000 else "late"))
    scores = aipw.aipw_scores(obs)
    assert abs(sum(scores) / len(scores)) < .05
    naive = sum(o.succeeded for o in obs if o.injected) / sum(o.injected for o in obs)
    naive -= sum(o.succeeded for o in obs if not o.injected) / sum(not o.injected for o in obs)
    assert naive > .4
    adjusted = eprocess.process(obs, prior_success=[.9] * 1000 + [.1] * 1000)
    assert adjusted.maximum < 20
    assert adjusted.e_value != eprocess.process(obs).e_value


def test_signed_era_ledger_and_cli(tmp_path):
    from commontrace import integrity
    from commontrace.mcp_server import build_server
    pytest.importorskip("mcp")
    from tests.test_mcp_server import call, cli

    root = str(tmp_path)
    rows = [integrity.Assignment("lesson", str(i), i % 2 == 0, .5, "salt", i % 2 == 0,
                                  schedule="era-1" if i < 30 else "era-2") for i in range(60)]
    entries = value.record_evidence(root, rows)
    assert len(entries) == 2
    assert len(value.record_evidence(root, rows)) == 2
    holdout_io.configure(root, rate=.5, salt="salt")
    for i in range(6):
        holdout_io.assign_and_log(root, ["lesson"], occasion_id=str(i), rate=.5, salt="salt")
        holdout_io.record_outcome(root, str(i), i % 2 == 0)
    out = cli("experiment", "--evidence", "e-values", "--dest", root)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["e_values"]["lesson"]["n"] == 6
    assert "e_value" in call(build_server(root), "causal_gate_report")


@pytest.mark.parametrize("bad", [math.nan, math.inf, -1])
def test_nonfinite_evidence_rejected(bad):
    with pytest.raises(ValueError):
        eprocess.ebh([bad])


def test_missing_propensity_is_not_fabricated():
    obs = [experiment.HoldoutObservation("lesson", "task", True, True)]
    assert eprocess.report(obs) == {}


def _shared_task_observations(rng, n=600):
    """Two memories delivered on the same tasks: one harmful, one inert."""
    rows = []
    for i in range(n):
        occasion = f"task-{i}"
        harmful_on = rng.random() < .5
        inert_on = rng.random() < .5
        succeeded = rng.random() < (.2 if harmful_on else .8)
        rows.append(experiment.HoldoutObservation("harmful", occasion, harmful_on, succeeded, .5))
        rows.append(experiment.HoldoutObservation("inert", occasion, inert_on, succeeded, .5))
    return rows


def test_shared_task_ebh_never_weakens_harm_withdrawal(monkeypatch):
    rows = _shared_task_observations(random.Random(4))
    for i in range(600):  # a third memory that genuinely helps, on the same tasks
        rows.append(experiment.HoldoutObservation("helpful", f"task-{i}", i % 2 == 0, i % 2 == 0 or i % 7 == 0, .5))
    effects = {e.lesson_slug: e for e in experiment.analyze(rows, sequential=True)}
    assert effects["harmful"].verdict == experiment.VERDICT_HURTS
    assert effects["helpful"].verdict == experiment.VERDICT_HELPS
    assert effects["harmful"].e_value is not None and effects["harmful"].e_value > 1
    # e-BH governs positive (billing/release) claims only; withdrawal keeps its trigger.
    monkeypatch.setattr(eprocess, "ebh", lambda values, alpha=.05: [False] * len(values))
    effects = {e.lesson_slug: e for e in experiment.analyze(rows, sequential=True)}
    assert effects["harmful"].verdict == experiment.VERDICT_HURTS
    assert effects["helpful"].verdict == experiment.VERDICT_UNDERPOWERED


def test_dirty_log_is_reported_not_fatal():
    rows = _shared_task_observations(random.Random(5), 100)
    rows.append(rows[0])  # duplicated (memory, occasion) row
    assert experiment.analyze(rows, sequential=True)  # evidence of record still answers
    report = eprocess.report(rows, strict=False)
    assert report["harmful"]["duplicates_dropped"] == 1
    with pytest.raises(ValueError, match="duplicate"):
        eprocess.report(rows)


def test_regression_slope_is_predictable_and_reduces_variance():
    rng = random.Random(8)
    rows, priors = [], []
    for _ in range(1500):
        prior = rng.random()
        injected = rng.random() < .5
        rows.append(aipw.Observation(injected, rng.random() < min(1, .1 + .8 * prior + (.1 if injected else 0)), .5))
        priors.append(prior)
    plain = eprocess._adjusted(rows, None)[0]
    adjusted = eprocess._adjusted(rows, priors)[0]

    def variance(xs):
        mean = sum(xs) / len(xs)
        return sum((x - mean) ** 2 for x in xs) / len(xs)

    assert variance(adjusted[100:]) < variance(plain[100:])
    # The adjustment keeps the score unbiased for the true effect (0.1).
    assert abs(sum(adjusted) / len(adjusted) - .1) < .05
    # Mutation guard: an outcome-peeking slope (fit on all rows) is not what runs.
    assert adjusted[:10] == plain[:10]
