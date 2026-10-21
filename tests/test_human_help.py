"""Canonical outcomes determine correction labels; reviewers cannot rewrite them."""
from __future__ import annotations

from datetime import timedelta

import pytest

from unimem import frontier_protocol as fp
from unimem import hierarchical, holdout_io, memory_authority
from unimem import human_help as help_io
from tests.test_capabilities import NOW


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(fp, "now", lambda: NOW.isoformat())
    monkeypatch.setattr("os.fsync", lambda _: None)


def setup(root):
    facts = hierarchical.append_facts(root, [
        {"statement": "The unit requires a reviewed answer."},
        {"statement": "The previous attempted answer is incomplete."},
        {"statement": "The correction checks the source before answering."}])
    holdout_io.configure(root, rate=.5, salt="human-quality")
    return [fact.id for fact, _ in facts]


def log(root, ids, operator, index, *, baseline):
    unit, before, corrected = ids
    occasion = operator+"-"+str(index)
    prior, _ = holdout_io.read_log(root)
    if not any(row.lesson == unit and row.occasion_id == occasion for row in prior):
        fact = hierarchical.load_facts(root)[unit]
        holdout_io.assign_and_log(root, [unit], occasion_id=occasion, rate=.5, salt="human-quality",
                                  revisions={unit: fact.revision})
    return help_io.intervention(root, occasion, unit, operator, pre_window=[before], corrective_segment=[corrected],
        baseline=baseline, operator_seconds=30, episode_seconds=60,
        started_at=(NOW-timedelta(minutes=1)).isoformat(), ended_at=NOW.isoformat())


def test_good_and_bad_correctors_use_real_outcomes_and_review_before_training(tmp_path):
    root = str(tmp_path)
    ids = setup(root)
    for operator, success, baseline in [("good", True, 0), ("bad", False, 1)]:
        help_io.register_operator(root, operator, operator+"-principal", "trained")
        for index in range(40):
            log(root, ids, operator, index, baseline=baseline)
            holdout_io.record_outcome(root, operator+"-"+str(index), success)
    assert help_io.operator_quality(root, "good")["status"] == "eligible"
    report = help_io.operator_quality(root, "bad")
    assert report["status"] == "quarantined" and report["mean_advantage"] == -1
    with pytest.raises(PermissionError, match="quality"):
        help_io.assert_training(root, [ids[2]])
    assert not help_io.appeal(root, "bad", "Review the task-difficulty baseline.")["training_eligibility_changed"]
    with memory_authority.writer("bad-principal", "operator"):
        with pytest.raises(PermissionError, match="own"):
            help_io.review(root, "bad", report["report_digest"], decision="approve", reason="Self approval.")
    help_io.review(root, "bad", report["report_digest"], decision="approve", reason="Baseline confounding reviewed.")
    help_io.assert_training(root, [ids[2]])


def test_intervention_freezes_sources_before_outcome_and_has_real_duration_kpis(tmp_path):
    root = str(tmp_path)
    ids = setup(root)
    help_io.register_operator(root, "operator", "human-42", "expert")
    result = log(root, ids, "operator", 0, baseline=.5)
    assert result["intervention"]["snapshots"][ids[2]]["statement"].startswith("The correction")
    assert help_io.operator_quality(root, "operator")["status"] == "review"
    holdout_io.record_outcome(root, "operator-0", True)
    with pytest.raises(PermissionError, match="before"):
        log(root, ids, "operator", 0, baseline=0)
    report = help_io.interventions(root)
    assert report["operator_minutes"] == .5
    assert report["kpis"]["interventions_per_hour"] == 60


def test_asking_never_randomizes_unknown_safety_or_disabled_contexts(tmp_path):
    root = str(tmp_path)
    assert help_io.should_ask(root, "office", "preference", "task")["randomized"] is False
    for risk in ("safety", "unknown"):
        with pytest.raises(PermissionError, match="low-risk"):
            help_io.configure_asking(root, risk, "clarification", enabled=True, risk=risk,
                                    budget_seconds=1200, trial_size=40)
    help_io.configure_asking(root, "office", "preference", risk="low-risk", trial_size=40,
                            budget_seconds=1200)
    assert not help_io.should_ask(root, "office", "preference", "task")["randomized"]
    assert holdout_io.read_log(root)[0] == []


def test_actual_asking_trial_reserves_budget_for_both_arms_and_retries_are_idempotent(tmp_path, monkeypatch):
    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="asking")
    policy = help_io.configure_asking(root, "office", "preference", risk="low-risk", trial_size=40,
                                     budget_seconds=1200, enabled=True)
    from unimem import unit_trials

    effective = unit_trials.trial(root, policy["unit"])["eligible_after"]
    monkeypatch.setattr(fp, "now", lambda: effective)
    decisions = []
    for index in range(40):
        decision = help_io.should_ask(root, "office", "preference", "question-"+str(index))
        assert decision["randomized"]
        assert help_io.should_ask(root, "office", "preference", "question-"+str(index)) == decision
        decisions.append(decision)
    assert {row["decision"] for row in decisions} == {"ask", "dont-ask"}
    assert not help_io.should_ask(root, "office", "preference", "overflow")["randomized"]
    claims = fp.journal(root, "human-asking-reservations.jsonl")
    assert sum(row["reserved_seconds"] for row in claims) == policy["budget_seconds"]
    logged, corrupt = holdout_io.read_log(root)
    assert not corrupt and len(logged) == 40


def test_unbiased_asking_effect_is_separate_from_prior_and_planner_obeys_budget():
    import random

    rng = random.Random(222)
    estimates = []
    for repetition in range(80):
        rows = []
        for _ in range(300):
            p = rng.choice([.2, .5, .8])
            ask = rng.random() < p
            rows.append({"ask": ask, "probability": p, "outcome": rng.random() < (.65 if ask else .35),
                         "operator_seconds": 30 if ask else 0})
        report = help_io.asking_estimate(rows, prior=-.8, prior_strength=100)
        estimates.append(report["effect_ipw"])
        assert report["planner_value"] < report["effect_ipw"]
    assert sum(estimates)/len(estimates) == pytest.approx(.3, abs=.025)
    tasks = [{"id": "task-"+str(index), "risk": "low-risk", "modes": [
        {"mode": "text", "seconds": 30, "value": .2}, {"mode": "teleop", "seconds": 60, "value": .3}]}
        for index in range(6)]
    tasks.append({"id": "hazard", "risk": "safety", "modes": []})
    result = help_io.plan(tasks, budget_seconds=75)
    assert result["reserved_seconds"] <= 75
    assert next(row for row in result["decisions"] if row["id"] == "hazard")["mode"] == "safe-escalation"


def test_actual_rl_export_stops_before_training_bytes_until_quality_review(tmp_path):
    from unimem import rl_io

    root = str(tmp_path)
    ids = setup(root)
    help_io.register_operator(root, "new", "human-new", "novice")
    for index in range(16):
        log(root, ids, "new", index, baseline=.5)
        occasion = "new-"+str(index)
        assigned, _ = holdout_io.read_log(root)
        row = next(row for row in assigned if row.occasion_id == occasion)
        holdout_io.record_outcome(root, occasion, row.injected)
    # Corrections cited by the training unit must inherit the human-quality gate.
    from unimem import lineage
    fact = hierarchical.load_facts(root)[ids[0]]
    lineage.attest(root, memory_authority.fact_record(fact), fact.origin, inputs=[ids[2]], operation="derive")
    for index in range(16):
        rl_io.capture(root, "new-"+str(index), ids[0], "batch", "Reviewed question.", "Reviewed answer. "+"A"*19000)
    with pytest.raises(PermissionError, match="quality"):
        rl_io.prepare(root, [ids[0]], format="advantage")
    report = help_io.operator_quality(root, "new")
    help_io.review(root, "new", report["report_digest"], decision="approve", reason="Reviewed the supervised cohort.")
    exported = rl_io.prepare(root, [ids[0]], format="advantage")
    assert exported["row_count"] == 16 and len(exported["parts"]) > 1
    stream = rl_io.lines(root, exported["id"])
    assert next(stream)
    help_io.review(root, "new", report["report_digest"], decision="reject", reason="Review changed before next shard.")
    with pytest.raises(PermissionError, match="quality"):
        next(stream)


def test_ros_should_ask_is_an_actual_service_callback_with_bound_robot_context(tmp_path):
    import json
    from types import SimpleNamespace

    from unimem.robotics import bridge

    class Message:
        pass
    class Node:
        def __init__(self, name):
            self.services = {}
        def create_publisher(self, *args):
            return SimpleNamespace(publish=lambda message: None)
        def create_subscription(self, *args):
            pass
        def create_service(self, service_type, name, callback):
            self.services[name] = callback
    class Transport:
        def post(self, path, body):
            return {}
    robot = bridge.RobotBridge(Transport(), embodiment="arm", site="lab", skill="review", robot="r1")
    seen = []
    def decide(body):
        seen.append(body)
        return {"decision": "which-mode", "mode": "safe-escalation", "randomized": False}
    node = bridge.create_node(robot, node_factory=Node, message_type=Message, on_ask=decide, service_type=object,
                              topics={"ask_service": "/planner/should_ask"})
    response = SimpleNamespace()
    callback = node.services["/planner/should_ask"]
    callback(SimpleNamespace(occasion="task", question_type="preference"), response)
    assert response.ok and json.loads(response.decision_json)["randomized"] is False
    assert seen[0]["context"] == robot.context
    callback(SimpleNamespace(occasion="x"*129, question_type="preference"), response)
    assert not response.ok and json.loads(response.decision_json)["mode"] == "safe-escalation"


def test_asking_cost_report_joins_real_outcomes_and_requires_the_complete_cohort(tmp_path, monkeypatch):
    from unimem import unit_trials

    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="asking-report")
    configured = help_io.configure_asking(root, "office", "format", enabled=True, risk="low-risk",
        budget_seconds=1200, trial_size=40)
    monkeypatch.setattr(fp, "now", lambda: unit_trials.trial(root, configured["unit"])["eligible_after"])
    assert help_io.asking_report(root, "office", "format")["status"] == "incomplete"
    for index in range(40):
        occasion = "cost-"+str(index)
        decision = help_io.should_ask(root, "office", "format", occasion)
        holdout_io.record_outcome(root, occasion, decision["ask"])
        help_io.record_asking_cost(root, "office", "format", occasion, 30 if decision["ask"] else 0)
    result = help_io.asking_report(root, "office", "format")
    assert result["status"] == "complete" and result["effect_ipw"] > .5
    assert result["reserved_seconds"] == 1200 and result["reported_cost_overruns"] == 0
    assert result["operator_minutes_saved_estimate"] > 0


def test_enabling_is_explicit_and_fleet_budget_is_shared_across_contexts(tmp_path, monkeypatch):
    from unimem import unit_trials

    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="shared-budget")
    arguments = {"risk": "low-risk", "budget_seconds": 1200, "trial_size": 40}
    help_io.configure_asking(root, "office", "a", **arguments)
    assert not help_io.should_ask(root, "office", "a", "pre")["randomized"]
    first = help_io.configure_asking(root, "office", "a", enabled=True, **arguments)
    second = help_io.configure_asking(root, "office", "b", enabled=True, **arguments)
    effective = max(unit_trials.trial(root, item["unit"])["eligible_after"] for item in (first, second))
    monkeypatch.setattr(fp, "now", lambda: effective)
    for index in range(40):
        assert help_io.should_ask(root, "office", "a", "task-"+str(index))["randomized"]
    assert not help_io.should_ask(root, "office", "b", "cannot-overspend")["randomized"]
    assert sum(row["reserved_seconds"] for row in fp.journal(root, "human-asking-reservations.jsonl")) == 1200


def test_cli_and_native_mcp_expose_governed_asking_without_implicit_enablement(tmp_path, capsys):
    import json

    from unimem import cli

    root = str(tmp_path)
    assert cli.main(["fleet", "ask-policy", json.dumps({"action": "configure", "context": "office",
        "question_type": "preference", "risk": "low-risk", "budget_seconds": 1200, "trial_size": 40}),
        "--dest", root]) == 0
    assert not json.loads(capsys.readouterr().out)["enabled"]
    pytest.importorskip("mcp")
    from unimem import mcp_server
    from tests.test_mcp_server import call

    result = call(mcp_server.build_server(root), "should_ask", context="office", question_type="preference", occasion="task")
    assert result["ok"] and not result["randomized"]


def test_erased_intervention_evidence_never_restores_training_eligibility(tmp_path):
    from unimem import privacy_erasure

    root = str(tmp_path)
    ids = setup(root)
    help_io.register_operator(root, "human", "human-principal", "trained")
    log(root, ids, "human", 0, baseline=.5)
    holdout_io.record_outcome(root, "human-0", True)
    report = help_io.operator_quality(root, "human")
    help_io.review(root, "human", report["report_digest"], decision="approve", reason="Approved the current evidence.")
    help_io.assert_training(root, [ids[2]])
    privacy_erasure.purge_fact(root, ids[1], reason="Erase the pre-intervention window.")
    with pytest.raises(PermissionError, match="quality"):
        help_io.assert_training(root, [ids[2]])


def test_empirical_bayes_prior_uses_completed_contexts_and_finite_belief_evpi():
    value = help_io.evpi([.5, .5], [[1, 0], [0, 1]])
    assert value == .5
    reports = [{"status": "complete", "effect_ipw": .2+index*.05, "belief_prior": .1,
                "sampling_variance": .001, "n": 100} for index in range(3)]
    fitted = help_io.fit_asking_prior(reports, value)
    assert fitted["prior"] == pytest.approx(.65)
    assert 0 < fitted["prior_strength"] <= 1000
    assert len(fitted["calibration_digests"]) == 3
    with pytest.raises(ValueError, match="sum"):
        help_io.evpi([.8, .8], [[1, 0], [0, 1]])


def test_concurrent_planners_cannot_overspend_the_last_fleet_slot(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from unimem import unit_trials

    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="concurrent-ask")
    policy = help_io.configure_asking(root, "office", "preference", enabled=True, risk="low-risk",
                                     budget_seconds=1200, trial_size=40)
    effective = unit_trials.trial(root, policy["unit"])["eligible_after"]
    monkeypatch.setattr(fp, "now", lambda: effective)
    for index in range(39):
        help_io.should_ask(root, "office", "preference", "old-"+str(index))
    def compete(index):
        return help_io.should_ask(root, "office", "preference", "last-"+str(index))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(compete, range(8)))
    assert sum(row["randomized"] for row in results) == 1
    assert sum(row["reserved_seconds"] for row in fp.journal(root, "human-asking-reservations.jsonl")) == 1200


def test_erasing_decision_artifacts_never_refunds_the_experimental_budget(tmp_path, monkeypatch):
    from unimem import nextwave_store, unit_trials

    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="persistent-budget")
    policy = help_io.configure_asking(root, "office", "preference", enabled=True, risk="low-risk",
                                     budget_seconds=1200, trial_size=40)
    effective = unit_trials.trial(root, policy["unit"])["eligible_after"]
    monkeypatch.setattr(fp, "now", lambda: effective)
    for index in range(40):
        help_io.should_ask(root, "office", "preference", "old-"+str(index))
    key = fp.digest([policy["unit"], "old-0"])
    nextwave_store.erase(root, [nextwave_store.reference("asking-reservations", key)])
    assert not help_io.should_ask(root, "office", "preference", "new")["randomized"]
    with pytest.raises(PermissionError, match="erased"):
        help_io.should_ask(root, "office", "preference", "old-0")


def test_empirical_bayes_enrollment_reads_actual_past_trials_and_preserves_erasure_lineage(tmp_path, monkeypatch):
    from unimem import nextwave_store, unit_trials

    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="eb-prior")
    calibration = []
    for trial in range(3):
        context = "prior-"+str(trial)
        policy = help_io.configure_asking(root, context, "preference", enabled=True, risk="low-risk",
            budget_seconds=160, cost_seconds=1, trial_size=40)
        effective = unit_trials.trial(root, policy["unit"])["eligible_after"]
        monkeypatch.setattr(fp, "now", lambda value=effective: value)
        for index in range(40):
            occasion = context+"-"+str(index)
            decision = help_io.should_ask(root, context, "preference", occasion)
            holdout_io.record_outcome(root, occasion, decision["ask"])
            help_io.record_asking_cost(root, context, "preference", occasion, int(decision["ask"]))
        calibration.append({"context": context, "question_type": "preference"})
    current = help_io.configure_asking(root, "current", "preference", risk="low-risk", budget_seconds=160,
        cost_seconds=1, trial_size=40, beliefs=[.5, .5], utilities=[[1, 0], [0, 1]], calibration=calibration)
    assert current["belief_prior"] == .5
    assert current["prior_model"]["kind"].startswith("empirical-Bayes")
    assert len(current["prior_model"]["calibration_digests"]) == 3
    reference = nextwave_store.reference("asking-costs", fp.digest(["ask:prior-0:preference", "prior-0-0"]))
    nextwave_store.erase(root, [reference])
    with pytest.raises((PermissionError, ValueError)):
        help_io.should_ask(root, "current", "preference", "cannot-use-erased-prior")


def test_corrector_quality_enrollment_cannot_mix_ownership_scopes(tmp_path):
    root = str(tmp_path)
    help_io.register_operator(root, "human", "human-principal", "trained", context=["project:a"])
    with pytest.raises(PermissionError):
        help_io.operator_quality(root, "human", context=["project:b"])
    with pytest.raises(PermissionError, match="enrolled ownership"):
        help_io.operator_quality(root, "human", context=["project:a", "project:b"])


def test_native_ros_service_interface_when_generated_toolchain_is_available(monkeypatch):
    import importlib

    try:
        interface = importlib.import_module("unimem_interfaces.srv")
        rclpy = importlib.import_module("rclpy")
        messages = importlib.import_module("std_msgs.msg")
    except ImportError:
        pytest.skip("ROS 2 rclpy/std_msgs and generated unimem_interfaces are not installed; build with colcon")
    from unimem.robotics import bridge

    class Transport:
        def post(self, path, body):
            return {}
    robot = bridge.RobotBridge(Transport(), embodiment="arm", site="lab", skill="review", robot="native-test")
    import os

    monkeypatch.setenv("ROS_LOCALHOST_ONLY", "1")
    service_name = "/unimem_test_"+str(os.getpid())+"/should_ask"
    context = rclpy.Context()
    rclpy.init(context=context, domain_id=173)
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.node import Node

    executor = SingleThreadedExecutor(context=context)
    node = None
    try:
        node = bridge.create_node(robot, node_factory=lambda name: Node(name, context=context),
            message_type=messages.String, service_type=interface.ShouldAsk, topics={"ask_service": service_name},
            on_ask=lambda body: {"decision": "which-mode", "mode": "operator-review", "randomized": False})
        executor.add_node(node)
        client = node.create_client(interface.ShouldAsk, service_name)
        assert client.wait_for_service(timeout_sec=2)
        request = interface.ShouldAsk.Request()
        request.occasion, request.question_type = "native-task", "preference"
        pending = client.call_async(request)
        executor.spin_until_future_complete(pending, timeout_sec=2)
        assert pending.done() and pending.result().ok
    finally:
        executor.shutdown()
        if node is not None:
            node.destroy_node()
        rclpy.shutdown(context=context)


def test_unassigned_teleoperation_uses_canonical_outcome_without_randomizing_a_hazard(tmp_path):
    root = str(tmp_path)
    ids = setup(root)
    help_io.register_operator(root, "teleoperator", "human-principal", "expert")
    result = help_io.intervention(root, "ordinary-task", None, "teleoperator", pre_window=[ids[1]],
        corrective_segment=[ids[2]], baseline=.5, operator_seconds=30, episode_seconds=60,
        started_at=(NOW-timedelta(minutes=1)).isoformat(), ended_at=NOW.isoformat())
    assert result["intervention"]["canonical_assignment"] == {"unit": None, "occasion_id": "ordinary-task"}
    assert holdout_io.read_log(root)[0] == []
    holdout_io.record_outcome(root, "ordinary-task", True)
    assert help_io.operator_quality(root, "teleoperator")["resolved"] == 1


def test_newly_protected_context_stops_live_asking_before_assignment(tmp_path, monkeypatch):
    from unimem import safety, unit_trials

    root = str(tmp_path)
    ids = setup(root)
    policy = help_io.configure_asking(root, "office", "preference", enabled=True, risk="low-risk",
        budget_seconds=1200, trial_size=40, sources=[ids[0]])
    effective = unit_trials.trial(root, policy["unit"])["eligible_after"]
    monkeypatch.setattr(fp, "now", lambda: effective)
    reviewed = safety.review(root, ids[0], reviewer="safety-reviewer", reason="Newly identified hazard context.")
    safety.mark(root, ids[0], "human-proximity", reviewed)
    decision = help_io.should_ask(root, "office", "preference", "no-randomization")
    assert not decision["randomized"] and decision["mode"] == "safe-escalation"
    assert holdout_io.read_log(root)[0] == []
    assert fp.journal(root, "human-asking-reservations.jsonl") == []
