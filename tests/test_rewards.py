"""Graded and multi-objective outcomes: unbiased randomized rounding, utilities, audit and every surface."""

from __future__ import annotations

import json
import math
import os
import random

import pytest

from tests.test_gateway import call, recall
from tests.test_robotics_bridge import LocalTransport
from unimem import causal_gate, experiment, gateway, holdout_io, rewards
from unimem.cli import main
from unimem.commands import experiment_cmd
from unimem.measure import CausalMemory
from unimem.robotics import bridge, edge


@pytest.fixture(autouse=True)
def _no_fsync(monkeypatch):
    monkeypatch.setattr(os, "fsync", lambda fd: None)


# -- rounding --------------------------------------------------------------------------------------


def test_draws_are_reproducible_uniform_and_salted():
    assert rewards.draw("s", "o1") == rewards.draw("s", "o1") != rewards.draw("t", "o1")
    draws = [rewards.draw("s", f"o{i}") for i in range(20000)]
    assert all(0.0 <= u < 1.0 for u in draws)
    assert abs(sum(draws) / len(draws) - 0.5) < 0.01
    assert abs(sum(u < 0.3 for u in draws) / len(draws) - 0.3) < 0.01


@pytest.mark.parametrize("reward", [-0.01, 1.01, math.nan, math.inf, True, "0.5", None])
def test_a_reward_must_be_a_finite_fraction(reward):
    with pytest.raises(ValueError):
        rewards.realize("s", "o", reward)


@pytest.mark.parametrize("smallest", [1e-200, 1e-310])
def test_raw_reward_effects_preserve_weight_ratios_at_extreme_propensities(monkeypatch, smallest):
    rows = [
        causal_gate.Row("m", "treated-1", True, True, 0.5),
        causal_gate.Row("m", "treated-2", True, True, 0.5),
        causal_gate.Row("m", "control-1", False, False, smallest),
        causal_gate.Row("m", "control-2", False, True, 2 * smallest),
    ]
    monkeypatch.setattr(causal_gate, "current_rows", lambda root: rows)
    monkeypatch.setattr(
        rewards, "load", lambda root: {row.occasion_id: {"reward": float(row.succeeded)} for row in rows}
    )
    result = rewards.effects("unused")[0]
    assert result["mean_injected"] == 1.0
    assert result["mean_withheld"] == pytest.approx(1 / 3)
    assert result["effect"] == pytest.approx(2 / 3)
    assert result["se"] == pytest.approx(math.sqrt(8 / 81))
    assert all(math.isfinite(result[field]) for field in ("effect", "se", "ci_low", "ci_high"))


def test_the_extremes_are_certain():
    assert all(rewards.realize("s", f"o{i}", 1.0) for i in range(200))
    assert not any(rewards.realize("s", f"o{i}", 0.0) for i in range(200))


# -- utility ---------------------------------------------------------------------------------------


def test_a_utility_scales_each_term_in_its_direction_and_refuses_missing_terms():
    terms = rewards.parse_utility(
        {"terms": {"progress": {"weight": 3}, "energy_wh": {"low": 0, "high": 50, "better": "lower"}}}
    )
    assert rewards.score(terms, {"progress": 1.0, "energy_wh": 0}) == 1.0
    assert rewards.score(terms, {"progress": 0.0, "energy_wh": 80}) == 0.0
    assert rewards.score(terms, {"progress": 0.5, "energy_wh": 25}) == pytest.approx(0.5)
    assert rewards.score(terms, {"progress": True, "energy_wh": 50}) == pytest.approx(0.75)
    with pytest.raises(ValueError, match="missing"):
        rewards.score(terms, {"progress": 1.0})
    with pytest.raises(ValueError, match="finite"):
        rewards.score(terms, {"progress": math.nan, "energy_wh": 1})


@pytest.mark.parametrize(
    "spec",
    [
        {},
        {"terms": {}},
        {"terms": {"9bad": {}}},
        {"terms": {"x": {"weight": 0}}},
        {"terms": {"x": {"low": 1, "high": 1}}},
        {"terms": {"x": {"better": "sideways"}}},
        {"terms": {"x": {"bogus": 1}}},
        {"terms": {"x": {"weight": True}}},
    ],
)
def test_bad_utilities_are_refused(spec):
    with pytest.raises(ValueError):
        rewards.parse_utility(spec)


# -- the store -------------------------------------------------------------------------------------

MEMORIES = [
    {"id": "verify-grasp", "text": "re-verify the grasp pose before lifting"},
    {"id": "fast-path", "text": "skip the collision check to save time"},
    {"id": "neutral", "text": "log the battery level"},
]
PLANTED = {"verify-grasp": 0.15, "fast-path": -0.10, "neutral": 0.0}


def _graded_run(root: str, n: int, seed: int = 0) -> None:
    rng = random.Random(seed)
    memory = CausalMemory(lambda _q, **_: MEMORIES, root=root, key=lambda m: m["id"], text=lambda m: m["text"])
    for i in range(n):
        shown = {m["id"] for m in memory.recall("pick the part", occasion_id=f"o{i}")}
        progress = 0.5 + sum(PLANTED[m] for m in shown) + rng.uniform(-0.2, 0.2)
        memory.record_outcome(f"o{i}", reward=min(1.0, max(0.0, progress)))


def test_rounded_rewards_estimate_reward_effects_without_bias_under_unequal_rates():
    rng = random.Random(1)
    rate = {"verify-grasp": 0.2, "fast-path": 0.7, "neutral": 0.5}
    rows = []
    for i in range(20000):
        shown = {m: rng.random() >= rate[m] for m in PLANTED}
        progress = 0.5 + sum(PLANTED[m] for m in PLANTED if shown[m]) + rng.uniform(-0.2, 0.2)
        bit = rewards.realize("unbiased", f"o{i}", min(1.0, max(0.0, progress)))
        rows += [causal_gate.Row(m, f"o{i}", shown[m], bit, rate[m]) for m in PLANTED]
    for memory, planted in PLANTED.items():
        c = causal_gate.contrast([r for r in rows if r.lesson == memory])
        assert c is not None and abs(c.effect - planted) < 3 * math.sqrt(c.variance), (memory, c)


def test_a_graded_store_run_measures_on_the_reward_scale_and_audits_clean(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="graded")
    _graded_run(root, 600)
    rows, _rate, _ = experiment_cmd._load(root)
    effects = {e.lesson_slug: e for e in experiment.analyze(experiment_cmd._observations(rows))}
    raw = {r["lesson"]: r for r in rewards.effects(root)}
    for memory, planted in PLANTED.items():
        assert raw[memory]["ci_low"] <= planted <= raw[memory]["ci_high"], (memory, raw[memory])
        assert raw[memory]["n_injected"] + raw[memory]["n_withheld"] == 600
    assert effects["verify-grasp"].effect > 0 > effects["fast-path"].effect
    gate_rows = causal_gate.current_rows(root)
    for memory in PLANTED:
        bits = causal_gate.contrast([r for r in gate_rows if r.lesson == memory])
        assert bits is not None and raw[memory]["se"] < math.sqrt(bits.variance)  # the raw reward is less noisy
    assert rewards.verify(root) == []


def test_a_reward_is_recorded_once_and_never_rewrites_an_outcome(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="once")
    first = rewards.record(root, "o1", reward=0.4)
    assert first["recorded"] and first["succeeded"] == rewards.realize("once", "o1", 0.4)
    assert rewards.record(root, "o1", reward=0.4)["recorded"] is False
    with pytest.raises(holdout_io.ConflictingOutcome):
        rewards.record(root, "o1", reward=0.9)
    holdout_io.record_outcome(root, "o2", True)
    with pytest.raises(holdout_io.ConflictingOutcome, match="binary outcome"):
        rewards.record(root, "o2", reward=1.0)
    with pytest.raises(ValueError, match="exactly one"):
        rewards.record(root, "o3", reward=0.5, metrics={"x": 1})
    with pytest.raises(ValueError, match="exactly one"):
        rewards.record(root, "o3")
    with pytest.raises(ValueError, match="utility"):
        rewards.record(root, "o3", metrics={"x": 1})
    assert holdout_io.read_outcomes(root) == {"o1": first["succeeded"], "o2": True}


def test_metrics_are_scored_by_the_configured_utility_and_the_audit_catches_tampering(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="audit")
    configured = rewards.configure(
        root, {"terms": {"score": {"weight": 1}, "tokens": {"high": 4000, "better": "lower"}}}
    )
    out = rewards.record(root, "o1", metrics={"score": 0.8, "tokens": 2000})
    assert out["reward"] == pytest.approx(0.65)
    [row] = rewards.load(root).values()
    assert row["utility_digest"] == configured["digest"] and row["metrics"] == {"score": 0.8, "tokens": 2000.0}
    for i in range(2, 30):
        rewards.record(root, f"o{i}", reward=0.5)
    assert rewards.verify(root) == []

    rewards.configure(root, {"terms": {"score": {"weight": 2}, "tokens": {"high": 4000, "better": "lower"}}})
    rewards.record(root, "o99", metrics={"score": 0.1, "tokens": 100})
    assert any("different utilities" in p for p in rewards.verify(root))

    with open(rewards.rewards_path(root), encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    victim = next(r for r in rows if r["occasion_id"] != "o99" and not r["succeeded"])
    victim["succeeded"] = True
    with open(rewards.rewards_path(root), "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)
    assert any(victim["occasion_id"] in p and "not the rounding" in p for p in rewards.verify(root))


@pytest.mark.parametrize(
    "extra", [{"note": "fine"}, {"force": float("nan")}, {"pose": [1, 2]}, {"": 1.0}, {"x" * 65: 1.0}]
)
def test_a_bad_metric_is_refused_before_anything_is_recorded(tmp_path, extra):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="atomic")
    rewards.configure(root, {"terms": {"progress": {}}})
    with pytest.raises(ValueError):
        rewards.record(root, "o1", metrics={"progress": 0.5, **extra})
    assert "o1" not in holdout_io.read_outcomes(root) and rewards.load(root) == {}
    assert rewards.record(root, "o1", metrics={"progress": 0.5, "energy (Wh)": 12.0, "stalled": False})["recorded"]


def test_an_interrupted_write_is_reported_and_finished_by_recording_the_same_reward_again(tmp_path, monkeypatch):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="crash")
    record_outcome = holdout_io.record_outcome

    def power_loss(*_args, **_kwargs):
        raise OSError("power lost")

    monkeypatch.setattr(holdout_io, "record_outcome", power_loss)
    with pytest.raises(OSError):
        rewards.record(root, "o1", reward=0.6)
    monkeypatch.setattr(holdout_io, "record_outcome", record_outcome)
    report = rewards.audit(root)
    assert report.interrupted == ("o1",) and "interrupted write" in report.problems[0]
    with pytest.raises(holdout_io.ConflictingOutcome, match="refusing"):
        rewards.record(root, "o1", reward=0.7)
    out = rewards.record(root, "o1", reward=0.6)
    assert out["recorded"] and out["repaired"] and out["succeeded"] == rewards.realize("crash", "o1", 0.6)
    assert holdout_io.read_outcomes(root)["o1"] == out["succeeded"]
    assert rewards.audit(root).problems == () and rewards.record(root, "o1", reward=0.6)["recorded"] is False


def test_a_disagreeing_binary_outcome_is_a_conflict_even_for_the_same_reward(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="race")
    first = rewards.record(root, "o1", reward=0.5)
    with open(holdout_io.outcomes_log_path(root), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"occasion_id": "o1", "succeeded": not first["succeeded"], "at": "x"}) + "\n")
    with pytest.raises(holdout_io.ConflictingOutcome, match="disagrees"):
        rewards.record(root, "o1", reward=0.5)
    assert any("disagrees" in p for p in rewards.verify(root))


def test_refused_secondary_metrics_leave_no_reward_commitment(tmp_path, monkeypatch):
    from unimem import sycophancy_io

    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="secondary")

    def refuse(*_args, **_kwargs):
        raise ValueError("UniMem secondary metrics require an assigned preregistered personalization audit")

    monkeypatch.setattr(sycophancy_io, "record_metrics", refuse)
    with pytest.raises(ValueError, match="preregistered"):
        rewards.record(root, "o1", reward=0.5, secondary_metrics={"accuracy": 1.0, "agreement": True})
    assert rewards.load(root) == {} and holdout_io.read_outcomes(root) == {}


def test_rewards_from_an_earlier_salt_are_counted_as_uncheckable_not_passed(tmp_path, capsys):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="first")
    rewards.record(root, "o1", reward=0.5)
    holdout_io.configure(root, rate=0.5, salt="second")
    rewards.record(root, "o2", reward=0.5)
    report = rewards.audit(root)
    assert (report.checked, report.unverifiable, report.problems) == (1, 1, ())
    assert main(["reward", "verify", "--dest", root]) == 0
    assert "1 checked, 1 recorded under an earlier salt and not checkable" in capsys.readouterr().out


def test_the_audit_reports_a_malformed_reward_row_instead_of_crashing(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="malformed")
    rewards.record(root, "o1", reward=0.4)
    with open(rewards.rewards_path(root), encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    rows[0]["draw"] = None
    with open(rewards.rewards_path(root), "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)
    assert rewards.verify(root) == ["o1: unreadable reward"]


def test_utility_scoring_stays_finite_at_float_boundaries():
    terms = rewards.parse_utility(
        {
            "terms": {
                "a": {"weight": 1e308, "low": -1e308, "high": 1e308},
                "b": {"weight": 1e308},
            }
        }
    )
    assert rewards.score(terms, {"a": 0.0, "b": 0.5}) == pytest.approx(0.5)


@pytest.mark.parametrize("draw,bit", [(float("nan"), True), (float("inf"), True), (0.5, "false")])
def test_reward_audit_rejects_nonfinite_draws_and_nonboolean_bits(tmp_path, draw, bit):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="audit")
    rewards.record(root, "o1", reward=1.0)
    from unimem import _jsonl

    path = rewards.rewards_path(root)
    row = _jsonl.read_rows(path)[0]
    _jsonl.write_rows(path, [{**row, "draw": draw, "succeeded": bit}])
    assert rewards.verify(root)


def test_reward_load_does_not_expose_mutable_cache_rows(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="cache")
    rewards.record(root, "o1", reward=0.4)
    rewards.load(root)["o1"]["reward"] = 0.9
    assert rewards.load(root)["o1"]["reward"] == 0.4


def test_reward_audit_sees_a_same_size_in_place_edit(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="edited")
    rewards.record(root, "o1", reward=0.4)
    assert rewards.load(root)["o1"]["reward"] == 0.4
    path = rewards.rewards_path(root)
    with open(path, encoding="utf-8") as stream:
        original = stream.read()
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(original.replace('"reward": 0.4', '"reward": 1.4'))
    assert rewards.verify(root) == ["o1: unreadable reward"]


def test_power_loss_during_reward_append_does_not_swallow_the_retry(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="partial")
    rewards.record(root, "complete", reward=0.2)
    with open(rewards.rewards_path(root), "ab") as stream:
        stream.write(b'{"occasion_id": "interrupted", "reward":')
    assert rewards.record(root, "interrupted", reward=0.6)["recorded"]
    assert rewards.load(root)["interrupted"]["reward"] == 0.6
    assert rewards.verify(root) == []


def test_binary_outcome_cannot_override_an_interrupted_reward(tmp_path, monkeypatch):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="commit")
    original = holdout_io.record_outcome
    monkeypatch.setattr(holdout_io, "record_outcome", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        rewards.record(root, "o1", reward=1.0)
    monkeypatch.setattr(holdout_io, "record_outcome", original)
    with pytest.raises(holdout_io.ConflictingOutcome):
        original(root, "o1", False)
    assert rewards.record(root, "o1", reward=1.0)["repaired"]


def test_refused_binary_memory_outcomes_never_update_learners(gw, monkeypatch):
    from unimem import causal_policy, policy

    g, root = gw
    holdout_io.record_outcome(root, "o1", True)
    seen = []
    monkeypatch.setattr(policy, "outcome", lambda *_a: seen.append("policy"))
    monkeypatch.setattr(causal_policy, "record_outcome", lambda *_a: seen.append("exploration"))
    for value, status in [(False, 409), (0.5, 400), ("yes", 400)]:
        assert call(g, "POST", "/v1/memory/outcome", {"occasion_id": "o1", "succeeded": value})[0] == status
    assert seen == []


def test_refused_local_binary_outcome_never_updates_learners(tmp_path, monkeypatch):
    from unimem import causal_policy, policy
    from unimem.client import MemoryClient

    root = str(tmp_path)
    memory = MemoryClient(root)
    assert memory.outcome("o1", True)
    seen = []
    monkeypatch.setattr(policy, "outcome", lambda *_a: seen.append("policy"))
    monkeypatch.setattr(causal_policy, "record_outcome", lambda *_a: seen.append("exploration"))
    with pytest.raises(holdout_io.ConflictingOutcome):
        memory.outcome("o1", False)
    assert seen == []


def test_the_python_api_takes_exactly_one_outcome(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="api")
    memory = CausalMemory(lambda _q, **_: MEMORIES, root=root, key=lambda m: m["id"], text=lambda m: m["text"])
    with pytest.raises(ValueError, match="exactly one"):
        memory.record_outcome("o1")
    with pytest.raises(ValueError, match="exactly one"):
        memory.record_outcome("o1", succeeded=True, reward=0.5)
    assert memory.record_outcome("o1", succeeded=True) is True
    assert memory.record_outcome("o2", reward=0.25) is True


# -- surfaces --------------------------------------------------------------------------------------


@pytest.fixture
def gw(tmp_path):
    root = str(tmp_path / "store")
    holdout_io.configure(root, rate=0.5, salt="gw-tests")
    return gateway.Gateway(root, token="t" * 40), root


def test_the_gateway_takes_a_reward_or_metrics_and_refuses_ambiguity(gw):
    g, root = gw
    recall(g, "o1", MEMORIES)
    status, body = call(g, "POST", "/v1/outcome", {"occasion_id": "o1", "reward": 0.7})
    assert status == 200 and body["recorded"] and body["reward"] == 0.7
    assert body["succeeded"] == rewards.realize("gw-tests", "o1", 0.7) == holdout_io.read_outcomes(root)["o1"]
    assert call(g, "POST", "/v1/outcome", {"occasion_id": "o1", "reward": 0.7})[1]["recorded"] is False
    status, body = call(g, "POST", "/v1/outcome", {"occasion_id": "o1", "reward": 0.1})
    assert status == 409 and body["error"]["code"] == "conflicting_outcome"
    for bad in (
        {"occasion_id": "o2", "reward": 1.5},
        {"occasion_id": "o2", "reward": "high"},
        {"occasion_id": "o2", "reward": 0.5, "succeeded": True},
        {"occasion_id": "o2", "metrics": [1]},
        {"occasion_id": "o2", "metrics": {"progress": 1}},
    ):
        assert call(g, "POST", "/v1/outcome", bad)[0] == 400, bad
    rewards.configure(root, {"terms": {"progress": {}}})
    status, body = call(g, "POST", "/v1/outcome", {"occasion_id": "o3", "metrics": {"progress": 0.25}})
    assert status == 200 and body["reward"] == 0.25


def test_a_graded_outcome_keeps_its_secondary_metrics(gw, monkeypatch):
    from unimem import sycophancy_io

    g, _root = gw
    seen = []
    monkeypatch.setattr(
        sycophancy_io, "record_metrics", lambda _root, occasion, values: seen.append((occasion, values))
    )
    recall(g, "o1", MEMORIES)
    body = {"occasion_id": "o1", "reward": 0.6, "secondary_metrics": {"accuracy": 0.9, "agreement": False}}
    assert call(g, "POST", "/v1/outcome", body)[0] == 200
    assert seen == [("o1", {"accuracy": 0.9, "agreement": False})]


def test_the_gateway_contract_documents_graded_outcomes(gw):
    g, _root = gw
    status, doc = call(g, "GET", "/v1/openapi.json")
    schema = doc["paths"]["/v1/outcome"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    if "$ref" in schema:
        schema = doc["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    props = schema["properties"]
    assert props["reward"]["minimum"] == 0 and props["reward"]["maximum"] == 1
    assert props["metrics"]["additionalProperties"]["type"] == "number"


def test_a_robot_reports_graded_outcomes_and_its_offline_queue_keeps_them(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="robot")
    rewards.configure(root, {"terms": {"progress": {"weight": 3}, "cycle_s": {"high": 60, "better": "lower"}}})
    queue = edge.OutcomeQueue(str(tmp_path / "edge"))
    robot = bridge.RobotBridge(
        LocalTransport(root), embodiment="arm", site="cell", skill="pick", robot="r1", outcome_queue=queue
    )
    robot.start("t1", "pick", items=MEMORIES)
    assert robot.finish("t1", reward=0.6)["reward"] == 0.6
    robot.start("t2", "pick", items=MEMORIES)
    assert robot.finish("t2", metrics={"progress": 1.0, "cycle_s": 30})["reward"] == pytest.approx(0.875)
    with pytest.raises(ValueError, match="exactly one"):
        robot.finish("t3", True, reward=0.5)
    with pytest.raises(ValueError):
        robot.finish("t3", reward=2.0)

    class Offline:
        def post(self, path, body):
            raise ConnectionError("offline")

    robot.transport = Offline()
    assert robot.finish("t4", reward=0.3)["queued"]
    restarted = edge.OutcomeQueue(str(tmp_path / "edge"))
    assert restarted.flush(LocalTransport(root).post) == 1
    assert "t4" in rewards.load(root)
    with pytest.raises(ValueError, match="one bounded outcome"):
        restarted.enqueue({"occasion_id": "t5", "succeeded": True, "reward": 0.5})
    assert rewards.verify(root) == []


def test_the_reward_command_configures_records_verifies_and_reports(tmp_path, capsys):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="cli")
    _graded_run(root, 120, seed=3)
    assert main(["reward", "configure", '{"terms": {"score": {}}}', "--dest", root]) == 0
    assert "utility sha256:" in capsys.readouterr().out
    assert main(["reward", "record", "--occasion-id", "x1", "--metrics", '{"score": 0.9}', "--dest", root]) == 0
    assert "reward 0.9000 realized as" in capsys.readouterr().out
    assert main(["reward", "record", "--occasion-id", "x1", "--reward", "0.1", "--dest", root]) == 1
    assert "refusing" in capsys.readouterr().err
    assert main(["reward", "verify", "--dest", root]) == 0
    assert "OK" in capsys.readouterr().out
    assert main(["reward", "effects", "--dest", root]) == 0
    assert "verify-grasp:" in capsys.readouterr().out
    assert main(["reward", "effects", "--json", "--dest", root]) == 0
    assert {r["lesson"] for r in json.loads(capsys.readouterr().out)} == set(PLANTED)


def test_the_client_sdk_and_framework_integrations_report_graded_outcomes(tmp_path):
    from unimem.client import MemoryClient
    from unimem.integrations import pydantic_ai

    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="sdk")
    client = MemoryClient(root, agent_id="coder")
    assert client.outcome("t1", reward=0.75) is True
    occasion = client._occasion("t1")
    assert rewards.load(root)[occasion]["reward"] == 0.75
    assert holdout_io.read_outcomes(root)[occasion] == rewards.realize("sdk", occasion, 0.75)
    with pytest.raises(ValueError, match="exactly one"):
        client.outcome("t2", True, reward=0.5)

    calls = []

    class Memory:
        def record_outcome(self, occasion_id, **kwargs):
            calls.append((occasion_id, kwargs))
            return True

    class Result:
        conversation_id = "run-7"

    assert pydantic_ai.record_outcome(Memory(), Result(), reward=0.4)
    assert pydantic_ai.record_outcome(Memory(), Result(), succeeded=False)
    assert calls == [("run-7", {"reward": 0.4}), ("run-7", {"succeeded": False})]
    for ambiguous in ({}, {"succeeded": True, "reward": 0.4}):
        with pytest.raises(ValueError, match="exactly one"):
            pydantic_ai.record_outcome(Memory(), Result(), **ambiguous)
    assert len(calls) == 2


def test_a_remote_client_reports_graded_outcomes_through_the_memory_api(gw, monkeypatch):
    from unimem.client import MemoryClient

    g, root = gw
    sent = []

    def over_the_wire(_self, operation, payload):
        sent.append(payload)
        status, body = call(g, "POST", "/v1/memory/" + operation, payload)
        if status != 200:
            raise RuntimeError(f"{status}: {body['error']['code']}")
        return body

    monkeypatch.setattr(MemoryClient, "_request", over_the_wire)
    remote = MemoryClient(url="https://memory.example", token="t" * 40)
    assert remote.outcome("r1", reward=0.8) is True
    assert sent[-1] == {"occasion_id": "r1", "reward": 0.8}
    assert holdout_io.read_outcomes(root)["r1"] == rewards.realize("gw-tests", "r1", 0.8)
    assert remote.outcome("r1", reward=0.8) is False
    with pytest.raises(RuntimeError, match="409: conflicting_outcome"):
        remote.outcome("r1", reward=0.2)
    rewards.configure(root, {"terms": {"tests_passed": {"high": 12}}})
    assert remote.outcome("r2", metrics={"tests_passed": 9})
    assert rewards.load(root)["r2"]["reward"] == pytest.approx(0.75)
    assert remote.outcome("r3", True) and holdout_io.read_outcomes(root)["r3"] is True
    with pytest.raises(RuntimeError, match="409: conflicting_outcome"):
        remote.outcome("r3", False)


def test_the_memory_api_takes_exactly_one_outcome_and_documents_graded_ones(gw):
    g, _root = gw
    for bad in (
        {"occasion_id": "m1"},
        {"occasion_id": "m1", "succeeded": True, "reward": 0.5},
        {"occasion_id": "m1", "reward": 1.5},
        {"occasion_id": "m1", "reward": "high"},
        {"occasion_id": "m1", "metrics": {"x": 1}},
    ):
        assert call(g, "POST", "/v1/memory/outcome", bad)[0] == 400, bad
    status, body = call(g, "POST", "/v1/memory/outcome", {"occasion_id": "m1", "reward": 0.3})
    assert status == 200 and body["reward"] == 0.3 and isinstance(body["succeeded"], bool)
    doc = call(g, "GET", "/v1/openapi.json")[1]
    request = doc["components"]["schemas"]["OutcomeRequest"]
    assert request["required"] == ["occasion_id"] and request["properties"]["reward"]["maximum"] == 1
    assert doc["components"]["schemas"]["OutcomeResponse"]["required"] == ["recorded", "succeeded"]


def test_a_refused_graded_outcome_leaves_no_secondary_metrics(tmp_path, monkeypatch):
    from unimem import sycophancy_io

    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="order")
    seen = []
    monkeypatch.setattr(sycophancy_io, "record_metrics", lambda _root, occasion, values: seen.append(occasion))
    memory = CausalMemory(lambda _q, **_: MEMORIES, root=root, key=lambda m: m["id"], text=lambda m: m["text"])
    with pytest.raises(ValueError):
        memory.record_outcome("o1", reward=1.5, secondary_metrics={"accuracy": 1.0})
    assert seen == []
    assert memory.record_outcome("o1", reward=0.5, secondary_metrics={"accuracy": 1.0})
    assert seen == ["o1"]


def test_an_mcp_agent_reports_a_graded_outcome(tmp_path):
    pytest.importorskip("mcp", reason="needs the MCP SDK")
    from tests.test_mcp_server import call
    from unimem import mcp_server

    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="mcp")
    server = mcp_server.build_server(root)
    out = call(server, "record_reward", occasion_id="session-9", reward=0.9)
    assert out["recorded"] is True and out["reward"] == 0.9
    assert holdout_io.read_outcomes(root)["session-9"] == rewards.realize("mcp", "session-9", 0.9)
    assert "error" in json.dumps(call(server, "record_reward", occasion_id="session-9", reward=0.1)).lower()
    assert "error" in json.dumps(call(server, "record_reward", occasion_id="s2", reward=1.5)).lower()
    assert "error" in json.dumps(call(server, "record_reward", occasion_id="s2", metrics={"x": 1})).lower()
