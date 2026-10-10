"""Adaptive holdout allocation: schedules, the AIPW confidence sequence, audit and proof round trip."""
from __future__ import annotations

import datetime
import json
import math
import os
import random
import types

import pytest

from benchmarks import adaptive_allocation_bench as bench
from commontrace import allocation, experiment, functions, holdout_io, integrity, proof
from commontrace.cli import main
from commontrace.commands import experiment_cmd

T0 = datetime.datetime(2026, 3, 1, 9, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture(autouse=True)
def _no_fsync(monkeypatch):
    monkeypatch.setattr(os, "fsync", lambda fd: None)


class _Clock:
    def __init__(self):
        self.now = T0

    def tick(self, seconds: float) -> None:
        self.now += datetime.timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()

    class Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return c.now

    monkeypatch.setattr(holdout_io, "datetime", types.SimpleNamespace(
        datetime=Frozen, timezone=datetime.timezone, timedelta=datetime.timedelta))
    monkeypatch.setattr(allocation, "datetime", types.SimpleNamespace(
        datetime=Frozen, timezone=datetime.timezone, timedelta=datetime.timedelta))
    return c


def test_the_sequence_is_the_published_formula_and_the_intersection_never_widens():
    rng = random.Random(3)
    scores = [rng.gauss(0.1, 1.0) for _ in range(800)]
    mean, low, high = allocation.confidence_sequence(scores, 0.05, 2000)
    rho2 = (-2 * math.log(0.05) + math.log(1 - 2 * math.log(0.05))) / 2000
    t, var = len(scores), sum((s - mean) ** 2 for s in scores) / 799
    v = t * var * rho2 + 1
    assert high - mean == pytest.approx(math.sqrt(2 * v / rho2 * math.log(math.sqrt(v) / 0.05)) / t)
    assert bench._cs_prefixes(scores, [800])[800] == pytest.approx((mean, low, high))
    _m, ilow, ihigh = allocation.running_intersection(scores, 0.05, 2000)
    assert low <= ilow <= ihigh <= high


def test_aipw_is_unbiased_when_the_rate_changes_with_a_time_trend():
    # The baseline climbs over time while the holdout falls: a difference in means
    # would mix the eras; the propensity-weighted scores do not.
    rng, estimates, naive = random.Random(11), [], []
    for _ in range(300):
        obs = []
        for t in range(600):
            rate = 0.5 if t < 300 else 0.1
            base = 0.3 if t < 300 else 0.7
            injected = rng.random() >= rate
            obs.append(allocation.Observation(injected, rng.random() < base + (0.1 if injected else 0), rate))
        scores = allocation.aipw_scores(obs)
        estimates.append(sum(scores) / len(scores))
        inj = [o.succeeded for o in obs if o.injected]
        wit = [o.succeeded for o in obs if not o.injected]
        naive.append(sum(inj) / len(inj) - sum(wit) / len(wit))
    assert sum(estimates) / len(estimates) == pytest.approx(0.1, abs=0.01)
    assert sum(naive) / len(naive) > 0.15  # the bias the weighting removes


def test_policy_maps_verdicts_to_rates_and_validates():
    policy = allocation.Policy()
    assert allocation.next_rate(experiment.VERDICT_HELPS, policy) == 0.05
    assert allocation.next_rate(experiment.VERDICT_NO_EFFECT, policy) == 0.05
    assert allocation.next_rate(experiment.VERDICT_HURTS, policy) == 0.95
    assert allocation.next_rate(experiment.VERDICT_UNDERPOWERED, policy) == 0.5
    with pytest.raises(ValueError):
        allocation.Policy(explore=0.05, monitor=0.5)


def _occasions(root, start, n, clock, rng, *, truth):
    config = holdout_io.load_config(root)
    for i in range(start, start + n):
        slug = list(truth)[i % len(truth)]
        withheld = holdout_io.assign_and_log(root, [slug], occasion_id=f"T-{i}", rate=config.rate,
                                             salt=config.salt, revisions={slug: "r1"})
        holdout_io.record_outcome(root, f"T-{i}", rng.random() < 0.5 + (truth[slug] if slug not in withheld else 0))
        clock.tick(30)


def test_eras_move_a_proven_memory_to_monitoring_and_the_whole_run_verifies(tmp_path, clock, monkeypatch):
    real = holdout_io.configure
    monkeypatch.setattr(holdout_io, "configure", lambda *a, **kw: real(*a, **{**kw, "salt": "pinned"}))
    root = str(tmp_path / "store")
    proof.start(root, functions.builtin_kits()["support"], label="Adaptive", daily=300, value_per_occasion=5.0)
    with pytest.raises(ValueError, match="not enabled"):
        allocation.plan(root)
    first = allocation.enable(root, allocation.Policy(era_occasions=300))
    clock.tick(10)
    truth, rng = {"helps": 0.3, "null": 0.0}, random.Random(5)
    _occasions(root, 0, 600, clock, rng, truth=truth)
    second = allocation.plan(root)
    assert second.previous == first.digest and second.rates["helps"] == 0.05
    clock.tick(10)
    _occasions(root, 600, 600, clock, rng, truth=truth)

    records, _ = holdout_io.read_log(root)
    later = [r for r in records if r.lesson == "helps" and r.at >= second.effective_at()]
    assert later and all(r.rate == 0.05 and r.schedule == second.digest[:16] for r in later)
    assert allocation.verify(root) == []
    assert main(["allocate", "verify", "--dest", root]) == 0

    rows, _rate, _ = experiment_cmd._load(root)
    report = integrity.audit(rows, schedules=allocation.history(root))
    drift = next(f for f in report.findings if f.check == "assignment_drift")
    assert drift.severity == integrity.SEVERITY_OK and "schedules" in drift.headline
    effects = {e.lesson_slug: e for e in experiment.analyze(experiment_cmd._observations(rows), sequential=True)}
    assert effects["helps"].verdict == experiment.VERDICT_HELPS
    assert effects["helps"].ci_low <= 0.3 <= effects["helps"].ci_high

    out = str(tmp_path / "pkg")
    record = proof.build(root, out, key=b"k" * 32)
    assert [s["version"] for s in record["allocation"]] == [0, 1]
    checks = {c.name: c for c in proof.verify(out, key=b"k" * 32)}
    assert {c.status for c in checks.values()} == {proof.PASS}, [str(c) for c in checks.values()]

    # A schedule edited after the fact no longer hashes, and the package says so.
    path = os.path.join(out, proof.RECORD_NAME)
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["allocation"][1]["rates"]["helps"] = 0.5
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    assert any(c.status == proof.FAIL for c in proof.verify(out, key=b"k" * 32))


def test_a_rate_that_disagrees_with_its_schedule_invalidates(clock, tmp_path):
    schedule = allocation.Schedule(version=0, salt="s", effective_from=T0.isoformat(), default_rate=0.5,
                                   rates={"a": 0.05}, policy={}, basis={}, previous="", created_at=T0.isoformat())
    later = T0 + datetime.timedelta(minutes=1)
    rows = [integrity.Assignment("a", f"o{i}", True, rate=0.05, salt="s", at=later, schedule="x") for i in range(5)]
    rows += [integrity.Assignment("a", "o9", True, rate=0.5, salt="s", at=later, schedule="x")]
    assert integrity.check_assignment_drift(rows, [schedule]).severity == integrity.SEVERITY_INVALIDATES
    assert integrity.check_assignment_drift(rows[:5] + [integrity.Assignment(
        "b", "o8", True, rate=0.5, salt="s", at=later)], [schedule]).severity == integrity.SEVERITY_OK


def test_disable_returns_to_the_fixed_rate(tmp_path, clock):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.2, salt="fixed")
    allocation.enable(root)
    clock.tick(10)
    assert allocation.active(root, "fixed").rate_for("x") == 0.5
    allocation.disable(root)
    clock.tick(10)
    assert allocation.active(root, "fixed") is None
    holdout_io.assign_and_log(root, ["x"], occasion_id="o1", rate=0.2, salt="fixed", revisions={"x": None})
    [record] = holdout_io.read_log(root)[0]
    assert record.rate == 0.2 and record.schedule is None


def test_the_simulation_reports_power_coverage_and_cost():
    report = bench.run(reps=6, designs=("fixed50-aipw", "adaptive-aipw-strata"), strata=("flat",),
                       effects=(0.0, 0.1))
    assert {c["design"] for c in report["cells"]} == {"fixed50-aipw", "adaptive-aipw-strata"}
    adaptive = next(c for c in report["cells"] if c["design"].startswith("adaptive") and c["effect"] == 0.1)
    assert 0 <= adaptive["withheld_share_of_helpful"] <= 0.5 and adaptive["time_uniform_coverage"] <= 1
