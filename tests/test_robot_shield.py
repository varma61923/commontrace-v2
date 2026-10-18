import pytest

from commontrace import lineage
from commontrace.robotics import shield


def test_bounded_stl_robustness_and_memory_linked_hold(tmp_path):
    root = str(tmp_path)
    formula = {"op": "always", "from": 0, "to": 1,
               "arg": {"op": "predicate", "signal": "distance", "cmp": ">=", "value": 0.5}}
    shield.register(root, "safe_distance", formula=formula, memory_id="memory-1", embodiment="arm-v1",
                    description="Maintain clearance.")
    good = shield.check(root, "safe_distance", action={"grasp": "start"}, trace=[{"t": 0, "distance": 0.8}, {"t": 1, "distance": 0.6}],
                        source_memory_ids=["memory-1"])
    assert good["verdict"] == "safe" and good["robustness"] == pytest.approx(0.1)
    verdict_lineage = lineage.records(root)[good["lineage_id"]]
    assert verdict_lineage["inputs"][0]["id"] == "memory-1"
    from commontrace import recovery, safety

    context = {"site": "site-a", "robot": "robot-a", "embodiment": "arm-v1", "environment": "real"}
    registered = recovery.register(root, "clearance_violation", "Stop and move to the safe pose", context=context)
    recovery_id = registered["record"]["id"]
    review = safety.review(root, recovery_id, reviewer="safety-engineer", reason="reviewed safe fallback")
    safety.mark(root, recovery_id, "human-proximity", review, site="site-a", robot="robot-a")
    bad = shield.check(root, "safe_distance", action={"grasp": "start"}, trace=[{"t": 0, "distance": 0.8}, {"t": 1, "distance": 0.2}],
                       source_memory_ids=["memory-1"], recovery_memory_ids=[recovery_id],
                       recovery_context=context, failure_type="clearance_violation")
    assert bad["verdict"] == "violated" and bad["action"] == "hold-and-use-recovery"
    assert bad["recovery_memory_ids"] == [recovery_id] and bad["recovery_fallback"][0]["hazard"]
    missing = shield.check(root, "safe_distance", action={"grasp": "start"}, trace=[{"t": 0, "distance": 0.8}], source_memory_ids=[])
    assert missing["verdict"] == "memory-not-recalled" and missing["action"] == "hold"


def test_eventually_until_and_malformed_trace(tmp_path):
    root = str(tmp_path)
    eventually = {"op": "eventually", "from": 0, "to": 2,
                  "arg": {"op": "predicate", "signal": "ready", "cmp": ">=", "value": 1}}
    shield.register(root, "ready", formula=eventually, memory_id="m", embodiment="r", description="ready")
    result = shield.check(root, "ready", action={"move": "start"}, trace=[{"t": 0, "ready": 0}, {"t": 1, "ready": 2}], source_memory_ids=["m"])
    assert result["verdict"] == "safe" and result["robustness"] == 1
    until = {"op": "until", "from": 0, "to": 2,
             "left": {"op": "predicate", "signal": "force", "cmp": ">=", "value": 0},
             "right": {"op": "predicate", "signal": "ready", "cmp": ">=", "value": 1}}
    shield.register(root, "ready_until", formula=until, memory_id="m", embodiment="r", description="until")
    hand = shield.check(root, "ready_until", action={"move": "start"},
                        trace=[{"t": 0, "force": 2, "ready": 0}, {"t": 1, "force": 1, "ready": 0},
                               {"t": 2, "force": 1, "ready": 2}], source_memory_ids=["m"])
    assert hand["verdict"] == "safe" and hand["robustness"] == 1
    with pytest.raises(ValueError):
        shield.check(root, "ready", action={"move": "start"}, trace=[{"t": 1, "ready": 1}, {"t": 1, "ready": 2}], source_memory_ids=["m"])
    with pytest.raises(ValueError):
        shield.register(root, "bad", formula={"op": "always", "from": 2, "to": 1, "arg": eventually},
                        memory_id="m", embodiment="r", description="bad")


def test_monitor_is_fast_for_bounded_trace(tmp_path):
    root = str(tmp_path)
    formula = {"op": "always", "from": 0, "to": 0.1,
               "arg": {"op": "predicate", "signal": "force", "cmp": "<=", "value": 10}}
    shield.register(root, "force_limit", formula=formula, memory_id="m", embodiment="r", description="limit")
    trace = [{"t": i / 1000, "force": 5.0} for i in range(10_000)]
    result = shield.check(root, "force_limit", action={"move": "start"}, trace=trace, source_memory_ids=["m"])
    assert result["verdict"] == "safe" and result["latency_ms"] < 500
