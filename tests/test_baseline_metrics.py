import json
import os

BASELINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "metrics", "baseline.json")


def test_baseline_has_every_measurement_and_it_is_sane():
    with open(BASELINE, encoding="utf-8") as fh:
        data = json.load(fh)
    act = data["activation"]
    assert act["lesson_injected"] is True
    assert 0 < act["seconds_to_first_trace"] < act["seconds_to_first_active_lesson"] <= act["seconds_to_first_injected_lesson"]
    assert act["verdict"] == "HELPS", "the seeded +15pp effect must be detected on the simulated fleet"
    assert 0 < data["query_latency"]["p50_ms"] <= data["query_latency"]["p95_ms"]
    assert data["install"]["success"] == data["install"]["of"] == 6
    assert "what" in data["hub"]
