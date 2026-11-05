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
    with pytest.raises(ValueError, match="not both"):
        rewards.record(root, "o3", reward=0.5, metrics={"x": 1})
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

    rows = [json.loads(line) for line in open(rewards.rewards_path(root), encoding="utf-8")]
    victim = next(r for r in rows if r["occasion_id"] != "o99" and not r["succeeded"])
    victim["succeeded"] = True
    with open(rewards.rewards_path(root), "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)
    assert any(victim["occasion_id"] in p and "not the rounding" in p for p in rewards.verify(root))


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
    with pytest.raises(ValueError, match="/v1/outcome"):
        MemoryClient(url="https://memory.example").outcome("t3", reward=0.5)

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
