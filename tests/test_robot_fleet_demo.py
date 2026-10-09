"""The embodied fleet demo, end to end through the gateway, fleet tooling and causal engine."""
from benchmarks import robot_fleet_demo


def test_fleet_finds_the_sim_to_real_gap_and_withdraws_real_harm():
    report = robot_fleet_demo.run(occasions=1200, seed=0)
    real, sim = report["verdicts"]["real"], report["verdicts"]["sim"]
    assert real["path:aisle-7-shortcut"]["verdict"] == "HURTS"
    assert sim["path:aisle-7-shortcut"]["verdict"] != "HURTS"  # harmless where no one walks
    assert real["grip:soft-items-slow"]["verdict"] == "HELPS"
    assert "safety:estop-near-humans" not in real and "safety:estop-near-humans" not in sim  # never randomized
    assert report["pooling_sim_with_real"]["refused"]
    harm = report["harm_withdrawal"]
    assert harm["withdrawn"] == ["path:aisle-7-shortcut"]
    assert harm["deliveries_in_100_occasions"]["path:aisle-7-shortcut"] == 0
    assert harm["deliveries_in_100_occasions"]["safety:estop-near-humans"] == 100
    assert report["episodes_recorded"] == 2400 and report["recall_latency_ms"]["calls"] == 2400
