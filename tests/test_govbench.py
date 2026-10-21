"""GovBench: multi-principal utility, leakage and verified forgetting through the gateway."""
from benchmarks import govbench
from unimem import memory_control


def test_entitled_recall_no_leaks_and_forgetting_follows_lineage():
    report = govbench.run()
    before, after = report["before_forgetting"], report["after_forgetting"]
    assert before["utility"] == 1.0 and before["leaks"] == 0 and before["unentitled_checks"] > 50
    assert after["utility"] == 1.0 and after["leaks"] == 0
    assert after["forgotten_delivered"] == 0 and after["forgotten_checks"] > 0
    assert report["certificate_covers_derived"] is True
    assert "GovBench" in govbench.render(report)


def test_the_benchmark_detects_a_leak_when_scoping_is_broken(monkeypatch):
    monkeypatch.setattr(memory_control, "matches", lambda labels, context, governed=True: True)
    report = govbench.run()
    assert report["before_forgetting"]["leaks"] > 0


def test_governance_utility_is_independent_of_random_holdout(monkeypatch):
    from unimem import experiment

    # Every nonzero holdout rate would withhold all candidates on this draw.
    monkeypatch.setattr(experiment, "_uniform_from", lambda *args: 0.0)
    report = govbench.run()
    assert report["before_forgetting"]["utility"] == 1.0
    assert report["after_forgetting"]["utility"] == 1.0
    assert report["after_forgetting"]["forgotten_delivered"] == 0
